## Context

动机见 proposal.md。现有 BaseGraphStorage 和图测试要求无向关系、标量属性、错误不吞没、有界维护扫描。本机 DEFAULT/hugegraph 已有其他业务数据；仅新增独立 schema。

## Goals / Non-Goals

目标：完整后端兼容与可重复验证。非目标：导入既有业务图、改变四类存储分工、引入跨服务事务、修改 API 权限、部署或重启本机服务、自动迁移旧图数据。

## Decisions

- 复用 aiohttp；HTTP/Gremlin 协议与 schema 放在 `lightrag/kg/hugegraph_client.py`，图操作放在 `hugegraph_impl.py`。避免同步 SDK 和额外线程层。
- 客户端采用 graphspace REST 路径，Gremlin aliases 为 `graphspace-graph` 和 `__g_graphspace-graph`。绑定原生 JSON 标量/列表，脚本固定（不使用服务端 JsonSlurper，其类初始化被本机 Gremlin 沙箱拒绝）；自动解压、双层状态验证、有限只读重试。写请求不自动重试，以免在结果不确定时与后继写入重叠。
- v1 schema：顶点 `lightrag_entity_v1`，CUSTOMIZE_STRING ID；边 `lightrag_relation_v1`，同一顶点类型、SINGLE；TEXT/SINGLE 属性 `lightrag_scope`、`lightrag_name`、`lightrag_data`。scope 是 workspace/namespace 的 JSON 编码，内部顶点 ID 为 `lr-` + SHA256(scope 与原始实体 ID 的无歧义编码)。仅两个 SECONDARY 索引：顶点 `lightrag_entity_scope_name_v1` 按 scope/name 联合索引（左前缀覆盖 scope-only 分页），边 `lightrag_relation_scope_v1` 按 scope 索引。HugeGraph 1.7 会自动删除被新联合索引覆盖的旧前缀索引，并拒绝重建它，因此不得另建顶点 scope 单索引；已用本机 1.7.0 验证重复初始化与 scope-only 带游标查询。创建前仅一次索引清单预检，拒绝会隐式替换既有重叠索引的配置，不主动删除任何定义；运维不得并发修改专属标签 schema。业务标量属性全部存 JSON 文本，避免全局 schema 属性名冲突和类型损失。
- 原始 ID 对端点规范排序，唯一物理边；任何反向访问统一规范化。部分更新按属性合并，重复批量按输入顺序合并。内部 scope/name/ID 校验防止损坏或碰撞误写。
- 复用 shared_storage keyed-lock 的专用后端 mutation 锁，序列化同一目标范围内的读改写及删除；协调键包含规范化目标 URI、graph_path 与 workspace/namespace scope。目标 URI 使用与 aiohttp 一致的 yarl.URL 规范化，统一 scheme/host 大小写、默认端口、等价 URL 编码及 IPv6 表示，使这些等价拼写共用锁与 fence。它不是 NetworkX 的 requires_single_writer，也不取代核心调用方实体锁。多个独立 LightRAG 部署写同一范围不在安全支持边界内。
- URI 规范化不解析不同入口的真实后端身份：DNS 别名、localhost 与 127.0.0.1、不同代理地址不能自动合并。访问同一目标 scope 的全部 writer 必须统一使用同一个服务 URI 及同一 shared_storage 协调域；禁止改用地址别名、另一代理入口或另建协调域绕过 pending fence。
- 每个图数据 mutation 在发送前先写专用 shared namespace 的 pending fence，仅在成功响应且 acknowledgement 校验通过后清除。超时、取消、请求异常、异常 acknowledgement 或发送后 worker 死亡留下 fence；协调域存活时后继同键 mutation（包括删除与 drop）必须 fail closed，即使 keyed-lock 已释放或其死 worker 持有者被回收。读仍允许用于排查，其他目标 scope 不受阻。finalize/initialize、重建 Storage 或重启单个 worker 不清 fence。屏障不是跨完整协调域重启的持久化日志：恢复必须停止所有 writer，由运维确认服务端无在途请求并审计已提交状态，随后重启整个协调域，最后才人工 retry；不得未审计即新建协调域绕过屏障。
- Schema 按定义校验，支持 create-if-missing 和 verify-only；并发创建冲突后重新读取验证。索引任务必须等待成功。不会自动删改不兼容定义。
- 全量接口允许其返回类型要求的聚合，但 iter_labels/iter_edges 必须使用服务端游标分页。标签搜索和全图排名采用有界分页扫描和有限候选缓存保证完整性；明确计算成本，不伪造索引加速。BFS 按层扩展，节点上限及响应截断准确。
- 成功写入即时可见，index_done_callback 无缓冲。跨存储失败沿现有恢复流程处理，不改变 anchor/graph/KV/vector 顺序。

## Risks / Trade-offs

- 整包部分更新存在并发读改写风险 → 同一规范化目标 URI 与同一协调域内的 mutation 锁加发送前 pending fence。客户端超时并不取消服务端事务，只释放锁会让迟到旧整包覆盖后继已确认写入，因此不允许把这一数据损失窗口作为可接受残留。不能识别 DNS/代理别名是否指向同一后端，要求运维统一服务 URI；拒绝承诺跨别名入口或独立部署的串行化。
- 响应丢失可能已提交或仍在途 → 写入报错、不自动重试且保留 pending fence，拒绝后继同范围 mutation。即使实际未提交也可能需要人工恢复，接受可用性降低而不接受丢失已确认数据。先停全部 writer，确认服务端无在途请求并审计状态，重启整个协调域后，稳定 ID、非累加属性及核心恢复流程才允许受控重试。批次部分成功残留可重放收敛，不尝试危险的补偿删除。
- 全局度数排名/子串搜索需要扫描 → 有界网络分页与 top-k 内存，完整结果优先；文档明确复杂度。
- 本机 Gremlin 曾偶发 refCnt 500 → 隔离集成测试覆盖真实协议，错误必须向调用方暴露。
- 共享业务 graph → 专属 labels/properties/indexes；drop 只删 scope 数据并保留 schema。

## Migration Plan

先部署后端代码，通过专属 schema 初始化或预置，配置新 workspace 后写入。已有 LightRAG 数据若需切换必须另行受控重建，不能只切换图后端却复用不一致的其他存储。回滚配置不删除 HugeGraph 数据；显式清理只针对测试范围。无需 PostgreSQL、OpenFGA 或 Keycloak migration。
