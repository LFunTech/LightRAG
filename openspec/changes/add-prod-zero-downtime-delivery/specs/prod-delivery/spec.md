## Purpose

在现有 test 交付链路基础上增量提供生产发布，复用可信源码与镜像交付机制，独立管理生产密钥和运行配置，通过兼容性检查、显式接入及可审计的蓝绿切换支持查询连续服务，同时不改变或干扰现有 test 发布。

## ADDED Requirements

### Requirement: Extend the existing test delivery foundation

生产交付 SHALL 复用现有唯一源码获取、归档传递、镜像构建与校验链路，并基于现有 test 部署工作流的组织方式增量增加生产部署阶段。系统 MUST NOT 为生产重新搭建交付平台、重复构建环境专用镜像或重写现有 test 发布流程。

#### Scenario: A production release uses the existing artifact chain
- **WHEN** 一个获准生产发布通过现有源码与镜像交付阶段
- **THEN** 生产部署消费该链路已验证的源码和镜像 digest，不新增第二次 clone、环境专用构建或绕过已有依赖的部署

#### Scenario: Existing test delivery remains unchanged
- **WHEN** 推送符合原约定的 `vX.Y.Z-test` tag
- **THEN** 原 test 实例、Secret 映射、部署准备与健康验收行为保持不变，生产控制协议、配置和运行凭据不成为 test 的新增前置

### Requirement: Scope production activation and release events

生产自动部署 MUST 默认未接入；仅在生产环境身份、权限、协议基线及验收获准并显式启用后，受保护的正式 `vX.Y.Z` tag 才 SHALL 触发生产部署。`-test` MUST 仅部署 test，`-pre` MUST 保持仅构建和验证，普通 push/PR MUST NOT 获得生产部署能力。

#### Scenario: Production has not been activated
- **WHEN** 未启用生产接入时推送有效正式 tag
- **THEN** 现有构建及镜像校验继续运行，但不变更任何生产或测试部署

#### Scenario: Non-production events cannot select production
- **WHEN** 事件为 `-test`、`-pre`、普通 push 或 PR
- **THEN** 不执行生产部署，不向该事件下的非生产执行步骤注入生产运行或部署凭据

### Requirement: Deploy only the verified release identity

生产交付 SHALL 验证仓库、tag、事件 commit、`master` 来源、源码归档记录和校验和，并将部署绑定到已核验平台及来源的不可变镜像 digest。无法证明身份、跨 workflow 缺失产物或任一依赖失败 MUST 在生产变更前终止，不能使用本地工作区或其他流水线的旧产物补齐。

#### Scenario: Artifact or image identity is inconsistent
- **WHEN** 源码归档被篡改、tag 移动或镜像 revision/digest 与本次发布不符
- **THEN** 不切流、不关闭写入准入、不更新生产 workload，明确报告身份校验失败

### Requirement: Keep production and test secret sources independent

系统 MUST 将 `.secrets/prod.secrets` 仅映射到生产专属密钥，将 `.secrets/test.secrets` 仅映射到原 test 密钥；任一侧缺项、解析冲突或无效配置 MUST 明确失败，MUST NOT 跨环境读取、覆盖或兜底。文件中的历史字段名 MUST NOT 改变选定环境。密钥值 MUST NOT 出现在源码归档、镜像、日志、发布记录或 OpenSpec 文档中。

#### Scenario: Production credentials are incomplete
- **WHEN** prod 清单缺少必要字段而 test 清单具有同类字段
- **THEN** 生产接入失败，不读取或复制 test 的值，不改动 test CI Secrets 或运行 Secret

#### Scenario: Production input contains historical test-named aliases
- **WHEN** prod 清单中的一个字段沿用 `lightrag_test_*` 名称
- **THEN** 仅依据显式生产别名映射消费该 prod 文件中的值，不选择测试实例、访问 test 清单或修改测试密钥

#### Scenario: An onboarding operation produces diagnostics
- **WHEN** 导入或配置校验失败
- **THEN** 诊断只包含允许公开的字段名、错误类型和目标环境，不回显密钥、完整 DSN 或含凭据的 URL

### Requirement: Isolate environment configuration and resource authority

prod SHALL 独立声明集群身份、namespace、域名/TLS、workspace、数据域、对象存储作用域、存储卷、部署权限和发布策略。系统 MUST 在变更前核验实际隔离与最小权限，MUST NOT 将 test 的默认值、暂停路由、实例列表或存储初始化/恢复策略自动继承为生产策略。生产配置准备 MUST NOT 修改 Blue 正在使用的配置版本或任何 test 资源。

#### Scenario: A production target resolves to test scope
- **WHEN** 生产配置或凭据将目标指向 test 的 namespace、workspace、数据域或未经批准的对象作用域
- **THEN** 发布在资源变更前失败，保留两侧配置与部署状态

#### Scenario: Environments share a physical backend
- **WHEN** prod 和 test 使用同一个物理数据库、图服务或对象存储服务
- **THEN** 接入验证确认独立数据域和实际权限边界，不能仅以命名不同宣称已隔离

### Requirement: Terminate production TLS outside application Kubernetes resources

生产应用 TLS SHALL 只在云侧 EdgeOne/CLB 终止，Service/Pod SHALL 使用 HTTP 后端。交付 MUST NOT 创建或复制 TLS Secret、保存/挂载服务端证书与私钥，或在应用 Pod/Ingress 控制器启用 TLS。生产接入 MUST NOT 要求 Kubernetes `tls_secret` 引用；旧字段、应用 SSL 和证书文件参数 MUST 被拒绝，而非静默忽略或填占位。云侧证书 ID MAY 作为外部 CLB 路由控制元数据，但 MUST 保留云侧 HTTPS、精确目标、访问边界和流式验收，不得把外部 TLS 观察误报为 CLB 已验证。现有 test、其他业务和控制面 TLS MUST 保持不变。

#### Scenario: An input attempts to enable application Kubernetes TLS
- **WHEN** 生产接入包含 `tls_secret`，或候选渲染/启动配置试图启用应用 SSL、服务端证书或私钥文件
- **THEN** 在应用运行或集群变更前拒绝；合规产物只暴露 HTTP 后端且不含 TLS Secret/证书挂载，云侧 HTTPS 与路由验收门禁继续生效

### Requirement: Verify an explicit EdgeOne HTTPS entry with HTTP origins

用户选择 EdgeOne 时，交付 SHALL 使用显式 `ingress.mode=edgeone`，核对公网域名和云侧证书、正式 Host、与实际 Ingress VIP 一致的唯一 IPv4 源站及 HTTP/80 回源。该分支 MUST NOT 要求 CLB HTTPS 证书或 Kubernetes TLS；`direct` 分支 MUST 保留原 CLB HTTPS 门禁，MUST NOT 隐式降级至 HTTP。经审阅的域名、全局配置及完整有序规则快照 SHALL 分别绑定 hash 和审阅证据；hash 相等 MUST NOT 被描述为自动证明缓存/流式语义。每次切流 SHALL 同时核对 EO 未漂移、精确 CLB 后端及真实公网候选身份，观测身份 MUST 显式配置并与应用对象存储身份隔离。

#### Scenario: EdgeOne terminates public HTTPS without Kubernetes TLS
- **WHEN** 获准的 EdgeOne profile 使用 HTTP/80 回源到明确的 qcloud Ingress
- **THEN** 渲染仅有 HTTP 后端与路由元数据，不创建 TLS Secret、不填假 CLB certificate ID，保留公网 HTTPS、云配置及全部健康后端验证

#### Scenario: EdgeOne configuration is incomplete or drifts
- **WHEN** 域名/源站/证书状态不匹配、规则分页不完整、审阅 hash 缺失或改变，或 EO/CLB 只读身份无权限
- **THEN** 停止发布，不以空规则、浏览器截图、Pod Ready 或已保存配置代替自动化入口证据，不借用其他业务或应用 S3 凭据绕过

#### Scenario: Malformed ingress discriminators are rejected safely
- **WHEN** JSON 中 `mode` 或 `protocol` 为非字符串，或未声明支持的协议组合
- **THEN** 返回不含输入值/凭据的配置错误，不触发集群变更或 HTTP 降级，不泄露非预期类型异常

### Requirement: Require an initialized compatible production baseline

生产普通升级 SHALL 仅接收已初始化并接入发布协议的环境，以及存储、协调协议、命令格式、API 和新旧查询数据格式均兼容的候选版本。日常发布 MUST NOT 自动执行业务 bootstrap、故障 recover、不兼容 schema 迁移或 embedding 模型/维度切换。首次空环境初始化及已有环境协议接入 SHALL 是独立授权步骤，不改变 test 的初始化合同。

#### Scenario: A candidate is incompatible
- **WHEN** 新旧版本无法相互读取发布窗口内的数据，或协调/schema/model profile 不兼容
- **THEN** 在旧版查询仍可用且写准入尚未关闭时拒绝普通发布，要求另行评审迁移

#### Scenario: A legacy or unknown writer is present
- **WHEN** 存在未接入协议的旧 SDK、导入工具、作业或未知写入者
- **THEN** 不启用普通生产升级，不以更换发布记录、超时或移除 Pod 代替协议接入验证

#### Scenario: Production is initially empty
- **WHEN** 空生产环境已完成单独批准的初始化和首次验收
- **THEN** 允许开放首个版本，不要求不存在的旧实例参与交接，也不将初始化伪装为已经验证的零停机升级

### Requirement: Preserve continuous query routing during cutover

生产发布 SHALL 先保持 Blue 服务并启动 Green 待命，通过兼容及业务验收后再切换稳定入口。切换过程中 MUST 保持可用查询端点，MUST NOT 暂停全部查询路由、缩容全部服务到零或仅凭资源更新成功报告切流成功。已有流式连接 SHALL 由原实例完成。

当前入口 SHALL 为显式接入的腾讯云 qcloud 直连 Pod profile：公网采用 CLB HTTPS，或已核验的 EdgeOne HTTPS 加 CLB HTTP/80 回源。MUST 先扩展到两个已验证 slot、确认 EndpointSlice 与 CLB 全部健康后端后再收窄，并通过 CLB 目标/健康及真实 HTTPS 候选身份确认传播。每阶段 MUST 核验精确 digest/config/profile 与实际 imageID，不得仅以相同 slot/Pod Ready 证明版本。真实入口参数和连接保持能力仍须隔离演练后才启用。

#### Scenario: The standby version is not ready
- **WHEN** Green 启动、兼容性或私有验收失败
- **THEN** Blue 继续提供原查询与写服务，不将正式流量切到 Green

#### Scenario: Routing changes propagate asynchronously
- **WHEN** 正式入口正在从 Blue 切到 Green
- **THEN** 两个 slot 在传播窗口均能处理兼容查询，实际端点和入口验证确认后才推进阶段，旧流式连接不因切流断开

### Requirement: Plan four production pods and eight during blue-green release

生产初始稳态 SHALL 配置 4 个应用 Pod，每 Pod MUST 使用 1 个 worker（`WORKERS=1`）；蓝绿发布 SHALL 为 Blue 4 + Green 4 共 8 个应用 Pod 预留调度、计算和后端连接/额度容量。该基线 MUST 仅作用于 prod，MUST NOT 改变 test 的副本数、worker 或资源配置；数据库、图服务及维护 Job MUST NOT 计入这 8 个应用 Pod。系统 MUST NOT 以容量不足为理由先停止 Blue、减少正式服务副本或提前回收未排空连接。

#### Scenario: Production starts with the approved baseline
- **WHEN** 生成生产初始部署或首次空环境上线配置
- **THEN** 正式服务配置为 4 个应用 Pod、每 Pod 1 个 worker，test 配置与原有发布基线一致

#### Scenario: A release prepares its standby slot
- **WHEN** Blue 的 4 个 Pod 正常服务且准备部署 Green
- **THEN** 为 Green 另外准备 4 个单 worker Pod，核验共 8 个应用 Pod 的资源及后端预算后才推进发布

#### Scenario: The eight-pod release budget cannot be met
- **WHEN** 无法调度 Green 4 个 Pod 或合并后端连接/模型预算不足
- **THEN** 不切流、不削减 Blue，明确报告容量预检失败，不能以缩容旧版或强杀连接腾出容量

### Requirement: Validate capacity before production scaling

4 Pod 基线 MUST NOT 被报告为已证明的高并发容量。后续容量决策 SHALL 通过获准隔离环境中的单 Pod、4 Pod 稳态和 4 + 4 蓝绿场景压测，覆盖普通/流式查询、缓存差异及读写混合负载，记录吞吐、在途请求、延迟分位、错误、排队、资源和共享后端/模型指标，并对照预先批准的负载及 SLO。扩至稳态 N Pod 前 MUST 核验发布期间 2N Pod 的资源、总连接与额度预算，显式批准 prod 专属配置变化；test MUST 保持不变，MUST NOT 因本基线自动启用伸缩。

缓存与性能准备 SHALL 先确认实际开关、能力边界和分阶段成本，分别统计各类请求/cache 的命中、旁路、检索、真实模型调用与等待。优化 SHALL 以同负载对照和数据/权限正确性为前提；新增缓存或请求合并 MUST 保持有界容量/等待、身份/版本隔离、必要计量及既有 release/coordination 保护。Embedding 配置字段、答案命中率或增加 Pod MUST NOT 作为能力已实现/容量已达标的替代证据。Redis 等新组件 MUST 单独评审授权，文档补充 MUST NOT 被当作代码实施或环境发压授权。

应用阶段及客户端耗时的固定直方图 SHALL 包含已确认目标所需的0.2s、3s、20s边界，精确边界值 SHALL 计入该上界，超出值 MUST NOT 向下舍入。分位 SHALL 明确为桶上界；区间报告 MUST 拒绝旧/混合桶布局或跨进程epoch的快照，MUST NOT 补零、插值或平均Pod分位伪造精度。未知验收参数及执行批准 MUST 保持未确认，新增分桶 MUST NOT 启用采集或实际负载。

协调器 SHALL 在 release 开关两种状态下记录一次脱敏首异常类型、有效 SQLSTATE 或未知值、连接用途、作用域/Pod/owner、实际连接 PID 与独立 witness PID、超时和隔离时间；polling 因协调错误停止时 SHALL 记录实际停止时间。MUST NOT 用 witness PID 冒充未知事务连接，MUST NOT 记录异常正文/SQL/凭据或因日志失败放弃 fail-closed。OpenAI-compatible SDK 的同步 client/lazy resource 准备 MUST NOT 占用协调器事件循环；取消 MUST 等待准备与自有资源关闭，实际异步请求/流 MUST 保留调用方循环、上下文及既有计量/许可。以上本地修复 MUST NOT 被报告为历史 SQLSTATE 已找回、真实现场已恢复或 SLO 已通过，MUST NOT 自动重试未知写入、recover 或清理证据。

流式OpenAI-compatible请求 SHALL 默认请求末尾usage并保留显式opt-out；不支持时 MUST NOT 删除参数自动重发。只有完整非负整数、总量一致且来自末尾chunk的usage SHALL 计入完整报告；中间累计、部分、冲突总量 MUST 保持未知，重复相同末量 MUST NOT 重复累计。观测 SHALL 区分`usage_pending`与结束后`usage_unknown`，未消费的流也须显式关闭结算；合法真实零与缺失 MUST 区分。已接入的验收客户端遇到unknown、身份/epoch漂移、指标缺失/重置或观测失败 SHALL 停止新增工作，已发送工作按原有边界收尾，MUST NOT 为补usage而重放或重启。未接入或成员不完整的窗口 MUST NOT 报告usage通过；该观测 MUST NOT 替代必要计费/审计authority，严格accounting原有流式拒绝保持不变。

v2计时 SHALL 新增包含关键词/Embedding/图/chunk及已启用rerank等待的`retrieval_total`，在context就绪时结束并排除最终生成；`embedding_total` SHALL 包含查询角色队列与结果解码，SDK/输入准备单列；PG的`vector_backend` SHALL 在Embedding就绪后开始并包含实际PG读取/池等待，不含Embedding。原`retrieval`/`vector`含义 MUST 保持，单调用与单请求、嵌套阶段 MUST NOT 混算；measurement版本或epoch不同 MUST 拒绝区间相减，未执行/无样本 MUST NOT 补0假通过。正式验收窗口/单位与外部容量仍须确认。

Embedding输入预处理 SHALL 不阻塞事件循环并在取消时join；同次调用内的完全相同文本可去重并按原用途还原，MUST NOT 因此跨请求/权限域缓存或改变向量归属。冷检索独立分支并行 SHALL 默认关闭、最多三路且保留结果合并顺序，有进度回调时保留原顺序；异常/取消 SHALL 等待同组读结束再传播，不提前释放许可。启用仍需共享后端峰值/质量预算，MUST NOT 把本地合成等待对照当成模型/后端SLO或真实容量证据。

#### Scenario: Four pods are insufficient at the target load
- **WHEN** 压测显示目标负载下延迟、错误、写入等待或后端指标超出批准预算
- **THEN** 容量报告明确瓶颈和未通过项，先评估资源/并发/模型与存储限制，再审批扩容并复测，不直接声称增加 Pod 即可解决

#### Scenario: A larger steady-state size is approved
- **WHEN** 根据压测批准从 4 扩至 N 个生产应用 Pod
- **THEN** 每 Pod 仍使用 1 个 worker，更新 prod 专属配置并核验 2N 蓝绿发布预算及连续性，原 test 配置和发布行为不变

#### Scenario: An answer cache hit still requires retrieval
- **WHEN** 当前查询路径构建检索上下文后命中答案缓存
- **THEN** 性能报告分别保留 PG/图/chunk 检索耗时与节省的最终模型调用，不将其报告为跳过全部查询链路，不仅根据命中率外推吞吐

#### Scenario: A workload uses streaming and conversation history
- **WHEN** 评估的负载包含流式新生成或带 conversation_history 的查询
- **THEN** 记录流式不回填、历史查询答案缓存旁路及关键词缓存的各自行为，分别度量 TTFT/总时长、实际模型调用和在途请求，不套用非流式重复问题的容量结论

#### Scenario: An optional cache optimization is proposed
- **WHEN** 计划启用 Embedding、检索、答案或进程内缓存优化，或把缓存读取提前
- **THEN** 先验证 scope/权限可见域、知识/模型/prefix/维度等依赖版本、删除/撤权/并发旧结果失效、有界容量及 gate/fence/ACK 合同；无法证明正确性则不启用，并保留原计量和抽取恢复证据

#### Scenario: Concurrent cold requests target the same question
- **WHEN** 多 Pod/slot 同时收到相同问题且缓存未命中
- **THEN** 基线记录实际模型重复调用及排队，合并方案只有在有界等待、逐请求授权/计量、取消与流资源生命周期验证后才可采用，不持业务锁/事务/连接等待模型，也不复用发布 owner 或写命令重放路径

### Requirement: Serialize releases and retain ownership evidence

同一生产数据 scope SHALL 同时最多有一个发布 owner；阶段转换及写执行权变更 MUST 核验 release 身份和单调版本。重复运行同一发布 SHALL 核对已记录阶段、digest 和配置后继续或拒绝，不重复执行已完成业务命令。进程消失或超时 MUST NOT 自动释放持久化发布锁、清除协调历史或触发接管。

#### Scenario: Two pipelines target the same production scope
- **WHEN** 第二个发布在第一个发布未完成时请求取得执行权
- **THEN** 第二个发布不修改路由、闸门、运行配置或命令队列，并报告当前 owner 冲突

#### Scenario: The release runner disappears
- **WHEN** CI/CLI 在交接期间中断
- **THEN** 后续操作者能核实实际阶段、写 owner 和队列状态，系统不因超时自行回收或重放不确定写入

### Requirement: Limit rollback to proven safe states

交权前确认无不确定业务写时，系统 SHALL 能中止发布并由原 owner 恢复排队写入，查询继续服务。交权后回退 MUST 重新证明数据向后兼容、写请求结束及执行权安全交接；系统 MUST NOT 自动清除 fence、执行故障恢复、回放未知结果写入或恢复跨存储的单边备份。

交权前安全中止 SHALL 先核验 Blue 实际路由再开闸，并在 `ABORTING` 保留发布 owner 直到待命实例真实关闭/缩容才进 `ABORTED`。交权后显式反向交接 MUST 绑定 from/to digest、旧 config/profile 和兼容证据，重新排空/核验路由并增加 epoch；旧 slot 已开始退休或发布已完成 MUST 拒绝直接复活旧版本。

#### Scenario: Drain exceeds the release budget before handover
- **WHEN** 排空将超出已批准预算且原 owner 状态确定
- **THEN** 中止本次升级、保留 Blue 查询并由其安全释放队列，不强杀正在执行的任务

#### Scenario: An uncertain mutation is found after writer handover
- **WHEN** 切换后存在未知 ACK、fence 或未确认旧写
- **THEN** 停止推进并保留证据，不能通过自动切回或 recover 宣称恢复成功

### Requirement: Verify continuity and test non-regression with real evidence

生产能力 SHALL 提供持续查询/流式连接、排队写入和失败拒绝的隔离演练证据，并记录 tag、commit、digest、配置版本、发布阶段、队列等待和命令终态。现有 test 发布 SHALL 有独立非回归证据。静态校验、Pod Ready 或 `/health` 成功 MUST NOT 被报告为零停机验收；Woodpecker MUST 保留不运行仓库测试套件的边界。

#### Scenario: A release continuity rehearsal passes
- **WHEN** 在已声明负载和预算内完成完整发布演练
- **THEN** 没有发布导致的查询断连或非预期错误，已接收写入在预算内可追踪完成或报告真实业务失败，且原 test 触发、配置和行为回归通过

#### Scenario: Only document validation has run
- **WHEN** 只创建提案并运行 OpenSpec/文本静态检查
- **THEN** 仅报告提案有效，不报告生产已部署、应用已实现、test 远端发布或零停机已验证


### Requirement: Provide fail-closed isolated rehearsal tools

工具 SHALL 默认离线校验，只有完整批准的隔离身份、负载/SLO/预算、质量真值、写确认及私有新attempt journal齐备时才执行受限混合写与原生4+4编排；MUST NOT 接入现有test流水线、使用任意hook/SQL写、自动recover或重放未知ACK。工具通过 MUST NOT 等同真实容量、领域语义或生产验收。

动态4+4演练 SHALL 在取得owner/发送业务前记录干净逐实例基线，固定UID/slot/digest/config/profile与进程epoch，允许PREPARED内Green逐步加入，并保留旧Blue退休后的最终SDK usage。旧监听关闭后 SHALL 以显式opt-in的有界终止摘要、既有closed六项零和实际退出/镜像身份联合证明，先在私有目录fsync全部四份final及journal再允许缩容；缺失/截断/重置/异常成员或usage_unknown MUST 停测，MUST NOT 填零或把Pod消失当结算。终止文件写入 MUST 不阻塞事件循环，取消须join且失败不发送closed ACK；MUST NOT 修改既有SQL/schema/权限或以观测代替写权。

混合runner SHALL 用共享stop阻断未发送读写/质量探针和后续advance；采样deadline SHALL 先停发再join只读收尾，已发送业务和长流 SHALL 按原有界预算自然结束，未知ACK不得重放。最终证据/样本总量和采样/转换deadline SHALL 在v2计划显式批准，无默认；未知正式值保持null。部分/未接入窗口 MUST NOT 报usage或SLO通过。原正常发布行为、既有test/保留候选和真实复测授权边界 MUST 保持。

执行输入与门禁细化见[本地验收准备包](../../verification/acceptance/README.md)。其中参数空白、批准false与not_run只表示准备状态，不能被当作任一场景已执行或生产授权；准备包不改变以下场景的验收语义。

用户另行批准的现有 test 分层路径 SHALL 为候选应用使用独立 namespace，并优先复用 test 已有 PG、图服务与 S3，以代码支持且经核验的数据域、权限和共享资源预算隔离原有业务。系统 MUST NOT 将独立数据域自动解释为重建物理组件或新增后端镜像要求；实际隔离不足 SHALL 报告具体缺口再评审必要增量。该结果 MUST 明确排除 qcloud/CLB 真实传播及连接保持验收，MUST NOT 放宽生产 Transport 或原 test 发布合同。无 Git 发布授权时，候选 MAY 在本地生成可审计的 working-tree overlay，绑定实际基础镜像 digest、逐文件及归档/分片 SHA256；MUST 标记非生产产物，不将 dirty 源码冒充已提交镜像，不打包凭据、接受符号链接或覆盖既有产物。实际候选产物/后端连接与隔离/预算前置不满足时 MUST 暂停相应变更，不能将未采用的新后端镜像拉取失败当作复用既有服务的阻断。

#### Scenario: Rehearsal input lacks isolated write authorization
- **WHEN** 未提供隔离与写入双确认、完整输入，或试图覆盖既有journal/续跑未知owner
- **THEN** 在连接/取得owner/发送新写之前拒绝，不读取test/prod密钥，不改变原发布路径

#### Scenario: Mixed writes span the closed gate and writer transfer
- **WHEN** 获准隔离演练发送本run的文本入库、图创建/编辑与受限文档删除
- **THEN** 使用原ReleaseOrchestrator和真实Transport核验固定4+4成员/路由，观察闭闸command的QUEUED及交权后同UUID终态，并以处理/删除终态或图读回确认，不以HTTP受理冒充完成；必然卡住闭闸的依赖预检拒绝

#### Scenario: A rehearsal fails or is cancelled after a write was sent
- **WHEN** 读写/控制/证据故障、负载错误/提前耗尽预算或调用者取消
- **THEN** 停止后续推进和所有未发送工作，已发送写有界收尾并保留未知结果，不自动abort/recover/replay，关闭HTTP/journal资源且不把未知ACK标成未执行失败

#### Scenario: Continuous reads and an old stream cover handover
- **WHEN** 演练报告发布连续性检查通过
- **THEN** 持续读负载覆盖整个发布窗口，旧Blue活跃query与持续消费的长流跨交权并正常结束，各phase普通/流式探针完成，不以离散探针、8个Ready或暂停流消费凑通过

#### Scenario: Quality evidence is missing or exposes forbidden context
- **WHEN** 质量case缺失/重复、变更后仍出现禁止事实/引用，或拒绝case仍暴露答案/检索内容
- **THEN** 规则门禁拒绝并输出不含正文/key的失败分类，核验明确事实变体、引用及chunk真值，不将字面rubric或独立检索探针声称为完整语义判断或生产质量验收

### Requirement: Explicit versioned first-time storage provisioning
生产业务/向量结构及图schema MUST 由受控、版本化迁移准备，应用启动和普通蓝绿发布 MUST NOT 自动补DDL或迁移旧业务数据。迁移与scope登记、release enroll MUST 分离；test关闭release时保持原初始化行为。

#### Scenario: Fresh target is provisioned explicitly and repeated safely
- **WHEN** 独立迁移身份对核验的空目标显式执行获准迁移
- **THEN** PG事务记录版本/checksum/profile/结构指纹，图逐项记录意图和最终状态；相同迁移重复执行仅验证，不自动升级模型/索引或改旧coordination checksum

#### Scenario: Existing storage or migration evidence is incompatible
- **WHEN** 发现无版本业务表、checksum/profile不匹配、结构漂移、runtime拥有DDL权限或并发迁移
- **THEN** 在进一步结构变更前拒绝，不自动采纳旧结构、清记录、扩权或把IF NOT EXISTS当兼容性证明

#### Scenario: Graph schema mutation has an uncertain result
- **WHEN** 图POST已派发但响应丢失，或创建后尚未达到CREATED
- **THEN** 保留逐项EXECUTING意图，不回滚PG或删除图，不盲目重发；只有后续GET证明已存在且兼容就绪才可确认，确认不了则停止

#### Scenario: Production runtime is not migrated
- **WHEN** 真实release实例启动发现账本缺失或结构不匹配
- **THEN** 不补建扩展/表/索引或图schema、不进入可服务状态；release关闭的原test路径不新增迁移依赖

#### Scenario: Initialization Job is prepared without deployment authority
- **WHEN** 本地渲染或检查初始化计划/Job
- **THEN** 仅输出固定镜像与既有凭据引用，不创建Secret、业务Deployment或入口，不执行迁移/模型调用或修改test及普通prod发布gate
