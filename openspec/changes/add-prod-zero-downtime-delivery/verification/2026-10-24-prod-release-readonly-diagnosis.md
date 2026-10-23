# prod现有故障Pod只读诊断：首异常和注册冲突链路

## 执行边界及证据

用户明确批准仅读取现有故障Pod工作盘首日志、在Pod内只读事务查询该workspace的release登记。北京时间2026-10-24 **00:29:35**，复核原4个Pod UID及prod namespace UID后，利用`lightrag-blue-74576c58f9-tx6bh`自然运行窗口做一次`python -B`诊断；没有为诊断创建、启动、重启、删除或替换Pod，不访问原test和两个候选。

日志只读取`/app/data/logs/lightrag.log`，无文件写入。PG默认只读、repeatable-read只读事务，确认`transaction_read_only=on`；显式SELECT **7条**，不调用任何release业务函数/DDL/DML/enroll/recover。脚本原硬编码计数误记8，已按实际7个SELECT调用点修正证据说明；原始私有结果保留，不重跑数据库请求掩盖计数错误。没有发起新的S3/图/模型请求，不读取对象正文或输出凭据。

脱敏结果见[JSON证据](2026-10-24-prod-release-readonly-diagnosis.json)；私有原始输出、探针、前后Pod快照及计数更正保存在忽略目录`temp/prod-readonly-diagnosis-20261024/`，不提交私有输入/日志。

## 1. 确认的首次失败：S3 HeadBucket 403

该Pod日志首条时间为2026-10-23 **23:41:57.864北京时间**；首次异常在**23:42:00.450**：

```text
lifespan → object_store.preflight → client.head_bucket
ClientError: An error occurred (403) when calling the HeadBucket operation: Forbidden
ObjectStoreUnavailableError: object-store bucket preflight failed
Application startup failed. Exiting.
```

因此，后续日志中反复出现的`ReleaseConflict`不是该Pod的首异常。首异常前已有`initialize_storages`成功注册实例，S3预检失败触发lifespan的finally，完成storages关闭。该Pod闭合时间与数据库实例记录`observed_at=23:42:00.440`吻合；日志也明确记录`Successfully finalized 12 storages`。

**只检查了其中一个Pod的持久首日志**，不能将它写成已逐个验证其他3个Pod的首异常。4个登记记录的最终状态则已在同一只读快照中确认。

## 2. 确认的当前注册拒绝原因：已关闭实例身份被复用

| 元数据 | 只读结果 |
| --- | --- |
| scope | `lightrag_prod/lightrag_prod` |
| active/candidate slot | blue / null |
| phase / epoch / gate | IDLE / 1 / open |
| owner | 无 |
| runtime对scope的config/profile | **两项均匹配** |
| instances | 4条，instance_id与原4个Pod UID一致 |
| instance config/profile/slot | 均为原批准hash及blue |
| instance closed/retiring | **全部true/true** |
| release commands / tickets | 快照内均0条 |

代码链路：`ReleaseRuntime.close_instance`在已注册且排空后调用数据库`close_instance`，后者保留记录并标记`closed=true, retiring=true`。Pod容器自动重启不会改变Downward API的Pod UID；`register_instance`按scope/instance主键遇到原记录，并因`i.closed`拒绝。此外新进程的coordinator owner也会改变，但仅`closed=true`已经足够触发拒绝，不需要推测新owner值。

这排除了**当前scope/profile/config不匹配**的分支。不得据此清实例记录、把closed改回false、放松owner保护或重跑migration/enroll；这些是安全保护，不是临时数据库故障。没有检查doc-status/协调器全状态，commands/tickets为0也不等于所有业务队列都为空。

## 3. S3寻址差异：已离线证实，真实403细码仍待核验

对比早先[成功的TKE只读探针](2026-10-23-prod-incluster-readonly-completed.md)和当前应用：

- 早先探针明确指定`Config(s3={"addressing_style":"virtual"})`，HeadBucket曾200。
- 当前`S3_FORCE_PATH_STYLE=false`仅表示代码**不显式配置path**，并不保证SDK使用virtual；`S3ObjectStore._client_kwargs`未指定addressing_style。
- 本机相同SDK版本（boto3/botocore 1.40.61、aioboto3 15.5.0、aiobotocore 2.25.1），使用假凭据、before-send截断及网络发送双重禁止，复现两个实际请求形状：

```text
应用原auto：HEAD https://cos.ap-shanghai.myqcloud.com/lightrag-1302760640
探针virtual：HEAD https://lightrag-1302760640.cos.ap-shanghai.myqcloud.com/
```

离线发送数0；既有批准runtime中的S3 key与当前prod清单两项逐值匹配，只输出比较布尔值。没有换密钥、套用test或访问云API。早先探针与真实应用的寻址方式不一致，是此前连通性检查没覆盖真实启动路径的缺口。

腾讯云[域名合规FAQ](https://cloud.tencent.com/document/faq/436/102489)说明2024-01-01后创建的bucket不支持path-style，该访问方式会返回403。该文档与已观察现象相符，**但本次没有bucket创建时间或403响应细码证据，不能把`PathStyleDomainForbidden`写成已收到的错误码，也不能仅凭旧的200认定当前virtual仍成功**。

已请求最小补充授权：仅在同一现有Pod做最多2次无自动重试HeadBucket对照，原auto与明确virtual各一次；原模式预期失败后只执行已批准的第二个对照，不调用会写对象的`preflight`、不PUT/DELETE。答复前不发送新S3请求。

## 4. 修复顺序建议，不执行

1. 先用上述只读HEAD对照确认真实寻址修复方向，不再重试应用启动碰运气。
2. 若virtual通过，本地修复应用S3寻址，使prod行为与已验证模式一致，保留test原默认；补真实SDK离线请求形状回归。不能用更换密码、扩权或关闭预检掩盖路径错误。
3. 单独审阅`/api/*`默认免鉴权风险和收紧方案。原配置hash绑定不能通过临时env偷偷绕开；任何新配置、镜像或首发例外绑定变更必须明确授权。
4. 通过流水线正常替换这4个失败实例时使用新Pod UID，保留旧closed记录；但仅换Pod而不修S3将重现首异常，不能先重建验证。仍不重跑成功初始化，不清锁/recover或重放业务。
5. 重新确认实际4/4 Ready后才创建HTTP入口，再按另行授权做认证/功能验收；不把K8s就绪当1000QPS或4+4通过。普通容器重启对实例incarnation身份的长期支持需独立审阅，不能简单删除`i.closed`/owner条件。

本轮仅诊断和文档同步，没有应用代码/配置/数据库修复、部署重试、Git提交推送或tag操作。prod仍不可用；OpenSpec保持60/74、14项未完、73场景，13.6/14.3未勾选。
