## Context

动机和范围见 proposal.md。现有 pipeline 已分离 parse/analyze/process worker，但一个 shared_storage 域内仅有一个 busy pipeline；不同 Pod 彼此不可见。实体合并在 get_storage_keyed_lock 的 GraphDB namespace 内完成；该锁覆盖来源追踪、图和向量读改写。HugeGraph 对整个 scope 的 mutation 锁及内存 pending fence 不能跨独立 Manager。图、KV、向量、状态没有跨库事务。

## Goals / Non-Goals

目标是在显式受控的存储配置上提供跨独立进程/Pod 的实际写并发，保留全部现有入口及 FAILED 人工重试语义，异常时持久化保守阻断。不同资源并行，同资源互斥，不承诺线性扩容。

非目标：自动无损接管、HugeGraph 服务端 CAS 扩展、跨库 ACID、跨 workspace 全局事务、只读服务角色、对象存储适配器、改变既有业务图或身份 ACL。本次选择共享持久文件系统这一完整部署路径，不同时开发 S3 文件系统。

## Decisions

### 1. Opt-in 支持配置

`LIGHTRAG_DISTRIBUTED_WRITES=true` 启用，`LIGHTRAG_COORDINATION_DSN` 从环境读取且不进入日志或 API 配置输出，`LIGHTRAG_SHARED_STORAGE=true` 表示运营方确认所有 Pod 的 input/working 路径是同路径共享持久卷。分布式模式只接受 PGKVStorage/PGDocStatusStorage/PGVectorStorage/HugeGraphStorage，非空 workspace；拒绝不一致的 POSTGRES_WORKSPACE 覆盖及本地存储。相同 workspace 的存储目标、图 URI/scope、embedding 模型维度和共享路径需要一致；协调库持久化配置指纹并拒绝不匹配的参与者。指纹不存密钥。

协调层放在 `lightrag/distributed/`，独立 asyncpg 连接池，不把同步 Manager dict 换成阻塞远程字典。所有实例访问同一协调库是部署前置条件；不同 DSN 指向同一物理目标的身份识别不在自动发现范围。

### 2. 持久化状态而非可过期 Redis 锁

协调库包含版本记录、workspace、operation、resource lock、document claim、mutation 和 recovery audit。短事务在 workspace 协调行上序列化元数据变更；业务/LLM/HTTP 不持有该行事务。资源锁的已提交所有权持续到正常释放，不能凭租约/心跳超时抢占。

一次工作区操作先获得持久化 shared ticket；维护操作获得 exclusive ticket，存在共享操作时等待有界时间或明确拒绝，独占期间禁止新 shared admission。参与者/操作心跳只用于诊断，不是判断 HugeGraph 已结束的证据。资源键多锁按固定顺序原子领取，冲突者有界等待；不能为同一 workspace 的所有正常实体写使用一把全局长锁。

正常完成的操作记录保留结果及阶段。进程消失的未完成操作和锁留存：即使发生在发出图请求前，也允许保守要求人工审计恢复；这是本次方案 A 明确接受的可用性代价，不伪装自动恢复。

### 3. 业务协调与物理写入屏障两层保护

核心完整读改写使用跨 Pod GraphDB keyed locks；关系锁覆盖两个端点，避免实体删除与加边竞争。ContextVar 仅传播参与协调的上下文，不作为跨进程事实来源；锁重入必须限定拥有该锁的任务，不能因为子任务继承同一 context 而互相绕过。

每个实际存储 mutation 在发送前记录 durable pending，正常返回并验证 ACK 后标记完成；异常、取消、ACK 丢失、进程死亡保留 pending。出现不确定写时整个 workspace 可被保守 fence；其他已在途请求可能完成，但不能据此清除别人的 pending。下一次存储 mutation 必须重新核对持久化状态/操作版本，避免已入场但暂停的调用绕过新的屏障。查询读可以诊断；查询 cache 写也不能绕过。

HugeGraph 在分布式模式下不再依赖其进程内全 scope 锁完成跨 Pod 保证；直接 Storage API 要么参与同样的锁/写屏障，要么明确拒绝未受控调用。默认单机路径完全保留。PG 写错误不能被封装层当作成功：必须确认当前支持的 PG 方法错误语义，修正或选用 strict 路径。

### 4. 文档领取和流水线

保留 doc_status 为事实来源；对候选文档先原子领取，再严格读回状态，再做一致性修复/解析/提取/提交。已经被其他 Pod 领取的文档不得进入修复、parse worker、feeder 或 custom-chunk 路径。领取持续覆盖全部阶段；重复入库不能覆盖处理中的文档。

本地 busy 和 mailbox 仅负责本 Pod 内的 worker 组织与唤醒；跨 Pod 用持久化领取和周期 strict scan，通知可以合并但任务不能依赖通知可靠性。分页必须在一页全部被别的 Pod 占用时仍正确前进，不能忙循环同一页。任务数量受 max_parallel_insert/parse 参数限制，不一次抢占整库。

FAILED 不自动重入。人工 retry/scan 在工作区独占门下发布持久化意图并完成一次性状态重置；重启保留意图，已完成重置不再扩大重试次数。业务失败和未确认持久化失败需要区分，后者优先 fence，而非清除屏障继续。

### 5. 全入口和运维

受控入口包括 enqueue、pipeline、parse/feeder、custom chunks/KG、entity/relation CRUD/merge、文档 purge/delete/clear、scan、retry、source conflict repair、cache clear、查询 cache write、初始化/数据迁移。SDK 与 HTTP 后台任务均需要覆盖，HTTP preflight 不能代替业务门。共享文件变更也必须在对应门内；后台任务必须自己持有有效 ticket，不能使用已结束请求的上下文。

维护类操作初版用 workspace exclusive，保证正确性而不追求与入库并行。启动迁移由显式维护执行路径与协调门保护，不能让额外副本无协调地自动迁移。API 暴露不含密钥的分布式状态；忙冲突与恢复阻断提供可识别错误，不报告伪成功。

人工恢复 CLI：只读 inspect；显式 recovery 要求所有 writer 已停止、HugeGraph/其他存储在途请求已结束、已提交状态与来源锚点已审计的运营确认。恢复保留审计历史、增加 generation 后解除选定 scope 的遗留锁/领取/屏障；停止旧进程是前置条件，generation 不是 HugeGraph 服务端 fence，不宣称可以拒绝已经发出的旧图请求。不得提供启动自动清屏障或简单 TTL 解锁。

### 6. 幂等和跨存储恢复

操作 ID 与文档 ID/内容版本绑定，来源按真实 chunk ID 集合合并，不按投递次数累加 weight。保留 full_entities/full_relations 等 attribution carrier；pending 写不是“没有发生”。部分已确认批次与未确认批次记录保留，人工恢复后的已有 pipeline/purge/custom-chunk journal 流程收敛，不做危险补偿删除。需要补齐无法由既有锚点恢复的阶段记录；不能只记录任务完成/失败两个状态。

## Risks / Trade-offs

- PostgreSQL 故障导致协调不可用 → 拒绝新写，不能降级本地锁；单机模式不受影响。
- 一次不确定 mutation 暂停整个 workspace → 保守正确，文档说明人工恢复，不承诺自动 HA。
- 热点实体仍串行 → 正确性必需；parse/extract 和互不冲突实体跨 Pod 并行。
- 外部客户端或旧版 Pod 绕过协议 → 部署必须停旧写入者后升级，不允许混合 local/distributed writer。
- 用户确认共享卷不是技术证明 → 文档给出 RWX 同路径部署验证，启动校验配置但不伪称检测整个集群挂载。

## Migration Plan

1. 备份并停止全部旧 writer；确认无未确认写。使用独立测试 workspace 验证。
2. 显式版本化 SQL/CLI migration 创建协调表和索引；重复执行幂等，应用默认 verify-only，不靠启动偷偷补 schema。
3. 将文档相关存储切换/迁移至受支持的外部 PG 配置，保留内容、来源与恢复锚点；不自动清库。
4. 挂载共享持久卷，运行协调维护初始化/数据迁移并登记相同配置指纹，然后启动多个启用分布式模式的 Pod。
5. 回退需先停所有 distributed writer、确认无 pending/未结束操作后再降为单 writer；保留协调历史表，不自动删除迁移。
