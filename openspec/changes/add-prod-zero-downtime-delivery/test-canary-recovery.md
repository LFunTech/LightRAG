# test 单 Pod 金丝雀恢复记录

后续状态：本文记录最初仅恢复一个 Pod 的阶段结果；用户后来另行授权恢复其余九个实例，最终状态见 [顺序恢复记录](/Users/minwang/Projects/LightRAG/openspec/changes/add-prod-zero-downtime-delivery/test-sequential-recovery.md)。

## 授权与结论

用户明确授权：先只读复核；无待处理任务及协调遗留状态时，仅重建 `lightrag-test-01` 的一个 Pod，直连验收至少 5 分钟。其余 9 个不动，不 recover、不变更 PostgreSQL、不操作 prod。

**本次单 Pod 金丝雀验收通过，未扩大恢复范围。** 新 Pod 当前协调接口正常；其余 9 个旧 Pod 仍为 503，所以不是整个 test 环境恢复完成。

## 执行前只读门禁

- 五个 workspace 均未 fenced；active operation、orphan、claim、资源锁、pending mutation、未完成 retry request/target 均为 0。
- 文档状态只有 processed：test-01 为 5，test-02 为 0，test-03 为 1，test-04 为 3，test-05 为 0。未发现 pending/parsing/analyzing/processing、遗留 preprocessed 或未知状态文档；无待处理任务，因此未触发用户要求的暂停条件。
- 协调数据库和业务数据库新只读连接均成功。直接数据库核查使用只读事务，没有运行 migration、bootstrap、recover、清锁或更改配置。
- 保存五个 Deployment 的 UID/spec 哈希和全部 10 个 Pod 的 UID、镜像、spec 哈希及重启次数；核验目标 Pod → ReplicaSet → Deployment 的控制器链。
- 保存目标镜像、Secret/卷配置引用及版本标识；归档脱敏的旧 Pod stdout 和应用日志。归档后、删除前重新执行同样的只读门禁，确认仍满足条件且身份/spec 未变化。

## 唯一一次变更

- 集群选择：`--kubeconfig /Users/minwang/.kube/test-config --context default`。
- namespace：`lightrag-test-01`。
- 正常删除旧 Pod：`lightrag-7bd8fd4f5-cmtpv`，UID `d4afdf0e-51b4-4479-96c5-765d1e01b785`。仅一次删除，没有 force、缩容或 rollout restart。
- 原 ReplicaSet 自动补建：`lightrag-7bd8fd4f5-77x9r`，UID `da52276a-c9fb-4d4d-be4a-07ff49cf94ea`。
- ReplicaSet 和 Pod template hash 未改变；实际镜像及 imageID 与旧 Pod 一致，digest 为 `sha256:82be927d0d3e8123179587311f2d12c5f8292fd5ac7fe43cd836d8f65a8614ca`。
- 全部 Deployment 的 UID/spec、所引用 Secret 的 UID/resourceVersion 均未改变。新 Pod 重启次数为 0。没有创建新版本镜像或修改配置/权限。

本次通过正常启动重建协调 pool、witness 和 polling，没有修改 `_broken`。应用正常启动及 polling 会写入必要的协调 operation 记录；“未变更 PostgreSQL”指未执行数据库运维、配置、schema、恢复或人工数据变更，不是声称正常服务完全没有数据库写入。

## 直连验收结果

- 通过只绑定 `127.0.0.1` 的 `port-forward pod/lightrag-7bd8fd4f5-77x9r` 访问；没有使用混合新旧 Pod 的 Service 随机验收。
- 观察时间（北京时间）：**2026-10-21 11:28:27 至 2026-10-21 11:33:27**，实测 **300.068 秒**。
- `/documents/pipeline_status`：**31/31 次 HTTP 200**，约每 10 秒采样。
- 每次均 polling_active=true、polling_error=null、paused=false、discovery_stopped=false；generation=1，未 fenced，无 recovery_required、orphan、claim 或待处理 retry。
- 使用 scope/state 索引只读采样活跃 pipeline operation，再按主键确认完成，捕获同一 owner 的 **2 个不同且已 completed 的实际 polling 周期**；不是仅凭后台 Task 对象存活判断。
- 启动日志存在正常 startup complete 信号；未发现 CoordinationUnavailableError、WorkspaceFencedError、witness 丢失、事务失败、Traceback 或 startup failed 等错误信号；现有 PG18 环境下此 Pod 启动和协调接口验收成功，不外推为所有功能的 PG18 兼容测试通过。
- 收尾直测新 Pod 仍为 200、polling_active=true、polling_error=null。

## 收尾边界

- **其余 9 个原 Pod 的 UID、spec、镜像和重启次数全部未变**；收尾逐 Pod 诊断仍均返回 503，没有继续替换。
- 五个 workspace 的 fence/generation 与执行前相同，仍无业务 active operation、orphan、claim、资源锁、pending mutation 或待处理 retry。
- 业务文档数量/状态与执行前一致，没有提交模型查询、上传、入库、删除或手动重试。
- PostgreSQL postmaster 启动时间仍为 UTC `2026-10-18 18:54:21.123654`，本次无数据库重启或其他运维变更；未操作 prod。
- port-forward 已退出；新 Pod 正常保留运行。未删除业务数据、日志归档或协调恢复证据。

## 解释与后续限制

这次结果支持“旧进程协调器已永久失效，而新进程可以重新建立协调链路”的判断，但不能据此确定 10 月 18 日首次失效的外部触发原因。readiness 假绿、首异常记录缺失和失效处置机制均未修复；单 Pod 恢复也不等于集群高可用或零停机发布能力已实现。

继续恢复另外 9 个 Pod，或实施 readiness/生命周期代码变更，均需下一步单独授权。本次不执行这些步骤，也不勾选生产 OpenSpec 的实施任务。

## 证据

- [脱敏机器证据](/Users/minwang/Projects/LightRAG/openspec/changes/add-prod-zero-downtime-delivery/verification/2026-10-21-test-canary.json)。
- 脱敏日志和一次性执行脚本：`/Users/minwang/Projects/LightRAG/temp/test-canary-recovery-20261021/`；目录权限 0700，归档日志权限 0600，属于忽略的临时目录，不保证随 Git 保存。不要重跑该一次性替换脚本。
- 本次只做运维操作和验收，没有修改应用代码、流水线或 manifest；未运行仓库测试套件。执行脚本通过 Python 语法检查，业务结论来自真实集群上述采样及最终审计，不来自语法检查。
