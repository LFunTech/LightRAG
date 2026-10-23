# #76 仅应用续部署：实现和本地验收

## 范围

用户明确授权复用#76成功初始化结果、v1.5.9原commit/digest/配置，通过流水线仅续部署应用；不重做migration/register/enroll、不构建镜像/覆盖tag。原test和两个候选不变；K8s只HTTP，无TLS。另一次用户中断后已重新核对：续部署未触发，无新的prod写；随后用户要求继续尽快完成。

新增独立manual/master单步`resume-prod-app.yml`，从同仓库检出CI精确commit的交付工具；不同于原应用commit，单独记executor_commit。审批绑定Job UID/spec hash、唯一Pod UID、immutable输入及claim、termination结果和原digest；只读复核配置/profile及集群UID，不读取operator Secret、不连接PG/图/S3/模型/云入口。已有runtime Secret必须逐字段一致，不创建/改写凭据；仅创建缺失ConfigMap、4×1 Blue和HTTP Service/Ingress，已有同spec只观察、漂移拒绝。

## 验证

- 初始18 RED；修正新输入校验错误与测试夹具后18 GREEN，重新运行所有负例避免早拒绝造成假阳性。
- Pod错误停发3 RED→GREEN、已有Ingress绑定漂移1 RED→GREEN；增加same-spec幂等、错误manual事件/branch、真实CLI旧image commit与新executor commit分离正例，共27项通过。
- 实际prod只读核对原Job/Pod/完整termination/runtime Secret通过；未执行K8s资源create或后端调用。
- 本轮最终相关目录 `tests/ci tests/provisioning tests/distributed tests/api/config`：**717 passed /146 skipped /2既有DeprecationWarning**（4.91s）。这是定向组，不是完整后端suite；本轮不需要真实PG或镜像构建，未将历史PG结果混作本轮。
- stdlib-only runner及真实非敏感approval通过；5文件Ruff/format、6份workflow strict lint、workflow命令sh语法、OpenSpec strict、diff检查通过。
- 原4份workflow/test脚本/base/test overlay及旧迁移SQL/checksum未变；只为新manual入口更新对应workflow合同测试，没有给原test增加触发条件。

## 唯一有效审查及一次修复

此前review在用户中断时取消，无报告，不计为完成审查。唯一有效fresh reviewer基于base `38ed7e3dd`、不可变patch SHA256 `15cb1e8426f4065eeca9b91e1312bd88ac2b2b188d549d1352878c52e9d7fb95`，0 Critical /2 Important /0 Minor。

1. 子集匹配会接受额外args/livenessProbe/hostNetwork等行为漂移。
2. Ready Pod未绑定完整原runtime/spec及Pod→ReplicaSet→Deployment UID链。

一次fix pass：8项可达反例先8 RED，严格行为spec归一化（仅明确API默认值）+创建前再核对+逐Pod完整spec/owner链后8 GREEN；补API默认正例和TOCTOU创建前漂移反例。最终717相关测试通过，无第二轮review。服务分配IP/标准probe/volume/调度默认值有明确规则；未知admission行为不自动接受。

Declined to judge的真实环境/云入口/API/性能/ACL：生产K8s结果仍须实际pipeline证明；其余按用户范围不增加门禁、保持未验收，不把review或K8s就绪冒充通过。

## CI配置准备

仅将既有`lightrag_prod_kubeconfig`的事件由tag扩为manual/tag，**值未改**，未新增Secret；其余所有CI Secret元数据逐项不变，特别是test。manual流程只注入这一既有K8s引用；confirmation为非敏感显式CI变量，审批JSON不含凭据。未导入prod.secrets或operator/模型密码，不改应用配置。真实提交/流水线结果另记。

## Rulings

- 独立manual入口而非整条重跑#76：避免重建镜像、改变digest或重放初始化；代价是维护一个明确受限的续部署入口。
- 请求60秒并受原startup总deadline约束：避免任意15秒组合预算导致误判；代价是单次失败最多等待更久，不据此追认#76底层根因。
- 不增加云入口/容量/ACL门禁：用户明确收窄为K8s交付；代价是K8s成功不能证明公网/业务/容量通过。

真实续部署尚未执行，不能认领prod已部署。未知参数继续保留。
