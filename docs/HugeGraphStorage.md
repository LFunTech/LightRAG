# HugeGraph 图存储

`HugeGraphStorage` 将 LightRAG 的实体和关系存入外部 Apache HugeGraph，适配基线为 **HugeGraph 1.7.x**。它只替换图存储；KV、向量、文档状态仍由另外三类后端承担。不需要同步 HugeGraph SDK，复用项目已有的异步 `aiohttp`。

## 部署与配置

先由运维创建 HugeGraph 服务、graphspace 和 graph，后端不会创建、重启或删除服务或数据库。1.7.x 使用 `/graphspaces/{graphspace}/graphs/{graph}` REST 路径。非 HStore 部署只能使用 `DEFAULT` graphspace；其他 graphspace 的可用性取决于服务配置。参见 [HugeGraph 1.7 REST API](https://hugegraph.apache.org/versions/1.7/docs/clients/restful-api/) 和 [Graphspace API](https://hugegraph.apache.org/versions/1.7/docs/clients/restful-api/graphspace/)。

在仓库根目录运行：

```bash
make env-base       # 首次配置 LLM、embedding 等
make env-storage    # Graph storage 选择 HugeGraphStorage
make env-validate
```

向导**仅连接外部 HugeGraph**，不会生成 HugeGraph compose 服务。已有 `.env` 的 URI、认证、graphspace、graph 和性能参数作为可编辑默认值保留；切换认证方式会清除冲突凭据。

也可手动配置：

```dotenv
LIGHTRAG_GRAPH_STORAGE=HugeGraphStorage
HUGEGRAPH_URI=http://localhost:8080
HUGEGRAPH_GRAPH=hugegraph
HUGEGRAPH_GRAPHSPACE=DEFAULT
HUGEGRAPH_USERNAME=
HUGEGRAPH_PASSWORD=
HUGEGRAPH_TOKEN=
HUGEGRAPH_TIMEOUT=30
HUGEGRAPH_BATCH_SIZE=100
HUGEGRAPH_MAX_CONNECTIONS=10
HUGEGRAPH_RETRIES=2
HUGEGRAPH_AUTO_CREATE_SCHEMA=true
```

| 变量 | 默认值 | 约束与含义 |
| --- | --- | --- |
| `HUGEGRAPH_URI` | 无，必填 | HTTP(S) 服务根地址，可含反向代理路径前缀；不要追加 graph/schema/Gremlin API 路径，不得包含用户名、密码、查询参数或 fragment |
| `HUGEGRAPH_GRAPH` | `hugegraph` | 已存在的非空图名称，不能为 `.` / `..` 或含控制字符 |
| `HUGEGRAPH_GRAPHSPACE` | `DEFAULT` | 已存在且服务支持的非空 graphspace，名称限制同 graph |
| `HUGEGRAPH_USERNAME`、`HUGEGRAPH_PASSWORD` | 空 | 可选 HTTP Basic 认证，必须同时提供 |
| `HUGEGRAPH_TOKEN` | 空 | 可选 Bearer token，与 Basic 凭据互斥 |
| `HUGEGRAPH_TIMEOUT` | `30` | 单个 HTTP 请求超时秒数，有限正数 |
| `HUGEGRAPH_BATCH_SIZE` | `100` | 请求与维护迭代的批次上限，正整数 |
| `HUGEGRAPH_MAX_CONNECTIONS` | `10` | 连接池上限，正整数 |
| `HUGEGRAPH_RETRIES` | `2` | 可重试只读请求的额外尝试次数，`0` 到 `10` 的整数；**不适用于写请求** |
| `HUGEGRAPH_AUTO_CREATE_SCHEMA` | `true` | 初始化时补齐专属 schema；设为 `false` 时只校验已有定义，仅接受 `true` / `false`（不区分大小写） |

`.env` 始终应供宿主机使用，不要把容器内部服务名写回其中。若 LightRAG 在容器内运行，`localhost` 指该容器而不是宿主机；请使用容器可达的外部服务地址，或在自管 compose 的 `lightrag.environment.HUGEGRAPH_URI` 中单独设置容器可用地址。向导不会凭空生成 `http://hugegraph:8080`。远程认证连接应使用 HTTPS，并通过部署侧网络策略限制服务访问。

## 独立 schema 与属性

LightRAG 使用固定的 v1 专属 schema，不导入或改写已有业务图：

- 顶点标签：`lightrag_entity_v1`，`CUSTOMIZE_STRING` ID。
- 边标签：`lightrag_relation_v1`，连接上述顶点标签，`SINGLE` 频率。
- 专属属性：`lightrag_scope`、`lightrag_name`、`lightrag_data`，均为 `TEXT` / `SINGLE`。
- 顶点联合索引 `lightrag_entity_scope_name_v1` 按 scope/name 顺序覆盖两字段查询及仅 scope 的左前缀查询；边索引 `lightrag_relation_scope_v1` 覆盖 scope，二者均为 `SECONDARY`。

不另建冗余的顶点 scope 单字段索引：HugeGraph 1.7 创建联合索引时会自动删除已有前缀索引，之后也拒绝重新创建该前缀，因而维护两者会破坏重复初始化。仅 scope 的带游标分页查询已在 1.7.0 验证可用。该规则见 [HugeGraph 1.7.0 IndexLabelBuilder](https://github.com/apache/hugegraph/blob/1.7.0/hugegraph-server/hugegraph-core/src/main/java/org/apache/hugegraph/schema/builder/IndexLabelBuilder.java)。需要创建索引时，客户端额外读取一次索引定义清单，发现可能触发隐式删除的既有重叠索引便拒绝初始化，而不是替用户删除或替换它；初始化期间不要由其他运维工具并发修改这些标签的 schema。

`lightrag_data` 保存 JSON 对象。业务字段不直接扩展 HugeGraph 的全局 property schema，因此 `weight`、字符串、布尔值等标量在读回时保留类型，不会全部变为字符串。属性键须为字符串，值只接受字符串、布尔值、整数与有限浮点数，不接受 `null`、数组、嵌套对象或非有限数。部分更新合并已存在的属性，不删除本次未提供的字段；批量重复写入同一对象按输入顺序合并，冲突字段最后一次输入生效。

第一次使用默认会创建缺失的专属定义，并等待索引任务成功。以后初始化校验定义兼容性；并发创建冲突会重新读取并验证。发现同名不兼容 schema 会报错，**不会删除或自动修复它**。已有兼容定义时可用 `HUGEGRAPH_AUTO_CREATE_SCHEMA=false` 限制启动为只校验。预置 schema 时应由运维根据当前后端定义创建，或在受控维护窗口使用自动创建模式初始化一次，再切换到只校验。只校验不免除所需的读 schema、读图和正常业务读写权限。

服务的 graph/schema 权限与 LightRAG 的 API 认证分属两层。后端不会创建用户或授予权限。schema 定义可使用 [HugeGraph 1.7 Schema API](https://hugegraph.apache.org/versions/1.7/docs/clients/restful-api/schema/) 检查。

## 隔离、ID 与并发边界

- `workspace` 和 storage `namespace` 一起编码为 scope；相同实体名在不同范围对应不同物理顶点。二者必须在所有相关存储中保持一致。
- 内部顶点 ID 是 scope 与原始实体 ID 无歧义编码的 SHA-256 摘要，带 `lr-` 前缀。外部 API、图导出与检索仍返回原始实体 ID。
- 无向关系先规范排序两个原始端点，再写一条物理边。反向读取和重复 upsert 指向同一关系，不产生双份边，也不自行累加权重。
- 读写、遍历和删除同时校验专属标签与 scope。`drop()` **只删除当前 workspace/namespace 的数据**，保留其他 scope、既有业务数据、整个 graph、graphspace 和所有 schema。

**scope 是逻辑分区，不是 ACL 或安全租户边界。** 拥有 HugeGraph 访问权限的其他客户端仍可能读取其他 scope。需要强隔离时，应由部署侧划分 graph/graphspace、账户、权限及网络边界，不要仅依赖 workspace。

部分更新是读改写：后端使用 `shared_storage` 专属 mutation 锁串行化同一范围的写入和删除。协调键同时包含**规范化后的目标服务 URI**、graph 路径与 workspace/namespace scope，互不相同的数据库目标不会共用不确定写状态。URI 使用与 `aiohttp` 一致的 `yarl.URL` 规范化，统一 scheme/host 大小写、默认端口、等价 URL 编码与 IPv6 表示，避免这些等价拼写生成不同的锁或屏障身份。该保护只覆盖**同一 `shared_storage` 协调域**内的进程；多个完全独立的 LightRAG 部署同时写同一 scope 不在安全支持范围。它不是 NetworkX 的 `requires_single_writer` 模型，不替代核心实体锁，也不提供数据库级跨部署锁。维护全量扫描若要求无遗漏、无重复，应在图静止时执行。

URI 规范化**不是服务发现或后端身份识别**：不同 DNS 别名、`localhost` 与 `127.0.0.1`，以及多个反向代理地址即使最终连接同一 HugeGraph，也不能自动合并为同一协调键。**访问同一目标 scope 的全部 writer 必须统一使用同一个服务 URI，并属于同一协调域。** 不得改用别名、另一代理地址或另建协调域绕过 pending fence，否则会失去并发写入和不确定写保护。

仅靠锁和“不自动重试”还不够：HTTP 请求超时不代表服务端事务已经停止，迟到的旧整包仍可能覆盖后继写入。因此每个图数据 mutation 在发送前，先在专用共享 namespace 中记录 **pending fence（待确认写入屏障）**；只有成功响应及写入确认内容均校验通过后才清除。超时、取消、请求异常、异常确认内容，或发送后 worker 异常退出，都会保留该屏障。在协调域仍存活时，其他 worker 即使已取得释放后的锁，也必须拒绝同一目标 scope 的后续写入和删除，包括 `drop()`；不会继续发请求，也不会把前一次写入当作未发生。只读操作仍可用于排查，其他目标 scope 不受该屏障影响。

屏障属于整个 `shared_storage` 协调域，不属于某个 client 连接；`finalize()` / `initialize()`、重建 Storage 实例或单个 worker 重启都不能将其解除。它不是跨整个协调域重启的持久化数据库日志，因此不能靠未经审计的全量服务重启绕过不确定写保护。恢复步骤见下节。

## 持久化、失败与恢复

成功返回的写入已提交到 HugeGraph；`index_done_callback()` 不再刷写本地缓冲。但这**不构成跨图、KV、向量和文档状态存储的事务**，也不保证一批多个请求全成功或全失败。

- 确认不存在才返回空值；认证失败、网络异常、超时、非法响应或 Gremlin 错误均抛出异常，不能当作不存在或空图。
- 写请求不自动重试。响应丢失可能意味着服务已完成写入，也可能仍有服务端事务在途；异常意味着结果未确认，不能解释为“没有发生任何修改”。待确认屏障会阻止同域 scope 的排队或新写请求继续执行。
- 批量中前面的请求可能已经提交。后端显式报错，不吞没失败，也不通过补偿删除已成功对象来伪造原子性。
- 稳定 ID、规范无向边和非累加属性赋值使**相同确定性写入**可以幂等重放，但必须先按下面的流程解除不确定写状态。屏障存续期间，普通重试同样会被拒绝，不会自行清除屏障或重放。
- 保留所有恢复锚点及其他存储。文档删除/重试仍遵循 [PurgeRecoveryContract](./design/PurgeRecoveryContract.md) 和 [PipelineConcurrencyContract](./design/PipelineConcurrencyContract.md)；不要绕过核心流程直接清空 graph。

### 不确定写的人工恢复

1. **停止全部 writer**，包括自动重启或新建 worker 的调度器；不要只关闭报错 client，也不要改用服务 URI 别名、另一代理地址或启动独立协调域来绕过屏障。
2. 由运维在 HugeGraph 侧**确认没有仍在途或可能迟到提交的旧 mutation**，并审计已提交图状态、批次进度及 LightRAG 恢复锚点。仅健康检查成功、等待一个客户端 timeout、重新连接或一次读回都不足以证明旧请求已经停止。不能确认时保持停止状态。
3. 确认安全后，**重启整个 LightRAG `shared_storage` 协调域**，包括 Manager 及共享它的全部 worker，使旧 pending fence 随旧协调域一起退出。仅 `finalize()` / `initialize()` 或重启一个 worker 不属于此恢复步骤。
4. 再人工重试原来的确定性操作，或通过 LightRAG 既有文档恢复流程继续；验证所有存储和锚点一致后再恢复正常流量。不要通过删除图、scope、schema 或恢复锚点来“解锁”。

这是一项有意的保守策略：即使最终发现请求没有写入，也可能需要上述人工恢复；宁可停止后继写，也不允许迟到事务覆盖已经向其他调用方确认的更新。已提交的部分批次保留，后续受控重放收敛，不做危险的补偿删除。关系权重规则仍由 [核心 relation weight contract](./ProgramingWithCore.md#relation-weight-contract) 决定。

## 图浏览与成本

`iter_labels()` 和 `iter_edges()` 使用服务端分页，客户端不先收集全图。普通批量请求按 `HUGEGRAPH_BATCH_SIZE` 分片；完整导出接口因返回值是完整列表，最终仍需要与结果大小相称的内存。

子串标签搜索和全图热门度排名**不是全文索引或常数开销查询**：它们可能扫描当前 scope 全图并计算度数，网络和服务端成本随图规模增长。实现通过分页和有限候选集约束客户端工作内存；`limit` / `max_nodes` 限制返回规模，不能理解为只检查这么多条数据。度数相同的候选按实体名 Unicode 码点升序稳定排序，热门标签包含孤立实体。

局部图按 BFS 层级扩展，遵守节点数上限；被截断时响应显式标记，不返回端点缺失的悬空边。大规模图应合理设置请求超时和页面大小，并在真实数据量下评估搜索、排名与导出成本。

## 使用示例与迁移

完整 Python 示例：[examples/lightrag_hugegraph_example.py](../examples/lightrag_hugegraph_example.py)。它使用现有 OpenAI embedding 函数，先 `await rag.initialize_storages()`，再执行异步入库与图检索，最后在 `finally` 中 `await rag.finalize_storages()`。

**不会自动迁移已有 LightRAG 后端的数据。** 不要只把 `LIGHTRAG_GRAPH_STORAGE` 改为 HugeGraph，却继续混用原图对应的 KV、向量和文档状态。迁移时停止写入、备份四类存储，在隔离的新 workspace/目录和匹配的其他存储中受控重建、验证，然后切换流量。换 embedding 模型同样需要重建匹配的向量数据，参见 [Custom Embedding Functions](./ProgramingWithCore.md#custom-embedding-functions)。

回滚配置不会删除 HugeGraph 数据。清理测试范围必须明确指定该次测试的 workspace 与 namespace；不要调用 HugeGraph 的全图 clear、graph 删除或 schema 删除 API。
