# 本地元数据目录（Local Metadata Catalog）

一个仅依赖本地环境的元数据目录服务：登记数据资产（含列级）与上下游血缘、沿血缘做
深度受限的影响分析，并通过乐观并发控制在结构演进与并发更新下给出一致、可解释的结论；
在此之上提供血缘变更的**全程审计**、按时间点/资产版本的**历史还原与回溯影响分析**，
以及**原子批量**血缘增删。

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
| `AssetNotFoundError` | `asset_not_found` | 404 | 血缘端点、审计/影响分析起点指向未知资产 | `asset` |
| `ColumnNotFoundError` | `column_not_found` | 404 | 列级血缘引用了该资产上不存在的列 | `asset`, `column` |
| `LineageCycleError` | `lineage_cycle` | 422 | 新边会成环（含资产自依赖 a→a、列自依赖 a.x→a.x；同一资产两列之间 a.x→a.y 不算自环） | `cycle`（检测到的环路径） |
| `ConcurrentUpdateError` | `concurrent_update` | 409 | 乐观锁版本不匹配 | `expected_version`, `actual_version`（资产不存在时 actual=0） |
| `InvalidTimePointError` | `invalid_time_point` | 400 | 回溯/审计的时间参数本身不合法（非数字、NaN、无穷、start>end） | `value` |
| `TimePointOutOfRangeError` | `time_point_out_of_range` | 422 | 时间合法但落在可还原窗口之外（早于首条变更/晚于最新状态） | `requested`, `earliest`, `latest` |
| `BatchLineageError` | `batch_lineage_rejected` | 422 | 批次中存在坏关系，整批拒绝 | `failures`（逐条 `index`/`code`/`message`/边四元组） |

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

## 3. 血缘变更审计（Audit Trail）

血缘的每一次实际登记（add）与取消（remove）都会在同一把写锁内**追加一条不可变
审计记录** `LineageChange`，绝不修改历史：

| 字段 | 含义 |
| --- | --- |
| `seq` | 全局单调递增的变更序号（从 1 开始，无空洞） |
| `at` | 变更时间（unix 秒）。同一进程内做了严格递增处理，保证审计是全序 |
| `op` | `add` / `remove` |
| 边四元组 | `upstream` / `downstream` / `upstream_column` / `downstream_column` |
| `before_present` / `after_present` | 该边变更前/后是否存在（前后状态） |
| `batch_id` | 所属批次（单条登记为 None；同一批次的所有变更共享） |

查询：`catalog.lineage_audit(asset_id, start=None, end=None)` → `LineageAuditResult`，
可按闭区间 `[start, end]` 时间范围筛选，记录按时间从旧到新，逐条说清某条关系是
哪一步加上、哪一步去掉。两种“看起来都空”的情形**明确区分**：

- 资产不存在：抛 `AssetNotFoundError`（`asset_not_found`），不返回空列表；
- 资产存在但从未参与任何血缘登记/取消：`status="no_changes"`、`records=[]`；
- 有变更：`status="found_changes"`。

幂等操作（重复登记同一条边、取消一条本不存在的边）不产生变更、不写审计。

## 4. 时点还原与回溯影响分析

- `catalog.lineage_as_of(at)`：还原 `at` 时刻（**含** `at` 当刻，即 time ≤ at
  的变更均已生效）的血缘图，返回不可变快照 `HistoricalLineageView`。
- `catalog.lineage_at_version(asset_id, version)`：按资产**当年的某个版本**还原——
  取该版本创建时刻的血缘；版本早于任何血缘变更时得到空图。
- 快照暴露与 live catalog 相同的 `asset_exists` / `upstream_neighbors` /
  `downstream_neighbors`，因此**同一份** `analyze_impact(view, start, direction,
  max_depth)` 可直接在历史图上跑上下游影响分析，行为与现场一致。

严格的时间语义（绝不拿空结果/看似正常的结果糊弄）：

- 参数非法（非数字、布尔、NaN、±∞）→ `InvalidTimePointError`；
- 时间合法但落在可还原窗口 `[首条变更时间, 最新状态时间]` 之外
  （更早 / 更晚，或系统尚无任何血缘历史）→ `TimePointOutOfRangeError`，
  错误中带回 `requested / earliest / latest`；
- 同一时间点反复查询结果**完全一致**（不可变快照 + bisect 定位，纯函数式）。

性能：审计按 `at` 有序索引（bisect 定位变更序号），并惰性构建每 128 条变更一个的
**重放检查点**，任意时点的重放最多跨越约两个检查点区间，与总历史规模无关；
**正常（当前）查询完全不触碰审计/重放路径**，历史再大也不会拖慢日常登记与影响分析。

## 5. 批量血缘变更（原子提交）

`catalog.commit_lineage_batch([...])`：一次提交一批 add/remove（接受 `BatchOp`
或 dict 列表），**要么全部生效、要么一条都不生效**：

1. 先在锁内模拟出“整批提交后”的候选图；
2. **逐条校验**：未知资产（`asset_not_found`）、未知列（`column_not_found`）、
   依赖绕回自己（资产级 a→a，或列级 a.x→a.x）、以及对候选图做环检测
   （`lineage_cycle`，含同一批次内新增边之间形成的环，如本批先加 d→a 再加 a→d）；
3. 只要有一条不过，抛 `BatchLineageError`，`failures` 里**逐条**给出
   `index / code / message / 边四元组`，图保持字节级不变；
4. 全部通过后按输入顺序应用，整批共享一个提交时间戳与 `batch_id`，每条实际
   变更各有独立 `seq`；幂等项（重复 add / 删除缺失边）合法但不产生变更。

并发：批次的“校验 + 应用”全程在同一把写锁内原子完成——多批次并发提交不丢改动，
边提交边查询也读不到只改了一半的图（读路径持同一把锁，拿到的永远是整批前或整批后
的图）。单条 `add_lineage` / `remove_lineage` 只是批次能力的特例，老用法不变。

## 6. 版本与并发语义

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

## 7. HTTP API

| 方法与路径 | 说明 |
| --- | --- |
| `POST /assets` | 登记/演进资产（body 可带 `expected_version`） |
| `GET /assets` / `GET /assets/{id}` | 列出/查看资产 |
| `GET /assets/{id}/versions` | 查看全部历史版本快照 |
| `POST /lineage` | 登记血缘（资产级或列级） |
| `GET /lineage` | 列出全部血缘 |
| `GET /impact/{id}?direction=downstream&max_depth=2` | 影响分析（当前图） |
| `GET /lineage/audit/{id}?start=&end=` | 某资产的血缘变更审计（含时间窗过滤） |
| `GET /lineage/as-of?at=<unix秒>` | 还原某时刻血缘图（不可变快照） |
| `GET /lineage/at-version/{id}?version=n` | 按资产版本还原血缘图 |
| `POST /lineage/batch` | 原子批量增删，body：`{"changes":[{op,upstream,downstream,...}]}` |

新端点全部为**叠加**：原有端点、请求体、状态码与错误响应结构保持不变
（老用法零回归）。错误响应统一为 `{"error": {"code": ..., "message": ..., ...}}`。

快速启动：

```bash
uv run fastapi dev main.py      # 或: uv run uvicorn main:app --reload
```

## 8. 本地验证方法

仅需 Python 标准库 + FastAPI（TestClient 随 fastapi[standard] 安装），无外部依赖：

```bash
# 运行全部用例（含关键输入与判定依据日志，[CASE] 行打印场景与输入）
uv run python -m unittest discover -s tests -v

# 仅核心逻辑（不启动 HTTP，零第三方依赖，可直接用系统 python）
python3 -m unittest tests.test_lineage tests.test_impact tests.test_concurrency \
    tests.test_audit tests.test_history tests.test_batch -v
```

测试覆盖（每个失败断言均带有 `reason:`，日志中打印关键输入）：

- `tests/test_lineage.py`：悬空资产引用、悬空列引用（错误码区分）、自依赖、
  三环路径与回显、菱形快捷边不误判、列级边独立性、拒绝后无部分写入。
- `tests/test_impact.py`：上下游方向、最小深度、`max_depth` 截断边界、
  菱形重复路径、1000 条并行路径不膨胀、30 次运行确定性、含环图终止性。
- `tests/test_concurrency.py`：幂等不产生版本、合并不丢列与血缘、历史快照
  可追溯、409 冲突携带 expected/actual、12 线程 CAS 无丢失更新（首轮必然
  冲突 + 重试成功 + 陈旧写必败）。
- `tests/test_audit.py`（新）：登记/取消留前后状态与全局 `seq`、幂等不写审计、
  **未知资产 404 与“资产在但无变更” `no_changes` 明确区分**、时间窗闭区间过滤、
  非法时间窗报错、从上下游两端还原同一条关系生命周期。
- `tests/test_history.py`（新）：时间线上每个时刻逐一对拍、**非法时间点**
  （字符串/None/布尔/NaN/±∞）报 `invalid_time_point`、**范围外**（早于首变更/
  晚于最新/无历史）报 `time_point_out_of_range` 且回带窗口、20 次重复还原一致、
  **还原图影响面与当时现场查询一致**且与今天不同、按资产 v1/v2 版本还原、
  跨检查点（300+ 变更）重放与删除正确性。
- `tests/test_batch.py`（新）：**批次混入一条坏关系整批回滚**（含合法边不落库）、
  多条坏关系逐条列出 index+原因（未知资产/未知列/列自依赖/成环）、同批次内
  互相成环被挡、增删混合整体提交且共享 `batch_id`、**16 线程并发批次无丢失**、
  边提交边读取永不出现“半批”图。
- `tests/test_api.py`：HTTP 全链路状态码与错误码区分、版本端点、深度参数，
  以及新审计/回溯/批量端点与整批回滚（422 + 逐条 failures）。

## 9. 与原有用法的兼容范围

- 完全向后兼容：`register_asset` / `add_lineage` / `remove_lineage` /
  `list_lineage` / `analyze_impact` / 版本与 CAS 接口签名与行为均保持不变；
  审计、回溯、批量均为**新增能力**，原有 HTTP 端点与错误结构不变。
- 一处收紧：列级“同资产同列”自依赖 `a.x -> a.x` 现在与 `a -> a` 一样按
  `lineage_cycle` 拒绝（此前仅资产级自依赖被挡）；同资产不同列 `a.x -> a.y`
  仍允许。重复登记、删除不存在边仍为幂等无审计。
- 时钟可注入（`MetadataCatalog(clock=...)`）便于对拍与确定性测试；默认走墙钟，
  同一进程内做严格递增保护，不影响默认部署。
- 存储仍是进程内内存结构，审计与检查点同处内存，无需任何外部服务。

## 10. 目录结构

```
metadata_catalog/
  models.py    # Column / LineageEdge / AssetVersion / Asset 领域模型，
               # 以及 BatchOp / LineageChange / LineageAuditResult
  errors.py    # 可区分错误（稳定 code + 诊断字段），含时间与批次错误、BatchFailure
  catalog.py   # 登记、幂等合并、血缘校验与环检测、邻接查询、
               # 追加式审计、时点重放（惰性检查点）、原子批量提交
  history.py   # HistoricalLineageView：不可变历史快照（可直接跑 analyze_impact）
  impact.py    # 确定性 BFS 影响分析（深度限制/重复边/截断），对 live 与历史图通用
  api.py       # FastAPI 路由与异常映射
tests/         # unittest，纯本地可复现
main.py        # 服务入口
```
