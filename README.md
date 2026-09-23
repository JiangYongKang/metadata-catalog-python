# 本地元数据目录（metadata-catalog-python）

一个仅依赖本地环境即可运行的元数据目录，用于登记数据资产与血缘、沿血缘做
影响分析，并在结构演进与并发更新下给出一致且可解释的结论。

- 资产（含列级）与上下游血缘登记，重复登记幂等合并且不丢失已有关联
- 指向未知资产/列的引用、会形成环的血缘，被明确拒绝并给出可区分原因
- 任一资产出发的上游/下游影响分析，深度可限定，结果稳定、可复现、有界
- 资产定义的版本演进历史可追溯；乐观并发控制杜绝丢失更新，冲突显式暴露
- 测试仅用 Python 标准库 `unittest`，无需数据库或外部服务

## 快速开始

```bash
# 环境：Python >= 3.14，使用 uv 管理依赖
uv sync
uv run uvicorn main:app --reload        # 启动 HTTP 服务，默认 http://127.0.0.1:8000
uv run python -m unittest discover -s tests -v   # 运行全部测试
```

交互式文档见 `/docs`（Swagger）。

## 领域模型

代码位于 `metadata_catalog/`：

| 模型 | 说明 |
| --- | --- |
| `Asset` | 数据资产（表/任务等），不可变值对象，含 `id / kind / name / columns / description` |
| `Column` | 资产内的列，含 `name / type / description` |
| `LineageEdge` | 一条有向血缘 `upstream_id -> downstream_id`，含可选的列级血缘集合 |
| `ColumnLineage` | 列级血缘对 `(upstream_column, downstream_column)`，表示下游列由哪个上游列派生 |
| `AssetRevision` | 资产某次修订的不可变快照：`version / asset / changed_at / reason` |
| `RegisterResult` | 登记结果：版本号、是否新建、是否合并、判定说明 |
| `ConflictResult` | 并发冲突的结构化结果：`expected_version / actual_version / reason` |

血缘图（`graph.LineageGraph`）用双向邻接表保存边，边以
`(upstream_id, downstream_id)` 去重。

### 幂等合并语义

- **资产重复登记**：列按列名做键合并——只增不删；新登记可补充列的类型/
  描述；`kind/name/description` 的空值不覆盖已有非空值。合并后若结构无变化，
  视为幂等空操作，**版本号不增长，不产生新修订**。
- **血缘重复登记**：同一对端点的边只保留一条，列级血缘对按集合求并。
  重复登记只会增补关联，绝不删除已有列级关联。
- 资产重新登记或结构演进**不会删除任何已有血缘边**。

### 拒绝原因（异常类型一一可区分）

| 异常 | HTTP | error code | 触发条件 |
| --- | --- | --- | --- |
| `DanglingReferenceError` | 422 | `dangling_reference` | 端点资产未登记，或列级血缘引用了不存在的上游/下游列（消息会区分具体是端点还是列、上游还是下游） |
| `CycleDetectedError` | 422 | `cycle_detected` | 新增边会使图成环（含自环） |
| `DuplicateColumnError` | 422 | `duplicate_column` | 资产定义内存在同名列 |
| `ConcurrentUpdateError` | 409 | `concurrent_update` | 携带的 `expected_version` 已过期 |
| `AssetNotFoundError` | 404 | `asset_not_found` | 查询/分析的资产不存在 |

悬空引用的判定优先于环判定：先保证两个端点与列都存在，再讨论结构合法性。
环判定依据：若下游端点已能沿下游方向到达上游端点，则新边将闭合成环。

## 影响分析规则

`MetadataCatalog.impact(asset_id, direction, max_depth=None)`：

- `direction="downstream"` 分析“我变更会影响谁”；`"upstream"` 分析“我的数据从哪来”。
- BFS 遍历，输出每个节点的**最短深度**：起点深度为 0，直接上下游为 1，依此类推。
- `max_depth` 限定遍历层数；达到上限且边界外仍有节点时，结果中 `truncated=true`。
  `max_depth=None` 表示不限深度；`max_depth=0` 只返回起点本身。
- `visited` 集合保证每个节点只访问一次。因此在**含环、菱形重复路径、大量节点**
  的图上：结果有界（O(V+E)）、不会重复计算或超时；节点按 `(depth, id)` 排序输出，
  多次运行结果完全一致、可复现。

返回示例：

```json
{
  "start": "a", "direction": "downstream", "max_depth": 1,
  "truncated": true, "node_count": 3, "affected_count": 2,
  "nodes": [{"id": "a", "depth": 0}, {"id": "b", "depth": 1}, {"id": "c", "depth": 1}]
}
```

## 版本与并发语义

- 每次**实质性**结构变更追加一条不可变 `AssetRevision`，可通过
  `history(asset_id)` 查看完整结构演进（含变更原因与时间戳）。血缘独立于资产
  修订保存，因此“变更前后的血缘归属”始终可解释。
- 所有写操作由单一可重入锁串行化，保证内存状态一致。
- 跨请求的并发安全采用**乐观并发控制（CAS）**：
  - `register_asset(asset, expected_version=v)`：基于版本 `v` 修改；若期间已被
    他人推进，抛 `ConcurrentUpdateError`，被拒变更**不落库、不覆盖**。
  - `try_update_asset(asset, expected_version=v)`：不抛异常，成功返回
    `(True, RegisterResult)`，冲突返回 `(False, ConflictResult)`，调用方必须
    显式处理冲突——系统绝不会静默吞掉冲突。
  - 多写者竞争同一版本时，恰有一个提交成功，其余全部收到冲突；最终版本号
    严格等于“初始版本 + 成功提交数”，不发生丢失更新（见并发压测用例）。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `PUT` | `/assets/{id}` | 登记/演进资产；可带 `expected_version` 做乐观更新 |
| `GET` | `/assets/{id}` | 查询当前定义与版本 |
| `GET` | `/assets/{id}/history` | 查询版本修订历史 |
| `PUT` | `/lineage` | 登记血缘边（悬空/成环返回 422 可区分错误码） |
| `GET` | `/impact/{id}?direction=downstream&max_depth=2` | 影响分析 |
| `GET` | `/health` | 健康检查 |

调用示例：

```bash
curl -X PUT localhost:8000/assets/users -H 'content-type: application/json' \
  -d '{"kind":"table","columns":[{"name":"id","type":"bigint"},{"name":"name"}]}'
curl -X PUT localhost:8000/assets/orders -H 'content-type: application/json' \
  -d '{"kind":"table","columns":[{"name":"id"},{"name":"user_id"}]}'
curl -X PUT localhost:8000/lineage -H 'content-type: application/json' \
  -d '{"upstream_id":"users","downstream_id":"orders",
       "columns":[{"upstream_column":"id","downstream_column":"user_id"}]}'
curl 'localhost:8000/impact/users?direction=downstream&max_depth=5'
```

## 本地验证

```bash
uv run python -m unittest discover -s tests -v
```

测试覆盖（均只依赖标准库，可离线复现）：

- `tests/test_lineage_validation.py`：悬空端点、悬空列（上/下游区分）、
  自环与间接环、悬空与环同时存在时的优先判定、血缘幂等合并不丢关联。
- `tests/test_impact_analysis.py`：最短深度、深度限制与 `truncated`、
  环上有界且可复现、上下游对称、500 节点链的性能与无界膨胀防护。
- `tests/test_versioning_concurrency.py`：历史修订、演进后血缘保留、
  幂等不增版本、过期版本冲突（抛异常与结构化结果两种形式）、
  8 线程竞争的丢失更新压测、20 线程并发血缘登记。
- `tests/test_api.py`：HTTP 层 422/409 错误码可区分、历史与影响分析接口。

每个用例都会通过 `[case] ...` 日志打印关键输入与判定依据；失败断言均带
`msg=` 说明具体判定原因。

## 目录结构

```
metadata_catalog/
  exceptions.py   # 可区分的拒绝原因
  models.py       # Asset / Column / LineageEdge / Revision 等领域模型
  graph.py        # 血缘图存储、环判定、有界 BFS
  analysis.py     # 影响分析与稳定结果格式化
  catalog.py      # 门面：幂等登记、版本演进、乐观并发、校验
  api.py          # 可选 FastAPI HTTP 门面
tests/            # 标准库 unittest 测试
main.py           # uvicorn 入口
```
