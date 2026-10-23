## Why

当前 test 已有 `validate-release → build-image → pre-deploy → deploy-test` 交付链路，但其停流、缩容到零和测试专用初始化/恢复流程不适合生产查询连续服务。需要**以现有 test 流水线为基础做增量扩展**，实现 prod 发布期间查询及已有流式连接不中断、写入可有界排队，同时保证 prod/test 密钥、配置和发布互不干扰。

本提案仅定义方案和实施任务；创建提案不授权实施、同步 Secret、提交推送、打 tag 或操作任何集群。

## What Changes

- 保留现有源码单次 clone、COS 源码归档、Kaniko 构建、镜像来源/digest 校验、内部工具镜像及 Kustomize 交付方式；不重建一套 CI/CD 平台、不另建生产 Dockerfile。
- 从现有 `deploy-test` workflow 的组织方式增量派生 `deploy-prod`，继续依赖同一 `pre-deploy`。保留 `-test` 的现有触发、五套实例、Secret 映射和部署行为；`-pre` 仍只构建及校验。正式 `vX.Y.Z` tag 仅在生产接入显式启用后允许部署 prod。
- 增加 prod overlay、生产专属配置和薄发布入口。生产日常发布采用“新版本待命 → 关闭新写准入并排空旧写 → 蓝绿切流 → 新版本接管排队写入 → 旧实例连接排空退出”，不暂停查询路由、不把全部服务副本缩到零。
- 用户明确禁止在应用 Kubernetes 交付中做 TLS：TLS 终止及证书管理在云侧 EdgeOne/CLB，Pod/Service 后端仅 HTTP；不创建/复制 TLS Secret、不挂载证书或私钥、不在 Pod/Ingress 控制器启用 TLS。云侧证书 ID 仅作为外部路由元数据核验，不能用公网证书或占位 TLS Secret 代替真实入口证据。
- 生产初始容量基线为 **4 个应用 Pod，每 Pod 1 个 worker（`WORKERS=1`）**；蓝绿发布期间按 **Blue 4 + Green 4，共 8 个应用 Pod** 预留调度资源和后端容量。此数量不含数据库、图服务或维护 Job，不修改现有 test 的副本数、worker 数和资源配置，也不将起步基线等同于已验证的高并发容量。
- 增加默认关闭、prod 显式启用的发布控制能力：持久化发布锁/阶段、实例待命与排空、跨实例写入闸门、有界且可追踪的排队请求、查询与写入分类、旧版本写权限撤销及条件回退。查询缓存等附带写入不能绕过写入交接，已发送而结果不确定的存储写入不能自动重放。
- `.secrets/prod.secrets` 仅供 prod，`.secrets/test.secrets` 仅供 test；通过显式字段映射分别导入环境专属 CI Secrets 与 Kubernetes Secret。缺项直接失败，不跨环境读取、覆盖或兜底，不将密钥放入源码包、镜像、日志或提案。
- 将生产首次初始化、发布控制元数据迁移与日常兼容升级分开；日常发布不自动执行 `bootstrap`、`recover`、不兼容 schema 迁移或 embedding 模型切换。
- 增加对现有 test 发布的非回归验证，以及持续查询/流式连接、排队写入、交接竞态、故障拒绝和环境隔离的验收要求。保留 Woodpecker 不运行仓库测试套件的边界，发布验收不等同于运行全量测试。
- 后续以隔离环境的单 Pod、4 Pod 稳态及 8 Pod 蓝绿场景压测确定可承载负载、资源规格、并发上限和扩容策略；同时核算模型额度、PG/协调连接、HugeGraph 及对象存储容量。扩至稳态 N Pod 前必须评审发布期间 2N Pod 的预算，扩缩容沿用安全准入/排空，不因本提案自动启用 HPA 或修改 test。
- 补充缓存与性能准备方案：先核实缓存实际开关及能力边界，建立查询分阶段、模型调用、缓存收益和后端等待基线，再按证据评估检索/并发调优、Embedding 精确缓存、有界进程内缓存及热点请求合并。完整答案缓存不等于跳过检索，流式/多轮负载不得套用普通答案命中率；不因方案文档直接新增 Redis、修改代码或启动压测。详细决策见 design 第 10 节，新增任务均待实施授权。

## Capabilities

本地验收准备材料见[验收准备包](verification/acceptance/README.md)：集中待确认参数、只读指标来源、全写入口/故障覆盖、隔离作业与停止预案。未知值保持空白，文档完成不授权执行，不代替真实连续性/容量/质量验收或生产接入。

### New Capabilities

- `prod-delivery`: 基于现有 test 交付链路扩展生产发布、环境隔离、可信产物、生产配置、条件回退及发布证据。
- `release-write-handover`: 在兼容升级中保持查询连续，通过持久化闸门、有界排队及实例级排空交接写入执行权。

### Modified Capabilities

无。`openspec/specs/` 尚无正式 capability；不改写未归档 `add-woodpecker-test-delivery` 的 test 合同。本 change 在生产显式启用时扩展正式 tag 的部署行为，不将该行为倒灌至 test。

## Impact

- 交付层：预计新增 `.woodpecker/deploy-prod.yml`、`scripts/ci/deploy-prod.sh`、生产配置映射工具、`k8s-deploy/lightrag-kustomize/overlays/prod/` 及对应 `tests/ci/`。原有三个构建/验证 workflow、`deploy-test.yml`、`deploy-test.sh` 和 test overlay 原则上保持不变；必要的通用静态检查扩展必须通过现有回归。
- 运行时：预计增量修改 `lightrag/distributed/`、API 生命周期/写入入口及查询缓存处理，增加可选发布协议及受限运维控制；默认关闭，现有 test、本地模式及未接入 SDK 不被静默切换。
- 数据：仅在生产显式接入时新增独立、版本化的发布控制元数据；不改动既有 coordination migration checksum，不在普通发布中迁移业务数据，不以关闭分布式保护实现查询可用。
- 文档：补充生产接入/发布 runbook，并在实施时同步分布式运行、pipeline、缓存及退出合同。既有测试交付、S3 接入的未完成验收仍需独立核实，不由本提案代为标记完成。
- 生产连接、域名、存储类型、发布权限和排队预算须在接入预检中核验；密钥清单存在不代表连通性、授权、数据隔离或零停机已经通过验证。

不修改 `.secrets/*`、本机 `.env`、现有业务数据、`main`、其他仓库或 CI 基础设施；不提供任意不兼容版本的零停机升级、不承诺基础设施故障下的自动 HA 或自动数据回滚。
