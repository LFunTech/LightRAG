# Production Delivery Implementation Plan

> **For agentic workers:** Use `superpowers:executing-plans` for native execution, or `superpowers:subagent-driven-development` only after the user selects delegation. This file is the OpenSpec implementation plan and task tracker; do not create a parallel Superpowers plan.

**Goal:** 在现有 test 产物链上构建生产蓝绿交付，查询连续、写入短暂有界排队，prod/test 配置和密钥独立。

**Architecture:** 原四个 workflow 与 test 部署路径保持原样；生产新增薄 workflow/script、独立 overlay 和发布编排器。应用通过默认关闭的发布协议提供持久化 owner/epoch、类型化命令队列、standby、查询/写入分类和实例级排空；缺少任一安全前置时拒绝生产发布，而不是降级成逐个重启。

**Tech Stack:** Woodpecker、现有 COS 源码包与 Kaniko 镜像、Python/asyncpg/FastAPI、Kustomize、既有 PostgreSQL/pgvector/HugeGraph/S3。

**Spec:** [proposal](proposal.md)、[design](design.md)、[prod-delivery](specs/prod-delivery/spec.md)、[release-write-handover](specs/release-write-handover/spec.md)。下面 59 项是唯一完成度清单（原 45 项 + 8 项性能准备 + 6 项首次初始化迁移补全任务）；后面的实施切片给出原任务的文件、接口和验证顺序，性能增量以 design 第 10 节为准。

## Global Constraints

2026-10-23 最新实际部署推进：用户确认正式域名 `llm-rag.eplus2.cn`、沿用 EdgeOne。`v1.5.8` 的流水线 #75 已完成构建/校验且镜像可在目标 TKE 拉取，但**应用未部署、生产未切流**。最新 prod 凭据经 TKE 只读核验仍未满足 runtime/operator 分离；首次结构未初始化，云身份无目标 CLB 只读权限、EO 可见站点为 0；当前 CLB HTTPS/direct 发布 profile 与已确认的 EO 入口也尚未对齐，属于实现侧待补而非要求 K8s TLS。临时诊断 Pod 已删除，异常后暂停，未再打 tag 或解除门禁，原 12 项不勾选。详见[实际部署阻断与最小运维交接](verification/2026-10-23-prod-deployment-blockers.md)；该最新现场记录不覆盖下文历史验证快照。

- prod 初始稳态 **4 Pod × 1 worker**，蓝绿 **4 + 4 = 8 个应用 Pod**；容量不足时保留 Blue，不自动 HPA。
- test 的四个 workflow、部署脚本、五实例、配置、Secret 映射和 base/test overlay 保持不变；通用静态检查仅作增量扩展。
- 发布协议默认关闭；未启用的 test/local 不新增发布元数据、迁移、接入或退出前置。
- `.secrets/prod.secrets` 只供 prod，`.secrets/test.secrets` 只供 test；不执行清单，不跨环境补缺，不输出凭据。
- 正式 tag 仍先经过既有源码/镜像校验；生产启用前不得选择实际 prod 部署。`-test`、`-pre`、push、PR 不获得 prod 部署能力。
- 新发布 schema 独立为 `lightrag_release`；不修改现有 coordination SQL/checksum，正常应用启动与日常发布均不 DDL/bootstrap/recover。
- 不串行化任意回调，不重放未知 ACK，不因 owner 超时接管，不把队列受理或 Pod Ready 当业务成功。
- 不自动提交、push、打 tag、归档、同步密钥、操作 test/prod 或其他仓库；线上接入与演练须另行授权。
- 保留工作区已有 query-accounting 改动；是否作为必要生产审计依赖须按最终代码单独验收，不代为提交或认定已交付。

以上禁止 Git 发布的约束是原实施授权边界；2026-10-23 用户另行明确允许本次生产必需改动的 commit、push 及正式生产 tag。按本次授权纳入发布代码所必需的既有 query-accounting 模块及其调用/回归依赖，保留业务语义，不启用或认定必要审计 authority 已可靠交付。该授权不解除生产接入、兼容性、权限、入口、真实验收或静态 gate 的前置。

## Review Focus

1. 写请求与关闸并发、旧 slot 迟到、嵌套 SDK 写入：首个业务变更之前必须得到可排空的持久化 ticket，等待不持业务锁。
2. 流式 iterator 返回后仍在运行、客户端断开、尚未开始消费：permit/并发槽/实例请求计数在真正结束或关闭后才释放，不能泄漏或提前变零。
3. 执行已生效但终态 ACK 丢失、执行者死亡、CI 中断：保留 `EXECUTING`/`UNCERTAIN` 和恢复关联，重跑不能重复业务效果或抢 owner。
4. 文件上传排队跨实例、会话到期、清理同时运行：只记录持久化内容/对象引用，保留源与结果，不序列化临时路径、文件句柄或凭据。
5. 路由传播慢、仅部分 Green 可用、协调器 broken 但 `/health=200`：逐实例业务 readiness 与真实入口证据不齐就不推进，不能据 Pod Ready 缩减 Blue。
6. 答案缓存命中后的检索成本、流式/多轮 bypass、同题冷启动放大：按阶段/实际模型调用度量，不能用总体命中率或 Pod 数外推高并发容量。
7. 新缓存/请求合并的 scope、权限、知识/模型版本、字节/时间上限与取消：不能混用数据、借用发布 owner、提前释放流资源或绕过 gate/fence/ACK。

## 计划状态与批准边界

- 2026-10-21：用户“就按照这个逻辑构建生产流水线”批准本 change 的代码实施方向，不包含真实发布、集群操作、Secret 导入或 Git 发布。
- 本次将原任务细化为下方可执行切片；**细化计划已获用户确认，选择当前会话本人逐项实施**；仅代码与本地验证获准，不包含线上操作。
- [本地交付基线](verification/2026-10-21-delivery-baseline.json)：当前 `tests/ci/` 83 passed；四个 workflow strict lint 与 test Kustomize render 通过。该证据不是 prod 实现或零停机验收。
- 既有 test/S3 change 仍有未完成的远端流水线/验收任务；本 change 不替它们勾选。此前 4 Pod 风险测试与 10 Pod 恢复记录只证明各自报告中的结果。
- 2026-10-21 性能补充后，用户“继续实施”已授权当前会话代码/本地验证；随后确认 1000 QPS 为包含模型生成的完整 RAG、约10000在线用户。**未授权真实发压、新组件、Secret 或集群/Git 发布**；现有生产 gate 和 test 保持不变。
- 2026-10-23 后续授权：先只读预检 test，条件满足后在独立 namespace/数据域部署候选，低负载验证功能、并发和 4+4；异常立即暂停。原 test 流水线/实例/数据不变，不操作 prod、不导入 prod 密钥、不提交/push/tag，1000 QPS 压测另行授权；“逐步改进直到指标”不取消这些限制。实测见[本次预检](verification/2026-10-23-test-preflight.md)：10/10 逐 Pod 协调接口 200/polling 正常，无待处理文档，原资源前后不变；但只观测到 nginx IngressClass，qcloud/CLB 隔离目标和完整前置未核验，候选尚未部署。等待确认分层测试或腾讯云非生产目标，不勾选 8.3/8.6 或整体性能任务。
- 用户随后明确选择“现有 test 分层验证，CLB 另验”：候选应用独立namespace，默认复用已有test PG、图服务与S3，以数据域/权限及共享资源预算隔离原业务；应用层低载/4+4不冒充qcloud/CLB或1000 QPS验收。此前将独立数据域扩大成重建物理组件是执行者错误，不是用户要求，已撤回；MinIO镜像不再是前置，不再要求用户补镜像。已实现的本地candidate_bundle及其测试仅作为可选交付准备保留。下一步先按实际配置只读定位现有服务，核对代码支持的数据域、权限及资源余量，再准备获准隔离数据域和候选；确有缺口才说明必要增量，不重建整套基础组件，生产Transport及原test流程不改。

## 本地交付状态（2026-10-21）

2026-10-23首次现场：用户要求本阶段先单独部署一个实例，由执行者选择namespace并随机生成Secret，不将发布operator凭据作为单实例功能测试的前置。已在 `lightrag-loadtest-20261023` 创建1 Pod×1 worker候选，复用现有test PG/图/S3，新逻辑数据域，明确关闭发布协议；不改4/4+4生产规划、不声称蓝绿或容量验收通过。候选因新LOG_DIR子目录未创建而在应用初始化前退出，按异常即暂停保留现场，未自动重建/重启；新协调scope和文档数据仍不存在、模型/业务请求为0。离线复现定位到日志目录前置，后续仅建议将该候选LOG_DIR指向已有可写挂载根后重试，尚未应用。见[单Pod尝试及停止记录](verification/2026-10-23-single-pod-attempt.md)。原10 Pod/配置/Secret前后未变、无后端重建/迁移或prod/Git发布操作，41/53及12待办不变，1000 QPS仍另授权。

后续用户分别批准只改LOG_DIR重建，以及补齐执行者遗漏的四项PG/HugeGraph存储选择后再建；完整离线构造预检0网络连接后，候选启动通过。1份258字节合成文档真实S3上传/读回及complete受理通过，但入库Stage 1协调异常，pipeline_status=503；脚本立即停止，6次计划RAG/并发查询均未发送。新scope留有1个orphan pipeline operation和claim，文档analyzing，未重启/recover/重放或清理。事件循环lag12.408s与协调默认10s超时仅为待验证线索，底层SQLSTATE因首日志仅release模式开启而未知。原10 Pod逐一health/协调接口200、身份/配置及文档计数不变。详见[最新重建与入库停止报告](verification/2026-10-23-single-pod-ingestion-failure.md)；主任务/未知参数/关闭的prod gate不变，不签署任何性能或发布通过结论。

本轮用户仅授权本地定位/修复及回归，明确保留现场、不重启/recover/重放，真实复测另确认。已将SDK同步client/lazy resource准备移出协调事件循环，补release开/关首异常与polling停止脱敏日志，并验证并行协调事务、取消join/关闭及OpenAI/Azure caller-loop边界。见[本地修复报告](verification/2026-10-23-coordinator-local-fix.md)及JSON。离线阻塞机制已复现，现场原始异常/SQLSTATE仍未知；本地代码未部署，旧419文件overlay只保留为历史现场，未重新打包/上传。未连接集群/原test/后端或读密钥，未签署实际入库/查询/4+4/CLB或SLO通过，41/53、12未完成和57场景保持不变。

随后用户授权新的独立namespace/全新数据域，复用已有PG/图/S3部署修复候选、单文档/查询/最多2并发复测，异常立即暂停。本run选定`lightrag-retest-20261023-0915`与新scope；cluster/原10 Pod清单/PG只读门禁通过，但执行者新编Gremlin空域探针收到400/MissingMethodException，立即停止，namespace尚不存在、创建和业务/模型请求0。版本/schema GET正常，不能据探针错误称图服务不可用；本地已改用storage同名vlabel/elabel，未重发修正请求或恢复部署。原故障Pod/文档/operation/claim及原test前后未变。见[本轮预检暂停记录](verification/2026-10-23-retest-preflight-stop.md)；S3/逐Pod HTTP/候选启动及低载复测尚未完成，主任务不新增勾选，未知正式参数与prod关闭gate不变。

用户再次明确允许恢复后，修正图探针、全部只读/离线构造门禁通过，才在该全新namespace创建1×1修复候选（旧故障域不变）。新260B合成原件S3上传/读回SHA一致，真正processed/1 chunk；6次完整RAG含普通/流式/多轮/两路并发均事实与引用正确，随后304.599s/31次只读稳定观察无503/quarantine，累计最大loop lag221.304ms。末端原10 Pod、原文档计数、旧故障Pod/overlay及旧文档/operation/claim快照不变，新scope无未决工作。**性能/计量验收未通过**：查询Embedding7样本均>100ms（均值255ms），首次冷检索1.753s，3次流式usage未知；向量/检索计时边界仍需拆清，不把6次热样本、无错误或小样本桶分位当1000 QPS/质量/SLO通过。见[新候选复测报告](verification/2026-10-23-fixed-candidate-retest.md)及JSON。候选/数据保留，无旧现场重启/recover/重放、prod或Git发布；41/53、12待办、57场景、未知正式参数不变，不勾真实发布/容量或10.2整项。

本轮按后续仅本地授权补流式末尾usage/完整整数校验、pending与terminal unknown区分、固定成员只读停测watch及新计时边界；输入预处理off-loop/同批去重与默认关闭的三路只读优化已补回归。未连接集群/后端/模型或读密钥，未重新打包/覆盖两个候选overlay。见[本地计量与冷路径验证](verification/2026-10-23-usage-timing-local.md)。2212 passed/66 skipped/2 warnings是本轮相关目录结果，不是全套或真实SLO；4+4动态退休计量连续性/混合写停控、提供商与后端遥测、正式参数及真实对照仍待，主任务41/53、12待办及57场景不变。

原 1–9 节授权的代码实现与本地验证已完成；其 **39/45** 项完成、6 项远端未完成的历史结果保留。
本轮性能增量已完成安全缓存策略（10.3）及混合写/4+4/质量工具代码与本地验证（10.7）；10.2外部遥测仍部分完成。当前总计 **41/53** 项完成、**12/53** 项未完成。
下列勾选不表示真实云端验收或 1000 QPS 已达到；原测试快照不覆盖本轮新增代码。

- 1.3 的依赖核对只记录真实现状，不替其他 change 勾选远端任务。
- 1.4/2.3/2.4 是接入清单、拒绝门禁和渲染/预检实现完成，真实 Secret/ACL/资源仍属 9.2。
- 5.4 保留用户未提交计量改动；当前发现必要 authority 时拒绝 release 激活，不宣称其可靠性已交付。
- 7.x/8.4 的故障注入包含真实本地 PG/ASGI 与 fake K8s/CLB，不包含真实 CI/Pod/腾讯云故障。
- 8.3、8.6、8.7、9.2–9.4 保持未勾选：真实连续性、压测扩容、接入与启用/首发均未运行。
- 8.5 原 48 场景记录和测试结果为原交付快照；新增4个性能场景及5个工具场景按矩阵区分本地新增证据与真实负载未验，由 10.x 补证，不套用原通过结果。
- [原本地验证结果](verification/local-verification.md)、[57 场景矩阵](verification/scenario-evidence.md)、
  [原发布最终审查与修复](verification/final-review.md)、[实施账本](verification/implementation-ledger.md)。
- 本轮另存 [性能本地验证](verification/performance-local-results.md) 与 [性能唯一审查/修复](verification/performance-final-review.md)，不将历史结果套用于新代码。
- 工具补全另存 [10.7本地验证](verification/rehearsal-local-results.md) 与 [唯一审查/修复](verification/rehearsal-final-review.md)，不覆写原40/53或9383项的历史快照。

## 1. 评审与现有 test 基线

- [x] 1.1 取得本 proposal/design/specs 的实施批准；明确创建提案不包含实施、Secret 同步、提交推送、打 tag 或生产操作授权。
- [x] 1.2 固定现有四个 workflow、源码归档、镜像解析、test 脚本/overlay 和 `tests/ci/` 的行为基线，列出生产复用点与最小增量清单，不重写 test 发布流程。
- [x] 1.3 核对 test 交付、S3 接入和分布式写入 change 的实际实现/验收依赖；只记录本 change 所需证据，不替其他 change 勾选任务或宣称通过。
- [x] 1.4 建立生产接入清单：目标身份、首次/已有环境路径、全部 writer/SDK、协议基线、数据域、代理链、容量及有限排队/排空/切流预算；缺项阻止启用发布。

## 2. 环境独立配置与密钥映射

- [x] 2.1 先添加清单解析回归：中文/带空格/引号/列表标签、历史别名、重复冲突、缺项、非法值与日志脱敏，再实现只解析不执行的环境显式导入工具。
- [x] 2.2 定义 `.secrets/prod.secrets` 到 `lightrag_prod_*` 及应用字段的白名单映射与显式 API key 别名；证明 prod 缺项不读取 test 文件、不覆盖 test CI Secrets，反向亦同。
- [x] 2.3 定义 prod 非敏感配置、独立 kubeconfig/部署授权及 registry pull 引用，核验共享构建服务的复用不会修改 test 凭据；不在本任务中自动上传实际密钥。
- [x] 2.4 实现目标身份、数据作用域、图域、S3 ACL/前缀、存储路径和权限的预检及脱敏配置差异检查，禁止默认继承 test 的目标、实例列表和存储类。
- [x] 2.5 为生产运行 Secret/ConfigMap 增加版本化引用，验证准备 Green 不改变 Blue 引用或任何 test 资源；补源码包/镜像/日志不含密钥的回归。

## 3. 可选发布控制协议与元数据

- [x] 3.1 先添加默认关闭、prod 显式启用、scope/profile 校验和 test/local 无新增元数据依赖的回归，再实现接入及协议标记。
- [x] 3.2 添加独立版本化 `lightrag_release` 元数据迁移、verify-only 启动和显式接入 CLI；测试重复迁移、结构漂移和权限失败，不修改原 coordination migration SQL/checksum。
- [x] 3.3 实现持久化发布 owner、阶段和条件 epoch 更新；测试并发发布、旧 owner、重复运行、CI 中断和遗留锁，禁止超时自动接管。
- [x] 3.4 为发布控制 CLI 增加最小运维权限、环境限制及脱敏进度输出；验证普通业务身份不能变更 slot、闸门或执行权。

## 4. 写入准入、有界命令队列与安全执行

- [x] 4.1 先列出并测试 HTTP/SDK/后台领取及隐式写入口，覆盖关闭闸门与操作准入竞态，再实现持久化准入；等待者不得持有业务锁/事务。
- [x] 4.2 实现显式类型化命令登记、版本化参数/S3 引用、顺序和幂等身份；覆盖入库、上传会话/complete、图编辑/合并、删除/清空及人工重试，拒绝未登记外部 writer。
- [x] 4.3 实现跨实例的同步请求等待/原响应语义和异步任务原有受理语义；测试切流、接收者丢失、客户端断开、同幂等键同/异负载，不将队列受理伪报业务成功。
- [x] 4.4 实现 Green 有权消费与逐命令授权复核，保留 `EXECUTING`/`UNCERTAIN` 和恢复关联；测试权限撤销、结果丢失和 executor 死亡，禁止未知 ACK 自动重放。
- [x] 4.5 实现有限容量/等待预算/超时余量预检、接收前限流及交权前预算中止；验证已接收请求不丢弃、长任务不被强杀、队列能在安全条件下回交 Blue。
- [x] 4.6 将队列引用纳入对象/会话/结果保留策略，测试会话过期边界、清理竞态和 doc-status 已受理任务不重复创建、不扩大 FAILED 重试次数。

## 5. 查询连续性与实例级生命周期

- [x] 5.1 实现 Green standby：正常依赖验证和私有查询可用，但不生产写、不自动 polling、不 DDL；测试生产积压存在时不被待命实例领取。
- [x] 5.2 区分只读查询 permit 与待排空业务 writer，测试写闸门关闭时普通/流式查询不等待，并保留现有数据/权限/协调保护。
- [x] 5.3 对查询缓存增加发送前的 slot/写权检查与可观测跳过，已发送 mutation 纳入排空；测试旧流在交权后结束不补写缓存，不吞掉存储错误。
- [x] 5.4 核实最终合并代码中的查询计量/审计等副作用；必要持久化事件实现可靠保存与确认，不可靠则拒绝发布，不能将现有未提交改动当作已交付依赖。
- [x] 5.5 实现实例停止领取、活跃请求/流/排队等待连接和后台写 drain，调整实例资源关闭与 workspace 维护独占的边界；测试旧实例退出不阻断 Green、不提前关闭 witness/pool，并证明 test 默认退出行为不变。

## 6. 基于现有 test 的生产交付增量

- [x] 6.1 在原 workflow 回归基础上增加正式 tag 的生产显式启用选择测试，以及 `-test`/`-pre`/push/PR 不选择 prod 的拒绝测试。
- [x] 6.2 从现有 `deploy-test.yml` 的结构派生 `deploy-prod.yml`，复用相同 `pre-deploy` 依赖、内部工具镜像、`skip_clone`、COS 源码下载校验和镜像解析；不改原三个构建/验证阶段或 test 入口。
- [x] 6.3 增加薄 `deploy-prod.sh` 和必要 CLI 调用，复用现有安全工具；测试复杂运行协议不进入 YAML、不复制整份 test 脚本、不依赖跨 workflow 的本地文件。
- [x] 6.4 增加 prod overlay 的稳定入口与 slot selector、digest 注入、独立配置/Secret、云侧TLS/HTTP后端及流式代理、资源/存储/网络策略及待命参数；render 检查无测试目标残留、不重叠 selector、不暂停全部查询；禁止应用K8s TLS/证书Secret与挂载。
- [x] 6.5 最小扩展现有交付静态检查覆盖生产脚本/manifest；证明不增加仓库测试套件步骤、不重复构建、不改变现有 test 实例与验收行为。
- [x] 6.6 固定 prod 稳态 4 Pod、每 Pod `WORKERS=1`、蓝绿 Blue 4 + Green 4 的容量参数和 render 回归；验证首次上线也是 4 Pod、8 个应用 Pod 资源/后端预算不足时保留 Blue 并拒绝推进，test/base 的副本、worker、资源默认值不变。

## 7. 发布编排、切流与条件回退

- [x] 7.1 实现 `PREPARED → DRAINING → READ_SWITCHED → WRITER_SWITCHED → RETIRING → COMPLETED`，每步核验 owner、epoch、digest、配置与实际状态；待命失败时 Blue 保持原服务。
- [x] 7.2 汇总实际 writer、claim、mutation、缓存写和后端请求证据确认排空；测试查询持续存在时仍可交接，读取错误不是空闲，禁止固定 sleep/Pod 数/手写快照替代证明。
- [x] 7.3 实现稳定入口切换及真实端点/入口核验，交权后 Green 唯一有权执行新命令；测试路由传播期、Blue 迟到写和长流保持，不在排队连接结束前回收旧实例。
- [x] 7.4 实现交权前安全中止及交权后显式条件回退，覆盖超预算、fence、未知 ACK 和 CI 中断；不自动 recover、清锁、降级 local writer、`rollout undo` 或还原单边备份。

## 8. 本地验证与隔离演练

- [x] 8.1 运行与实际改动匹配的 `tests/ci/`、`tests/distributed/`、`tests/pipeline/`、API routes、LLM/cache 及 setup 测试，记录命令、pass/skip/fail；公共基础或前端改动按仓库规则扩大检查。
- [x] 8.2 运行 Woodpecker strict lint、shell/Python 静态检查、prod/test Kustomize render 和完整现有 test 交付回归；对比基线证明触发、五实例、Secret、初始化/恢复与健康验收未改变。
- [ ] 8.3 经单独授权在隔离环境持续普通/流式查询并跨各阶段提交写操作，记录连接、错误、延迟、排队预算、幂等执行和最终存储结果；不把单次健康检查当作零停机验收。
- [x] 8.4 注入并发发布、CI/Pod 中断、长任务、旧 writer 迟到、队列满、丢失 ACK、授权撤销、Secret 串环境/缺失、切流失败及不兼容版本，验证拒绝和恢复边界。
- [x] 8.5 对照两份 capability 的全部场景逐项保存证据并运行 `openspec validate add-prod-zero-downtime-delivery --strict`；未真实运行的验收明确保留未完成。
- [ ] 8.6 经单独授权在隔离环境完成单 Pod、4 Pod 稳态和 8 Pod 蓝绿的阶梯/持续压测，覆盖缓存、普通/流式及读写混合负载；记录 QPS/在途请求、首包/总延迟 P95/P99、错误/超时、排队、CPU/内存、模型额度/等待、PG/协调池和 HugeGraph 指标，形成对照目标负载/SLO 的容量报告，不改现有 test 实例或在线 prod 基线。
- [ ] 8.7 基于容量报告评审后续稳态 N/发布 2N 的资源、后端连接/模型预算、并发参数、扩容上限及触发/回落策略；扩缩容复测实例排空和发布 owner 协调，保持每 Pod 1 worker，仅更新获准的 prod 配置，不自动启用 HPA 或影响 test。

## 9. 生产接入、文档与正式启用

- [x] 9.1 同步分布式 runtime/pipeline、查询缓存、退出及部署合同，新增生产 runbook，明确与 test 的继承关系、差异、4 Pod/单 worker 与蓝绿 8 Pod 基线、压测扩容证据、预算、权限、首次初始化和条件回退。
- [ ] 9.2 经独立授权导入 prod 密钥/配置及受限部署凭据，执行只读连接与数据域隔离检查，准备备份和显式初始化；不得修改 `.secrets/test.secrets`、test CI Secrets 或 test 资源。
- [ ] 9.3 完成全部受控 writer 的协议基线接入及隔离预演评审后，取得明确生产启用/发布授权，再启用正式 tag 的 prod 部署；未授权前不提交推送、打 tag 或操作生产。
- [ ] 9.4 保存第一次获准生产发布的真实 tag/commit/digest、配置/epoch、查询连续、排队/排空和 test 非回归证据；确认 `main`、现有 test 发布与业务数据边界未被越界修改，不自动归档提案。

## 10. 缓存与性能准备增量（代码/本地验证获准，真实发压未获准）

先审批工作负载与测试边界，再按 **P0 目标/观测/正确性 → P1 检索和额度 → P2 按证据选择缓存/合并** 推进。不得为完成清单而无条件安装 Redis、实现所有可选优化或更改现有 test；未采用的优化须给出评审理由，不等于跳过性能验收。

- [ ] 10.1 固定容量验收输入：目标 QPS/在途并发、普通/流式/多轮比例和时长、模式/token、知识规模与读写比、P95/P99/TTFT/错误/等待 SLO、全部资源/额度和隔离授权；未批准输入不做容量承诺。
- [ ] 10.2 在真实查询/模型/缓存/后端路径增加有界阶段指标与关联：等待、关键词、Embedding、向量/图/chunk、生成首包/总耗时、各 cache hit/miss/bypass/skip/error、实际调用/token、池/锁/流/资源；不输出凭据/正文或新增高基数标签。
- [x] 10.3 核实实际缓存开关并设计 prod 显式版本化策略；补答案命中仍检索、stream 不回填、多轮旁路、抽取恢复缓存保留及数据/删除/权限/模型变更失效的真实路径回归；未证明安全的优化不启用，不改原 test 默认值。
- [ ] 10.4 根据分阶段基线评估并实施获准的检索/调用优化和角色预算：查询计划/向量索引、批量读取/往返、质量约束下的 Top-K/token、查询与入库并发/池/有限队列；核算 4/8 Pod + 其他调用者，结构/索引变更另走显式迁移，不绕过协调或重放未知写。
- [ ] 10.5 基于重复成本评审 Embedding 精确缓存/有界 L1/已有 PG 持久层；仅实施被批准的选项，明确 scope/可见域/知识及模型 revision/prefix/维度、容量/TTL/失效、并发旧结果写回和故障行为，并通过 TDD；可记录有证据的不采用结论，不能把现有配置字段当成已接线能力。
- [ ] 10.6 评估同题热点合并：先有界进程内 singleflight，再按跨 Pod 放大证据决定是否需要分布式只读去重；验证取消/失败/等待上限及逐请求授权/必要计量，不持业务锁/PG 事务或连接等模型，不复用写队列/owner，不默认广播未完成流；是否采用必须有证据。
- [x] 10.7 准备可复现的负载、分阶段采集及质量/缓存一致性验证工具，覆盖冷/热、随机/热点、普通/流式/多轮、读写混合与 4+4 发布；实际发压归入另行授权的 8.3/8.6，不清理现有 test/prod 或抽取恢复缓存。
- [ ] 10.8 保存同数据/资源/目标下优化前后对照及所有新增场景证据，核对 SLO/召回与引用质量/权限/缓存/发布安全，再评审 8.7 的 N/2N 和 9.3 的启用条件；Redis/新全局额度组件或读写拆分必须另案批准，未达标明确阻止容量承诺与生产启用。

### 性能任务的实际边界

- 10.1：已确认1000完整RAG QPS/约10000在线；SLO、流式/多轮比例、数据/读写比、资源/额度和隔离授权待补，draft plan 缺项拒绝网络。
- 10.2（部分）：真实 SDK/角色/存储读/OpenAI/cache/ASGI/协调调用、PG读池、流生命周期和进程资源已插桩，默认关闭/认证健康可见；固定词汇/桶，无正文/高基数。逐 Pod 采集、提供商内部重试/全账户额度、后端锁/池/云资源遥测联调仍需隔离环境，不能据本地数据算容量。
- 10.3：prod 显式 `ENABLE_QUERY_ANSWER_CACHE=false`，保留关键词开关与抽取恢复缓存；四开关进入不可变配置/hash/schema，test/local 默认不变。实际 kg/cache 测试证明 lookup 后检索/多轮旁路和禁用策略不复用知识/权限/模型变更前的答案；不是已实现知识版本缓存或线上 ACL 验收。
- 10.4–10.6：角色并发预算为 `4×MAX_ASYNC+EMBEDDING_FUNC_MAX_ASYNC`；现仅有单候选低载样本，已按本轮授权实现输入off-loop/同批去重、默认关闭的三路只读优化及分项定位。完整阶段/质量/4与8Pod峰值预算及采用评审仍缺；不擅改索引/Top-K/全局限额，不引入跨请求Embedding/L1/singleflight，10.4–10.6仍未完成。
- 10.7（工具完成）：原只读open-loop/离线阶段差值已补typed混合写、原生4+4编排/精确队列终态、持续负载/旧流证据与确定性事实/引用/chunk/禁止内容rubric；默认离线、未来写演练双确认，故障冻结新工作/未知ACK不重放。本地全后端9420项、隔离PG70项通过；字面rubric不穷尽语义，真实发压/领域质量仍属8.3/8.6/10.8，不因10.7勾选放行生产。
- 10.8：同负载前后对照、质量/权限/容量/发布安全真实验收均未运行，不放行8.7/9.3。

## 本地验收准备包（文档准备完成，真实任务仍未完成）

2026-10-21用户授权将[验收准备包](verification/acceptance/README.md)补入本change。参数底稿未知值为`null`、执行授权false；指标来源/全入口/故障/隔离作业/停止预案已形成文档，未访问环境、创建作业、读取/导入真实密钥、真实发压或执行Git发布。

这只是10.1/10.2、8.3/8.6/10.8等任务的本地准备子成果，不新增任务编号或checkbox；**41/53和12项未完成保持不变**。10.4–10.6仍须基线与采用评审，8.7/9.2–9.4仍需真实证据及独立授权。准备包中的空白、not_run或拒绝用例不当作通过；后续批准顺序见准备包，文档核验见[acceptance-preparation-results.md](verification/acceptance-preparation-results.md)。

2026-10-22用户“继续推进”后，在同一本地材料边界补齐[指标/执行登记表](verification/acceptance/records-guide.md)及[缺项报告](verification/acceptance/preflight-gaps.md)，不新建实施计划、不增加运行工具。原198个待定参数和12类false批准保持不变；20指标来源未验，51入口/26故障/5容量单元/6phase均not_run。当前任务计数不变，接下来需要责任方确认参数/来源及单独授权，而非继续补默认值或自动发压。

同日后续用户提供7项性能数值目标，已更新[参数底稿](verification/acceptance/parameters.json)及[目标/口径映射](verification/acceptance/metrics.md#用户确认的性能目标2026-10-22)，缺项报告随新参数SHA更新。前述198项是数值更新前历史快照；P99/写等待、成功率统计及计时边界、负载/额度等仍缺，12类批准保持false。无reranker的profile适用性、0.2/3/20秒分位证据精度均保留待解决；仅记录目标不等于10.1/10.2完成，仍41/53、12未完成，不新增场景或环境操作。

随后用户明确技术成功与相关性分验、合法空结果计技术成功、Rerank非必需，并授权代选写排队最大值。参数v3记录30s规划基线、成功语义和可选重排策略，完整说明见[指标文档](verification/acceptance/metrics.md#写排队30秒的规划依据与边界)；仅剩195个待定参数叶子，相关性阈值/P99/负载与预算仍未代填。空结果不替代真实生成容量，已受理写不因30s自动取消/重放；无运行代码/配置、test/prod、Secret或Git发布变更，任务计数及执行批准不变。

用户另行授权的200ms/3s/20s直方图子切片已完成代码与本地验证，见[本轮独立结果](verification/histogram-local-results.md)。应用新增三桶，客户端补缺少的两桶并保留原20s；默认关闭、边界包含/略超、累计差值/P95/P99及旧布局拒绝回归通过。参数底稿/195个空值及12类false授权不变；外部遥测/跨Pod联调和真实容量仍未验，10.2保持未勾选，53项/41完成/12未完成不变，不新增任务或场景编号。

用户后续授权补齐该轮延期Minor：新增客户端混合分位秩3例、报告零增量2例及溢出分位4例；9项定向通过、4种临时内存变异均被捕获，性能目录123项通过。见[延期项补齐记录](verification/histogram-local-results.md#延期分位测试补齐2026-10-22)。仅修改测试及OpenSpec材料，未改生产算法/参数/配置，不回写历史30/257测试结果；历史Minor已关闭，10.2整项及主任务/场景计数不变。

随后用户指向secrets并授权按经验判断负载：只读核对prod清单非敏感元数据，未找到明确模型型号/额度字段，未使用密钥访问账户或模型、未读test清单。参数v4仅代选4项负载字段（80%流式、30%多轮及输入/输出Token离散分布），并标注规划假设/未核验模型兼容，详情见[输入规划](verification/acceptance/README.md#授权代选的负载基线与模型清单核对2026-10-22)。当前191个待定叶子；其余参数及全部12类批准不变，10.1等未完成项不勾选，不把模型算术需求或清单存在当真实额度/容量。

## 实施切片与接口决策（计划评审稿）

顺序为 A → B → C → D → E → F → G → H。每个切片按 **先写行为失败测试 → 单独运行证明失败 → 实现真实路径 → 运行对应目录及既有回归 → 审查 diff/记录证据** 推进；通过前不进入依赖切片。不在切片间自动 commit。这里的路径均相对于仓库根目录，测试不得连接现有 test/prod；需要真实后端的验收保留为单独获准步骤。

### A. 固定交付身份、生产接入契约与秘密映射（1.3–2.5）

**Files**
- 新增 `scripts/ci/prod_config.py`、`scripts/ci/import-environment-secrets.py`、`k8s-deploy/lightrag-kustomize/overlays/prod/onboarding.schema.json`。
- 新增 `tests/ci/test_prod_config.py`、`tests/ci/test_environment_secrets.py`；复用 `scripts/ci/delivery.py` 的来源/digest/归档校验，不修改其 test 选择语义。

**Interfaces**
- `load_prod_config(path: Path) -> ProdConfig`：严格字段、禁止未知值，含目标身份、数据域、显式凭据别名、TLS/代理、资源/后端预算、writer 清单和有限时间/容量预算；schema 只定义结构，不编造生产域名或预算值。
- `parse_secret_inventory(path: Path, *, environment: Literal["prod", "test"], aliases: Mapping[str, str]) -> dict[str, str]`：只读所选文件；解析中文标签、列表、引号、空格及显式历史别名；重复冲突、缺项、非法值仅按字段名报错。
- 导入 CLI 默认只输出脱敏映射计划；`--environment` 必填，真正导入需要明确 `--apply`，CI 名称只允许该环境前缀。密钥只从选定本地清单读取；此阶段测试全部使用临时假值，不运行 `--apply`。
- `validate_budget(config: ProdConfig) -> None`：所有值正且有限，`drain + cutover + safety_margin < request_wait`，`request_wait + response_margin < min(proxy_timeout, client_timeout)`；入库等异步任务原受理时间与长任务执行时间分开。队列保留窗口必须覆盖等待/执行/结果读取预算；缺项不启用。

**Red/green checks**
- `test_prod_missing_value_never_reads_test_file`、`test_conflicting_aliases_redact_values`、`test_inventory_is_not_executed`、`test_environment_prefix_cannot_be_overridden`。
- `test_prod_rejects_test_scope_before_mutation`、`test_budget_rejects_infinite_missing_or_inverted_limits`、`test_same_backend_requires_verified_acl_evidence`。
- `./scripts/test.sh tests/ci/test_prod_config.py tests/ci/test_environment_secrets.py tests/ci/test_delivery.py -q`；源码归档与镜像上下文检查必须排除凭据输出文件。

### B. 独立发布控制面与有界只读诊断（3.1–3.4、7.1 的控制基础）

**Files**
- 新增包 `lightrag/distributed/release/`：`config.py`（opt-in/profile）、`models.py`（固定版本类型）、`store.py`（PG 状态/条件更新）、`__main__.py`（运维 CLI）、`migrations/001_release.sql`。
- 按实际打包方式更新 `MANIFEST.in`/`pyproject.toml` 的发布 SQL 包数据；不更改 `lightrag/distributed/migrations/`。
- 新增 `tests/distributed/release/test_config.py`、`test_store.py`、`test_cli.py`、`test_packaging.py`。

**Interfaces**
- `ReleaseConfig.from_env() -> ReleaseConfig | None`：`LIGHTRAG_RELEASE_ENABLED=false` 默认；启用必须为 distributed profile，明确 scope、slot、实例身份和协议版本。新旧共用既有 coordination identity，颜色不进入该 identity。
- `ReleaseIdentity` 包含 scope/release_id/tag/commit/digest/config_hash/protocol_version；`ReleaseSnapshot` 只含阶段、epoch、slot、计数和错误类别，不含原始命令正文/令牌/历史全集。
- `ReleaseStore.verify_schema() -> None`、`migrate() -> None`：启动只调用前者；迁移仅显式 CLI 调用，校验版本/checksum/结构与权限，重复迁移幂等。
- `begin(identity, *, expected_epoch: int) -> ReleaseSnapshot` 与 `transition(identity, *, expected_epoch: int, expected_phase: Phase, next_phase: Phase) -> ReleaseSnapshot`：同一 scope 行锁和条件更新；身份不同或遗留 owner 一律拒绝，无 TTL 接管。
- `snapshot(scope: Scope) -> ReleaseSnapshot`：索引化聚合、有限分页，不调用已有全历史 `coordinator.inspect()` 作为 readiness/排空判定。
- `python -m lightrag.distributed.release {migrate,onboard,status,prepare,drain,switch-writer,retire,complete,abort,rollback}`：控制 DSN 仅环境注入，不在 argv/日志；业务运行身份不能修改控制字段。命令输入为身份与预期 epoch，不接受“已排空=true”之类用户手写证明。

**Red/green checks**
- 默认 test/local 不创建连接或查询新表；结构漂移、权限不足、旧 epoch、并发发布、相同发布重入、runner 消失均 fail closed。
- 普通业务身份不能执行迁移/交权；快照不返回凭据或无限历史；构建 wheel 后可读取发布 SQL。
- `./scripts/test.sh tests/distributed/release/test_config.py tests/distributed/release/test_store.py tests/distributed/release/test_cli.py tests/distributed/release/test_packaging.py -q`。

### C. 关闸准入、类型化队列与跨实例执行（4.1–4.6）

**Files**
- 新增 `lightrag/distributed/release/commands.py`、`admission.py`、`executor.py`、`authorization.py`。
- 接入 `lightrag/distributed/runtime.py`、`lightrag/api/distributed.py`、`lightrag/api/routers/{document_routes,graph_routes}.py`、`lightrag/lightrag.py`、`lightrag/pipeline.py`、`lightrag/object_storage/sessions.py` 及现有清理调用点；不重排 purge/anchor 写入。
- 新增 `tests/distributed/release/test_{admission,commands,executor,authorization}.py`、`tests/api/routes/test_release_commands.py`、`tests/object_storage/test_release_retention.py`。

**Interfaces**
- `CommandEnvelope`：固定 `operation/version/scope/principal/idempotency_key/payload_hash/payload/source_refs`；`CommandResult` 保留原状态码、响应结构或 SDK 结果类型，不把排队转成成功。
- `CommandRegistry.register(name: str, version: int, codec: CommandCodec, handler: CommandHandler) -> None`：显式登记入库、上传 presign/complete/兼容 upload、图创建/编辑/合并/删除、自定义 KG、清空、人工重试、source-conflict repair、pipeline cancel/scan 等全部现有写入口；只读分页 POST 不因 HTTP 方法误入队。无 codec 的 SDK 写入在首次变更前拒绝。
- `ReleaseStore.admit_writer(scope, *, slot, epoch, command_id) -> WriterTicket | None`：与关闸锁同一 scope 行；ticket 先于原协调 operation 获准，覆盖两者之间的进程死亡窗口。已获准 ticket 正常排空，不因关闸中途撤销。原协调 mutation/pending/ACK 保护继续存在。
- `enqueue(envelope: CommandEnvelope) -> CommandReceipt`：同幂等键同负载关联原记录，异负载冲突；先判容量再接收。`wait_result(receipt, *, deadline) -> CommandResult` 不持业务锁/事务，断开不删除命令；超时只报告可追踪未知/待处理状态，不宣称未执行。
- `claim_next(*, slot, epoch) -> ExecutingCommand | None` / `finish_command(command, result) -> None`：有权 Green 消费，执行前复核当前主体/作用域/过期与权限；运维身份不替代用户。只存主体标识、凭据指纹/授权版本和有效期，不存明文 token。未知执行结果保留 `EXECUTING`/`UNCERTAIN`，不自动重排。
- 文件只使用可跨实例验证的持久化内容或不可变 S3 引用；临时文件路径/回调不得落入命令。multipart 的内部暂存对象属于受限队列持久化，不创建 doc-status 受理事实；未完成命令保护对象、会话与结果免于清理。大于批准暂存额度的请求在受理前明确拒绝。

**Red/green checks**
- 关闸竞态二选一（ticket 或队列）；嵌套调用不二次入队/死锁；排队无业务锁/事务；新旧实例相同幂等键只执行一份。
- 同步删除不提前 `202`，异步入库仅在既有持久化受理完成后返回 track id；接收者死亡、客户端断开仍可查；执行者死亡/ACK 丢失不自动重试。
- 授权撤销/过期、恶意 operation/payload、上传会话过期与清理竞态、FAILED 一次重试、不支持 writer 均覆盖。
- `./scripts/test.sh tests/distributed/release tests/api/routes/test_release_commands.py tests/object_storage -q`；修改文档/graph 路径前读取 PipelineConcurrency/PurgeRecovery 合同，保留并运行既有对应 routes/SDK/pipeline 回归。

### D. standby、流生命周期、缓存与真实 readiness（5.1–5.4）

**Files**
- 新增 `lightrag/distributed/release/query.py`、`readiness.py`；接入 `lightrag/distributed/{runtime,pipeline}.py`、`lightrag/api/lightrag_server.py`、`lightrag/api/routers/{query_routes,ollama_api}.py`、`lightrag/operate.py`、`lightrag/utils.py`、必要的 provider stream 收尾路径。
- 新增 `tests/distributed/release/test_{standby,query,readiness}.py`、`tests/api/routes/test_release_queries.py`；扩展 `tests/llm/test_global_concurrency_queue.py` 的真实 iterator 消费测试。

**Interfaces**
- `ReleaseRuntime.start(rag) -> None`：verify-only，standby 不启动生产 polling/命令消费/缓存填充；私有验收查询保留既有认证和协调校验。后台发现关闭闸门时保持任务未领取，不用全 workspace pause/cancel。
- `ManagedQueryStream`（`__aiter__`/`__anext__`/`aclose`）管理跨 ASGI task 的真实 iterator 生命周期；query durable operation/实例请求计数与并发槽直到消费完成或关闭才释放，不能在返回 iterator 时释放 ContextVar permit。不修改既有用户 query-accounting 契约。
- `cache_write_ticket() -> AsyncContextManager[WriterTicket | None]`：每次可选查询缓存写在发送前原子尝试取得 writer ticket；无权时计数并跳过，已发送异常不能吞掉。实体抽取的归因缓存不是可随意丢弃的查询填充，保留原 anchor 顺序。
- `readiness() -> ReadinessReport` 与新增 `/ready`：检查初始化、协调连接/witness、scope fence、应运行的 polling/executor 及本实例是否允许新请求；standby 的“合法不 polling”和故障“polling 已死亡”必须区分。只返回状态/错误类别，kubelet 可调用，不泄露 scope/配置。prod probe 使用它；`/health`、base/test probe 保持原样。
- coordinator 首异常记录 SQLSTATE、用途、Pod/workspace、backend PID 和 polling 停止时间，不记录 SQL 参数、正文或 DSN；永久 quarantine 不清 `_broken`、不自动 recover/重试未知写。新 readiness 必须识别它。
- 若最终 query-accounting 被确认为必要持久化审计，使用独立可靠事件保存/确认路径并纳入准入测试；未能证明可靠性时拒绝启用 prod，不修改用户未提交功能来绕过该条件。

**Red/green checks**
- standby 在真实 pending fixture 下不领取；关闸期间普通/流式查询仍返回且不排队；旧流交权后不补写缓存。
- 4 个流在并发上限 1 下最多 1 个 provider stream 活跃；iterator 未消费/正常结束/异常/取消/显式关闭不提前释放也不泄漏；同时测试 Ollama 与 REST 路径。
- 协调器 broken、witness 丢失、polling 意外退出时 `/health` 可仍 200，但 `/ready` 为 503；无发布元数据的原 test 启停回归不变。
- `./scripts/test.sh tests/distributed tests/api/routes/test_release_queries.py tests/llm/test_global_concurrency_queue.py -q`；`utils.py` 属公共基础，最终里程碑按仓库规则扩大非 integration 后端验证，不能只运行新增测试。

### E. 实例级排空与安全关闭（5.5）

**Files**
- 新增 `lightrag/distributed/release/lifecycle.py`、`lightrag/api/release_lifecycle.py`（纯 ASGI 计数/收尾，不使用会缓存流正文的 middleware）。
- 接入 `lightrag/distributed/runtime.py::finalization_guard`、`lightrag/api/lightrag_server.py::lifespan`、既有 managed background task drain。
- 新增 `tests/distributed/release/test_lifecycle.py`、`tests/api/test_release_lifecycle.py`。

**Interfaces**
- `InstanceLifecycle.begin_retire() -> None`：持久化停止本实例新执行/领取，readiness 退出入口；不取消已准入操作，不全局 pause。
- `snapshot() -> InstanceSnapshot`：HTTP/流/结果等待连接、writer ticket、后台任务、后端请求数量及错误状态；来自真实注册/收尾而非操作者传入零值。
- `await drain() -> None`：只在本实例所有计数和协调证据确认排空后完成；未知状态拒绝完成，等待者取消不取消正在执行的业务写。
- release-enabled 的 finalization 只关闭本实例资源，不取得阻断 peer 的 workspace 独占；原默认分支保留。pool/witness 最后关闭；有活跃 permit 时不得先关连接。

**Red/green checks**
- A 退出时 B 长查询持续成功；A 自身流/排队结果连接未完不得关闭；取消 drain waiter 后可继续等待；standby 与 active shutdown 均覆盖。
- `./scripts/test.sh tests/distributed/release/test_lifecycle.py tests/api/test_release_lifecycle.py tests/distributed/test_runtime.py tests/distributed/test_sdk_runtime.py -q`。

### F. prod overlay、独立权限与薄流水线（6.1–6.6）

**Files**
- 新增 `.woodpecker/deploy-prod.yml`、`scripts/ci/deploy-prod.sh`、`scripts/ci/prod_deploy.py`，以及 prod overlay 内的 stable entrypoint、slot Deployment 模板、profile、RBAC、NetworkPolicy、资源/存储配置。
- 仅增量修改 `scripts/ci/delivery-check.sh`；原四个 workflow、`deploy-test.sh`、`k8s_deploy.py` 的 test 编排和 base/test overlay 不修改。
- 新增 `tests/ci/test_prod_{workflows,deploy,kustomize}.py`，对原有 83 项保留完整回归。

**Interfaces**
- `select_prod_release(*, event: str, tag: str, activated: bool) -> bool`：只接受已启用的正式 `vX.Y.Z` tag。workflow 本身默认静态禁用，脚本再校验接入策略；不靠尚未获准的 CI 全局配置、runtime Secret 变量或仅 shell 中判断来阻止非 prod 事件注入生产凭据。
- workflow 沿用 `depends_on: [pre-deploy]`、`skip_clone: true`、相同内部工具镜像、COS 按 commit/pipeline 下载并校验源码、自行 `resolve-image`。只在获准正式 tag 的部署步骤注入 `lightrag_prod_*` 和独立 prod kubeconfig；不继承 `kubeconfig_test`/test key/S3 别名。
- 薄 shell 只做必要环境与临时凭据文件权限设置、调用 Python 编排、清理本地敏感临时文件；不在 trap 自动回退/清锁，不运行 pytest/Bun 或 `bootstrap/migrate/recover`。
- `render_slot(config: ProdConfig, identity: ReleaseIdentity, slot: Slot) -> RenderedResources`：为该 slot 生成 digest 和配置版本固定的资源；不可覆盖 Blue 的 Secret/ConfigMap、selector 或 image。复用 base 的安全结构时显式删除/替换所有 test 默认项；对另一 slot 和稳定入口的变更分开提交。
- 稳定 Service 的 selector 只含共同 identity + 当前 slot，无暂停全路由标签；两个 Deployment selector 不重叠，首次 4 Pod/升级额外 4 Pod、`WORKERS=1`、`/ready`、TLS/流式代理、节点分散、显式生产存储/资源。无法核验 8 Pod/PG+coordination+witness/HugeGraph/模型额度预算则不推进。
- `validate_target(kube, config) -> TargetEvidence`：先核验集群/namespace 身份、最小 RBAC、实际数据域/ACL 与版本引用；不执行生产写探针验证隔离，缺少可信接入证据则拒绝。

**Red/green checks**
- 未启用/所有非生产事件不选择 prod；产物错源/旧包/错 digest、跨 workflow 文件缺失、目标串 test 在 mutation 前拒绝。
- 两 slot 渲染 4+4、worker=1、无 test 域名/作用域/存储类残留；Blue 引用不变；test render 与基线逐资源相同；禁止缩容 Blue 腾资源。
- `./scripts/test.sh tests/ci -q`；`woodpecker-cli lint --strict .woodpecker/*.yml`；`bash -n scripts/ci/deploy-prod.sh`、`sh -n scripts/ci/deploy-prod.sh`；prod/test render 使用本地 fixture 参数，不连接集群。新增模板不允许把缺失真实参数默认成 test 值。

### G. 发布编排、证据与条件中止/回退（7.1–7.4）

**Files**
- 完成 `scripts/ci/prod_deploy.py`，新增 `lightrag/distributed/release/orchestrator.py`、`evidence.py`；新增 `tests/distributed/release/test_{orchestrator,evidence}.py` 与 `tests/ci/test_prod_cutover.py`。

**Interfaces**
- `ReleaseOrchestrator.advance(identity: ReleaseIdentity) -> ReleaseSnapshot`：严格 `PREPARED → DRAINING → READ_SWITCHED → WRITER_SWITCHED → RETIRING → COMPLETED`，每次读取实际状态并核验 owner/epoch/digest/config；同发布恢复只能依据持久化阶段，不靠本地临时文件。
- `collect_drain_evidence(scope, *, old_slot) -> DrainEvidence`：从活跃 ticket、原协调 active/claim/lock/pending、所有参与者与后端在途注册采集有界证据；任一读错误/未知实例/不确定 mutation 均拒绝。只读长查询不计作 writer，但它的缓存写 ticket 必须纳入。
- `switch_read_route(target: Slot, *, expected_selector) -> RouteEvidence`：条件 patch 稳定 Service，校验 EndpointSlice 中逐 Pod 身份与真实入口的候选版本响应；传播期两个 slot 都保留。切流副作用发生而阶段 ACK 未写时，重入先核实路由再推进，不盲目重复操作。
- `abort_before_transfer(identity) -> ReleaseSnapshot`：仅证明旧 owner 确定、无不确定写后，确认 Blue 路由及执行权，安全开闸释放队列；不强杀长任务。
- `rollback_after_transfer(identity, *, approved_compatibility) -> ReleaseSnapshot`：显式操作，重新关闸/排空/验证兼容并完成反向所有权交接。没有证明就停止并保留 owner，不自动 undo/recover/恢复单边备份。
- `EvidenceReport` 记录 tag/commit/digest/config_hash/epoch/阶段及脱敏状态，关联连续请求与命令终态；它是执行观测输出，不是可手工伪造并输入的“完成凭证”。

**Red/green checks**
- 并发 runner、每个阶段前后 CI 中断/重入、关闸超预算、只剩查询的排空、部分 Green 失败、EndpointSlice 迟延、切流已生效但 ACK 丢失、迟到旧写、UNCERTAIN、终态丢失均覆盖。
- `./scripts/test.sh tests/distributed/release tests/ci/test_prod_cutover.py -q`；fake client 记录调用序列，断言任何失败不调用 `recover`/`rollout undo`/全量缩容/删除 owner。

### H. 验证、合同、接入与发布启用（8.1–9.4）

**Files**
- 同步 `docs/design/{DistributedRuntimeContract,DistributedPipelineContract,PipelineConcurrencyContract}.md` 和必要缓存/退出合同；新增 `docs/ProdDelivery.md`。
- 在本 change 的 `verification/` 保存本地验证、scenario 对照、获准隔离演练和容量报告；保持调查/恢复历史记录不被覆盖。

**验证与启用顺序**
1. 执行以上全部实际改动目录的定向测试；公共 `utils.py` 改动在里程碑运行 `./scripts/test.sh tests -q` 并分别记录 integration skip。仅修改前端时运行完整 Bun/tsc/lint，不为本任务新增前端改动。
2. strict lint、Python/shell 静态检查、双环境 render 与交付基线 hash/diff；原四 workflow/test 脚本/overlay 不变。按两份 spec 的全部 scenario 列证据，`openspec validate add-prod-zero-downtime-delivery --strict`；静态通过不勾选远端演练。
3. 另行授权后使用独立测试资源持续普通/流式查询，跨各阶段提交全部登记写操作并注入故障；记录断连、错误、首包/总延迟、命令执行与最终存储事实。不得重跑此前 test Pod 恢复脚本代替演练。
4. 单 Pod、4 Pod、8 Pod 阶梯/持续/读写混合容量测试，对照提前批准的负载、SLO 与有限排队预算；核算全部调用方的 PG/协调/witness、HugeGraph、模型及 S3 预算，未达标明确报告，不自动扩容或 HPA。
5. 单独获得 prod 接入授权才允许实际导入 Secret、核验目标权限/数据域、显式初始化或接入既有全部 writer；未满足的 onboarding 字段、代理超时、容量证据、协议基线保持硬阻断。
6. 单独获得生产启用/发布及 Git 发布授权后，才解除 workflow 静态禁用并走正式 tag；保存首次生产真实证据，不自动归档。完成本地实现不等于 9.2–9.4 已完成。

## 2026-10-23 本地动态usage/混合停控子切片（不勾真实验收）

本轮补齐10.2/10.7相关的4→8→4逐UID/epoch计数、旧Blue关闭后的最终usage、证据落盘gate、共享异常停发及已发写/长流自然收尾。执行计划升级v2并显式要求五项usage预算，无默认；参数v5仅新增五项null，当前196个未知叶子，原191项未知与12类false批准不变。详见[本轮独立报告](verification/2026-10-23-dynamic-usage-local.md)及[工具合同](../../../scripts/performance/rehearsal.md#动态usage与停控合同v2)。

本地Mock/原生工具串联不代表真实Pod/CLB/容量或SLO通过；10.2整项、8.3/8.6/10.8及其余真实任务保持未完成，仍41/53、12项未完、57场景。两个候选现场和旧overlay保留，原test实例/数据/流水线未动，真实复测另行确认；无重启/recover/重放、prod/密钥导入或Git发布。

## 2026-10-23 prod只读预检（用户单独授权，不部署）

按用户新授权实际读取prod集群、共享qcloud入口、工作负载/资源及现有PG/图/S3连接与权限证据。10节点Ready但无可识别LightRAG基线；PG/图私网从本机不可达、CLB DescribeListeners UnauthorizedOperation、S3 HEAD/List通过但ACL/prefix/写权限未验，当前RWX模板无已核验工作卷。requests余量仅资源快照，不是4+4或1000QPS验收。见[实际预检与阻断顺序](verification/2026-10-23-prod-readonly-preflight.md)。9.2/9.3等不勾，仍41/53、12待办、57场景；未知参数/正式表单授权不自动改。原test/两个候选未访问，不部署/切流/发压/Secret导入/Git发布。

后续按用户纠正将内网验证观察点限定目标TKE（S3同样适用），区分Ingress管理入口与当前发布器的CLB只读观测依赖；撤回把模板RWX选择当业务必需的解释。仅检查一个已有Pod的工具及两个Ingress状态，未向后端发请求，内网认证校验仍未完成，不新建诊断Pod/基础组件、不改发布门禁或存储模板。见[观察点和volume纠正](verification/2026-10-23-prod-incluster-observation-correction.md)，任务/未知参数状态不变。

用户随后单独授权一个临时诊断Pod及结束后删除；仅完成目标/namespace和所选镜像可拉取性预检，标准匿名鉴权后manifest/token阶段最终401，按异常要求停止。未创建Pod、未读取后端凭据、PG/图/S3内网请求0，无待清理资源；需要可拉取诊断镜像或既有拉取Secret引用，不导入密钥、不把镜像鉴权归因后端故障。见[停止报告](verification/2026-10-23-prod-diagnostic-preflight-stopped.md)，真实内网及原12待办未通过。

用户明确要求自行解决镜像后，已自行复用`prod/lfun`引用创建唯一诊断Pod；最终事件证明镜像约161秒成功拉取/启动，但本地预设150秒启动截止点过短，已触发正常清理并验证删除。不是镜像鉴权或后端异常，不再要求用户提供镜像；未读取/传入后端凭据，PG/图/S3请求0，依异常即停要求未创建第二个Pod。原27Pod/3工作负载身份及spec前后一致，见[本轮真实事件与执行问题](verification/2026-10-23-prod-diagnostic-startup-timeout.md)；内网验收仍未完成，不勾任务。

再次获准延长等待后，沿用`prod/lfun`创建新诊断Pod，600秒启动窗口/900秒有效期，固定digest实际启动并验证asyncpg/boto3/httpx。PG私网解析成功，但1次连接目标拒绝TLS升级；未建立认证会话、SELECT0，依异常即停未请求图/S3，未明文降级或重试。Pod已正常删除并再次确认缺席，原27Pod/3工作负载UID+spec一致；见[实际内网PG结果与清理](verification/2026-10-23-prod-incluster-pg-tls-stop.md)。不把探针TLS策略自行升级为上线硬要求，仍需确认PG传输约定；三组件内网验收未完成，任务/参数不勾不改。

用户随后仅对目标TKE内本次探针授权PG非TLS。新诊断Pod成功启动，1次`ssl=False`连接返回InvalidPasswordError/28P01；SELECT0，依异常即停未请求图/S3，不重试、不换账户、不改PG配置。Pod已正常删除并再次核验缺席，原27Pod/3工作负载UID+spec一致；见[非TLS认证结果](verification/2026-10-23-prod-incluster-pg-auth-stop.md)。需核对prod清单与PG目标凭据是否匹配，不读test或在聊天索取密码；三组件验收、任务/正式参数状态不变。

用户更新密码并授权重试后，新进程读取当前prod清单、沿用相同非TLS/TKE限定：9条PG只读元数据SELECT、3次HugeGraph版本/schema/常量查询、2次S3 HEAD/List全部完成。PG17.6/pgvector0.8.0、图1.7.0、S3 HTTPS200且请求域名解析私网；PG协调/发布schema及七项图定义尚未出现，runtime最小权限、S3 GET/写删/ACL未验，不签署初始化/应用/4+4/容量。临时Pod正常删除并二次确认缺席，原27Pod/3工作负载UID+spec一致，原test和候选未访问。见[本轮完整只读结果](verification/2026-10-23-prod-incluster-readonly-completed.md)；9.2仅有限子步骤，不勾整项，41/53与12待办不变。

## 11. 首次初始化migration补全（本地实施获准，真实执行另批）

此前41/53与12项未完成保持历史快照；本节新增6项后当前总任务59项。用户“继续补齐”仅批准代码/本地验证，不扩展到生产写、Secret导入、开启流水线或Git发布。唯一实现计划仍为本OpenSpec。

- [x] 11.1 冻结业务/向量和图v1契约，新增纯离线plan；绑定physical profile/模型/维度/索引参数，拒绝危险标识符、越界维度、无版本旧表及不支持索引；先观察对应RED。
- [x] 11.2 实现独立PG版本账本、事务迁移、并发锁、checksum/结构漂移校验和最小runtime授权；调用既有coordination/release迁移，不改原SQL/checksum或历史业务数据。
- [x] 11.3 实现图schema逐项版本化执行与PG意图日志；覆盖POST未知ACK、GET确认/漂移/缺失、索引CREATED等待，拒绝自动删除和未知重放，不假设跨存储事务。
- [x] 11.4 补独立plan/apply/verify/register CLI及离线初始化Job渲染，校验目标/身份/确认/基线；不构造应用/调用模型、不生成Secret、不修改普通prod/test流水线或自动enroll/recover。
- [x] 11.5 将迁移验证接入真实prod runtime，生产普通启动及maintenance不创建扩展/表/索引/图schema；release关闭时维持现有test/local路径，补真实调用链回归。
- [x] 11.6 完成本轮定向测试、隔离本地PG及HTTP边界验证、打包/静态/合同审查与OpenSpec同步；记录真实未验项，未知参数保留、不把本地结果当生产放行。


第11节本地实施完成：当前 **47/59**，仍有原 **12项真实任务未完成**；62场景已列证据。
[本轮验证](verification/2026-10-23-storage-provisioning-local.md)记录1273 passed/166 skipped的定向组及211 passed的独立本地PG联合组（两组有重叠，不相加）；[唯一审查及4项修复](verification/2026-10-23-storage-provisioning-review.md)。
原test基线和prod静态关闭门禁不变，未执行真实迁移/登记/enroll或环境操作；未知参数不填批准值。新增本地迁移工具不等于9.2–9.4/容量已通过。

## 2026-10-23 首发优先级与 Git 发布授权

用户将工作优先级改为尽快首次上线 prod，并明确允许本次生产必需改动的提交、推送和正式生产 tag。执行者暂停 test 只读预检；暂停时原五套 test 和两个候选的身份/spec/配置引用一致，诊断 Pod 创建、后端请求及变更均为0。prod 仅复核目标身份和指定 namespace 的三个现有工作负载，未发现该 namespace 的 LightRAG Deployment；这不是整个集群或外部 writer 为空的证明。

完整本地后端里程碑 **9661 passed / 384 skipped / 2 warnings / 0 failed**；原 test 交付基线未变。首次失败的本机 Cairo 搜索路径和 scratch dotenv 夹具隔离已明确记录，未改业务超时或安装依赖。干净源码 wheel/sdist 中65项发布源码/迁移资源逐字节一致；详见[首发准备快照与阻断](verification/2026-10-23-first-release-preparation.md)。

Git 发布与生产放行分开：runtime/operator 引用、生产域名/TLS、显式迁移/登记/enroll、S3 权限及工作卷/预算、CLB 和真实发布/性能证据仍需补齐。未打 tag、未解除 gate，也未以“尽快上线”默认为同意降低既定 SLO 或免除上线前置。当前 **47/59，12项未完成** 不变，不勾9.2–9.4，不覆盖原现场和历史验收记录。

## 2026-10-23 只读引用核对与禁止应用 K8s TLS

用户要求执行者自行从prod清单和既有资源核对账号、域名、TLS引用，随后明确禁止在K8s中做应用TLS。已经核对目标身份、全局Deployment/StatefulSet及21份Ingress、6份TkeServiceConfig、208项Secret服务端Table元数据，并仅解析所需公开证书；无生产写、Pod/Job创建、exec/port-forward、PG/图/S3请求或密钥导入，原test/两个候选未访问。原三个prod工作负载、21份Ingress及6份ServiceConfig的UID/spec/路由引用前后一致。

可复用候选为prod清单已有PG/图连接、该文件内唯一历史名称API key别名、`prod/lfun`拉取引用及既有CLB；不能把其他应用凭据或APISIX客户端证书当成LightRAG运行权限/公网服务端证书。公网`*.eplus2.cn`证书有效，但CLB只读调用被当前COS用途身份拒绝，真实云侧绑定未证实；正式LightRAG域名未配置。最小补充集中在独立runtime/运维身份分离、正式域名/DNS、既有云侧证书/入口引用及CLB只读观测权限，**不需要K8s TLS Secret**。详见[本轮核对和最小补充清单](verification/2026-10-23-prod-reference-audit.md)。

本地已将该硬约束接入6.4：移除无实际用途的`target.tls_secret`和schema字段、拒绝旧字段及应用SSL/证书路径、Pod显式`SSL=false`、启动profile再次拒绝TLS；外部云证书/HTTPS/direct及流式门禁不放宽。TDD初始10 failed/1 passed后11 passed，新增外部HTTPS负例及相关目录最终数见报告。仅本地改动，未推送、打tag、触发流水线或应用到prod；47/59与原12项真实未完成不变，场景从62增至63，不把禁止K8s TLS解释成取消外部HTTPS验收。

## 2026-10-23 最高规则与正式流水线授权

用户再次明确要求将禁止应用K8s TLS写入最高规则，然后跑流水线。已将约束置于本仓库根`AGENTS.md`最前部署规则；不改仓库边界外的全局规则。按既有Git授权交付本轮必要代码/规则/验收记录，再选择未占用正式semver触发既有构建和产物校验链；**不推test tag、不修改test发布，prod静态gate继续false**。这不是对已有账号/域名/云入口阻断的豁免，不将跳过prod部署的流水线报告成已上线，不勾9.2–9.4。执行前核对见[本轮流水线准备快照](verification/2026-10-23-formal-pipeline-preparation.md)。

## 2026-10-23 EdgeOne浏览器配置及阿里云DNS

用户对准确域名、HTTP80回源、云侧免费证书、五项缓存/流式规则及阿里云单条CNAME分别明确批准。实际已完成域名已生效/证书已部署、精确HOST专属规则启用及DNS权威/递归核验；未改全站/参考域名、启用付费项或创建K8s TLS。实现侧补显式EO分支、prod源标签/工作目录及配置类型回归；定向460 passed/60 skipped/2既有warning。见[实际云配置及本地验证](verification/2026-10-23-edgeone-browser-delivery.md)。

当前仍47/59、12项真实待办，capability共66场景。角色/观测身份、首次初始化及首发流水线串接、资源/ACL与真实端到端/SLO门禁未满足；EO配置不等于应用部署，不勾9.2–9.4，不以浏览器截图代替CI API证据，不再打只构建而不部署的tag。原test和两个候选保持不变。

## 12. 首次上线专用账号共用例外（用户明确批准，2026-10-23）

- [x] 12.1 增加默认拒绝的版本化例外，绑定批准ID/scope/profile/正式tag/PG数据库及PG图用户；配置/schema/环境和迁移计划纳入hash，测试缺项/漂移/跨环境。
- [x] 12.2 初始化CLI允许显式准确共用身份，保留独立凭据输入和额外执行确认；检查真实PG无超级/CREATEROLE等权限，不创建角色、不改旧迁移SQL/checksum。
- [x] 12.3 贯通migration/verify/register及只读runtime，保留schema/profile/CRUD/注册验证；实例只允许首个Blue，持久release policy绑定例外，test默认不变。
- [x] 12.4 生产render/产物入口核对准确tag，普通发布/Green/回退在副作用前拒绝例外，不把首次授权延伸到后续版本。
- [x] 12.5 运行定向回归和隔离本地PG权限/完整迁移链验证，补默认拒绝/错误身份/漂移/首次例外正例；运行静态/打包/OpenSpec核验与新鲜审查。
- [x] 12.6 同步运行手册和实际证据，区分专用性与权限分离风险、准确记录尚未执行的prod步骤；不因本地完成勾选原真实上线/性能任务。


第12节本地实施完成：仅豁免已批准首发的运行/迁移身份分离，不再索取CREATEROLE或额外运行角色。当前 **53/65**，仍有原 **12项真实任务未完成**、68场景。定向646 passed/144 skipped、独立本地PG联合240 passed（两组重叠、不相加）；唯一审查3 Important均经21项RED→GREEN和一次fix pass修复。20文件Ruff/format、干净wheel/sdist各17项源码/资源、OpenSpec与原test/SQL基线核验通过，见[本轮验证](verification/2026-10-23-first-shared-credentials-local.md)及[审查修复](verification/2026-10-23-first-shared-credentials-review.md)。

本轮未操作prod/原test/两个候选，未导入密钥或创建tag。首发初始化与4×1 Blue的真实流水线串接仍是实现侧待办，合格EO/CLB只读引用/审阅hash及S3/入口/资源/真实验收仍需完成；不把账户例外或本地通过当上线，不勾原9.2–9.4/性能任务，prod gate保持false。

## 13. 首次 K8s 部署串接（用户要求不做 CLB/EdgeOne 校验）

- [x] 13.1 同步K8s-only部署边界、配置/schema/HTTP Ingress与S3有界scratch；保留旧test/原模式，不伪造云侧/ACL/性能已验收。
- [x] 13.2 复用可信源码/digest校验，增加显式initial settings和固定digest初始化Job；额外执行确认由本次输入提供，operator凭据不进入app。
- [x] 13.3 串接已有migration apply→verify→register→enroll，准确绑定scope/profile/计划/tag/目标；未知结果即停，不自动recover/重放/rollback。
- [x] 13.4 CI依次部署初始化Job、Blue4×1与HTTP Service/Ingress，确认K8s Pod就绪；既有应用拒绝替换，Job观察超时保留现场，不调用CLB/EO或公网入口。
- [x] 13.5 补对应反例/正例、真实隔离PG联合、原test非回归、静态/包/OpenSpec检查及一次新鲜审查；区分本地与真实结果。
- [ ] 13.6 按已获授权准备仅prod输入、必要commit/push和真实首发流水线；仅通过本次K8s边界后报告部署，失败立即停并保存实际证据，不认领外部入口/容量验收。

第13节本地13.1–13.5完成：**58/71，13项剩余**（原12项真实验收及13.6真实部署），capability共71场景。见[本轮验证](verification/2026-10-23-kubernetes-initial-local.md)及[唯一审查修复](verification/2026-10-23-kubernetes-initial-review.md)。本轮已按授权准备23项prod-only CI输入，实际commit/tag/流水线/Pod结果另列，不将本地模拟或镜像构建当部署。
