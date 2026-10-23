# prod 首发流水线 #76：初始化成功，应用部署失败（已暂停）

## 实际执行

- 正式tag：`v1.5.9`；commit：`18df4f5c389d8f5e5fbd258f5f42316a4ec06421`。
- [流水线 #76](https://woodpecker.f123.pub/repos/57/pipeline/76)：源码归档、镜像构建、revision/platform/digest及pre-deploy均成功；deploy-prod于2026-10-23 21:54:58（北京时间）失败。
- 实际镜像digest：`sha256:496b46a29266d62895842d09d7ce132be9c4a326699e32095467bebc13b8d4db`。
- prod初始化Job `lightrag-initial-1fb1bdc91acdac1c78cc`，Pod `lightrag-initial-1fb1bdc91acdac1c78cc-dqzxw`；实际Pod退出码0、restartCount=0，21:54:35完成，termination result为`INITIALIZED/epoch=1`。

## 失败后只读核对

初始化Job模板、Pod owner UID、原tag/commit/digest、termination result全部字段、批准settings/profile/plan/runtime hash均一致；不是初始化失败。运行Secret `lightrag-blue-e5da9efed3c2cfbb4ad61420`于21:54:55已创建，immutable且全部字段与批准runtime逐项相同，未输出其值。

后续运行ConfigMap `lightrag-blue-config-a0ca635575278851786b6064`、Deployment `lightrag-blue`、Service/Ingress `lightrag`均未创建。因此**prod应用尚未部署成功**，没有4个Ready Pod，不勾13.6或原上线/性能任务。既有prod Deployment/Service/Ingress的UID/spec未变；未访问原test或两个候选。

失败定位在初始化成功之后、运行Secret落地后至运行ConfigMap/Deployment建立前的CI编排。当前CI日志仅保留`ProdConfigError`，脚本统一捕获并丢弃kubectl底层原因，不能据此断言Forbidden、资源参数错误或网络超时。namespace配额只读核对无耗尽证据。15秒GET+POST共享预算可能不足，与资源创建时序相符，**仅是待确认假设，不是已证实根因**。

## 已完成的本地诊断修复

针对已证实的“底层错误被统一隐藏”缺陷，增加只含固定event/operation/resource-kind/reason/exit-code/timeout/elapsed/outcome的结构化失败日志。服务器原因只允许标准错误码白名单，客户端超时单独标记；不输出请求body、Secret、命令参数、stdout或原始stderr。已受理但ACK未知仍标unknown，不改变停止/超时/迁移或恢复行为。

新增两项超时/Forbidden测试，2 RED→2 GREEN，断言合成凭据不出现在任何输出。定向`tests/ci tests/provisioning tests/distributed tests/api/config` **680 passed /146 skipped /2既有warning**（5.17s）；Ruff/format及diff检查通过。该修复只在本地/后续源码中，不冒充已经修复旧tag的运行器或实际K8s失败。

## 停止边界及后续

未重试流水线、迁移、enroll或初始化Job；未删除或修改成功Job、execution claim、账本和已创建Secret；没有recover/rollback/新tag/CLB或EO调用。原现场保留。当前58/71与原12真实验收不变，13.6仍未完成。

后续必须明确采用复用成功初始化回执的“仅续部署应用”步骤；仍绑定原release身份、digest与运行配置，不重新构建/覆盖原tag，不重新apply/verify/register/enroll。普通整条pipeline start会重新执行构建，并不能保证仍是同一digest，不作为默认恢复动作。真实续部署前需要限定授权范围并完成对应本地验证；失败继续停止，不借此规避产物或目标约束。
