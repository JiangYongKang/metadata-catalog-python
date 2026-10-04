# 本地元数据目录（Local Metadata Catalog）

一个仅依赖本地环境的元数据目录服务：登记数据资产（含列级）与上下游血缘、沿血缘做
深度受限的影响分析，并通过乐观并发控制在结构演进与并发更新下给出一致、可解释的结论。
在此之上还提供一层**血缘变更审计、时间点/版本回溯与批量原子提交**能力：

- 每次血缘登记/取消都自动落入**只追加审计日志**（时间戳、序号、前后状态、批次号），
  可围绕单个资产从头到尾翻看“每条关系是哪一步加上、哪一步去掉的”，支持时间范围筛选；
- 可按**过去某个时间点**或**某资产当年的某个版本**把那一刻的血缘图还原为不可变快照，
  并在快照上照常做上下游影响分析，同一点反复查询结果完全一致；
- 支持**一批关系增删的原子提交**（all-or-nothing），逐条给出坏关系与原因，
  并发提交不丢改动，并发查询读不到改到一半的图。

- 运行时：Python ≥ 3.14，FastAPI（仅用于 HTTP 暴露；核心逻辑零第三方依赖）
- 存储：进程内内存结构（单实例、单写锁 + CAS），无需任何外部服务

## 1. 资产与血缘模型

### 资产 Asset

| 字段 | 含义 |
| --- | --- |
| `asset_id` | 全局唯一标识（主键） |
| `name` / `asset_type` / `description` | 名称、类型（table/topic/…）、描述 |
| `columns` | 列定义 `Column(name, data_type, description)` 的有序元组 |
| `current_version` | 当前结构版本号，从 1 开始单调递增 |
| `history` | 每个版本一份不可变快照 `AssetVersion` |

### 血缘 LineageEdge

- 方向语义：`upstream -> downstream`，表示 downstream 依赖 upstream。
- 资产级边：`(upstream, downstream)`。
- 列级边：额外携带 `upstream_column` / `downstream_column`。列级边与资产级边
  互相独立、按四元组唯一标识，**列级边不会覆盖资产级边或其它列的边**。

### 登记规则（幂等合并）

- 新资产：创建为版本 1，记录首版快照。
- 重复登记且内容完全一致：**幂等**，不产生新版本。
- 重复登记且内容变化：列按 `name` 做并集合并——
  - 已有列不会被删除（只增不丢）；
  - 同名列采用新定义（类型/描述可演进）；
  - 新列追加到末尾；
  - 元数据变化同样产生新版本。
- **已有关联（血缘）在任何重复登记后都不会丢失。**
- 被拒绝的登记/血缘不会产生任何部分写入（先校验后落库）。

### 拒绝原因（可区分的错误码）

| 错误类型 | `code` | HTTP | 触发条件 | 附加字段 |
| --- | --- | --- | --- | --- |
| `AssetNotFoundError` | `asset_not_found` | 404 | 血缘端点、审计查询、影响分析起点或版本回溯指向未知资产 | `asset` |
| `ColumnNotFoundError` | `column_not_found` | 404 | 列级血缘引用了该资产上不存在的列 | `asset`, `column` |
| `LineageCycleError` | `lineage_cycle` | 422 | 新边会成环（含资产级/列级自依赖） | `cycle`（检测到的环路径） |
| `ConcurrentUpdateError` | `concurrent_update` | 409 | 乐观锁版本不匹配 | `expected_version`, `actual_version`（资产不存在时 actual=0） |
| `LineageTimeError` | `invalid_time` | 400 | 回溯时间非法/越界或版本号非法 | `reason`, `requested`, `earliest`, `latest`, `version` |
| `BatchRejectedError` | `batch_rejected` | 422 | 批量中存在坏关系，整批回滚 | `batch_id`, `failures[]`（逐条 `index/code/message/op/asset/column/cycle`） |

环检测：加入 `u -> v` 前，先检查当前图上是否存在 `v → ... → u` 的有向路径；
存在则拒绝并返回该路径（加上闭环边）。菱形上的“快捷边”（如 `a -> d` 与
`a->b->d` 共存）**不是环**，允许登记。

## 2. 影响分析规则

`analyze_impact(catalog, start, direction, max_depth)`：

- `direction = downstream`（默认）：谁依赖我；`upstream`：我依赖谁。
- **BFS 分层遍历**，起点深度为 0，每条边一跳；每个节点只记录其**最小深度**。
- `max_depth`：最大跳数；到达边界且仍有未展开邻居的节点进入 `truncated_at`，
  结果中 `truncated=true`；`None` 或负数表示不限深度，`0` 表示只看起点。
- 确定性：邻居按资产 ID 字母序展开，节点按 `(深度, ID)` 排序输出，
  重复运行结果完全一致。
- 环与重复路径：重复到达的边进入 `repeated_edges`（含环的闭合边、菱形的第二条
  路径），**不会再次展开**。因此每个节点恰好入队一次，时间/空间复杂度 O(V+E)，
  环不会造成超时或无界膨胀。

返回结构 `ImpactResult`：

- `depths`：节点 -> 最小深度；`nodes` / `impacted_nodes`（不含起点）
- `paths`：BFS 树边（发现顺序）
- `repeated_edges`：重复/回边，用于解释“为什么有环或多路径但没重复计算”
- `truncated_at`：深度截断位置

## 2a. 血缘变更审计（Audit Trail）

每次 `add_lineage` / `remove_lineage`（含批量提交中的每一条）都会向**只追加**
审计日志写入一条 `LineageChange`：

| 字段 | 含义 |
| --- | --- |
| `seq` | 全局单调序号，无间断，严格反映落日志顺序 |
| `timestamp` | 变更时刻（epoch 秒；可注入时钟，测试可完全复现） |
| `action` | `add` / `remove` |
| `upstream`/`downstream`/`*_column` | 关系四元组 |
| `before` / `after` | 变更前后该边是否存在：`present` / `absent` |
| `changed` | 是否真正改动了图；幂等重复登记/删不存在的边记为 `False`（尝试仍留痕） |
| `batch_id` | 批量提交时同批共享的批次号；单条登记为 `None` |
| `change_note` | 批次备注 |

查询入口：

- `catalog.lineage_changes(asset_id, start=None, end=None)`：围绕某资产的完整
  上下游关系变更史（端点资产即计入），`start/end` 为**闭区间**时间过滤。
- `catalog.all_lineage_changes(start=None, end=None)`：全局审计流。

两种“看起来都没数据”的情形被**显式区分**，绝不都返回空：

- 资产根本不存在 → 抛 `AssetNotFoundError`（HTTP 404，`code=asset_not_found`）；
- 资产存在但确实还没有任何血缘变更 → 返回 `[]`。

## 2b. 时间点 / 版本回溯（Reconstruction）

审计日志同时是血缘图的**事件源**。回溯 = 把 `changed=True` 的事件按序折叠到目标
时刻：

- `catalog.lineage_as_of(t)`：返回 `t` 时刻的不可变快照 `LineageSnapshot`；
- `catalog.lineage_at_asset_version(asset_id, v)`：取该资产第 `v` 版快照的
  `created_at`，再按该时刻还原血缘。

语义与保证：

- **闭区间折叠**：`t` 时刻已发生的变更全部计入；同一批共享一个时间戳，因此一个批次
  在该时刻整体可见（不会看到半批）。
- **可重复**：折叠是 `(事件序列, t)` 的纯函数，同一 `t` 反复查询得到完全相同的边集；
  快照是冻结视图，之后的实时写入不会改变老快照。
- **现场一致**：在还原图上调用 `analyze_impact(snapshot, ...)`，得到的上下游影响面
  与当时在现场实时查询的结果一致（测试在多个时刻同时捕获现场答案后逐一比对）。
- **轻量**：回溯只在查询时多做一次线性折叠（O(E)），正常登记/邻接查询仅多一条
  追加日志，不会被历史拖慢（测试用 5000 条边验证折叠耗时与实时查询开销）。

可还原窗口为 **[目录创建时刻, 当前时钟]**，越界/非法一律显式报错，绝不返回空图或
看似正常的结果（`LineageTimeError`，HTTP 400，`code=invalid_time`）：

| `reason` | 触发条件 |
| --- | --- |
| `invalid_type` | 时间不是数值（如字符串） |
| `invalid_value` | `NaN` / `±Inf` |
| `before_history` | 早于目录创建时刻（之前的状态不可知） |
| `future` | 晚于当前时钟 |
| `invalid_version` | 版本号不是正整数；未知资产/未知版本仍走 `asset_not_found` |
| `invalid_range` | 审计查询 `start > end` |

注：目录创建后、第一条血缘变更前的时刻，还原结果是一张**可证明为空**的图（返回空
快照而不是报错）；早于目录创建则无法证明，按 `before_history` 报错。

## 2c. 批量原子提交（Batch Commit）

`catalog.commit_lineage_batch(ops, batch_id=None, change_note="")` 一次提交一批
增删：

- **全部生效或全部不生效**：每条操作都在“已包含本批更早操作”的试探图上校验；
  只要有一条引用不存在的资产/列、或会引入环（含 `a.id -> a.id` 这类列级自依赖、
  以及整条资产依赖自己），整批拒绝，抛 `BatchRejectedError`
  （HTTP 422，`code=batch_rejected`），图与审计日志均无任何变化。
- **逐条可解释**：异常里的 `failures` 是 `BatchItemFailure` 列表，每条带
  `index`（批次内下标）、`code`、`message`、出错的 `op`、`asset`/`column`/
  `cycle` 诊断字段；一批多条坏关系会全部列出而不是只报第一条。
- **幂等不报错**：重复加已存在的边、删不存在的边记为 `skipped`（尝试在返回中可见，
  不产生变更事件）；同资产不同列（`a.id -> a.v`）视为正常的列级转换，允许提交。
- **提交记录**：成功批次返回 `BatchResult`（`batch_id`、`applied`、`changes`、
  `skipped`），每条生效边都有带同一 `batch_id` 的审计记录。
- **并发**：校验与落库在同一把目录锁内原子完成。不同批次并发提交串行化、不丢改动；
  一边提交一边查询的读者要么看到批前、要么看到批后，绝读不到半成品图。

op 形如 `{"action": "add"|"remove", "upstream": ..., "downstream": ...,
"upstream_column": ..., "downstream_column": ...}`，也可直接传 `LineageOp`。

## 3. 版本与并发语义

- 每次**结构性变更**产生一个新版本，旧版本以不可变快照保留，可通过
  `get_version(id, n)` / `GET /assets/{id}/versions` 追溯；快照内同时保留
  变更说明 `change_note`（未提供时自动生成，如“columns added: email”）。
- 血缘归属：血缘挂在资产 ID 上，不随版本复制；任何版本的资产都保持已登记的
  上下游关联，结构演进不会使血缘失效或丢失（删除列导致的悬空列血缘会在
  后续重新登记该边时显式暴露为 `column_not_found`）。
- 并发控制：所有写操作在同一把可重入锁内原子完成，并且支持**乐观并发（CAS）**：
  - `register_asset(..., expected_version=v)` 要求当前版本等于 `v`
    （创建资产时传 `0`）；
  - 不匹配则抛出 `ConcurrentUpdateError`（HTTP 409），携带
    `expected_version` 与 `actual_version`，**冲突显式暴露，绝不静默覆盖**；
  - 调用方读取最新版本后重试即可完成更新（测试 `test_parallel_cas_writes_...`
    演示 12 个并发写者无一丢失更新）。

## 4. HTTP API

| 方法与路径 | 说明 |
| --- | --- |
| `POST /assets` | 登记/演进资产（body 可带 `expected_version`） |
| `GET /assets` / `GET /assets/{id}` | 列出/查看资产 |
| `GET /assets/{id}/versions` | 查看全部历史版本快照 |
| `POST /lineage` | 登记血缘（资产级或列级） |
| `GET /lineage` | 列出全部血缘 |
| `GET /lineage/changes?start&end` | 全局血缘审计流（可按时间闭区间过滤） |
| `GET /assets/{id}/lineage-changes?start&end` | 单资产血缘变更史（未知资产 404，无变更返回 `[]`） |
| `GET /lineage/as-of?as_of=T` | 还原 T 时刻的血缘快照 |
| `GET /assets/{id}/lineage-at-version/{v}` | 按资产版本还原血缘快照 |
| `GET /impact/as-of/{id}?as_of=T&direction&max_depth` | 在 T 时刻的还原图上做影响分析 |
| `GET /impact/at-version/{id}/{v}?start&direction&max_depth` | 在某资产 v 版时刻的还原图上做影响分析（`start` 默认为该资产） |
| `POST /lineage/batch` | 原子批量增删（body：`{"ops":[...], "batch_id"?, "change_note"?}`） |
| `GET /impact/{id}?direction=downstream&max_depth=2` | 当前图影响分析 |

错误响应统一为 `{"error": {"code": ..., "message": ..., ...}}`。

快速启动：

```bash
uv run fastapi dev main.py      # 或: uv run uvicorn main:app --reload
```

## 5. 本地验证方法

仅需 Python 标准库 + FastAPI（TestClient 随 fastapi[standard] 安装），无外部依赖：

```bash
# 运行全部 57 个用例（-v 让 [CASE] 关键输入日志可见）
uv run python -m unittest discover -s tests -v

# 只看场景日志（每个 [CASE] 打印覆盖场景与关键输入）
uv run python -m unittest discover -s tests -v 2>&1 | grep -B1 -A4 "\[CASE\]"

# 仅核心逻辑（不启动 HTTP，零第三方依赖，可直接用系统 python）
python3 -m unittest tests.test_lineage tests.test_impact \
    tests.test_concurrency tests.test_audit tests.test_timeline \
    tests.test_batch -v
```

测试覆盖（每个失败断言均带有 `reason:`，日志中通过 `[CASE]` 打印场景名与关键输入）：

- `tests/test_lineage.py`（原有，9 例）：悬空资产引用、悬空列引用（错误码区分）、
  自依赖、三环路径与回显、菱形快捷边不误判、列级边独立性、拒绝后无部分写入。
- `tests/test_impact.py`（原有，8 例）：上下游方向、最小深度、`max_depth` 截断边界、
  菱形重复路径、1000 条并行路径不膨胀、30 次运行确定性、含环图终止性。
- `tests/test_concurrency.py`（原有，6 例）：幂等不产生版本、合并不丢列与血缘、
  历史快照可追溯、409 冲突携带 expected/actual、12 线程 CAS 无丢失更新。
- `tests/test_api.py`（4 老 + 5 新）：老的 HTTP 全链路用例保持不变；新增审计端点
  未知/空区分、as-of 与还原图影响端点、非法时间（NaN/Inf/过去/未来/非数值）显式
  报错、版本快照端点、批量提交成功与整批回滚、坏批次形状返回 400。
- `tests/test_audit.py`（6 例）：登记/重复登记/删除/删空四种尝试的前后状态与
  `changed` 标记、围绕单资产完整变更史、时间闭区间过滤、未知资产报错 vs 在册但无
  变更返回 `[]`、倒置区间报错、批次审计记录共享 `batch_id`。
- `tests/test_timeline.py`（7 例）：**非法时间点**（字符串/NaN/Inf/早于创建/未来，
  各自的 `reason`）、四个历史时刻的还原边集与上下游影响面**与当时现场答案逐一相等**、
  同一时刻 20 次重复结果一致、按资产 v1/v2 版本还原（含未知版本与非法版本号）、
  批次在其时刻整体可见、**5000 边大图折叠的线性耗时与实时查询不被拖慢**、
  老快照不受后续写入影响。
- `tests/test_batch.py`（12 例）：**一条坏关系导致整批回滚**（图与审计均不变）、
  多条坏关系逐条回显 index/code/asset/column、资产级与列级自依赖拦截、同批内部
  成环整批拒绝、`a.id -> a.v` 列转换允许、幂等跳过、空批次、同批内先删再加、
  **16 线程并发提交 192 条边无丢失**、提交期间读者读不到半成品（配对边始终成对
  出现）、老单条登记/取消 API 行为不变、40 线程提交者与拒绝者混合压力测试。

### 手工快速验证（无需测试框架）

```bash
uv run fastapi dev main.py
# 另一个终端：
curl -s -XPOST localhost:8000/assets -H 'content-type: application/json' \
  -d '{"asset_id":"a","asset_type":"table"}'
curl -s -XPOST localhost:8000/assets -H 'content-type: application/json' \
  -d '{"asset_id":"b","asset_type":"table"}'
curl -s -XPOST localhost:8000/lineage -H 'content-type: application/json' \
  -d '{"upstream":"a","downstream":"b"}'
curl -s localhost:8000/assets/a/lineage-changes     # 审计记录
curl -s 'localhost:8000/lineage/as-of?as_of=0'      # 明确 400 报错而非空图
curl -s -XPOST localhost:8000/lineage/batch -H 'content-type: application/json' \
  -d '{"ops":[{"action":"add","upstream":"a","downstream":"ghost"}]}'  # 422 整批拒绝
```

## 6. 兼容性说明

- **老用法完全保留**：`add_lineage` / `remove_lineage` / `list_lineage` /
  `upstream_neighbors` / `downstream_neighbors` / `analyze_impact(catalog, ...)`
  签名与返回不变；原有 25 个老用例方法零改动通过（另在 `test_lineage.py`
  追加 1 例直接覆盖列级自依赖收紧点，在 `test_api.py` 追加 5 例覆盖新端点）。
- 唯一的语义收紧：列级自依赖 `X.c -> X.c`（及整资产↔自身列）现在与资产级
  `X -> X` 一样被显式拒绝（此前仅资产级自依赖被拦截）。这是需求明确要求的行为；
  跨资产的一切列级血缘语义不变。
- 新增能力都是**增量**的：审计日志在登记/取消路径上自动产生，调用方无需改动；
  `MetadataCatalog(clock=...)` 为可选注入，默认使用系统时钟。
- 单条删除沿用历史语义：不校验端点是否存在（墓碑式取消），但每次尝试都会审计留痕。

## 7. 目录结构

```
metadata_catalog/
  models.py    # Column / LineageEdge / AssetVersion / Asset 领域模型
  errors.py    # 六类可区分错误（稳定 code + 诊断字段）
  catalog.py   # 登记、幂等合并、血缘校验与环检测、邻接查询、审计/回溯/批量入口
  impact.py    # 确定性 BFS 影响分析（深度限制/重复边/截断），同时适用于实时图与快照
  audit.py     # 只追加血缘审计日志 LineageChange / AuditLog
  timeline.py  # 时间点/版本折叠还原 LineageSnapshot 与非法时间判定
  batch.py     # 批量操作类型、逐条校验与试探图规划（纯函数，无锁）
  api.py       # FastAPI 路由与异常映射
tests/         # 七组测试，unittest，纯本地可复现
main.py        # 服务入口
```
