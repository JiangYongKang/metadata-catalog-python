# 本地元数据目录（Local Metadata Catalog）

一个仅依赖本地环境的元数据目录服务：登记数据资产（含列级）与上下游血缘、沿血缘做
深度受限的影响分析，并通过乐观并发控制在结构演进与并发更新下给出一致、可解释的结论。

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
| `AssetNotFoundError` | `asset_not_found` | 404 | 血缘端点或影响分析起点指向未知资产 | `asset` |
| `ColumnNotFoundError` | `column_not_found` | 404 | 列级血缘引用了该资产上不存在的列 | `asset`, `column` |
| `LineageCycleError` | `lineage_cycle` | 422 | 新边会成环（含自依赖） | `cycle`（检测到的环路径） |
| `ConcurrentUpdateError` | `concurrent_update` | 409 | 乐观锁版本不匹配 | `expected_version`, `actual_version`（资产不存在时 actual=0） |

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
| `GET /impact/{id}?direction=downstream&max_depth=2` | 影响分析 |

错误响应统一为 `{"error": {"code": ..., "message": ..., ...}}`。

快速启动：

```bash
uv run fastapi dev main.py      # 或: uv run uvicorn main:app --reload
```

## 5. 本地验证方法

仅需 Python 标准库 + FastAPI（TestClient 随 fastapi[standard] 安装），无外部依赖：

```bash
# 运行全部 26 个用例（含关键输入与判定依据日志）
uv run python -m unittest discover -s tests -v

# 仅核心逻辑（不启动 HTTP，零第三方依赖，可直接用系统 python）
python3 -m unittest tests.test_lineage tests.test_impact tests.test_concurrency -v
```

测试覆盖（每个失败断言均带有 `reason:`，日志中打印关键输入）：

- `tests/test_lineage.py`：悬空资产引用、悬空列引用（错误码区分）、自依赖、
  三环路径与回显、菱形快捷边不误判、列级边独立性、拒绝后无部分写入。
- `tests/test_impact.py`：上下游方向、最小深度、`max_depth` 截断边界、
  菱形重复路径、1000 条并行路径不膨胀、30 次运行确定性、含环图终止性。
- `tests/test_concurrency.py`：幂等不产生版本、合并不丢列与血缘、历史快照
  可追溯、409 冲突携带 expected/actual、12 线程 CAS 无丢失更新（首轮必然
  冲突 + 重试成功 + 陈旧写必败）。
- `tests/test_api.py`：HTTP 全链路状态码与错误码区分、版本端点、深度参数。

## 6. 目录结构

```
metadata_catalog/
  models.py    # Column / LineageEdge / AssetVersion / Asset 领域模型
  errors.py    # 四类可区分错误（稳定 code + 诊断字段）
  catalog.py   # 登记、幂等合并、血缘校验与环检测、邻接查询
  impact.py    # 确定性 BFS 影响分析（深度限制/重复边/截断）
  api.py       # FastAPI 路由与异常映射
tests/         # 四组测试，unittest，纯本地可复现
main.py        # 服务入口
```
