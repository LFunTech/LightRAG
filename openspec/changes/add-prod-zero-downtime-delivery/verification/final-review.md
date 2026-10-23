# 最终审查与单次修复记录

日期：2026-10-21。基线 HEAD：`beb4d64681c06d393255c577cdf0d988fdeecbfb`。
按 inline execution 工作流只安排一次 fresh-context 全量审查，由
`/root/final_prod_delivery_review` 完成；以下 Critical/Important 在同一次修复轮中处理，
每项先复现 RED，再实现并验证 GREEN。不声称做过第二次独立复核。

| 编号/级别 | 问题与修复 | RED → GREEN 证据 |
| --- | --- | --- |
| C1 Critical | detached 任务借用父 HTTP 命令未绑定 ticket；父命令完成后子任务失权。改为独立 durable ticket，关联原 command，覆盖后续实际物理写。 | `test_review_regressions::test_detached_background_owns_an_independent_ticket_after_command_finishes`；RED 为真实 writer guard 拒绝，GREEN 真实本地 PG。 |
| I1 Important | namespace Role 无法读取 cluster-scoped Namespace UID。首次 foundation 增加仅 get 指定两个 Namespace 的 ClusterRole/Binding；日常发布不创建。 | `tests/ci/test_prod_deploy.py::test_foundation_gives_control_only_exact_namespace_identity_reads`。 |
| I2 Important | 排空预算耗尽没有可靠安全中止，且清理 candidate 前可能释放 owner。新增 typed budget、有界等待、Blue route proof、ABORTING 保留 owner，Green closed/scale0 后才 ABORTED。 | `test_orchestrator::test_known_drain_budget_expiry_restores_blue_and_retires_only_green` 与真实 PG `test_handover_integration::test_budget_exhaustion_is_typed_and_abort_keeps_owner_until_candidate_closes`。 |
| I3 Important | 文本任务仅后台开始便将命令标成功，早于原 doc-status 持久化受理。release-only accepted 信号延至原 enqueue 成功。 | `test_review_regressions::test_real_text_handler_waits_for_durable_enqueue_before_reporting_success`；真实 FastAPI handler 阻塞 enqueue 时 RED 读到 SUCCEEDED，修复后保持 EXECUTING。 |
| I4 Important | 未消费流漏关已打开的 response/client；断开清理与在途 anext 竞态可能早放模型槽。显式 upstream owner、iteration lock、先 managed owner 后 provider 的 shielded cleanup；未知关闭保留 backend 活跃。 | `test_query::test_real_openai_unconsumed_stream_closes_already_open_response_and_client` 与 `test_request_cleanup_waits_for_inflight_iteration_before_releasing_provider_slot`；真实 OpenAI helper/并发包装器，fake upstream 无外部模型调用。 |
| I5 Important | 解除静态 false 后 prod 凭据 step 缺独立正式 tag 限制。增加 workflow ref 排除及 step 严格 regex，静态 gate 仍 false。 | `test_prod_workflows::test_formal_tag_filter_survives_lifting_the_static_workflow_gate`；strict Woodpecker lint。 |
| I6 Important | 后续阶段只看 slot/profile 可能接受漂移镜像。持久化旧 digest，每阶段检查精确 digest/config/实际 imageID，入口响应绑定候选身份。 | `test_review_regressions::test_route_proof_rejects_post_transfer_candidate_image_drift`；RED DID NOT RAISE，GREEN 拒绝。K8s/CLB 为 fake。 |
| I7 Important | 同 slot/配置下一版本复用 immutable 身份 ConfigMap 名而冲突。名称绑定完整 identity hash，Secret 仍绑定 config hash。 | `test_prod_deploy::test_reusing_a_slot_with_same_config_has_a_new_immutable_identity_configmap`。 |

修复后最终验证：全后端 **9330 passed / 355 skipped / 0 failed**；独立 PG release
**68 passed / 0 failed**；CI + docstring **144 passed**。测试命令和限制见
[local-verification.md](local-verification.md)。没有遗留 Minor 建议。

## Final: Ruling — 审查未判项目的裁定

1. 用户既有 query-accounting 业务逻辑不代为完成或提交；release 启用在 attached authority
   可靠性未证明时硬拒绝。OpenAI stream 生命周期与发布直接相关，未以此排除，已按 I4 修复。
   代价：需要计量的生产接入仍被阻断，不能靠跳过事件上线。
2. 真实密钥、线上 ACL/特权与资源容量不在此次授权内，全部保留远端前置；不拿合成凭据或
   fake CLB 作为通过证据。代价：本地实现不能宣称已经可上线。
3. 原 HEAD 的 84 行 docstring 违反现有预算；已压缩为规则摘要并保留合同指针，无行为改变。
   Cairo 已安装但 macOS 搜索路径缺失，只为测试进程设置 `DYLD_FALLBACK_LIBRARY_PATH`，
   没有安装依赖或改机器配置。代价：本机复跑全量须使用记录的直接 Python 命令。
4. 跨 PG/HugeGraph 的非事务 residue 仍遵守原恢复/归因合同；不在发布功能中重设计跨库事务。
   代价：未知 ACK 仍要求独立运维审计，不能自动 exactly-once 重放。
5. 已攻陷数据库凭据/恶意特权管理员不是普通发布竞态模型；最小权限、身份/hash/CAS 是本次
   边界，不声称能抵御合法超级用户篡改。代价：仍依赖基础设施权限与接入审查。
6. 旧 slot 开始退休后拒绝直接 reverse/undo 是安全边界，保留 owner 和证据，走新的兼容发布。
   代价：不是任意时点都能快速二进制回滚。

## 原始日志位置

日志为本地合成测试输出，保留于仓库 ignored 目录 `temp/prod-delivery-implementation/`：
`review-detached-red.log`、`review-accept-red.log`、`review-stream-red.log`、
`review-bag-red.log`、`review-budget-red.log`、`review-drift-red.log`、
`review-delivery-red.log`；对应 green 包括 `review-background-green.log`、
`review-stream-bag-green.log`、`review-budget-pg.log`、`review-drift-green.log`、
`review-delivery-green.log`，最终 fresh 回归为 `final-full-suite.log`、
`final-release-pg.log`、`final-ci.log`。不包含真实 prod/test 凭据或业务内容。
