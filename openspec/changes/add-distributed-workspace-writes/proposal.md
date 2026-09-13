## Why

当前 HugeGraph 后端的实体锁、pipeline 状态和未确认写入屏障只覆盖一个 shared_storage 协调域。多个独立 Pod 同时写同一 workspace 时可能发生来源覆盖、重复调度、删除竞争和迟到提交；仅增加副本或分布式租约不能解决这些问题。

用户已批准先实现方案 A：真正的同 workspace 多 Pod 写入，异常时以持久化屏障保守停止，不承诺无损自动故障接管；先完成本 proposal，再执行并验证。

## What Changes

- 新增显式启用的 PostgreSQL 分布式写入协调模式，默认单机行为不变。
- 持久化工作区操作、文档领取、细粒度资源锁、写入确认和恢复审计；不因超时、失去心跳或 Pod 重启而自动清除未确认状态。
- 不同文档与不同实体可以由多个 Pod 同时处理；同一文档只有一个有效处理者，同一实体/关系的完整读改写过程协调互斥。
- 文档状态仍是任务事实来源；持久化领取及周期扫描补足跨 Pod 唤醒。FAILED 仍需显式人工重试。
- 图、向量、KV、文档状态的写入均纳入协议，保留既有来源锚点、关系权重和 purge 恢复规则；不引入跨存储 ACID 承诺。
- 删除、清空、扫描分类、手工编辑、自定义 KG/chunks 和启动迁移接入相同工作区屏障；不能仅保护 HTTP 正常入库。
- 分布式支持配置收敛为 PGKVStorage + PGDocStatusStorage + PGVectorStorage + HugeGraphStorage，文件使用同路径共享持久存储。其他组合启动时拒绝分布式模式，而非默默降级。
- 提供显式版本化迁移、只读诊断及人工恢复命令、部署文档与多独立进程故障测试。

## Capabilities

### New Capabilities

- `distributed-workspace-writes`: 跨 Pod 写入协调、任务领取、持久化故障屏障、完整入口覆盖、恢复运维与兼容部署。

### Modified Capabilities

无已归档正式 spec。既有 `add-hugegraph-storage` change 的默认单协调域合同保持不变，本功能为显式 opt-in 扩展。

## Impact

涉及 lightrag 核心生命周期、pipeline、shared_storage 实体锁、HugeGraph 与 PostgreSQL 存储写入口、API 管理操作及配置、SQL 迁移、部署文档和对应测试。不读取或改造现有 HugeGraph 业务图；不切换正在运行的本地测试服务配置；不改身份权限模型。

新增协调表及迁移仅使用本仓库的 Python/PostgreSQL 工具约定，不引入其他项目的 Flyway/Alembic/租户 Schema 体系。原有 JSON/Nano/File 存储继续限于非分布式模式。
