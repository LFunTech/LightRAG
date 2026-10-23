# 剩余九个 test Pod 的顺序恢复记录

## 授权与结果

用户明确授权：先恢复 test-01 的另一副本，再依次处理其余 workspace；每次仅重建一个并直连验收，保持原镜像和配置；发现待处理任务或异常立即暂停；不 recover、不变更 PostgreSQL、不操作 prod。

**九个旧 Pod 已逐个正常替换且验收通过；收尾逐 Pod 直测，五个 workspace 共 10 个 Pod 的协调接口均为 200、polling 正常。** 上一轮金丝雀未被再次替换。

- 执行时间（北京时间）：2026-10-21 11:43:06 至 2026-10-21 12:39:39。
- 每个新 Pod 均独立观察至少 300 秒，实际范围 301.836–310.552 秒；累计直连采样 **221/221 次 HTTP 200**。
- 每个替换实例均捕获至少两个实际 completed polling operation，并排除该 workspace 已恢复实例的 owner，避免用同伴的 polling 充当新 Pod 验收。
- 九次删除严格顺序执行，前一实例完成验收及收尾门禁后，才发起下一次删除；没有批量 rollout restart、强制删除、自动重试删除或改变副本数。

## 分步记录

| 顺序 | namespace | 原 Pod | 新 Pod | 观察秒数 | 直连 HTTP 200 |
| --- | --- | --- | --- | ---: | ---: |
| 1 | lightrag-test-01 | `lightrag-7bd8fd4f5-dr7nf` | `lightrag-7bd8fd4f5-ndddh` | 306.084 | 26/26 |
| 2 | lightrag-test-02 | `lightrag-79fb9488cf-5ncn6` | `lightrag-79fb9488cf-6tvk4` | 301.836 | 26/26 |
| 3 | lightrag-test-02 | `lightrag-79fb9488cf-dnkx9` | `lightrag-79fb9488cf-95q5q` | 307.046 | 26/26 |
| 4 | lightrag-test-03 | `lightrag-7ffd9bb78c-cnpgs` | `lightrag-7ffd9bb78c-j9j7r` | 308.610 | 25/25 |
| 5 | lightrag-test-03 | `lightrag-7ffd9bb78c-d827c` | `lightrag-7ffd9bb78c-gt6g9` | 310.552 | 25/25 |
| 6 | lightrag-test-04 | `lightrag-657678fbdc-lztb4` | `lightrag-657678fbdc-h28jz` | 309.258 | 24/24 |
| 7 | lightrag-test-04 | `lightrag-657678fbdc-n7q9j` | `lightrag-657678fbdc-t524k` | 302.050 | 23/23 |
| 8 | lightrag-test-05 | `lightrag-cdd7685f9-q8h26` | `lightrag-cdd7685f9-77mvk` | 302.181 | 23/23 |
| 9 | lightrag-test-05 | `lightrag-cdd7685f9-vzwwl` | `lightrag-cdd7685f9-jm86r` | 305.554 | 23/23 |

初始金丝雀 `lightrag-7bd8fd4f5-77x9r`（UID `da52276a-c9fb-4d4d-be4a-07ff49cf94ea`）全程受保护，UID、spec、镜像及重启次数未变。

## 门禁与验收方法

1. 每次替换前及日志归档后重新核验五个 Deployment/全部当前 Pod 的身份、spec、镜像、重启次数，以及所有引用 Secret/ConfigMap 的 UID/resourceVersion。
2. 独立只读连接检查 workspace fence/generation、活跃操作、orphan、claim、资源锁、pending mutation、待处理 retry request/target，以及全部文档状态。空闲 polling 是正常活动；删除前取得无活跃 operation 的快照，不将正常空闲 polling 误报为业务遗留，也不关闭 polling 来制造空闲。
3. 新实例只按原 ReplicaSet/Pod template 重建；镜像和实际 imageID 一致。使用一次性、不落盘的随机 HMAC key，在删除前确认旧 Pod 的声明配置与容器环境一致，再确认新旧环境指纹一致；没有保存或展示凭据。
4. 每个新 Pod 都经 `127.0.0.1` 绑定的 `port-forward pod/<name>` 直连，不通过混合新旧副本的 Service 随机验收。持续检查 polling_active、polling_error、fence、generation、recovery_required、orphan、claim 和 retry 状态。
5. 观察期间约每 30 秒复查全局队列及所有已恢复实例；每阶段通过后归档新旧 Pod 脱敏日志，检查启动/协调错误，并复核所有非目标 Pod 未变化。
6. 每阶段 port-forward 均正常关闭；失败即停止后续步骤，没有以重启数据库、recover、清锁、放宽权限或修改镜像/配置绕过错误。

## 收尾状态与变更边界

- 全部 10 个 Pod 最终直测 `/documents/pipeline_status` 返回 200，polling_active=true、polling_error=null；新建九个 Pod 的重启次数均为 0。
- 五个 Deployment 的 UID/spec、副本数、镜像、配置引用和权限均未改变；所有引用 Secret/ConfigMap 的 UID/resourceVersion 与恢复开始时一致。
- 五个 workspace 均未 fenced，无 orphan、claim、资源锁、pending mutation、未完成 retry 或活跃的非 polling 操作；generation 保持原值 1/1/2/1/1。
- 文档始终仅有 processed，五个 workspace 数量保持 5/0/1/3/0；没有检测到待处理任务或文档清单变化，也没有提交模型查询、上传、入库、删除或人工重试。
- PostgreSQL postmaster 启动时间保持 UTC `2026-10-18 18:54:21.123654`。仅进行只读运维核查；新应用正常启动和 polling 写入必要协调记录，不代表执行了数据库人工数据修改、DDL、配置变更、重启、migration、bootstrap 或 recover。
- 未访问 prod，未触发流水线、发布新镜像、提交/push/tag，未改变现有 test 发布流程；用户已有未提交产品代码保持原样。

## 结论限制

本轮证明重建应用进程后，现有 test 的协调接口及 polling 能恢复并持续通过上述检查；**不证明历史首次失效的外部原因已经查明，也不表示 readiness 假绿、永久 quarantine 后的失效处置、流式并发或生产蓝绿发布问题已修复**。

未进行真实模型问答、检索质量、并发入库/删除或全功能回归，不把本轮协调恢复验收扩展为这些测试已通过。后续应用代码修复、数据库操作或生产接入均需独立授权，生产提案的 45 个实施任务保持未勾选。

## 证据与验证

- [脱敏机器证据](/Users/minwang/Projects/LightRAG/openspec/changes/add-prod-zero-downtime-delivery/verification/2026-10-21-test-sequential-recovery.json)。
- [此前单 Pod 金丝雀记录](/Users/minwang/Projects/LightRAG/openspec/changes/add-prod-zero-downtime-delivery/test-canary-recovery.md)。
- 新旧 Pod 的脱敏日志、逐阶段完整检查及一次性脚本保存在 `/Users/minwang/Projects/LightRAG/temp/test-sequential-recovery-20261021/`，目录 0700，归档日志 0600；该目录被 Git 忽略，不保证随仓库保存，不要重复运行替换脚本。
- 操作门禁的 14 项离线检查已执行通过；没有修改产品代码，未运行仓库全套测试。业务恢复结论来自实际集群逐实例验收和最终核查。
