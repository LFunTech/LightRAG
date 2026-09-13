# Execution contract

Spec: `specs/distributed-workspace-writes/spec.md`. Design: `design.md`. 用户已批准先 proposal 再执行方案 A；在当前目录功能分支工作，不建立 worktree。

Global Constraints: 默认单机兼容；分布式 profile 为 PGKV/PGDocStatus/PGVector/HugeGraph 与共享文件路径；真正不同资源并发，不是全局单 writer；未确认写持久化且不自动解锁；FAILED 仅人工重试；所有 SDK/API/后台变更入口均受控；保留 attribution anchors 与 relation weight 合同；本地独立测试 workspace，不修改现有业务数据或 .secrets；代码/注释/提交英文；说明中文。遵循 TDD 和 mirror 测试目录。先使用定向测试，跨切面完成后全量里程碑验证。

## 1. 持久化协调基础

### Task 1: PostgreSQL coordinator, migration and recovery CLI

- [x] 1.1 新增 `lightrag/distributed/` 协调模块、显式幂等版本化 SQL 迁移和 verify-only 初始化；不修改现有 PG 业务表/数据。
- [x] 1.2 实现 workspace shared/exclusive operation、持久化资源多锁、文档 try-claim、操作/领取阶段和心跳，以及 mutation pending/ack/fence；所有协调变更在短事务内完成。
- [x] 1.3 实现无密钥 inspect 和显式带三项运维确认的审计恢复 CLI；进程死亡/协调池重建不能丢屏障、不能因 TTL 自动抢占。
- [x] 1.4 TDD 验证独立客户端资源并行、同 key 互斥、取消/崩溃/迟到写屏障、维护排他、迁移幂等和恢复审计。真实 PostgreSQL 测试标记 integration 并使用隔离 scope。

Ownership: 新 `lightrag/distributed/coordinator.py`、`__init__.py`、`__main__.py`、`migrations/`、`tests/distributed/test_coordinator*.py`；不修改 pipeline/API/core。无需运行真实 LLM。允许复用 asyncpg，依赖未装则报告，不能输出 DSN 密钥。

Required integration surface: `PostgresCoordinator` 用独立 asyncpg pool，作用域包含 deployment identity + workspace，配置 manifest 比较；`operation(kind, exclusive=False)` async context manager 返回持久化 operation（id/generation），上下文可显式传递；`lock(operation, keys)` 为整段 async context manager；`try_claim_document(operation, doc_id)` 及所有者校验/释放；`mutation(operation, backend, namespace, method)` async context manager 在 yield 前 durable pending，成功后 ACK，异常保留并 fence；`inspect()`、`recover(...)`。可以调整命名但完整公开接口须在 report 中列出供 Task 2/3 使用。重入限制为同一 task；子任务不能因继承同 operation 而绕过彼此的 keyed lock。无法确认结束的 operation 不自动回收。已提交 ticket/lock 只能由 owner 正常完成或显式恢复释放。并发错误及取消必须 fail closed，但普通等待超时不能毒化从未入场的操作。

## 2. 核心写入口和存储接入

### Task 2: Runtime guards and storage integration

- [x] 2.1 新增分布式配置解析与 startup profile/manifest 校验；只在 opt-in 时创建协调器，失败不回退本地模式。
- [x] 2.2 接入核心 public SDK 写入口、查询 cache 写及实体/关系 keyed lock；图、KV、向量、doc_status mutation 必须 durable pending/ack，完整读改写跨 Pod 互斥。
- [x] 2.3 配置 HugeGraph 分布式写路径：不同实体真实并行，同实体/端点冲突受控，单机原行为不变；不能用一把长期 workspace/global mutation 锁掩盖缺口。
- [x] 2.4 生命周期、数据迁移、自定义 KG/chunks、purge、cache clear 接入协调，后台继承失效 ticket 必须拒绝/重新取得许可；补全错误传播与对应 TDD 回归。

Ownership: `lightrag/distributed/runtime.py`（可拆文件）、`lightrag/lightrag.py`、`lightrag/kg/shared_storage.py`、`lightrag/kg/hugegraph_impl.py`、必要 `operate.py/utils_graph.py/storage_migrations.py/postgres_impl.py`，以及相应 mirror tests。不得修改 Task 1 coordinator 协议而不向主 agent 报告。读 Pipeline/Purge/File-backed 合同后修改调用。禁止自动迁移运行中的既有数据。运行时必须提供后续 pipeline/API 可复用的 shared/exclusive operation 装饰器/上下文和当前 coordinator；直接 storage 写入不能绕过确认屏障。对 pipeline 入口的具体领取和 API 路由 wiring 由 Task 3 完成。

## 3. 调度、管理 API 与应用生命周期

### Task 3: Distributed pipeline and API control plane

- [x] 3.1 文档在一致性修复/feeder/parse 之前原子领取并严格读回，完整生命周期保持所有权；分页跳过其他领取、限额调度及周期 strict scan 支持多个独立进程处理同 workspace。
- [x] 3.2 FAILED 维持显式一次性 retry；scan/retry/冲突修复/清空/删除等维护、文件变更和 HTTP 后台任务在 durable workspace 门内；不能仅加 preflight。
- [x] 3.3 API lifespan 初始化/迁移与 polling worker 启停受控，状态接口展示分布式运行/阻断且不泄露密钥；忙/恢复阻断错误可辨识，不伪成功。
- [x] 3.4 TDD 覆盖同文档领取竞争、丢通知、同实体来源合并、空候选页、跨 Pod 维护竞争、后台 ticket 失效及关闭后的重启；保留旧单机行为。

Ownership: `lightrag/pipeline.py`、`lightrag/distributed/pipeline.py`（可选）、`lightrag/api/config.py/lightrag_server.py/routers/`、必要 runtime 接口与 mirror tests。SDK 和 HTTP 皆需可用。不得通过 workspace 单 pipeline leader 或整库领取将真并发降级。用于 LLM 的模拟必须留在测试；生产使用实际现有 pipeline。文档内容与锚点是事实，claim 表不复制替代文档内容。

## 4. 完整交付验证与部署

### Task 4: Deployment, fault tests and final verification

- [x] 4.1 文档/env/Helm 配置支持完整推荐部署：受支持外部数据库、同路径共享 RWX 卷、Secret、迁移维护入口、多个写 Pod、升级/回退和恢复 runbook；默认单副本不变。
- [x] 4.2 真实 PostgreSQL + HugeGraph 的独立进程集成测试：真实重叠写、共享实体来源/weight、重复文档、kill/ACK 丢失与重建协调器后 fence、跨存储失败恢复、维护竞争；不得只测两个共享 Manager 实例。
- [x] 4.3 运行相关 mirror 子集、跨切面里程碑全量测试、Ruff/format/配置验证、OpenSpec strict，记录准确范围、通过数与未验证项。
- [x] 4.4 独立最终代码审查，修复重要问题；逐条对照 spec 更新 tasks/verification，不把 foundation 或 mock 测试当完整多 Pod 交付。

Ownership: docs/env/Helm、集成 tests、OpenSpec verification；不实际部署 Kubernetes，不改正在运行的本地测试配置，不自动清理既有业务 scope。编排环境验证若未执行必须明确标注，独立进程测试不能伪称 kubectl 多 Pod 测试。
