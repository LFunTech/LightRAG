# 生产蓝绿发布手册

## 当前边界

- 在原 test 的唯一源码归档、COS 传递、Kaniko 构建及平台/revision/digest 校验链上新增生产部署；不复制第二套构建。
- 原四个 workflow、test 脚本、五个 workspace、base/test overlay 和 Secret 映射保持原样。
- `.woodpecker/deploy-prod.yml` 仍以常量 `evaluate: 'false'` 静态禁用。解除此 gate 需要单独生产启用授权，不能删除正式 tag 限制：workflow 排除 `-test`/`-pre`，接收 prod 凭据的 step 另有独立严格 `vX.Y.Z` 条件。脚本还校验事件、仓库、tag、commit、归档记录、digest 和 enabled onboarding。
- **禁止应用侧 Kubernetes TLS**：TLS 终止与证书管理放在云侧 EdgeOne/CLB；K8s Service/Pod 仅 HTTP，不创建/复制 TLS Secret、不挂载证书或私钥、不在应用 Pod/Ingress 控制器开启 TLS。不再需要 `target.tls_secret`，旧字段直接拒绝；Pod 显式 `SSL=false`，渲染及启动均拒绝应用 SSL/证书路径。Ingress 中的云侧证书 ID 仅指示外部 CLB，不包含证书材料；云侧 HTTPS/入口核验仍是门禁。其他业务和控制面 TLS 不变。
- 本地代码/测试通过不代表 Tencent CLB、跨节点或高并发验收通过。2026-10-23 用户另行授权本次生产必需改动的提交、推送和正式生产 tag；该 Git 授权不替代运行账号隔离、初始化、入口及实际验收。最新准备快照见[首次发布准备记录](../openspec/changes/add-prod-zero-downtime-delivery/verification/2026-10-23-first-release-preparation.md)，生产静态 gate 仍关闭，尚无应用上线或切流完成证据。

## 架构与容量

生产稳态 **4 Pod × 1 worker**；蓝绿为 **Blue 4 + Green 4 = 8 个应用 Pod**。
维护/control Job、数据库、故障替换实例的容量另计，不能缩 Blue 给 Green 腾资源。

生产为腾讯云 **qcloud Ingress、HTTPS 七层入口、CLB 直连 Pod、HTTP 后端**。
当前已验证的应用 profile 为 PostgreSQL/pgvector + HugeGraph + S3、OpenAI-compatible
LLM/embedding、native parser，不自动继承 test 的模型、域名、存储类、连接池或 NGINX 注解。
未列入接入 profile 的 provider/reranker 要先补连接/并发预算与验证，不能靠默认值启用。

- 每个 Pod 的 PG 总连接包括业务池、协调池和独立 witness；目前固定协调池 4 + witness 1。
- `other_pg_connections` 至少计入 control/operator 与只读连接验证，还须包含所有非 Pod 调用者。
- HugeGraph 和 LLM/embedding 并发按全部 8 Pod 聚合，不能把单 Pod 限额当全局额度。
- query/keyword/extract/VLM 各有独立模型队列，当前支持 profile 的模型上限至少 `4×MAX_ASYNC+EMBEDDING_FUNC_MAX_ASYNC`/Pod，再按8 Pod及其他调用者核算；并发上限不等于提供商 RPM/TPM。
- 使用显式 RWX StorageClass 与节点分散约束。Green 全部可用前不关写闸门。
- 无自动 HPA；4 Pod 只是待压测基线，不是“已经能承受高并发”的结论。

### 上线前容量报告

经独立授权，在隔离资源完成单 Pod、4 Pod、8 Pod 的阶梯、持续、读写混合压测，
包含冷/热缓存、普通/流式查询及全部已登记写入口。报告至少记录目标 QPS/并发、
TTFT/总延迟 P95/P99、错误/超时、排队长度/等待、CPU/内存、模型限额/等待、
业务 PG/协调池/witness、HugeGraph 和 S3 指标，并给出提前批准的 SLO。
之后才能评审稳态 N、发布 2N、扩容上限/触发/回落策略，保持每 Pod 一个 worker；
扩缩容仍须复测实例 drain、owner 和路由，不修改现有 test 实例。

### 缓存与性能准备（P0本地实现，真实容量未验）

详细方案见 [OpenSpec design 第 10 节](../openspec/changes/add-prod-zero-downtime-delivery/design.md)，
待办见 [tasks 第 10 节](../openspec/changes/add-prod-zero-downtime-delivery/tasks.md)，工具见 [性能说明](../scripts/performance/README.md)。
本轮增加默认关闭的真实链路观测、显式安全缓存策略和离线/隔离负载工具；10.7另补[混合写、4+4联动与字面质量rubric](../scripts/performance/rehearsal.md)，默认离线，真实执行仍需独立授权。没有新增 Redis、改 test 或运行真实负载。
已确认目标为 **1000 QPS 完整RAG（含生成）/约10000在线用户**，七项延迟/技术成功率目标已登记，流式/多轮及 token 规划基线见 OpenSpec 验收参数；在途数、P99、数据规模、模型额度和正式验收窗口仍缺，不能宣称4 Pod达标。

- 当前支持 PG 中的关键词、非流式答案和文档抽取缓存；答案命中发生在检索上下文构建之后，
  主要省最终生成，不代表跳过 PG/HugeGraph。流式新生成不回填完整答案，多轮历史请求旁路
  答案缓存；不能把热缓存非流式吞吐套给这些请求。Embedding 字段不等于已接线的缓存能力。
- 接入时核对实际缓存开关，不根据 API 默认 true 或示例 false 推断线上状态；抽取缓存还承担
  重建/恢复归因，不能为压测清空。新策略需显式 prod 配置/版本化，不继承 test 环境值。
- **P0**：先批准工作负载、数据规模、QPS/并发、P95/P99/TTFT/错误/等待 SLO，补分阶段计时、
  cache hit/miss/bypass/skip/error、真实模型调用/token、连接池/锁/额度与长流占用；指标不含正文/凭据。
- **P1**：按证据优化向量/图/chunk 查询和批量往返、质量约束内的 Top-K/token、查询/入库角色
  并发与有限队列，核算 4/8 Pod 及其他调用者的共享额度，不把优先级当硬隔离。
- **P2**：只在重复成本证明收益后评审 Embedding 精确缓存、有界 L1/已有 PG 层和热点
  singleflight；先验证权限/知识/模型版本失效、取消、必要计量及 release ticket。
  不默认广播未完成流、不把 TTL 当作数据/权限正确性证明。Redis 或读写拆分必须另案批准。
- 完成冷/热、普通/流式/多轮、热点突发、读写混合与 4+4 交接的对照矩阵，才能作容量判断。
  没有目标/SLO 或任一正确性/额度/等待预算不达标，明确报告未通过，不通过加 Pod 或单看命中率放行。

### 显式 prod 策略与观测边界

生产 runtime 必填 `LIGHTRAG_QUERY_METRICS=true`、`ENABLE_QUERY_ANSWER_CACHE=false`、
`ENABLE_LLM_CACHE_FOR_EXTRACT=true` 以及显式 `ENABLE_LLM_CACHE`（关键词）。严格布尔进入版本化配置/hash，
未证知识/权限失效前拒绝开启完整答案缓存，代价是必须按冷态模型额度估算。test/local 默认不变。
有凭据的既有 `/health` 才返回 `query_performance`，逐 Pod 直测区间差值，不通过 Service 随机抽样。
观测包含 SDK/ASGI总耗时、关键词/向量/图/chunk/context、模型排队/流首输出、cache行为、读池/协调调用和进程CPU/峰值RSS；
固定桶和标签、不保存输入/输出。query_total为operation_guard内真实owner业务耗时，http_total包含外层等待，两者不相加；后台模型调用单列process。
OpenAI-compatible调用按SDK dispatch，默认内部重试为0；若显式改内部重试需另核隐藏HTTP次数。
未返回usage标未知而非0 Token；不等于账单。全账户额度/后端锁等待/CLB/节点指标须与外部遥测联合核对。
指标不是写权、readiness或排空权威；不改变release/fence/ACK/流关闭边界。进程重启epoch变更后必须分段采集。

## 接入配置与密钥

真实 `onboarding.json` 不随源码提供，也没有可误应用的生产占位默认值。
字段结构见 `k8s-deploy/lightrag-kustomize/overlays/prod/onboarding.schema.json`；
最终校验器是 `lightrag_delivery.config.load_prod_config`，还验证有限预算不等式、
全部连接额度、明确模型/prefix、目标隔离和 HTTPS/direct 合同。

必须确认并登记：

1. 仓库、Kubernetes API endpoint SHA256、kube-system/目标 namespace UID、独立 kubeconfig；
2. workspace/deployment、PG database/运行角色、HugeGraph graphspace/graph、S3 bucket/prefix 的实际 ACL；
3. 生产域名、云侧 CLB/listener/location/certificate 引用、实际 TLS 终止/回源边界、控制器版本、直连能力、无强制断流策略及流式演练证据；不索要 Kubernetes TLS Secret；
4. 已初始化的业务、coordination、release schema 与物理 profile/协议 manifest；
5. 全部 API/SDK/后台 writer、命令格式及新旧读写兼容批准；
6. 应用/控制 Job 的 ServiceAccount、registry pull Secret、独立 operator Secret 和受限网络出口；
7. 8 Pod 的资源/连接/模型预算及所有 client/proxy 超时链。

`isolation.acl_verified`/evidence_id 是接入审查的受控记录，不是“名字不同即隔离”的证明。
实际 PG/图/S3 ACL、外部写入者和入口配置必须由授权人员只读核查，缺证据不得启用。
日常发布另外核对当前 cluster/namespace、协议 hash、运行数据库角色的 release 控制权限、
各 Pod 的身份/状态和 CLB 实际路由；不通过生产业务写探针“验证隔离”。

### 环境清单

- `.secrets/prod.secrets` 只解析 prod，`.secrets/test.secrets` 只解析 test，绝不 source/执行。
- 历史 `lightrag_test_*` 字段只在显式 API-key alias 选择时映射 prod 文件里的值，不读取 test。
- `scripts/ci/import-environment-secrets.py` 默认仅输出脱敏计划，真正导入需要 `--apply`、明确目标 CI/repo 和允许的 image；本次没有执行导入。
- 生产运行 CI 名称为 `lightrag_prod_*`，生产 kubeconfig 为 `lightrag_prod_kubeconfig`；不使用 `kubeconfig_test`。
- COS 源码归档服务仍复用原共享构建凭据，它不等于应用 S3 凭据，也不等于 CLB 观测授权。
- control Job 使用独立 K8s operator Secret，含 `LIGHTRAG_RELEASE_OPERATOR_DSN` 和受限 CLB **只读**凭据 `LIGHTRAG_PROD_CLB_SECRET_ID/KEY`（临时凭据可带 TOKEN）。不能假定现有 COS key 有此权限。
- 应用 Pod 不接收 operator DSN、CLB key 或 Kubernetes API token。运行数据库角色不得更新/拥有 release 控制表，不得是超级角色或继承特权角色。
- API-key-only profile 的 JWT secret 以有域分隔的 API key 派生，使实例一致，不打印派生输入；账号/必要计量接入必须另行核验，不能混入未批准的凭据轮换。

### 不可变配置与物理指纹

运行 Secret 按配置/环境 hash 命名，发布身份 ConfigMap 按完整 identity hash 命名并包含
tag/commit/digest；同一 slot 复用同配置发布新版本不会碰撞不可变 ConfigMap。
准备 Green 不能覆盖 Blue 的引用。物理 profile 另外绑定 PG/图域、embedding binding/endpoint/
model/dim/asymmetric/两侧 prefix 以及 S3 bucket/prefix。空 embedding prefix 使用原解析器的
`NO_PREFIX` 显式哨兵，指纹统一规范化；API args 与部署的 embedding profile 不一致就拒绝启动。

离线渲染使用 `python -m lightrag_delivery --help`。只写新的私有目录，可由 Kustomize
解析；输出可能包含 Secret，不能提交或打印到公共日志。prod 目录不是带 test 默认值的
可直接 apply overlay。正常升级由已核验镜像内的 control Job 生成 inactive slot 资源。

## 首次/已有环境接入（均需另授权）

1. 保存备份和真实 ACL、全部 writer、入口/超时/容量证据，准备受限 CI 与 K8s 权限。
   离线 foundation 包含 namespace 内 Role/Binding，以及仅允许 `get` 目标 namespace 和
   `kube-system` 两个 Namespace 对象的 ClusterRole/Binding，用于 UID 核验。它们由首次接入
   身份审查/安装，普通发布不会创建或扩大 cluster RBAC，应用身份不获得这些权限。
2. 空目标先使用下述**独立版本化初始化**，依次完成 `plan → apply → verify → register`；
   不启动应用来补建表，不要求停止一个不存在的 Blue。已有无版本业务表/未知 writer 的环境，
   必须先完成独立审计/历史迁移，工具拒绝自动 adoption；不能为绕过检查清库或改账本。
3. `apply` 在一个 PG 事务内准备业务/向量、原 coordination、release 结构和 runtime 最小授权，
   随后逐项准备图 schema；原 coordination migration SQL/checksum 保持不变。
   **结构迁移、workspace 物理 manifest 登记、release enroll 是三个独立步骤**。
4. `python -m lightrag.distributed.release enroll --confirm-maintenance --config <approved.json> --environment <private-runtime.json> --active-slot blue`
   只允许匹配且 quiescent 的已登记协议基线，不初始化业务数据、不改旧 manifest、不 recover。
5. 独立应用身份启动 Blue 4×1，逐 Pod 验证 `/ready`、`/release/status`、协调与 polling；
   建立 HTTPS/direct 入口并完成持续普通/长流查询的 CLB 演练。首次对外开放也保持四个 Pod。
6. 首次接入和真实 ACL、控制器/连接行为等未满足，普通流水线必须拒绝；不得手工清 `_broken`、
   篡改 schema checksum 或通过旧 test recover 脚本绕过。普通发布没有 operator DSN，也不执行 DDL。

### 独立版本化初始化（工具已实现，真实执行另行授权）

入口为 `python -m lightrag.provisioning`，不构造 LightRAG/模型客户端、不运行 pipeline。
冻结资源位于 `lightrag/provisioning/migrations/001_*`：9 个业务表、按模型/维度命名的
3 个向量表、7 项 HugeGraph 定义；本首版支持 HNSW / HNSW_HALFVEC，后者是明确选择，
不是自动降精度。以后结构变化新增版本，不修改已发布版本。初始化只复用既有 PG、图及 S3，
不创建数据库/角色/图实例/bucket，不迁移文档、不写 S3 对象或新增基础组件。

**运行前必须确认**：目标身份/备份与维护排他（包括外部 SDK writer）、原数据域不被触及、
runtime/operator 角色已预置并分离、PG/图迁移授权、S3 对象/ACL验收计划、固定镜像和 Job
预算。数据库 `CREATE` / `public CREATE` 或迁移账本写权限仍在 runtime 身上时，工具拒绝，
不会自动提升/撤回既有账号权限。角色创建、ACL 整改和真实初始化各自另批。

- `--environment <private-runtime.json>` 是批准的、受文件权限保护的运行环境 JSON，
  不 source `.secrets`，不混用 test。必须提供 physical profile 全字段、绝对 `WORKING_DIR`/
  `INPUT_DIR`、runtime `POSTGRES_USER` 和图身份；S3 继续使用既有配置。
- `plan` 完全离线，只输出非敏感摘要、`profile_hash`、`plan_hash` 和
  `baseline_manifest_hash`，不输出 DSN/密码。审核并固定这些 hash 后才进入下一阶段：

  ```sh
  PYTHON_DOTENV_DISABLED=1 python -m lightrag.provisioning plan --environment <private-runtime.json>
  ```

- `apply` 必须同时给 `--confirm-maintenance --confirm-empty-target`、
  `--expected-profile-hash <approved-hash> --expected-plan-hash <approved-hash>` 和
  `--graph-wait-seconds <approved-budget>`。显式初建或相同版本重复验证，不接管无账本旧表。
  PG 迁移身份只从 `LIGHTRAG_STORAGE_MIGRATION_DSN` 读取；DSN 必须显式写出且匹配目标 host/port/db（不得省略端口或借用 PGPORT）、
  使用不同于 runtime 的显式用户，并声明 `sslmode`。只接受明确 TLS 文件参数，不允许 query
  覆盖 host/user/db/port；不会因曾授权非 TLS 诊断而替应用或迁移默认降级。
  图独立身份只从 `LIGHTRAG_GRAPH_MIGRATION_USERNAME/PASSWORD` 读取，用户名不得与 runtime 相同。
- `verify` 给相同两个批准 hash 和图等待预算，PG 强制只读事务，图只 GET；使用环境文件中的
  runtime 图身份，验证版本/指纹/七项最终状态及 runtime 权限，不更正任何结构。
- `register` 需相同两个 hash、`--expected-manifest-hash <approved-baseline-hash>`、
  `--confirm-maintenance` 和图等待预算。先 GET/只读 traversal 确认图 scope 为空，
  再在短 PG 事务内复核空业务 workspace、无 active/pending/fence，登记精确 manifest。
  不 seed、不 enroll、不启动实例、不调用模型。跨存储无法原子断言，仍依赖外部维护排他。
- `apply` 的 PG 账本/表/授权失败整体回滚；PG 提交后图失败可以留下 PG 结构和部分图定义。
  每次图 POST 前，PG autocommit 持久化 `EXECUTING`；收到 ACK 也要等 task success/CREATED
  才确认。超时/取消/未知响应不得重发或删图；下一次**显式获准**运行仅可通过 GET 核实已存在、
  精确匹配且就绪的定义后结算，缺失/不兼容/任务失败必须停下交运维审计。
  不自动 rollback、重试写事务、recover、清锁或清意图；保留现场和备份，不提供 destructive down。

生产 release 启动在 coordinator 注册前以 runtime 身份只读检查账本/当前结构、权限及已登记
manifest；缺失或漂移拒绝启动。业务 PG 不 `CREATE EXTENSION`，maintenance 不绕过表/索引
门禁，图客户端固定 verify-only；release 关闭的 test/local 保留旧行为。PG 图账本只是迁移
证据，真实图结构仍由 verify-only 客户端核对，不冒充当前图网络/ACL或 S3 写权限证明。

### 仅离线渲染初始化 Job

`python -m lightrag.provisioning.job --settings <approved-job-settings.json> --environment <private-runtime.json> --output <new-job.json>`
只创建新的本地 JSON 文件；不会调用 Kubernetes、生成 Secret 或安装任何资源。输出可作为
Kubernetes Job 人工审查输入，**本轮未执行 apply**。Job 固定 digest、`backoffLimit=0`、
`restartPolicy=Never`、无 service-account token、非 root/只读根，只有 `/tmp` scratch，
不挂载应用工作卷、不启动应用/入口。Job 中的凭据均是**已有且同 namespace**的引用。

settings 的全部必需字段如下；未知值继续留空，不使用单测 fixture 作为生产批准值：

| 类别 | 字段 | 确认要求 |
| --- | --- | --- |
| 目标 | `phase`, `namespace`, `name`, `image` | phase 为 apply/verify/register；镜像必须 `@sha256:…` |
| 已有运行配置 | `environment_secret`, `environment_key` | Secret 某个 key 为完整私有环境 JSON，挂载只读；不导入新密钥 |
| 独立 PG 身份 | `operator_secret`, `operator_key` | 与运行配置 Secret 分开；key 为完整且显式传输策略的 operator DSN |
| 独立图身份 | `graph_secret`, `graph_user_key`, `graph_password_key` | 引用已有独立身份；verify 使用运行配置中的图身份 |
| 拉取 | `image_pull_secret` | 已有且目标可用，不复制 test 凭据 |
| 预算 | `graph_wait_seconds`, `active_deadline_seconds`, `ttl_seconds` | 显式有限正值；deadline 大于图单步和连接预算，仍须覆盖全部阶段；TTL 前保存证据 |
| 资源 | `cpu_request`, `cpu_limit`, `memory_request`, `memory_limit` | 显式批准规格；非生产容量结论 |
| 审核 | `expected_profile_hash`, `expected_plan_hash`, `expected_manifest_hash` | 前两个必需，register 另需 manifest；其余阶段 manifest 可空 |
| 确认 | `confirm_maintenance`, `confirm_empty_target` | 只有单独授权后才置 true，verify 不需要，apply 两项都需要 |

Job 重建/节点故障也不能代替 exactly-once：持久意图/迁移锁负责拒绝重复未知操作。
任何失败先保留 Job 日志及 PG/图状态，再审计；不直接重建或重跑。
初始化与 enroll 的运维 DSN 不放进命令行、普通发布或日志。

## 日常发布状态机

`PREPARED → DRAINING → READ_SWITCHED → WRITER_SWITCHED → RETIRING → COMPLETED`

- owner、identity、原 epoch 和阶段在 PG 持久化，不按 TTL 抢占。PG session lock 仅防同一 owner 两个 runner 同时操作，不代替持久 owner。
- **PREPARED**：核验已接入 Blue，固定 digest/配置，准备 Green 4 Pod，逐个检查真实 readiness、协议、命令与认证格式、原生退出钩子。standby 不 polling、不消费、不业务写、不 DDL。
- 每次阶段观测都核验精确 digest/config/profile、Pod UID 和实际 container imageID；Blue 的真实旧 digest 固定在发布记录，不以“仍属于相同 slot”代替版本证明。无法识别的 imageID 格式拒绝推进，需在接入时确认运行时格式。
- **DRAINING**：scope 行锁关闭新 writer 准入。新写入持久化有类型/version 的命令，等待者不持业务锁/事务；既有 writer/ticket 正常完成，后台不领取新批次。查询独立通过。
- 入口先扩展到两个已验证 slot，等待 EndpointSlice **及 CLB 全部八个 Pod 后端健康**，再收窄到 Green；传播期保留 Blue，不暂停整个 Service。
- **READ_SWITCHED**：复查仅 Green 的 Service/EndpointSlice、CLB 目标/健康和真实 HTTPS 响应。基于活跃索引检查 writer ticket、coordinator active op/claim/lock/pending、未知 owner/witness、源清理和不确定结果；不扫描数千万历史行当空闲判据。
- **WRITER_SWITCHED**：同 scope 锁内再次核实已排空，单调 epoch+1，Green 唯一有权，开闸消费；旧 slot 新写及迟到 cache fill 无权。已有纯查询可继续结束。
- **RETIRING**：持久化本实例退休意图，由 native Uvicorn 关闭监听并等待已有 ASGI/流式连接，连同 waiter/background/backend 计数及 durable work 都结算后才标 closed；不取阻断 peer 的 workspace exclusive，不提前关闭 pool/witness。
- 所有旧实例真实 closed 后才缩旧 Deployment 为零，再完成发布。Pod Ready、固定 sleep、删除 Pod 或 SIGKILL 不是 drain 证据。

控制 Job 使用本次固定镜像，无自动重试、无自动杀业务进程的超时；终态只输出脱敏 JSON。
CI 等待超时不会删除 Job、清 owner 或自动 undo。重入必须观察实际阶段和已经生效的路由/交权，不能依赖 runner 本地文件。

## 命令队列与应用合同

- 只接受显式登记的 API/SDK 操作和 JSON/versioned transport，不序列化 Python callback、文件句柄、临时路径或 raw token。
- 本地文件入口在生产 profile 关闭；对象上传通过已有 presign/complete S3 流程。队列和既有 doc-status 的受理事实不同：排队不伪报 202/track 成功，同步删除仍等待原终态与响应。
- `Idempotency-Key` 在同主体/scope 下固定关联 payload；异负载拒绝。未指定会生成命令 ID，通过 `X-Release-Command-Id` 返回；客户端跨请求重试应主动提供稳定幂等键。
- SDK 写必须显式使用 `request_context(service_id=..., idempotency_key=...)` 且 service 在接入名单内；不支持的 writer 在业务变更前拒绝。
- 执行前重新验证账号/key/过期/作用域，内部 replay 仍走原 FastAPI validators/auth/admission。凭据指纹不是用户权限替身，不持久化 Bearer、续期 token 或预签名 URL。
- 接收者断开不删除命令。超时返回可追踪状态，不声称“没有执行”；查询 `/release/commands/{id}` 只能看当前认证主体自己的结果。
- EXECUTING/UNCERTAIN、终态 ACK 丢失和未知物理写不重放，保留 ticket/coordinator pending 供审计，不调用 recover。
- 队列保护尚未完成命令引用的 S3 对象/会话；清理与入队以同一 scope 锁排序。有效期前已接收的 complete 可跨短暂排队继续核验，其他过期请求不放行。
- 普通 query/keywords cache 发送前尝试 writer ticket，无权可观测跳过；发送后的错误不吞掉。抽取归因 cache 仍遵循原引用先于缓存行合同。
- 流式 permit/模型并发槽直到消费结束或显式关闭才释放；即使尚未消费也关闭已打开的上游 response/client。HTTP 断开后的清理等待正在进行的 provider read，不提前归还并发槽；关闭结果不确定则保留 backend 计数并拒绝退休。SDK 调用者必须 exhaust/aclose。
- detached 后台任务取得独立 durable ticket，不能借用随 HTTP 命令结束而失效的未绑定 ticket。文本/文件受理只有在原 enqueue 持久化点之后才确认，后续处理可以继续后台执行。实例关闭只 drain 自身，不取消另一实例查询。
- `/health` 保持原行为；prod 使用 `/ready`，识别 coordinator quarantine/witness/fence/polling/controller/retirement。首次异常记录错误类、SQLSTATE、连接用途、Pod/workspace、backend PID 与停止时间，不记录 SQL 参数/DSN。
- 必要 query-accounting authority 的可靠事件保存尚未证明时，明确拒绝启用 production release，不把用户未提交功能当成已交付依赖。

### 有限预算

`drain + cutover + safety < request_wait`，并且 `request_wait + response_margin < min(proxy_timeout, client_timeout)`。
结果保留至少覆盖等待+执行，源保留不少于结果保留；命令容量和 byte limit 在受理前限制。
这些值必须来自批准的真实负载/代理链，不是示例值就算验收。不能为过长任务强杀进程。

## 中止、回退与排障

- 交权前已知排空预算耗尽会进入安全中止：先确认 Blue 真实路由及旧写权再开闸，队列在 Blue 继续处理。`ABORTING` 仍保留发布 owner，待 Green 本实例连接退出、durable closed、缩容为零后才转 `ABORTED` 并释放 owner；中途失败可按实际状态重入。已知长任务不被取消。
- 任一 fence、未知 ACK、读证据失败或入口状态不明都停止推进并保留 owner；不能将“读失败”当“没有任务”。
- 交权后没有通用 `rollout undo`。只有旧 slot 尚未退休、显式批准绑定 `from_digest`、`to_digest`、`to_config_hash`、`profile_hash`、`read_write_compatible` 和 `evidence_id` 时，才能执行 `deliver --action rollback --approval <private-approved.json>`：重新关闸/排空/反向路由/epoch+1/实例退休。阶段为 `ROLLBACK_DRAINING → ROLLBACK_WRITER_SWITCHED → ROLLBACK_RETIRING → ROLLED_BACK`，原始 epoch 和批准持久化供中断恢复。
- 旧 slot 已退休或发布已完成时，不直接复活旧二进制；走新的兼容发布/单独数据迁移方案，不还原单边备份。
- 不能通过数据库重启、批量重启 Pod、清锁、篡改 `_broken` 或扩大权限解决发布失败。

## 腾讯云依据与尚需实测

适配器只调用 CLB `DescribeListeners`、`DescribeTargets`、`DescribeTargetHealth`，不直接修改 CLB 后端。
路由修改由 TKE controller 根据 Service 完成，CLB 只读凭据应限制到指定资源。

依据：[Ingress 注解](https://cloud.tencent.com/document/product/457/56112)、
[Ingress 优雅停机](https://cloud.tencent.com/document/product/457/60065)、
[CLB 连接/后端区别](https://intl.cloud.tencent.com/zh/document/product/214/5411)、
[查询后端](https://cloud.tencent.com/document/api/214/30684)、
[后端健康](https://cloud.tencent.com/document/api/214/34898)。

这些文档不是本环境验收。必须核实真实 TKE/controller/kubelet 能力、TLS、全部代理的 buffering/
request/idle timeout、后端连接保持/强制断流参数、Service selector 传播和长 SSE，在各阶段及
故障注入时持续观测连接、错误、延迟、排队/幂等/最终存储事实。未经过此演练不能开放生产 gate。

## 已批准首发共用专用账号（2026-10-23）

用户明确批准首次上线复用现有LightRAG专用PG/HugeGraph账号作为运行与迁移身份，接受应用进程可改表及发布控制数据的风险。这是对身份分离的有限例外，不是关闭所有权限检查，也不是要求给应用CREATEROLE；真实云入口、隔离、只读启动、版本化迁移及其他验收仍须通过。

- 默认配置不包含`onboarding.first_release_credential_exception`，保持独立最小权限模式。例外必须完整提供`version=1`、批准ID、准确scope/profile、正式release_tag、PG用户/数据库、图用户和`accepted_risk=true`；全部值来自获准的生产输入，不复制单测fixture。批准进入配置hash与migration plan hash，实际环境中仅用非敏感JSON字段`LIGHTRAG_FIRST_RELEASE_CREDENTIAL_EXCEPTION`及准确`LIGHTRAG_RELEASE_TAG`传递。
- 初始化仍通过独立`python -m lightrag.provisioning plan/apply/verify/register`和`python -m lightrag.distributed.release enroll`步骤。`apply/register`除原维护/空库/plan确认外，增加`--confirm-shared-credentials`；离线Job的`settings.confirm_shared_credentials`也须显式为true，不能由静态批准自动代填，只读verify无需该执行确认。迁移DSN和图账号/密码必须显式提供专用输入；即便与runtime相同也不隐式从runtime环境兜底。K8s运行与operator Secret引用继续分开，避免把云控制凭据注入应用；它不再被描述为数据库角色分离。
- PG连接必须同时核对current_user/session_user和目标库，并拒绝superuser、CREATEROLE/CREATEDB、复制、BYPASSRLS及相关继承。只放行已经接受的专用对象所有权/DDL/控制表权限；不创建角色、不改原migration SQL/checksum或使用启动DDL补建。版本/profile/结构漂移和完整CRUD验证不豁免。
- 首次release policy持久化同一批准，只允许Blue/epoch1/IDLE/open且无owner。实例启动/刷新重新核对，缺失或漂移拒绝；初始化所有写入口在迁移/授权/图操作前同样拒绝后续epoch、Green或批准漂移，尚未enroll才允许首发准备。enroll在连接前核对operator DSN的准确host/显式port/库/用户，不允许同名库克隆冒充目标；批准相同的首版Blue可重建。普通`prod_deploy`/`deliver`及持久store升级入口拒绝该例外，不能静默开Green或把它用于后续tag。未来4+4升级前需单独安排身份分离/权限整改及复验，不能直接删批准记录冒充已整改。
- 本地完成该能力不代表已部署。首发实际流水线还需显式初始化→verify→register/enroll→4×1 Blue及入口验收串接，云API观测权限/审阅hash与其他前置也必须齐备；不调用普通升级器假造一个已运行Blue，不用共用账号批准免除这些工作。
