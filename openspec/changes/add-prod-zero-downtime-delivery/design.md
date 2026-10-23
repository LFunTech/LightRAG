## Context

动机和授权边界见 [proposal.md](proposal.md)。2026-10-21 的只读检查确认：

- 现有 `.woodpecker/validate-release.yml` 唯一 clone 并上传源码包，`build-image.yml`、`pre-deploy.yml`、`deploy-test.yml` 各自下载和校验源码；不能假设 workflow 间共享解包目录或 `image.env`。
- 构建/验证已接受正式 `vX.Y.Z` tag；自动部署仅匹配 `-test`。`scripts/ci/deploy-test.sh` 约千行，承担五套测试实例的配置、初始化、恢复和部署，不适合全文件参数化或复制后全面改写。
- test 部署会暂停 Service、缩容到零、运行 coordination migration、按测试条件 recover、bootstrap，然后恢复路由。base 含测试名称、HTTP Ingress、测试存储类等默认值，不能仅替换 kubeconfig 后用于 prod。
- 分布式 API 启动会开始任务 polling；查询本身受协调保护且可能写缓存；实例最终清理会申请 workspace 独占。仅修改 Deployment strategy 或 Service selector 不能建立本提案的可用性合同。
- coordination schema 校验要求版本/checksum 精确匹配。为新发布协议直接增加一条所有实例都校验的 coordination migration，会扩大新旧版本及现有 test 的兼容风险。
- `.secrets/prod.secrets` 与 `.secrets/test.secrets` 均为本地环境输入，前者含非标准 dotenv 字段及沿用测试命名的字段。字段名不决定环境，文件中的值、主机和账户不进入本提案。

参考：[既有测试交付设计](../add-woodpecker-test-delivery/design.md)、[分布式运行合同](../../../docs/design/DistributedRuntimeContract.md)、[pipeline 合同](../../../docs/design/DistributedPipelineContract.md)、[pipeline 并发合同](../../../docs/design/PipelineConcurrencyContract.md)、[部署与恢复手册](../../../docs/DistributedDeployment.md)。这些合同不是已有零停机能力的证明；实施时必须同步新的可选发布语义。

用户后续单独授权了代码审查和 test 隔离测试，证据见 [多 Pod 风险实测](/Users/minwang/Projects/LightRAG/openspec/changes/add-prod-zero-downtime-delivery/risk-assessment.md)。4 Pod 的小型正常流程可以工作，但流式并发/operation 生命周期、readiness、实例退出和长期协调历史存在风险；原五套 test 的 polling 记录另停留在数日前，当前协调诊断返回不可用。本次审计不等于高并发或蓝绿发布验收，不批准生产操作，也不改变原 test 发布或实施任务完成状态。

用户随后又单独授权了 test 应用实例的顺序恢复，见 [恢复记录](/Users/minwang/Projects/LightRAG/openspec/changes/add-prod-zero-downtime-delivery/test-sequential-recovery.md)。全部 test 协调接口和 polling 已恢复；这没有修改原发布流程、readiness 或运行协议，不改变本生产提案的实施状态，也不构成生产启用授权。

## Goals / Non-Goals

**Goals**

- 兼容发布期间正常查询和已有流式连接不因发布被中断；新写入在明确预算内排队，不把排队返回成业务成功。
- 业务写入按发布 epoch 从 Blue 交给 Green，新旧版本不同时执行新的业务写入；查询可以在版本间短暂并存。
- 沿用 test 已有交付机制，只增加生产必要配置和发布差异；默认关闭新运行协议，不改变现有 test 发布。
- 所有失败可以回答当前流量、写执行权、排队命令和已提交存储写入分别处于何种状态，不以自动恢复掩盖不确定性。

**Non-Goals**

- 不承诺写入零等待，不把跨存储操作包装成事务或 exactly-once；不承诺基础设施故障期间无错误。
- 不支持未经验证的旧版本混用、不兼容 schema 在线转换、embedding 模型/维度切换或生产密钥轮换与普通发布捆绑。
- 不引入 Argo CD、Rollouts、额外消息中间件或生产专用 Dockerfile；不重构整套 test 流水线。
- 提案本身不授权实施、访问集群、导入密钥或标记旧 change 的远端验收完成；后续单独授权的 test 隔离风险审计不扩展为生产接入或旧 test 恢复授权。

## Decisions

### 1. 从现有 test 流水线增量派生 prod

| 现有基础 | prod 使用方式 | 对 test 的边界 |
| --- | --- | --- |
| `validate-release` | 复用相同来源校验、唯一 clone、COS 归档及记录 | 不新增 clone，不改变 master/PR 行为 |
| `build-image` | 复用相同 Dockerfile、Kaniko、registry/cache 和平台 | 不增加环境构建分叉 |
| `pre-deploy` | 复用镜像来源、平台及 digest 校验 | 不跳过或复写 test 的验证 |
| `deploy-test` 的源码下载、镜像解析、Secret 注入和 apply/status 组织方式 | 增量派生 `deploy-prod.yml`；同样 `depends_on: [pre-deploy]` 和 `skip_clone: true` | 原 workflow、实例列表和 Secret 映射保持原样 |
| `source-artifact.sh`、`delivery.py`、现有工具镜像及 Kustomize | 优先直接复用安全的现有能力；必要静态检查增补 prod 目标 | 不将仅支持 test 的校验器硬套到 prod，不全量参数化千行脚本 |
| test overlay 和 base | prod overlay 复用资源结构并显式覆盖测试默认值 | 不改 test overlay，不把 base 直接切换成生产配置 |

新增 `deploy-prod.sh` 只组织参数检查、环境准备和发布阶段调用。并发、排队和请求生命周期由可测试的应用/CLI 能力承担，不把业务协议堆入 YAML 或 shell。不复制整份 `deploy-test.sh`；若必须提取少量纯工具函数，保留 test 入口/默认值并先证明行为不变。

正式 tag 仍须来自 `master` 历史并校验事件 commit。生产接入显式启用前，正式 tag 维持“只构建和验证”；启用后才选择 prod 部署。`-test` 永远只选择既有 test 部署，`-pre` 不部署。人工批准通过受保护 tag/生产接入配置及 CI 权限实现，不假定现有平台已经具备新的审批功能。

实施后的 workflow 保留常量 false 静态 gate；正式 tag 限制同时存在于 workflow 的 ref 排除和接收 prod Secrets 的 step 严格正则中。单独解除静态 gate 不能让 `-test`/`-pre`/其他后缀获取 prod 凭据；启用还须独立授权及 enabled 接入配置。

**不选方案**：直接换 kubeconfig 会把测试恢复逻辑带到生产；只改 RollingUpdate 不能证明写入安全；重建完整发布平台违反小改约束。

### 2. 密钥和配置按环境单向映射

只允许以下来源链，禁止交叉读取或缺项兜底：

- `.secrets/prod.secrets` → `lightrag_prod_*` CI repo secrets → prod 命名空间中的生产运行 Secret。
- `.secrets/test.secrets` → 现有 test CI secrets → 原 test 运行 Secret。

本地导入工具使用白名单解析，支持清单中的中文标签、带空格/引号字段和列表形式，不能执行或 `source` 清单。prod 清单中的 `lightrag_test_*` 名称仅作为显式别名映射，不据此选择环境、读取 test 文件或修改原始字段。重复、冲突、缺项或非法 URL 明确失败且不打印值。API key 选择必须在 prod 非敏感配置中显式指定别名，不自动取 test 编号列表。

模型、PG/coordination、HugeGraph、S3、应用认证分别映射到现有应用字段。生产 kubeconfig 和 registry pull 权限须独立核验；可复用现有构建用 registry/COS 服务能力，但不把 test 运行凭据当作生产默认值，不修改共享构建凭据影响 test。

prod 的 namespace、域名/TLS、workspace、deployment id、PG 数据域、图域、S3 bucket/prefix、PVC 和权限全部显式声明。独立配置不等于强制购买独立物理服务；共享服务时必须验证数据库/图/bucket 或前缀 ACL 的实际隔离，不能仅靠名字不同判断安全。

运行 Secret/ConfigMap 按配置版本保留，启动 Green 不更新 Blue 正在使用的配置引用。生产缺失字段在接触业务资源前失败。密钥导入需另行授权，本提案不改任何 `.secrets/*`。

具体实现中 Secret 名绑定最终 config/environment hash，身份 ConfigMap 名绑定完整 release identity hash；因此同 slot/同配置的后续 tag 不碰撞不可变身份记录。物理 profile 还绑定 PG/图/S3 目标与 embedding endpoint/model/dimension/asymmetric/两侧 prefix。独立 `lightrag_delivery` stdlib 包供薄 CI 和固定镜像内 control Job 复用，不在 CI 工具镜像导入全部 RAG 依赖。

### 3. prod 专用蓝绿资源与兼容门禁

使用稳定的生产 Service/Ingress、两个有独立 slot 标签和不重叠 selector 的 Deployment。生产初始稳态为 **4 个应用 Pod，每 Pod 1 个 worker（`WORKERS=1`）**，首次空环境开放服务也采用相同基线。蓝绿发布期间保留 **Blue 4 + Green 4，共 8 个应用 Pod** 的调度及资源预算，不能因临时容量不足改成先停 Blue 或降低正式服务副本。8 Pod 是双 slot 的规划数量，不包含后端服务、维护 Job 或额外故障替换实例，也不是强杀长连接来腾出资源的上限。该基线仅在 prod overlay 声明，不更改 test/base 的副本、worker 或资源默认值。

实际 ingress controller、流式超时、禁止响应缓冲及 TLS 在 prod overlay 显式核验，不假定测试集群注解或 `syno-nfs` 在 prod 可用。按节点分散放置并核验可调度性；Green 4 个实例未就绪时不能进入切流，已有 Blue 始终保持原服务。

用户已确认入口为腾讯云 qcloud Ingress。首版仅接入 HTTPS 七层、CLB 直连 Pod、HTTP 后端：Service CAS 先扩展为两个已验证 slot，等 EndpointSlice 与只读 CLB API 观测到全部 8 个健康后端，再收窄 Green；再次核对 CLB 精确目标及真实 HTTPS 返回的候选 UID/config/profile。EndpointSlice 不能独自证明 CLB 传播。CLB 观测使用独立只读授权，不假定 COS 凭据有该权限。真实 controller/kubelet 的连接保持、超时和长流行为仍需隔离演练。

2026-10-23 用户新增硬约束：**应用交付不在 Kubernetes 内终止或承载 TLS**。云侧 EdgeOne/CLB 管理证书和 TLS，K8s Service/Pod 只承接 HTTP；禁止 TLS Secret、证书/私钥挂载和应用 SSL。此前未使用的 `target.tls_secret` 从校验器及 schema 移除，旧字段直接拒绝而非填占位；渲染前拒绝 SSL/证书路径并显式设置 Pod `SSL=false`，启动 profile 再复核。Ingress 中如存在云侧证书 ID，只是指示外部 CLB 的控制元数据，不包含证书/私钥或 K8s TLS 终止。原 test、其他业务和 Kubernetes 控制面自身的 TLS 不在本次修改范围。

本次[只读引用核对](verification/2026-10-23-prod-reference-audit.md)已找到现有 prod 账密、pull 引用和 CLB，但无 LightRAG 专属运行/控制 Secret 或已配置域名；公网通配证书已验证，CNAME 指向 EO 样式域名，K8s 可见 ServiceConfig 均 HTTP/80，CLB DescribeListeners 被现有身份拒绝。**尚不能据此认定既有 CLB 已满足 HTTPS/direct profile，也不能自动放宽为任意 HTTP 公网入口**。复用 EO/CLB 的实际终止、回源、访问边界和流式行为仍需只读权限及真实证据；本地去掉无用字段不解除这些门禁。

2026-10-23用户批准分层验证：在现有RKE2/nginx test内为候选应用使用新的namespace，**默认复用test已有PG、图服务和S3，通过受支持且经核验的数据域与权限隔离**，做功能、低并发及应用层4+4协议验证。独立数据域不等于独立物理组件；此前执行者将其扩大为重建PG/图/S3并写作用户批准要求，是范围判断错误，现已撤回，不再要求提供MinIO镜像。真实qcloud/CLB传播/长流保持另用腾讯云非生产目标，生产Transport与校验不扩展为nginx模式。先只读核对现有连接、数据库/受控workspace、图域、bucket/prefix、权限和共享资源余量，再按代码支持的方式准备获准隔离数据域与候选；不能仅换名字就认定隔离。确有隔离不足时报告具体缺口，再评审所需增量，不预设重建基础组件。已有本地overlay只是可选候选交付准备，不证明依赖/挂载或8.3/8.6通过，也不是继续排查现有服务的前置。

首次 foundation 显式安装 namespace Role/Binding，另以 ClusterRole/Binding 只允许 control 身份 `get` 指定目标 namespace 与 `kube-system` 对象供 UID 校验。普通发布不创建 cluster RBAC，应用无 K8s token。当前获准的预算 profile 限 PG/pgvector + HugeGraph + S3、OpenAI-compatible LLM/embedding、native parser、无 reranker；其他 provider 必须先补预算和验证，不静默继承 test 参数。

同一生产数据的两个 slot 使用相同 `WORKSPACE`、`POSTGRES_WORKSPACE`、`LIGHTRAG_DEPLOYMENT_ID`、coordination DB、后端物理目标、embedding 模型/维度及约定路径；slot 不是新的协调身份。prod 与 test 的这些环境配置分别维护。

发布前比较已批准基线与候选的存储/协调协议、只读数据格式、命令格式、API/前端兼容性和运行 profile。混合窗口内旧查询必须能够读取新版写入的数据，新代码必须读取旧数据；不满足时在旧入口仍可用、写闸门尚未关闭时拒绝普通发布。正式 tag 不是兼容性证明。

Green 以 `standby` 启动：只验证、不 DDL、不接管生产写、不自动 polling；通过私有预览入口检查查询。业务写入验收使用单独验收 workspace/数据域，不能为“验收”提前写正式数据。首次部署不存在旧 slot 时先显式初始化，再验收并开放所登记的唯一活动 slot，不伪造一次升级交接。

### 4. 持久化发布控制面，不改既有协调历史

在生产数据库增加独立、版本化的 `lightrag_release` schema，存放环境标识、发布记录、活动 slot、单调 epoch、写准入状态、参与实例及待执行命令。迁移由显式生产接入 CLI 执行，不增改现有 coordination migration 的版本/checksum，不要求 test 创建这些表。

发布锁按生产数据 scope 持久化取得，状态转换使用 owner/release id/epoch 的条件更新；进程死亡、超时或租约到期不能自动把执行权给另一个发布者。协调 generation 仍只用于原恢复协议，发布 epoch 不能充当 HugeGraph 服务端 fence。

生产接入时为运行 profile 登记发布协议标记，所有 SDK、导入工具、修复作业和应用实例必须在清单内且遵守协议。未接入旧 writer 不允许参与普通升级；不能在有未知 writer 时仅通过改发布记录“宣布接入完成”。测试和 local 模式未开启此功能时不改变原 profile/数据结构要求。

只允许受限运维身份驱动阶段、查询脱敏状态；普通 API key 不自动获得发布控制权限。发布 CLI 复用现有应用配置和协调状态，不增加未鉴权 HTTP 控制端点。

### 5. 写入闸门与有界排队

闸门关闭与新业务写操作准入必须在同一持久化控制协议下排序。已获准操作继续持原 permit 正常结束；后到请求不能占住业务锁、事务或独占门等闸门。闸门覆盖入库、上传会话/complete、图编辑/合并、删除/清空、重试、SDK 写入、后台任务和隐式写入，不仅匹配 `/documents/*` 路径。

关闭闸门期间到达的可排队写请求记录为有类型、版本化的发布命令，保存命令 id、幂等标识、脱敏主体/授权作用域、规范化参数或 S3 引用、scope、顺序和状态。只支持显式登记的现有操作，不序列化任意 Python 回调或创建通用作业平台。SDK 亦使用相同操作注册和准入，无法安全规范化的外部写入口必须在生产接入检查中拒绝。

同步 API 的原连接等待结果并返回原状态码/结构，不把删除等请求改成无条件 `202`。异步入库仍仅在原有受理事实持久化后返回 track id；进入发布队列本身不等于已经创建文档。Blue 不因切流立即退出，可保留等待连接，由 Green 执行持久化命令，Blue 只读取结果返回。客户端断开后命令仍可追踪，不能因此宣称执行失败或盲目重复执行。

异步任务必须取得独立 durable ticket，不借用会随 HTTP command 完成失效的未绑定 ticket；文本/文件后台处理只在原 enqueue 持久化后发出 accepted 信号，后续物理写仍由子 ticket 覆盖。默认 test 信号/退出行为不变。

执行权转给 Green 后按冲突资源和既有锁规则释放队列；再次检查授权与输入有效性，不记录明文 token。相同幂等标识与相同负载共享结果，不同负载拒绝。状态至少区分 `QUEUED`、`EXECUTING`、`SUCCEEDED`、`FAILED`、`UNCERTAIN`；未知 ACK 或执行者死亡不得将 `EXECUTING` 自动改回 `QUEUED`。这不是跨 PG/HugeGraph 的 exactly-once 承诺，仍须保留原 mutation/anchor 证据并按原恢复合同处理。

对象源、会话和队列结果的保留期覆盖排队/执行/重试窗口；清理不得删除被未完成命令引用的对象。已持久化在原 doc-status 的任务不重复包装成新文档，失败文档仍遵守一次人工重试语义。

生产非敏感策略必须显式给出有限的排队容量、请求等待预算、旧写排空预算、切流预算及客户端/代理超时余量；缺失或预算关系不成立则不启动发布。开始前检查积压和最长任务，排空逼近预算且尚未交权时中止发布并在确认安全后让 Blue 释放队列；不能让超长任务把“短暂排队”变成无限等待。超容量在接收命令之前返回明确限流结果，已接收的命令不能被丢弃以腾空间。

### 6. 查询连续性与附带写入

查询准入不等待发布写闸门，已有流式连接绑定原实例直到自然结束。发布不能使用 workspace 的全局 cancel/paused 控制替代实例排空，因为那会改变已有任务及手工重试语义。

缓存读保持可用；缓存填充是可选写入。进入交接窗口后在发送缓存 mutation **之前** 检查执行权，无权填充时明确跳过并记录指标，下次有权查询可补齐。这是允许的可恢复缓存缺失，不是吞掉已发送写入的错误。已经发送的缓存写纳入旧写排空。旧 slot 在丢失写权后仍可完成查询，但不能继续填充缓存。

必要的计费/审计等副作用不得按缓存处理：若存在，须先可靠记录可追踪事件并明确投递/确认规则；无法在不阻塞查询的前提下可靠保存时，候选版本不通过准入。当前未提交的查询计量改动不是已经批准的生产依赖，实施时按最终合并版本核实。

仅持有只读查询 permit 的操作不能被计为待交接的业务 writer。调整实例最终清理，将本实例连接/缓存句柄关闭与 workspace 维护独占分开；仍有活跃操作或在途 backend 请求时不能关闭其 witness/pool，也不能为了退出清除共享锁或 fence。默认关闭的新功能不改变 test 现有清理行为。

流式资源在真实消费/关闭后才释放 permit 和模型并发槽，未消费的已打开 response/client 也必须关闭。HTTP disconnect 的 cleanup 等待正在进行的 provider read；不能提前放槽或把未知关闭当 backend=0。退休意图持久化后由 native Uvicorn 停止监听，等待本实例 HTTP/stream/waiter/background/backend 与 durable work 全部结算，再标 closed；其他 launcher 未证明此能力则拒绝启用。

### 7. 发布状态机与条件回退

| 阶段 | 查询与连接 | 业务写入/安全动作 |
| --- | --- | --- |
| `PREPARED` | Blue 服务；Green standby 私有验收 | Blue 正常写；校验产物、profile、目标身份、容量和预算 |
| `DRAINING` | Blue 持续查询，缓存填充受控 | 关闭新写准入、停止 Blue 领取新任务，等已准入写和缓存写确认结束 |
| `READ_SWITCHED` | 稳定入口转 Green；传播期两 slot 均可读，旧流留在 Blue | 队列保持；核对真实入口、EndPoints/EndpointSlice 和各实例观测，不把 patch 成功当作切流完成 |
| `WRITER_SWITCHED` | Green 接新查询，Blue 继续已有连接 | 条件更新活动 writer/epoch 后只允许 Green 执行新命令，释放队列 |
| `RETIRING` | Blue 的流和排队等待连接自然结束 | Blue 不再领取/执行新写；仅在本实例完全排空后退出 |
| `COMPLETED` | Green 提供稳定服务 | 保存 digest、配置版本、阶段/epoch、排空、队列与持续业务验收证据 |

每个写操作执行前检查 slot/epoch 授权，不能只靠 Service 路由撤销 Blue 写权。已准入旧写必须在交权之前全部确认；后端请求已结束要结合实际操作/claim/mutation 记录、实例应答和必要后端观测，不能只靠 Pod 数、固定 sleep 或手写“空闲”快照。

每个阶段的逐 Pod 观测绑定精确 digest/config/profile、UID、实际 container imageID；旧 slot 的真实 digest 在 PREPARED 固定并持久化，后续重入不重新猜测。排空只查询活跃索引，不遍历千万历史 mutation。CI 等待超时保留 pinned-image control Job 和持久 owner，不删除 Job 作为超时补偿。

交权之前失败且无不确定 mutation 时，查询保持/安全恢复 Blue，撤销本次发布并由原 owner 释放队列。不能因为 CI 进程消失自动解锁。交权之后失败先收紧新写准入并保存现场；只有证明旧版本能读取新数据、无在途/未知写、授权交接完成时才允许受控切回，不自动 `rollout undo`、启动 local writer、运行 recover 或恢复单边备份。若数据/协调故障已使查询不可用，报告真实故障，不把本提案的正常发布连续性当作故障自动 HA。

已知排空预算耗尽用独立类型触发安全中止，不把 fence/未知结果归为可重试超时。先验证 Blue 路由再开闸，`ABORTING` 保留 owner 直至 Green native closed 并缩容为零才进 `ABORTED`。交权后反向交接需独立批准绑定 from/to digest、旧 config/profile、读写兼容和证据；仅旧 slot 尚未开始退休时允许，阶段为 `ROLLBACK_DRAINING → ROLLBACK_WRITER_SWITCHED → ROLLBACK_RETIRING → ROLLED_BACK`，再次 drain/route proof/epoch+1。已退休/完成发布不直接复活旧二进制。

### 8. 验证边界与 test 非回归

实施验证覆盖 `tests/ci/`、相关 `tests/distributed/`、`tests/pipeline/`、`tests/api/routes/`、`tests/llm/`、`tests/setup/`，按实际修改的镜像目录定向运行；若改动确属全局公共基础，再遵循仓库规定扩大验证。仅当修改前端时运行完整前端检查集。

Woodpecker 沿用现有交付静态检查和真实目标发布验收，不新增 pytest/Bun 全套测试步骤或重复镜像构建。现有 workflow、test manifest、五套实例、Secret 引用、初始化/恢复和健康验收行为纳入非回归；不能把生产的业务验收或排队协议强加为 test 的新发布前置。

隔离预演必须在发布全过程持续发起普通查询与流式查询，并在多个阶段提交写操作。记录发布归因的连接中断、非预期 4xx/5xx、首包和总延迟、排队时长、每个命令的执行次数/最终状态、所有权交接和存储证据；“Pod Ready”或单次 `/health` 不构成查询连续性验收。注入并发发布、CI 中断、长任务、丢失 ACK、旧 writer 迟到、Secret 缺失/串环境、切流失败和 queue 满等拒绝路径。

### 9. 容量基线、压测与后续扩容

4 Pod/单 worker 是用户确认的生产起步部署基线，不是吞吐或并发保证。CPU/内存 requests/limits、单实例查询/关键词/embedding/rerank 并发与队列大小须结合实际请求形态压测确定，不能直接复制 test 的小资源规格或用增加 worker 代替 Pod 扩展。

压测在获准的隔离环境执行，不将线上 prod 缩成单 Pod、不改动现有 test 发布或测试实例配置，至少包含：

1. 单 Pod 的可持续吞吐、在途请求及瓶颈基线，与 4 Pod 稳态对照，确认扩副本是否真实提高能力，而非仅放大后端竞争。
2. 代表性的检索模式/上下文长度、缓存命中/未命中、普通/流式查询，以及查询和入库/图写的混合负载；按阶梯增压和持续负载测试。
3. 4 + 4 蓝绿并存时持续查询和排队写入，包含旧流连接尾部、Green 查询预验收及写权交接后队列释放，不能只测稳态峰值。
4. 记录查询 QPS/在途连接、首包与总延迟 P95/P99、错误/超时、写入排队时长/积压、CPU/内存、模型排队及限流、PG/协调池等待、HugeGraph 延迟/并发和存储请求指标。按预先批准的目标负载、延迟/错误 SLO、排队预算及资源余量形成容量报告，不在无证据时声明“4 Pod 足够高并发”。

PG 业务池、coordination 池/witness、HugeGraph 客户端池以及 LLM/embedding/rerank 的额度和并发，要按全部并存实例及其他调用者合并核算。应用 Pod 增加不代表模型账户额度或共享后端容量增加；蓝绿阶段的 8 Pod 预算也不能只统计 CPU/内存。预检发现不足时保留 Blue、拒绝推进，不靠重试未知写入或临时放宽限流解决。

2026-10-23单实例入库失败后的获准本地修复：真实SDK的client构造与embeddings/chat lazy resource首次加载移至工作线程，实际async HTTP/流及关闭仍在调用方loop；准备/关闭期间重复取消必须join后才归还资源所有权，不共享client、不改超时/重试/数据库。协调器首异常和polling停止日志不再依赖release开关，记录安全类型/SQLSTATE、scope/Pod/owner、连接用途、实际PID与独立witness PID、时间；不输出错误正文/SQL/凭据，不因日志失败保持可用。`provider_open`不再包含被移出的lazy setup，model-role总阶段仍含准备等待，不能只比较前者声称性能改善。详见[本地定位与修复证据](verification/2026-10-23-coordinator-local-fix.md)；离线冷启动阻塞/并行协调超时可复现，不证明现场12.408s全部来源或找回已丢失SQLSTATE。旧候选及orphan/claim保持现场，修复未部署，真实复测和受理数据恢复分别另授权；不把本地通过替代8.3/8.6/10.2/10.8实测或扩大生产启用范围。

用户随后已单独授权新namespace/全新数据域的修复候选及单文档/查询/最多2并发复测，保留旧现场。最新run在只读图域探针HTTP400时暂停，未创建新namespace/Pod、未打包部署或发送业务/模型请求；图版本/schema读取正常，此次自编探针失败不等于图服务故障或协调修复复发。详见[预检暂停记录](verification/2026-10-23-retest-preflight-stop.md)。原test与故障scope保持，S3/逐Pod HTTP及候选复测仍未完成；本地探针调整不构成远端验证或自动继续许可。

再次获准恢复后，已完成修正探针/PG/S3/原10 Pod及离线构造预检，新1×1候选真实S3单文档processed、6个普通/流式/多轮RAG及最多2并发功能检查，另观察304.599秒，无503/quarantine，loop累计最大221.304ms，原test与旧故障证据前后不变。详见[真实低载复测](verification/2026-10-23-fixed-candidate-retest.md)。这是局部功能结果，不是性能/生产验收：Query Embedding7样本均>100ms、冷检索1.753s且检索子阶段不含keyword；naive向量含Embedding，不能混作纯向量P95。三次流式usage缺失须保留未知并补工具停止/计量验证，不能估值代替必要审计。当前不再追加真实业务请求；先按10.2/10.4在本地补观测边界与优化准备，真实复测/扩大规模另确认，不以加Pod绕过外部模型/预算和质量门禁。

后续扩至稳态 N Pod，先用容量报告定位瓶颈并审批资源规格、总连接/模型预算、并发参数、扩容上限和触发/回落条件，再更新 prod 专属配置，并为发布规划 Blue N + Green N 的 2N 预算。每 Pod 仍固定 1 个 worker，不能把此次规模确认解释为自动扩容授权。

本次不自动启用 HPA。若后续单独批准自动伸缩，指标应结合在途查询/模型等待和资源利用率，不能只因 CPU 偏低缩容；待命 slot 和交接期间的副本由发布 owner 控制，自动伸缩不得与发布状态机竞争。任何缩容均须停止本实例新领取并排空请求/流/写入后回收，不能打断查询或绕过写权协议。扩容后的容量和发布演练仍单独记录，不把改变副本数当作验证成功。

### 10. 缓存与性能准备方案（P0代码/本地验证实施中）

本节响应用户对高并发和缓存收益的担忧。用户已授权继续代码实施和本地验证，目标为 **1000 QPS 完整RAG（含模型生成）、约10000在线用户**；不是已发现线上瓶颈或已通过容量验收。不新增中间件、不改现有 test 流水线/环境、不导入密钥或操作 prod。原 4 Pod × 1 worker / 蓝绿 8 Pod 基线保持不变，压测前不宣称该规模足够。新增工作列入 tasks 第 10 节，实际隔离演练仍须另行授权。

#### 10.1 代码事实、能力缺口与待确认项

本轮只读代码依据：`lightrag/operate.py::{kg_query, naive_query, _answer_cache_kv, extract_keywords_only}`、`lightrag/utils.py::{handle_cache, save_to_cache, _save_to_cache}`、`lightrag/lightrag.py::embedding_cache_config`、`lightrag/api/config.py` 和 `lightrag_delivery/environment.py`。这里描述代码行为，不代表在线配置或命中率已经核验。

| 项目 | 当前代码事实 | 性能风险/准备动作 |
| --- | --- | --- |
| 关键词缓存 | 受 `ENABLE_LLM_CACHE` 控制；可省关键词模型调用 | 按检索模式分别测命中率与实际模型调用数，不能仅报“有缓存” |
| 完整答案缓存 | 在构建检索上下文之后读取，命中主要省最终生成；prod profile 使用 PGKV 持久化 | 命中后仍有向量/图/文本读成本；分开量检索耗时和模型节省 |
| 流式/多轮 | 流式新生成的 iterator 不回填完整答案；带 conversation_history 的请求不读写答案缓存，关键词缓存不受此项影响 | 单独建立流式、多轮的冷态容量；流式仍可能读取已有合格非流式缓存，不能写成“流式完全无缓存” |
| 文档抽取缓存 | 独立 `ENABLE_LLM_CACHE_FOR_EXTRACT`，默认 true，涉及抽取重建/恢复归因 | 不按普通临时缓存随意禁用/淘汰，不用清空抽取缓存制造压测冷态 |
| Embedding 缓存 | 存在默认关闭的配置字段，但未见通用结果缓存执行接线 | 向量落库不等于查询向量已缓存；先度量重复输入成本，再评估精确缓存 |
| 实际开关 | API 查询缓存默认 true，`env.example` 明确写 false；此前生成器未显式设置；本轮新增四个显式缓存/观测字段，prod关闭完整答案缓存、保留抽取缓存 | 接入时记录实际解析值，后续新增 prod 专属显式策略/版本 hash；不能拿示例或默认值冒充线上事实 |
| 冷热点并发 | 未见跨 Pod 的相同查询合并路径 | 同一问题同时 miss 仍可能多次生成；写命令幂等不等于只读模型请求合并 |
| 缓存一致性 | 现有答案 key 包含问题/参数/模型等，但不能据此认定知识变更、权限及所有检索依赖均已可靠失效 | 在复用或提前读取缓存前做完整审计与回归；未证明正确性时禁用对应可选优化 |

优先排查模型延迟/账户额度、PG 向量/缓存/协调竞争、HugeGraph 多次往返和图扩展规模、长流在途容量，以及入库解析/抽取对 CPU/内存/模型/存储的竞争。它们是**待测假设**，不是已定位的瓶颈；增加 Pod 不会增加共享模型额度或数据库容量。

#### 10.2 方案取舍与实施顺序

| 路线 | 收益与代价 | 决策 |
| --- | --- | --- |
| 直接加 Pod/Redis | 易增加副本或缓存介质，但不能自动省掉检索、生成和协调；还放大共享后端负载或增加新依赖 | 不作为默认第一步，禁止仅据 Pod 数/缓存命中率宣称容量达标 |
| 先测量，再做有界局部优化 | 可识别真正成本与收益，沿用 PG 和现有模型/后端；需要指标、正确性测试及对照报告 | **采用**：P0 观测/正确性 → P1 检索与预算 → P2 按证据引入缓存/请求合并 |
| 立即拆读写服务/引入全局缓存平台 | 可能隔离资源，但改变组件/权限/部署边界，超出流水线少改的当前范围 | 保留为后续独立评审，不在此节直接实施 |

**P0：先固定目标并建立可信基线。** 在运行负载前批准目标 QPS/在途并发、流式比例/持续时间、多轮比例、查询模式、输入/输出 token、数据规模（文档/chunk/向量/实体/边）、入库/图写比例，以及普通查询 P95/P99、流式 TTFT/完成时间、错误/超时与写入等待 SLO。没有批准的目标和预算，就只报告测得结果，不给“足够生产”的结论。

2026-10-22用户已确认7项性能数值目标，见[唯一参数底稿](verification/acceptance/parameters.json)和[指标映射与待确认口径](verification/acceptance/metrics.md#用户确认的性能目标2026-10-22)。数值目标不等于实测/全部参数或执行获批；成功率定义、阶段边界/窗口、token与多轮适用范围、P99和写等待等仍缺。当前prod profile无reranker，现有直方图缺0.2/3/20秒边界，均须在真实验收前解决适用性与证据精度，不能填0ms/粗分位假通过。10.1/10.2继续未勾选，本轮仅更新文档，不变更test、profile、观测实现或生产gate。

同日后续确认：技术成功与相关性分开验收，合法空结果计技术成功，99.9%记为技术目标，相关性阈值/真值不自动套用；空结果未生成不计作真实模型生成吞吐。Rerank非必选，当前profile继续关闭，200ms仅在以后显式启用的已验证profile适用。用户授权代选写排队最大值，采用30s初始规划基线；排空/切流、同步执行/响应、代理/客户端及源保留仍须独立预算核验，超30s不取消或重放已受理写。详见[排队理由与边界](verification/acceptance/metrics.md#写排队30秒的规划依据与边界)。这些局部决定不批准全部参数、外部执行或运行配置，原stage分位精度缺口和其余未知项仍保留。

随后用户明确授权补齐直方图边界及本地验证：应用BOUNDS增加0.2/3/20秒，客户端BOUNDS仅增加缺少的0.2/3秒，保留原20秒及双方其他桶；离线report继续共享应用桶并严格拒绝旧布局，不回填不存在的细分计数。新增测试覆盖边界相邻值、累计差值/P95/P99秩与溢出/默认关闭，证据见[边界本地结果](verification/histogram-local-results.md)。该子切片解决固定桶缺目标分界，不启用观测或发压；跨Pod合并、外部遥测及统计/负载/资源/额度输入仍未完成，10.2继续部分完成。

后续用户授权从prod secrets核对模型/额度，并代选负载组成：当前清单仅发现凭据类字段，未列明确型号或限额，模型资源字段不猜值，不以密钥调用账户。仅在验收底稿固定80%流式/30%多轮及最终生成输入4096/8192/16384、输出256/512/1024 Token的25%/50%/25%规划分布；历史包含在总prompt输入，联合分布/实际Tokenizer与模型窗口仍需验证。此经验基线不是实测/可执行plan，且不改变其余未知预算/授权；来源和仅最终生成算术需求见[负载规划](verification/acceptance/README.md#授权代选的负载基线与模型清单核对2026-10-22)。不据此给出4Pod足够或所有任务完成结论。

拟增加分阶段计时/直方图：请求等待、关键词、Embedding、PG 向量检索、HugeGraph 读取、chunk 获取/上下文构建、rerank（仅获准 profile）、模型队列/TTFT/生成、缓存 lookup/fill、release/coordination 等待。每类缓存区分 hit/miss/bypass/skipped/error，记录调用次数、实际省掉的模型调用/token、读写耗时、条目数/容量；不能只统计总 hit ratio。模型 quota/429、PG 业务池/协调池/witness、锁等待、HugeGraph 并发/延迟、在途流、RSS/CPU/事件循环等待一并关联。指标采用有界标签，不记录 raw prompt/token/凭据，不以用户 ID/问题文本作为高基数标签。

**P1：先控制成本与争用。** 以阶段证据检查 PG 查询计划/向量索引、批量 chunk/实体/边读取、重复查询与网络往返；在固定检索质量样本下调节 Top-K、chunk 数和 token 上限，不以牺牲召回/答案/引用正确性换取低延迟。索引/结构变更必须独立版本化迁移及维护授权，不能放入启动或普通发布 DDL，也不改原 coordination checksum。

按查询/关键词/Embedding/入库角色核算并发、连接池和有限队列，先调整已有参数、预约容量并减少竞争；请求优先级不能被描述为已经完成资源硬隔离。所有额度按稳态 4、并存 8 Pod 和其他调用者合计，纳入 Green 私有验收/预热。过载在受理前明确限流，不能用无限排队、强杀长流、关闭协调保护或重试未知业务 mutation 换吞吐；需要全局限额执行器或角色资源硬拆分时再单独设计/批准。

**P2：仅对实测高成本重复路径增加缓存/合并，允许评审结论为“不采用”。**

- 优先评估 **Embedding 精确缓存**、关键词或只读结果的有界进程内 L1，跨 Pod 持久化层先复用已有 PG；L1 明确条目数/字节上限、TTL/淘汰、命中和失效指标。新增 PG 缓存结构仍须显式迁移，不在本次文档变更中执行。
- Embedding key 至少绑定 workspace/可见域、provider/endpoint、模型及可用 revision、维度、归一化/截断、query/document 角色、asymmetric/prefix 和实际输入摘要。检查返回维度/格式；模型版本无法确定时采用获准的版本标识或旁路，不混用向量。文本摘要不是匿名化，不向公共日志输出输入或缓存值。
- 答案/检索缓存拟绑定数据 scope、权限可见域及权限版本、可信知识版本、模型/prompt/检索参数和依赖 profile。先证明知识新增/编辑/删除、权限撤销、新旧版本及进程重入下的跨实例失效；TTL 只是容量/寿命控制，不是权限撤销或数据删除的正确性证明。**未经该验证，不把答案 cache lookup 前移来跳过检索**；没有必要直接引入语义近似答案缓存。
- 知识版本必须与已确认变更关联，不能只凭 release epoch、时间戳或“已发送写入”标记一致。跨存储写未完成/结果未知期间，先旁路受影响的可选答案/检索复用，保留原 fence/ACK/恢复合同；旧在途查询不能把旧结果写进新版本 key。版本推进/恢复无法完整证明时不启用该缓存优化，不假装有跨库存储事务。
- 热点合并先评估进程内 singleflight，单独测跨 Pod/slot 冷启动放大后，再决定是否需要分布式只读计算去重。等待不持业务锁、PG 事务或占用连接等待模型，不借用发布 owner/写命令队列，不允许 TTL 抢写权。单个等待者取消不能提前关闭仍被使用的计算/流或归还模型槽，所有等待必须有界。
- 不默认广播 live SSE 或缓存未完成流；保留逐请求授权、背压、取消和必要计量/审计。未完整确认的输出/失败/截断内容不当作成功答案缓存；未验证的多轮答案缓存保持旁路。任何填充仍遵守 gate/slot/ticket：交接后 Blue 可读，但无权补写。
- 只有对照测量证明 PG 缓存访问本身是瓶颈、局部优化不足，且故障行为/新 Secret/ACL/成本评审通过时，才另案引入 Redis 等独立 L2。L1/L2 均不能缓存发布写权、fence、ACK 或授权校验的权威状态；不能将已发送写失败吞成“可选 cache miss”。

#### 10.3 压测、回归与容量判定

在独立获准的数据域和资源上，将下列负载与第 9 节的 **1 Pod / 4 Pod / 4+4 蓝绿** 矩阵交叉：

| 负载 | 必须观察的差异 |
| --- | --- |
| 冷缓存/不同问题、热缓存/重复问题 | 分别计关键词/答案/Embedding 命中、检索成本和实际模型调用；不能用纯重复问答外推真实 QPS |
| 非流式、流式、多轮 | 分开报 TTFT、完成时间/流持续时间、在途请求与 bypass 原因；不把普通答案缓存收益套给多轮/流式 |
| 同题热点突发、跨 Pod 冷启动 | 实际生成次数、模型排队/限流、合并等待、取消与超时；逐层判定是否需要跨 Pod 去重 |
| 查询 + 持续入库/图编辑/删除 | 检索质量、缓存失效/权限变化、模型与存储竞争、写入排队、恢复证据，无重复业务效果 |
| 蓝绿关闸/切流/旧长流收尾 | standby/旧 slot 的 cache skip、新写唯一执行权、模型额度总量、旧连接正常结束；不要求先取消全部查询 |

冷态使用受控新建的隔离数据域或缓存命名空间，不清理现有 test/prod、抽取归因缓存或恢复证据。先阶梯负载再持续负载；超出批准资源/错误/队列预算按预案停止发压并保留在途工作，不用强制终止掩盖问题。

回归应覆盖真实调用路径：开关实际解析值、查询缓存 hit 后仍发生的检索、stream 不回填/多轮旁路、模型/prefix/维度变更、数据更新/删除/权限撤销、缓存并发失效、进程替换、热点合并失败/取消、发布关闸/旧 writer 迟到以及必要计量不丢失。新增行为遵循 TDD，外部服务单测用 mock；真实容量测试另授权，不能以单测时长声称 QPS。

容量报告同时交付工作负载/数据与镜像/config 身份、资源/全部额度、实际开关、各阶段分位/等待、cache hit/miss/bypass/skip、模型调用/token、检索质量、错误/超时、CPU/RSS/后端指标、故障记录，以及优化前后同负载对照。通过条件是目标吞吐、各类请求 SLO、资源与排队预算均满足批准值，知识/权限正确且发布不中断；任一不满足就记录瓶颈/未通过项，不以热缓存平均耗时、低 CPU 或 Pod Ready 代替验收。

**交付顺序和门禁**：先完成目标/观测及缓存正确性审计，再运行 8.6 的基线；仅实施已批准且证明确有收益的优化后重复同矩阵，最后进入 8.7 的 N/2N 容量评审。9.3 的生产启用仍需 8.3/8.6 的真实证据及未解决正确性问题清单；可选优化评审可决定暂不采用，但不能免除容量/正确性验证。本轮P0实现见 [性能工具说明](../../../scripts/performance/README.md) 和 [tasks实际边界](tasks.md)；不是已经部署/达到目标的能力，不向现有 Woodpecker/test 路径添加测试套件或新依赖。

### 验收执行前的本地准备包（2026-10-21）

用户已明确授权仅补充OpenSpec材料、未知参数留空，不含任何真实环境执行。[本地验收准备包](verification/acceptance/README.md)细化既有10.1/10.2、8.3/8.6/10.8的输入与门禁，不新增另一份实施计划。

- [参数底稿](verification/acceptance/parameters.json)固定用户已确认目标，其余值为`null`，所有执行授权为false；不是load/rehearsal/onboarding可执行配置，批准后才能生成严格私有输入。
- [指标清单](verification/acceptance/metrics.md)区分现有进程插桩、原控制证据及外部待接入遥测，给出逐Pod/epoch、窗口/分位与脱敏规则，不冒充采集器已联调。
- [完整写入口](verification/acceptance/write-entrypoints.md)按源码列19个HTTP、17个SDK、7个后台kind及隐式/禁止入口；[场景矩阵](verification/acceptance/scenarios.md)明确当前四类typed工具不覆盖全部真实入口、正常/负例/故障run分别签署。原57个capability场景及历史证据不变。
- [隔离作业](verification/acceptance/isolated-job.md)只定义集群内受限operator、固定源码/镜像、专用数据域/ACL、预算与分段审批；本轮不创建可执行Job/资源、不修改CI或镜像。
- [停止预案](verification/acceptance/stop-plan.md)区分停客户端/下一步控制与服务端已受理尾部；保留未知写和owner，不自动abort/rollback/recover/清理。外部资源/费用阈值尚需批准和观测侧/人工联动，不虚构已实现自动停止。

2026-10-22继续补齐[逐项登记说明](verification/acceptance/records-guide.md)、20项[指标来源底稿](verification/acceptance/metric-source-map.json)、全入口/故障/容量/phase[执行台账](verification/acceptance/execution-ledger.json)及[离线缺项报告](verification/acceptance/preflight-gaps.md)。它们只把既有合同展开为未执行记录，不新增采集器/控制器或变更CLI输入协议；参数底稿保持原样，全部现场记录not_run、引用/批准待确认，execution_ready=false。形式与ID校验不能证明来源、ACL、审批或证据真实性。 本轮记录见[2026-10-22离线登记核验](verification/local-records-results-2026-10-22.md)。

材料准备完成仍保持41/53、12项未完成；参数批准、外部遥测及真实演练/上线任务不勾选。先确认输入/只读来源，再专项批准隔离预演，得到基线后才评审优化/N/2N及生产接入。文档验证记录见[本地准备核验](verification/acceptance-preparation-results.md)。

## Risks / Trade-offs

- [应用改造大于 YAML 差异] → 小改约束属于交付链路，不以假排队或关闭协调保护缩减正确性；应用侧独立切片验证。
- [长入库/LLM 超过短暂等待预算] → 启动前预算核验、交权前中止、保留 Blue；不强杀已有工作。
- [持久化命令可能已经提交但响应丢失] → 幂等标识、终态记录与 `UNCERTAIN` 阻断，保留原存储恢复证据。
- [Blue 的长连接延迟资源回收] → 给两 slot 预留容量，回收等待不延长写闸门；不为了清场中断查询。
- [共享后端放大环境误配] → 显式作用域、最小权限和无跨环境兜底；非敏感配置 diff 也须评审。
- [读格式不兼容或旧前端调用新版 API] → 发布前兼容性拒绝门禁；在线迁移另案评审。
- [运行协议未部署到已有生产 writer] → 首次接入是明确前置，不声称任意历史版本能直接零停机加入本协议。
- [缓存命中被误当成端到端加速、4 Pod 被误当成容量保证] → 按阶段/负载实测模型调用、检索及协调成本，先完成第 10 节的正确性和对照报告；Redis/请求合并均为有条件的后续优化，不靠更换缓存介质掩盖瓶颈。

## Migration Plan

1. 评审本提案与预算策略，核实已存在 test/S3/分布式能力的实际验收状态；不修改其任务完成记录。
2. 先完成默认关闭的应用协议和本地回归，再从现有 test workflow 增量派生 prod 配置/入口；在未启用 prod 接入时正式 tag 仍不部署。
3. 经单独授权导入 prod 密钥、配置和受限部署凭据；核验与 test 的数据域/权限隔离，准备生产资源及备份。
4. 经单独维护授权显式初始化业务存储、coordination 与独立发布控制 schema；已有环境只有在所有旧 writer 被核实并完成协议基线接入后才允许开启新模式。首次空环境上线不要求先停一个不存在的 Blue。
5. 在隔离环境完成持续查询、排队写入、失败中止、条件回退和 test 非回归演练；保存真实证据后才批准 prod 自动部署接入。
6. 通过受保护正式 tag 进行第一次获授权的 prod 发布；只部署验过的 digest，保存发布记录，继续保留 test 原发布路径。
7. 回退遵循第 7 节。发布控制 schema 不在失败时删除；未处理命令、协调历史和业务备份不能随回退清理。

## Open Questions

- 生产域名/TLS、namespace、StorageClass、显式配置别名、资源规格、发布预算数值和后续扩容上限在接入时按环境核验；初始 4 Pod/每 Pod 1 worker、蓝绿 8 Pod 基线已确定。未确定项不影响已选架构，但任一必要项缺失都阻止启用生产发布，不能使用 test 默认值。
- 实际 prod 是否首次上线、有哪些既有 writer/SDK，由接入清单确认并选择上面的空环境或已有环境路径；这不是省略协议基线的理由。

## Local implementation and verification

2026-10-21 的本地实现、唯一最终审查修复与全部 capability 场景对照分别保存在
[实施账本](verification/implementation-ledger.md)、[最终审查](verification/final-review.md)、
[本地验证](verification/local-verification.md) 和 [场景矩阵](verification/scenario-evidence.md)。
这些记录区分真实本地 PG/ASGI/原生 socket 结果与 fake K8s/CLB 测试；不代替远端
4/8 Pod、ACL、TKE 路由、容量或第一次生产发布验收。当前生产 gate 仍关闭。

本轮性能增量的真实本地测试、审查修复和未完成项另见
[性能本地验证](verification/performance-local-results.md)、[性能最终审查](verification/performance-final-review.md)；
原历史9330结果不作为新增实现证据，1000 QPS容量与所有远端验收未通过/未运行。

#### 10.4 已批准工具补全的实施接口（2026-10-21）

用户授权补齐10.7代码及本地验证，仍禁止真实发压/集群/Secret/Git发布。沿用当前会话原地执行与唯一账本，不另建计划。

- `scripts/performance/quality.py`：严格版本化golden cases；事实允许变体/禁止事实、预期与禁止引用、chunk召回、引用精确度、预期拒绝；真实普通/流式/多轮HTTP采集，缺少/重复证据拒绝，脱敏报告不含正文/key。只做确定性rubric，不声称自动证明全部语义正确。
- `scripts/performance/rehearsal.py`：固定phase事件、同一run的typed insert/create/edit/delete、有界并发及持久attempt journal；API异步受理后追踪真实终态/读后状态，未知ACK不重试，删文档仅引用本run已确认insert，始终delete_llm_cache=false。运行时并行普通/流式负载、阶段质量检查、最终验证；不使用任意URL/SQL/shell，不清理/恢复。
- `scripts/performance/control.py`：复用真实ReleaseOrchestrator逐阶段推进；调用已有ProductionTransport核验Blue/Green各4成员的UID/digest/config/profile/readiness及真实qcloud路由，COMPLETED只允许Green4/Blue0。不会以8个Ready或手写快照签署拓扑。
- 复用交付上下文的最小提取仅提供同一受限operator会话/锁，默认deliver行为不变；工具缺计划/SLO/隔离身份、写授权确认、私有输出/journal或ground truth，在连接/变更前拒绝。未来执行仍需独立授权。
- 本地使用fake外部HTTP/K8s/CLB和真实编排器/ASGI；验证混合写跨关闸的等待、无重复、未知ACK保留、源状态/引用/事实变化、旧流收尾、部分Pod/路由失败拒绝。工具通过不是8.3/8.6/10.8的真实容量与质量验收。


工具完整输入/终态/取消合同见[10.7说明](../../../scripts/performance/rehearsal.md)。来源身份使用原API规范化后仍保留的 `perf-<run_id>-<safe-name>.txt` basename；持续负载预算须覆盖发布窗口，耗尽即停，不追加请求。真实Blue digest先按原store记录baseline后才验路由。故障停止未发送依赖写，已发写与长流保持有界收尾；未知结果/证据缺失不自动重放。

本轮实际完成及限制见[10.7本地结果](verification/rehearsal-local-results.md)、[唯一审查](verification/rehearsal-final-review.md)。57场景与41/53状态已同步，真实环境验收仍未执行。

### 2026-10-23 本地流式计量、计时边界与冷路径修补

后续用户已批准本地补齐动态4+4与混合写停控，沿用本计划：观测Blue4/Green0→4/4→0/4，拒绝未解释替换/epoch变化；旧实例在自然排空与finalizer之后、closed ACK之前写显式启用且≤3500字节的Kubernetes终止摘要（仅有界角色计数/身份，不含正文）。operator以Pod UID/固定镜像/配置、exit=0及PG实例closed记录联合核验，先fsync最终证据再放行缩容；不新增schema/RBAC/组件。活跃、加入中、关闭中和已结算严格区分，消失不当0。新增v2演练观测预算必须显式批准，未知值留null；共享停止信号阻止未发送读/写/advance，但已发写的必要只读追踪及已有长流自然收尾。当前仅授权代码/Mock本地验证，不运行上述集群/文件/模型操作，不触碰两个候选。

本轮用户仅授权本地代码、回归及文档；两个候选现场均保留，未连接集群/后端/模型、未读密钥，不重启/recover/重放或重新打包旧overlay。实际实现/结果见[本地验证](verification/2026-10-23-usage-timing-local.md)；指标与停测接口详见[性能工具合同](../../../scripts/performance/README.md#v2计时边界与流式usage本地实现未部署)。这些是10.2/10.4的子切片，不勾整项或真实10.8，不更改未知参数/生产gate。

- 流式默认include_usage；末尾完整且一致的整数总量只计一次，中间/部分/冲突不当完整usage，token_tracker同步收紧。非流式无新增字段、显式false保留、拒绝参数不删除参数后重发。SDK dispatch开始/结算与pending更新在同锁内，避免在途被短暂误判unknown；未消费关闭、异常、取消仍结算并保留原资源owner。严格query-accounting仍拒绝stream，不借此开启必要审计。
- `measurement_version=2`新增`retrieval_total`、`embedding_total`、`embedding_input`、`embedding_prepare`/`llm_prepare`及仅PG的`vector_backend`；旧retrieval/vector保留。前者不再漏掉冷关键词，后者不再把naive Embedding算成向量后端。报告拒绝混版本、epoch变化或counter消失；嵌套耗时/单调用分位不充当单请求全链路。
- 新只读UsageWatch与query_load CLI连接，要求完整固定成员直连身份/配置与干净基线，独立有界HTTP池/采样/响应/call预算。unknown≥1或证据异常停新增到达，pending在途中不误停，末端未结算不通过；不取消/重放已发工作。不配置watch时不得给usage/SLO通过。4+4动态成员退休的最终快照/完整聚合及混合写停控联调仍需补齐真实门禁，固定成员watch不能把消失当0，现有rehearsal功能检查不代替此项。
- 旧实测只支持“冷关键词约1.326s占首次1.753s大头、Embedding七样本均>100ms”的定位，不足以将255ms拆成网络/CPU或下容量结论。先增加准备/输入/后端边界；输入Tokenizer/prefix/截断移出loop并join、同批重复编码和Embedding去重；可选SDK addon_params.parallel_retrieval最多三路并行只读且默认关闭，保留progress callback和结果顺序，错误/取消join。无新HTTP客户端跨请求共享、缓存、索引/DDL、Top-K/池参数或线上开关变更。模型网络/冷关键词、4/8Pod共享峰值、相关性与实际100/200/500ms目标均待新授权复测，不以本地模拟提速替代。

### 动态usage本地实现补记（2026-10-23）

沿用上述显式终止摘要方案，完整合同见[演练工具](../../../scripts/performance/rehearsal.md#动态usage与停控合同v2)。本地覆盖4→8→4、旧监听关闭后的late usage、最终证据先fsync再缩容、共享停发及在途写/长流收尾。并发采样复核phase前后，只对真实向前变化重采；operator单连接短SQL段串行，避免observer/control竞争同一asyncpg连接。超时先发stop再join只读清理，终止文件写盘off-loop且取消join，unknown保留实际计数和失败样本。五项正式预算仍null，参数v5新增空项而非获批默认；不改数据库/schema/权限、正常发布协议、原test或两个现场。

## 11. 生产首次接入的版本化结构迁移（2026-10-23）

用户要求继续补齐本地migration与独立初始化流程；这不授权执行真实生产写、导入Secret、开启流水线或Git发布。沿用本仓库Python/asyncpg迁移模式，不引入Flyway/Alembic、不修改旧coordination SQL/checksum或test初始化路径。

- 新增 `lightrag/provisioning/`：冻结v1业务/向量DDL和图schema资源，计划绑定既有physical profile、模型/维度及HNSW参数。首版支持当前profile的HNSW及HNSW_HALFVEC；不默默切换索引/模型，不迁移旧数据。首次目标若有无账本的业务表或未知账本，拒绝自动接管，另做显式历史迁移。
- PG使用独立 `lightrag_storage` 版本/checksum/profile/结构指纹账本，事务与会话级try-advisory-lock防并发。保留原coordination/release迁移入口；独立初始化入口显式依次调用它们并只授予已存在runtime角色所需权限，不创建账号/密码。已存在pgvector仅校验，不自动升级；缺扩展由获准迁移身份明确安装，不让应用补建。
- 图无跨PG事务：先GET预检全部定义，再逐项持久化EXECUTING意图后单次POST，GET验证CREATED后CONFIRMED。异常不回滚PG、不删图、不重发未知POST；下次显式运行只可用已存在且精确匹配的定义结算未知结果，缺失/漂移保持拒绝。各阶段可独立只读verify，最终账本确认全部完成后应用才准入。
- 新CLI提供离线plan、受确认保护的apply、只读verify及独立register步骤。结构迁移、workspace物理manifest登记、release enroll相互分离，register不构造应用或调用模型。DSN和图迁移身份只从专用环境变量读取，环境文件只作为受保护输入，不输出凭据。目标/physical profile/基线manifest或operator/runtime身份不匹配，在变更前拒绝。
- 只渲染独立人工触发初始化Job，固定image digest、backoffLimit=0、无app/入口资源、引用已有Secret而不生成Secret；不把DDL自动塞入普通蓝绿发布或原test workflow。未知预算/真实引用不填示例值为获准参数。
- 真实release runtime必须核验迁移账本及当前PG结构；PG extension和图schema启动路径只读校验，即使maintenance也不借旧bootstrap补DDL。release关闭的test/local保留原行为。图当前结构仍由verify-only客户端校验，PG账本不能冒充图网络或ACL证明。
- 本地覆盖空库、重复执行/并发、checksum/profile/结构漂移、非事务图中断/未知结果、目标/权限/确认拒绝、真实runtime准入和test不回归。真实生产迁移、隔离对象写入、4+4/CLB/性能验收仍独立授权。

## 12. 已确认 EdgeOne 入口的交付适配（2026-10-23）

用户确认正式域名 `llm-rag.eplus2.cn`、沿用 EdgeOne，并要求继续通过流水线实际部署。此路径采用显式 `ingress.mode=edgeone`：公网 HTTPS 在 EO 终止，qcloud CLB HTTP/80 直连 HTTP/9621 Pod。既有 `direct`/CLB HTTPS profile 保留，不用任意 HTTP 降级代替 EO 核验，不创建 K8s TLS Secret/证书挂载。

- EO 配置严格绑定 zone、正式 host、与 Ingress 实际 VIP 一致的单个 IPv4 源站、HTTP/80 和原 host；不接受备用源站、未知域名状态或自动猜测站点。
- 独立只读 EO 身份读取加速域名、七层全局配置及完整有界规则列表。域名固定字段、全局设置和有序规则分别与经运维审阅的 SHA256 比较；只输出 hash/请求ID，不保存可能含签名/鉴权配置的原始云响应。哈希只证明与已审阅配置一致，不代替 HTTP 回源、Host、API 缓存禁用、缓冲及长流超时的语义审查和真实验收。
- 单独 `review_evidence_id` 与已有 streaming evidence 是首次接入的受控批准记录，缺实际审阅不得填写。未知 zone/hash/权限/资源预算继续为空，不用单测 fixture 当作生产配置。
- 每次路由传播观测同时核对 EO 配置未漂移和 CLB 全部已知 Pod 目标/健康，公开 HTTPS 仍必须返回实际候选身份。EO/CLB 权限失败均 fail-closed，不借用 S3/COS key 或控制器凭据。
- EO HTTP Ingress 不含 HTTPS/证书注解；CLB HTTPS 模式不因新增分支而放宽。原 test 流水线与运行配置不变。
- 首次初始化和首次 Blue 4×1 仍必须由获准流水线使用独立身份执行；本地适配不等于这些真实操作已经完成。当前未预置合格 runtime/观测身份时不解除静态部署 gate，也不以反复打 tag 代替接入。
