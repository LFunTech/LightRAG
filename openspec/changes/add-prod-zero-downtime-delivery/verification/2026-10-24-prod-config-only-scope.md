# 最新生产部署约束：不改应用代码，仅流水线/配置微调

## 当前有效约束

用户2026-10-24明确要求：

1. 必须以现有test流水线为基础微调，不能大改。
2. 不允许对应用代码做任何调整。
3. prod Kubernetes Ingress必须为qcloud，CLB准确为`lb-ao2qcdid`。

同时保留：prod/test密钥和配置独立，原test流水线/实例/数据及两候选不变；K8s仅HTTP、禁止应用TLS；不新增流水线S3/CLB/EdgeOne校验，不重跑已经成功的初始化/migration/enroll，不recover或清旧closed实例记录。

此前“修改S3/认证代码并发布新版本”的范围被本指令覆盖。执行者已只撤回自己尚未提交的`lightrag/api/config.py`、`lightrag/object_storage/__init__.py`、对应新增测试及临时tag筛选改动，完整patch保留在忽略目录，未使用reset/clean或丢弃其他用户修改。撤回后`git diff -- lightrag tests`为空；后续仅调整既有manual工作流中的部署配置分支，`.playwright-cli/`未动。未创建v1.5.10 tag、启动构建、执行登记更新或重启/替换prod；必要的配置/文档提交推送按本次授权执行。

## 已完成的两次Pod内HEAD对照

同一原故障Pod于北京时间**2026-10-24 01:00:37–01:00:38**完成：

| 寻址方式 | HEAD结果 | 次数 |
| --- | --- | --- |
| 原应用auto，实际path-style | 403 | 1 |
| 显式virtual-hosted-style | 200 | 1 |

相同凭据/endpoint/region，自动重试关闭、总HTTP发送2、PUT/DELETE为0，无配置/数据库改动。只测HeadBucket，不能据此认领对象写删、完整S3权限或业务功能；403细码仅`403`，不伪造响应体。见[实际结果](2026-10-24-prod-s3-head-comparison.json)。这次是已批准的Pod内手动检查，不转为流水线门禁。

## 不改代码的配置路径：本地已证明

使用**git HEAD原版源码**（而非待撤回的修复代码）、相同SDK、假凭据并阻止SDK网络发送，证明：

```ini
[default]
region = ap-shanghai
s3 =
    addressing_style = virtual
```

以只读ConfigMap挂载该配置、通过`AWS_CONFIG_FILE`指定其路径后，原版S3适配器实际构造：

```text
HEAD https://lightrag-1302760640.cos.ap-shanghai.myqcloud.com/
```

原版API解析器也接受显式`WHITELIST_PATHS=/health`，不需要修改认证代码。配置文件不包含密钥，生产凭据继续单独Secret引用。配置文件内容/路径和环境变量必须计入最终配置身份，不能通过隐藏挂载绕过hash绑定。

原prod输入本地渲染已核对：`ingressClassName=qcloud`、`kubernetes.io/ingress.existLbId=lb-ao2qcdid`、域名`llm-rag.eplus2.cn`、HTTP后端9621、无`spec.tls`。首次断言错误地预期Service targetPort为名称http，实际是数值9621，已修正本地断言，未改变生产配置或掩盖真实失败。

原版配置路径的两项离线检查通过，网络发送0；不代表该挂载已落地或prod已Ready。证据保存在忽略目录`temp/prod-readonly-diagnosis-20261024/configuration-only-offline-proof.json`和`config-only-ingress-proof.json`。

## 不再执行旧版本登记草案

此前用户批准的v1.5.10/f684…单行更新**尚未执行**。其前提是修改应用代码后的版本/首发批准组合，不是当前仅配置方案。原草案保留审计并标记`SUPERSEDED_DO_NOT_EXECUTE`；最终配置确定后重新算真实hash，如仍需登记更新，再列准确差异报批。不得沿用旧批准改成不同值，不重跑初始化来绕过。

## 最小部署增量与本地验收

保留原test源码归档、构建、产物验证及部署文件不变，直接复用#76产出的v1.5.9镜像digest与成功初始化。只在既有`resume-prod-app.yml`的manual/master续部署步骤增加一次性配置分支：采用test已有的`kubectl apply/patch → rollout status`方式，不重新构建、不发tag，不重跑初始化/migration/enroll/recover。

静态配置在`k8s-deploy/lightrag-kustomize/overlays/prod/config-only-20261024/`：

- `configmap.json`：新的immutable非敏感ConfigMap，原identity只改config_hash，附AWS SDK virtual配置。镜像仍为`sha256:496b46a29266d62895842d09d7ce132be9c4a326699e32095467bebc13b8d4db`。
- `deployment-patch.json`：以原Deployment UID及完整spec为原子前提，只替换容器env和已有只读release ConfigMap引用；不改镜像/Secret/副本/worker/服务账号/资源/卷/策略。新增环境为AWS_CONFIG_FILE、WHITELIST_PATHS、SDK配置SHA256，另更新release config_hash。原4×1保持不变。
- `entry.json`：仅新建prod/lightrag Service及Ingress；采用现有共享CLB入口一致的NodePort与`enable-group=true`，明确qcloud、`lb-ao2qcdid`、`llm-rag.eplus2.cn`和HTTP/9621，没有TLS或证书引用。既有其他业务入口不变。
- `plan.json`：保存原镜像/commit、原失败Pod UID、精确新旧hash和不变的profile/物理manifest。`approval_id`只是拟执行分支标识，不代表登记写入已获准或已经完成。

原Deployment是Recreate、原4Pod均非Ready且实例closed；本次只针对失败的首次上线，不宣称为已有服务提供零停机升级。工作流读取现有Job/Secret的UID与状态而不读Secret内容；确认旧4Pod、无既有入口后才patch，原子spec不符即停。全部GET先独立赋值传播错误，不把超时/403当不存在；无S3、CLB或EdgeOne API调用。4Pod更新/可用且镜像digest正确后才创建入口；任何失败不重试、不自动回滚或重放。

本地验证（2026-10-24）：

| 检查 | 实际结果 |
| --- | --- |
| `./scripts/test.sh tests/ci`（显式venv、禁用dotenv） | 299 passed / 1 skipped，应用及tests无diff |
| 六份现有Woodpecker工作流strict lint | 全部通过 |
| Shell语法及纯mock配置分支 | 14/14；成功、rollout失败、digest错误、错误namespace、原实例已服务、9种GET失败；真实K8s调用0 |
| 读取错误回归RED→GREEN | readiness/Service/Ingress GET exit42原先误放行，修正后立即停止且无后续资源变更 |
| 精确登记操作的全新本地PG | 20/20；含真实main连接设置、只读预检、仅config_hash更新、重复拒绝及14项待办/锁/漂移反例，prod连接0；本地PG已停止 |
| 登记入口回归RED→GREEN | 旧连接只读默认导致25006，修正为仅精确apply模式设可写；无应用源码变更 |
| 静态身份、SDK内容SHA、JSONPatch及Ingress检查 | 完整重算eead…，profile/物理manifest不变，原digest/Secret/4×1/HTTP/无TLS通过 |

独立审查发现0 Critical、2 Important、1 Minor：两项Important均有真实RED→GREEN；Minor通过准确名称输出新入口资源修正。测试只证明本地配置及失败传播，不等于prod已Ready。

## 精确登记更新仍待批准

仅拟更新PG `lightrag_release.scopes` 中scope=`lightrag_prod/lightrag_prod`这一行的`config_hash`：

- 旧：`e5da9efed3c2cfbb4ad614209cf349f7598b3715d5c76b7f525a7dd55ee1a049`
- 新：`eead1664412b7738eeedfc28e229b7e735d02cac80b2457830aa5ef9cea6b1ae`

新值完整绑定原生产配置/原runtime环境及AWS配置内容SHA、路径和认证白名单；profile、manifest、policy（仍v1.5.9原批准）、epoch、slot、旧closed实例和业务数据全部不变。与已撤销f684…草案不同，**尚未执行，须此次精确批准**。操作前同一事务复核原scope、epoch1/IDLE/open/no-owner、原4实例closed/retiring、无fence/待处理文档/operation/claim/锁/队列/未结算mutation；任一不符即停。执行器未知结果不自动重试。

## 当前实际结果

配置与本地检查已准备，登记更新及真实流水线尚未执行。最新只读Deployment复核仍为generation1、原UID/spec、0/4 Ready；流水线仍#77/#76失败，尚无prod可用结论。保持原成功初始化和故障证据，不以本地通过或配置提交冒充prod部署完成；原test与两个候选没有变更。
