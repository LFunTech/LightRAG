# Capability 场景逐项证据矩阵

日期：2026-10-23；当前 **63 个场景**全部列出：原57项保留既有证据，新增5个首次迁移场景及1个云侧TLS/HTTP后端场景列于末表，真实容量/优化对照未验证。**本地通过不等于真实上线验收**。
测试文件路径以仓库根为基准；`test_*` 缩写归属 `tests/distributed/release/`，除非显式写
`tests/ci/` 或 object_storage。所有实际最终命令/pass/skip 见 [local-verification.md](local-verification.md)。

真实层：本地 PG18.6、ASGI/原 handler/授权/真实 loopback Uvicorn；合成层：业务/模型/对象源及
K8s/CLB/入口 fake。真实 4/8 Pod、TKE/CLB、多节点存储、故障/容量演练和实际 ACL 未运行。
表中“待演练/核验”不勾远端任务，也不能作为 enabled onboarding 的证据 ID。

## prod-delivery 原交付（26 项）

| 场景 | 本地实现/证据 | 边界/未验证部分 |
| --- | --- | --- |
| A production release uses the existing artifact chain | `tests/ci/test_prod_workflows.py`、`test_prod_inputs.py`：复用依赖/归档/来源/digest；原链 hash 不变 | 本地通过；真实 CI 构建/发布另授权 |
| Existing test delivery remains unchanged | `test_prod_workflows::test_original_delivery_files_and_test_resources_match_preimplementation_baseline` + 原 `tests/ci` | 35 文件 hash 相同；远端 test 未操作 |
| Production has not been activated | `test_prod_workflows::test_prod_workflow_is_statically_disabled_and_has_no_test_credentials` | 本地通过；gate 仍 false |
| Non-production events cannot select production | `test_prod_deploy::test_prod_selection_is_formal_tag_and_explicitly_activated_only`；独立 step filter 回归 | 本地通过；CI repo Secret 权限接入时确认 |
| Artifact or image identity is inconsistent | `test_prod_inputs::test_invalid_inputs_fail_before_any_kubernetes_operation` 参数组 | 本地通过；错源/错身份先拒绝 |
| Production credentials are incomplete | `test_environment_secrets` 缺项/环境前缀；`test_prod_inputs` | 本地通过；无实际密钥读取/导入 |
| Production input contains historical test-named aliases | `test_environment_secrets::test_prod_mapping_parses_inventory_without_executing_it` | 合成清单通过；不 source/跨环境读取 |
| An onboarding operation produces diagnostics | `test_environment_secrets` diagnostics/dry-run 与 `test_diagnostics.py` | 本地脱敏通过；不打印凭据 |
| A production target resolves to test scope | `test_prod_config::test_unsafe_prod_config_is_rejected_before_mutation`；`test_prod_inputs` | 本地拒绝通过；真实目标 UID/ACL 待核验 |
| Environments share a physical backend | `test_prod_config` isolation evidence 硬门禁 + runbook 接入清单 | 配置拒绝通过；实际 PG/图/S3 ACL 未验证 |
| A candidate is incompatible | `test_runtime_integration::test_failed_verify_only_initialization_does_not_fence_blue`；schema/profile/输入回归 | 真实 PG+fake backend 通过；实际版本兼容批准待确认 |
| A legacy or unknown writer is present | `test_handover_integration::test_unknown_writer_blocks_transfer_without_releasing_owner`；scope/profile 标记 | 真实 PG 通过；全部外部 writer 清单/权限待审计 |
| Production is initially empty | `test_cli.py` 显式确认、`test_prod_config`、`test_prod_kustomize` 首次 4 Pod 资源 | 本地入口/渲染通过；真实初始化/首次接入未运行 |
| The standby version is not ready | `test_runtime_integration` verify-only 失败无 fence；transport prepare 先验 Blue/候选 | 本地通过；真实待命启动/私有查询待演练 |
| Routing changes propagate asynchronously | `test_orchestrator::test_reentry_observes_route_after_lost_ack_before_transfer`；`test_prod_transport.py`；`test_qcloud.py` | fake K8s/CLB 传播/ACK/精确后端通过；真实传播/长连接待演练 |
| Production starts with the approved baseline | `test_prod_config::test_config_preserves_four_single_worker_and_eight_pod_budget`；`test_prod_kustomize` | 真实离线 render 通过；未运行应用 4 Pod |
| A release prepares its standby slot | `test_prod_deploy.py`；`test_prod_kustomize` 双 slot 共 8；聚合 budget validator | 本地通过；8 Pod 调度/后端额度待核验 |
| The eight-pod release budget cannot be met | `test_prod_config` capacity/workers/预算拒绝；orchestrator 不削减 Blue | 本地通过；真实资源不足故障待演练 |
| Four pods are insufficient at the target load | `docs/ProdDelivery.md` 容量报告/SLO/瓶颈评审流程 | 未压测；8.6 保留未完成，不声称 4 Pod 足够 |
| A larger steady-state size is approved | ProdConfig replicas>=4、capacity>=2N、worker=1；runbook 扩缩容约束 | 校验器具备；实际 N/2N 审批/压测属 8.7 未完成 |
| Two pipelines target the same production scope | `test_store_integration::test_release_owner_cannot_expire_or_be_replaced`；持久 owner/CAS | 真实 PG 通过；两个真实 CI runner 未运行 |
| The release runner disappears | `test_handover_integration::test_runner_reentry_survives_epoch_transfer_and_refuses_older_version`；lost ACK 路由重入 | 真实 PG + fake transport 通过；CI/Pod 真故障待演练 |
| Drain exceeds the release budget before handover | `test_orchestrator` typed budget safe abort；`test_handover_integration` ABORTING owner/closed | 真实 PG + fake transport 通过；不取消长任务 |
| An uncertain mutation is found after writer handover | `test_handover_integration` unknown writer/uncertain ticket；`test_runtime_integration` unknown read mutation | 真实 PG 拒绝通过；无自动 recover/清锁 |
| A release continuity rehearsal passes | `test_native_server.py` 真 loopback 流退出；HTTP/PG 局部连续性测试 | 仅局部通过；完整多节点/CLB/真实存储发布演练 8.3 未运行 |
| Only document validation has run | 本报告区分静态、真本地、fake 与远端；OpenSpec strict 仅结构校验 | 没有以提案/Ready/health 代替零停机证据 |

## prod-delivery 性能增量（新增4项，P0本地证据/真实验收分列）

下列场景对应 design/tasks 第10节。本轮测试结果另记 [性能本地验证](performance-local-results.md)，
不套用原9330/68/144历史快照，未运行环境压测或宣称目标通过。

| 场景 | 本轮本地证据入口 | 边界/未完成 |
| --- | --- | --- |
| An answer cache hit still requires retrieval | `tests/performance/test_integration::test_actual_kg_cache_hit_keeps_retrieval_and_reports_history_bypass`：真实kg/cache路径两次检索、仅一次生成；`test_metrics.py`实际队列计时/SDK dispatch；prod policy回归 | 后端用fake、实际阶段成本/质量/1000QPS未测；不前移lookup |
| A workload uses streaming and conversation history | `tests/performance/test_metrics.py`流close/取消/未消费cleanup；`test_openai.py`真实OpenAI adapter terminal usage/close；`tests/llm/test_query_cache_conversation_history.py`旁路；`test_load.py`实际httpx transport/NDJSON错误测试；[200ms/3s/20s分桶本地回归](histogram-local-results.md) | 未发真实负载；普通/流式/多轮比例及完整SLO口径待批；本地流/合成分桶不等于CLB连续性或容量验收 |
| An optional cache optimization is proposed | `tests/ci/test_prod_performance.py`四flag/hash与拒绝未证策略；`test_integration.py`独立答案旁路、关键词/抽取保留及旧答案不复用；`tests/distributed/release`原cache ticket/fence保留 | prod答案cache关闭；无新Embedding/L1/Redis/知识版本缓存或提前lookup；真实ACL/数据更新/质量待隔离验收 |
| Concurrent cold requests target the same question | `tests/performance/test_load.py`open-loop/预算/调度lag/吞吐不假通过；工具支持重复热点dataset；`test_metrics.py`队列等待/有界观测 | singleflight未采用且无跨Pod冷启动数据；不得当成已合并/容量通过，10.6/10.8仍未完成 |

## release-write-handover（22 项）

| 场景 | 本地实现/证据 | 边界/未验证部分 |
| --- | --- | --- |
| Two slots access one production workspace | `test_config.py` 协议标记；`test_handover_integration` 同 scope 双 slot | 真实 PG/配置通过 |
| Existing test does not enable handover | `test_config::test_release_is_off_without_any_new_environment_dependency`；原全后端/交付基线 | 本地通过；原 test 无新增元数据要求 |
| A standby starts while documents are pending | `test_pipeline_gate.py` 每批关闸；`test_runtime_integration` standby verify；readiness standby | 本地通过；带真实积压的 8 Pod 待演练 |
| A write races with gate closure | `test_store_integration::test_gate_admission_is_ordered_and_unknown_tickets_are_not_released`；C1 回归 | 真实 PG 行锁/ticket 通过；真实并发压测待演练 |
| A background worker observes a closed gate | `test_pipeline_gate::test_closed_gate_stops_new_batches_even_with_an_existing_parent_ticket`；SDK manual retry | 本地/真实 PG 通过；原 doc-status/FAILED 测试全量通过 |
| A synchronous delete waits during handover | `test_http_integration` 原响应等待；explicit DELETE codec；`test_app_assembly.py` 全 route codec | HTTP 等待/响应局部通过；真实同步删除跨 CLB 切换待演练 |
| The accepting instance is replaced | `test_store_integration` durable commands/idempotency；`test_handover_integration` epoch 重入 | 持久性局部通过；未真实杀接收 Pod，故障演练待授权 |
| A long existing task threatens the wait budget | `test_orchestrator` budget abort；`test_handover_integration` typed budget owner 保留 | 真实 PG + fake transport 通过 |
| Queue capacity is exhausted | `test_store_integration::test_command_queue_is_bounded_idempotent_and_durable`；config byte/count limit | 真实 PG 通过；满队列在接受前拒绝 |
| A client repeats the same queued command | `test_http_integration::test_original_http_response_and_idempotency_survive_queue`；SDK request context | 真实 PG + ASGI 通过；effects count=1，异负载拒绝 |
| Authorization is revoked while waiting | `test_authorization.py` key/account/token 失效；`test_commands` executor recheck | 本地通过；不持久 raw token |
| The executor loses a mutation acknowledgement | `test_commands` unknown ACK；`test_store_integration::test_claimed_command_unknown_result_never_returns_to_queue` | 真实 PG 通过；UNCERTAIN 不重投 |
| Upload completion is queued across session expiry | `tests/object_storage/test_release_retention.py` expiry/cleanup；scope lock cleanup/admission 真 PG | 本地通过；真实 S3 跨环境/延迟待演练 |
| A cache miss occurs during write drain | `test_runtime_integration::test_query_cache_fill_is_optional_before_send_but_never_bypasses_ticket`；关闸 HTTP query | 真实 PG + fake cache 通过；发送后错误不吞掉 |
| An old streaming query finishes after writer transfer | `test_runtime_integration` query lifetime/cache authority；`test_query.py` stream/cleanup；native 长流 | 真实本地局部通过；CLB 交权后长流待演练 |
| A required query side effect cannot be preserved reliably | `test_http_lifecycle::test_required_accounting_authority_refuses_release_api_activation`；`install_release_api` | 拒绝路径实现；用户既有必要计量可靠性未验收，启用前置 |
| A stale old instance attempts a new write | `test_handover_integration::test_transfer_uses_real_tickets_keeps_readers_and_fences_late_blue` | 真实 PG 通过；旧 slot 不能获得新 ticket |
| Read-only queries remain active during handover | 同上 + `test_runtime_integration::test_release_queries_hold_permit_until_stream_close_without_blocking_peer_finalize` | 真实 PG/live reader 通过；不要求全 workspace 无查询 |
| Old connections outlive cutover | `test_lifecycle.py` own waiter/query/backend drain；`test_native_server.py` 真 Uvicorn 不取消长流 | 真实本地通过；真实 CLB 已有连接保持待演练 |
| A later scale-down selects a busy instance | `test_lifecycle` busy instance drain/cancel waiter；transport closed 后才 scale；runbook 无自动 HPA | 实例合同本地通过；后续真实扩缩容及 owner 协调仍待审批复测 |
| Storage inspection fails during drain | `evidence.py` 读失败传播/fence 拒绝；`test_handover_integration` unknown writer；unknown mutation finalize 拒绝 | 真实 PG 保护局部通过；网络断连/数据库故障远端演练未运行 |
| An ordinary API client attempts to control release | `test_store_integration::test_runtime_database_role_cannot_change_owner`；`test_grant_runtime_refuses_an_existing_control_table_writer`；CLI | 真实 PG 权限拒绝通过；生产实际 RBAC/CAM/DB grants 待核验 |



## 10.7工具补全（新增5项，属prod-delivery）

2026-10-23本地补充：原“流式/多轮”“可选优化”“冷同题”场景新增末尾usage/unknown停测、v2阶段、同批去重/有界并行与取消回归，见[本地结果](2026-10-23-usage-timing-local.md)。不新增场景编号，仍57场景；原远端/真实质量/容量空白不变。固定成员watch不冒充4+4动态退休计量或混合写停控已验。

| 场景 | 本地实现/证据 | 边界/未验证部分 |
| --- | --- | --- |
| Rehearsal input lacks isolated write authorization | `tests/performance/test_rehearsal.py` 双确认/默认CLI/源与payload/依赖拒绝；原control身份/origin及已有owner拒绝 | Mock/离线通过；真实ACL和授权未核验 |
| Mixed writes span the closed gate and writer transfer | 同文件真实ReleaseOrchestrator集成、native InsertTextRequest规范化；`test_rehearsal_integration.py` 真实PG baseline/精确scope-principal-key receipt | 本地PG/原编排器通过；HTTP业务、K8s和CLB边界为Mock，非真实8Pod |
| A rehearsal fails or is cancelled after a write was sent | 未知ACK一次发送、查询失败冻结依赖、负载错误停止、重复取消收尾、journal失败和重定向拒绝回归 | 有界本地故障注入通过；硬杀/真实网络故障未演练 |
| Continuous reads and an old stream cover handover | 原编排器+live Mock NDJSON、旧slot query证据、提前耗尽负载拒绝、原native长流不取消回归 | Mock拓扑+真实本地Uvicorn局部通过；真实CLB连接保持未验 |
| Quality evidence is missing or exposes forbidden context | `tests/performance/test_quality.py` 缺失/重复/禁止上下文/双接口拒绝；`test_rehearsal.py` revision新旧答案注入 | 确定性rubric本地通过；领域语义/真实知识与权限验收未运行 |


## prod-delivery 首次结构迁移（新增5项，真实执行另批）

| 场景 | 本地实现/证据 | 边界/未验证部分 |
| --- | --- | --- |
| Fresh target is provisioned explicitly and repeated safely | `tests/provisioning/test_postgres.py`、`test_engine.py`：独立本地PG空库/重复、原coordination/release调用、scope/enroll不混 | 本地PG18.6/pgvector0.8.7；真实prod17.6未迁移 |
| Existing storage or migration evidence is incompatible | `test_plan.py`、`test_postgres.py`、`test_engine.py`：范围/参数/无版本表/漂移/权限/并发锁拒绝 | 未接管既有数据；不把IF NOT EXISTS当adoption |
| Graph schema mutation has an uncertain result | `test_graph.py`：真实PG持久意图、MockHTTP timeout、GET确认/缺失、index失败与超时、caller事务拒绝 | 未运行真实HugeGraph DDL/ACL；不提供跨存储回滚或未知重放 |
| Production runtime is not migrated | `test_runtime.py`、`test_engine.py`：实际PG受限身份门禁、mock真实初始化调用链拒绝DDL，release关闭不新增门禁 | 没有部署新版本/重启现场；真实运行兼容待授权 |
| Initialization Job is prepared without deployment authority | `test_cli.py`、`test_job.py`：离线无网络、目标/编码/DSN覆盖拒绝、现有引用、固定digest、无app/Secret/apply | 真实引用/预算为空；未导入密钥或创建Job，不启用prod gate |

本轮最终命令与本地证据见 [首次迁移验证](2026-10-23-storage-provisioning-local.md)；旧表中的历史结果不与本轮数字重复相加。

## prod-delivery 云侧 TLS / HTTP 后端（新增1项）

| 场景 | 本地实现/证据 | 边界/未验证部分 |
| --- | --- | --- |
| An input attempts to enable application Kubernetes TLS | `tests/ci/test_prod_cloud_tls.py`：真实配置/schema/渲染/启动profile拒绝TLSSecret、SSL及证书路径；Blue/Green HTTP、无TLS Secret或证书挂载；云侧HTTPS/证书/流式门禁继续拒绝无效输入 | 本地RED→GREEN及相关目录结果见[引用核对与本地约束验证](2026-10-23-prod-reference-audit.md)；未部署，云侧CLB证书绑定/连接保持未验 |

## prod-delivery 显式 EdgeOne 入口（新增3项，总计66场景）

| 场景 | 本地实现/证据 | 边界/未验证部分 |
| --- | --- | --- |
| EdgeOne terminates public HTTPS without Kubernetes TLS | `tests/ci/test_prod_edgeone.py`：显式HTTP80 profile、无CLB证书/HTTPS注解、独立只读签名与observer接线；真实Transport拒绝TLS漂移 | EO域名/云证书/阿里云DNS/专属规则已实际配置，见[本轮记录](2026-10-23-edgeone-browser-delivery.md)；未部署app，不签署真实SSE/4+4 |
| EdgeOne configuration is incomplete or drifts | 同文件：域名/源站/状态/证书、settings/rules hash漂移、分页/计数/返回类型、缺观察器拒绝；原qcloud全目标/健康验证保留 | 本地Mock通过；云API只读身份未补齐，不用截图冒充API hash或规则语义证明 |
| Malformed ingress discriminators are rejected safely | 同文件：mode/protocol数组/对象/空值/bool/数值，先4 RED后10 GREEN，真实配置解析路径 | 只输出字段级ProdConfigError，不触发集群变更；不代表其他未知JSON字段获准 |


## prod-delivery 已批准的首发共用身份（新增2项，总计68场景）

| 场景 | 本地实现/证据 | 边界/未验证部分 |
| --- | --- | --- |
| A dedicated non-superuser initializes the approved first Blue | `tests/provisioning/test_shared_credentials.py`：隔离本地PG中无CREATEROLE等权限的专用owner实际apply/重复/verify/register、7项MockHTTP图DDL；实际release store持久批准及runtime注册；`tests/ci/test_prod_shared_credentials.py`：显式Job确认、配置hash及4×1 Blue渲染 | 共用owner的DDL/控制表风险已获批准；真实prod未迁移/登记/部署，MockHTTP不代表HugeGraph实测，初始流水线编排仍待实现 |
| A shared credential exception is reused or drifts | 三份`test_shared_credentials.py`：缺失/畸形/跨scope/profile/tag/身份拒绝、current/session用户与权限、持久policy/epoch/phase漂移、5个初始化写入口副作用前拒绝、enroll DSN host/port连接前拒绝、Job独立boolean执行确认及普通pipeline/deliver/store升级副作用前拒绝 | 默认test无例外路径不变；不是永久权限豁免，真实后续4+4需先完成身份整改及另行验收 |

## prod-delivery K8s-only 首次部署（新增3项，总计71场景）

| 场景 | 本地实现/证据 | 边界/未验证部分 |
| --- | --- | --- |
| The first target has no running Blue | `tests/ci/test_prod_initial.py`：实际CLI初次分支、产物/目标校验、4×1创建顺序、固定digest、无云API/应用TLS；`tests/provisioning/test_initial.py`：隔离PG实际apply→verify→register→enroll及持久首发批准 | 图服务为MockHTTP，真实TKE结果另列；不认领公网入口/模型额度/容量 |
| Initial deployment has an uncertain or failed result | 同CI测试：scope固定Job/immutable claim，不同pipeline/输入/缺失Job拒绝重放；阶段失败、错误digest、kubectl读写预算/迟到成功均停止，保留远端现场 | 没有自动recover/rollback/重启，真实网络未知故障未注入 |
| Production objects are durable outside the Pod | 同CI测试：null storage只允许S3+PG/图，拒绝文件storage/local ingestion；有界emptyDir、不造PVC；实际输入JSONSchema独立校验 | 不宣称S3真实ACL/写入已验；原RWX/test配置不改 |

## prod-delivery #76 仅应用续部署（新增2项，总计73场景）

| 场景 | 本地实现/证据 | 边界/未验证部分 |
| --- | --- | --- |
| Only the Kubernetes application remains undeployed | `tests/ci/test_prod_app_resume.py`：receipt/固定digest/runtime与config复用、成功仅创建ConfigMap/Deployment/Service/Ingress、已有同spec仅观察、manual/master及显式执行确认；实际原Job/Pod/runtime只读核验通过 | 未重跑迁移/enroll、未构建镜像或改tag；真实续部署结果另记 |
| Resume evidence or existing resources drift | 同文件：Job/Pod UID/spec/owner/digest/restarts/termination、claim/runtime/config/settings/appSecret/Green/foreign Deployment/Ingress绑定漂移副作用前拒绝；未知ACK停发保留、Pod异常停止入口创建 | 无prod故障注入；未将有限本地测试认领外部入口/4+4/容量通过 |
