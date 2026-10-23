# prod 诊断镜像可拉取，启动等待超时后已清理（2026-10-23）

用户明确要求执行者自行寻找可拉取镜像或复用已有 imagePullSecret，不导入新密钥；按原临时诊断 Pod 授权继续，只读验证 PG、图和 S3，结束后删除，异常立即暂停。此前匿名 manifest 401 记录保留，不能继续把“需要用户提供镜像”当作前置。

## 1. 找到并实际验证的拉取路径

- 固定目标仍为 TKE `cls-px392o6p`，只读核对 `kube-system` UID。只投影 Pod 镜像和 imagePullSecret 引用名称，未读取 Secret payload；自行选中已有 `prod/lfun` 引用，未创建或复制密钥。
- 在已有 `prod` namespace 创建唯一诊断 Pod `lightrag-ro-diag-20261023-02`，UID `975d3271-f675-4a7b-92ec-c17b19bea1e2`。namespace UID 为 `bd5be22f-6336-4200-815e-16e79278f213`，原有配额允许增加该 Pod，无匹配其独立标签的 Service selector，不新建 namespace。
- 镜像固定 `docker-hub.f123.pub/lfun/lightrag@sha256:82be927d0d3e8123179587311f2d12c5f8292fd5ac7fe43cd836d8f65a8614ca`，仅复用其 Python 客户端环境；显式覆盖应用入口为 `python -I -B -c 'import time; time.sleep(540)'`，不启动应用服务器，不部署候选源码。
- 单 Pod、restartPolicy=Never、只读根文件系统、非 root、禁提权、drop ALL、无服务端口、无业务卷/Secret/envFrom/ServiceAccount token，仅 `/tmp` 有界 emptyDir。requests 为 50m CPU/128Mi 内存/64Mi ephemeral，limits 为 500m/512Mi/256Mi；activeDeadlineSeconds=600，正常终止宽限10秒。不是应用容量或资源规格批准。

## 2. 实际异常是本地等待阈值，不是镜像不可拉

| 时间（北京时间） | 该 Pod 的事件 |
| --- | --- |
| 16:15:41 | Scheduled、Pulling |
| 16:18:22 | Pulled、Created |
| 16:18:23 | Started、Killing（清理） |
| 16:18:35 | 删除完成并确认对象不存在 |

所有已取得事件均为 Normal；**镜像实际成功拉取，约耗时161秒，容器也启动了**。本地控制脚本预设150秒启动观察截止点，未等到启动即进入异常清理，导致刚启动的容器被删除。这是执行者选择的等待窗口不足，不能归因 registry 凭据、TKE 网络、PG/图/S3 或应用故障；也不能将“客户端包检查未运行”写成依赖导入失败。

收到 TimeoutError 后没有重建或重试；只执行获准的正常删除及最终只读观察。Pod 创建=1、删除=1，无剩余诊断资源；未 force 删除、未创建基础组件或修改已有工作负载。

## 3. 后端校验仍未完成

- 后端凭据清单未读取，未向 Pod 传入凭据；PG、图和 S3 请求均为0。没有真实 SQL、图查询、对象读取或写入，不得标记内网认证/ACL、功能、4+4、SLO 或生产验收通过。
- 已在本地准备只读探针：9条有界系统元数据 SELECT、只读事务/语句与锁超时、无明文回退的 PG TLS；图版本/schema GET 与常量 Gremlin 表达式；S3 HEAD/List(MaxKeys=1)、不读对象正文、不调用已知无关的 ACL/policy 管理 API。语法及 SQL/变更调用约束检查通过，**这些是未执行的准备，不是后端结果**。
- 后续继续时无需用户提供镜像或密钥：已有拉取引用与目标镜像已验证；应调整启动等待/Pod有效期，使其覆盖镜像准备、探针与清理，再在相同只读边界执行。当前依“异常即停”和已作出的不重试承诺保持暂停，不擅自创建第二个 Pod。

## 4. 非干扰证据与记录

最终对 namespace 内原有27个 Pod、3个 Deployment/StatefulSet逐对象比较 UID 与 spec hash，均与本轮创建前一致。未访问原 test 或两个候选；未切流、发压、导入密钥、初始化/恢复数据库或 Git 发布。不存在业务请求重放。

详见[脱敏 JSON](2026-10-23-prod-diagnostic-startup-timeout.json)。固定脚本、无凭据 Pod manifest、前后投影和清理证据位于仓库私有 `temp/prod-diag-20261023-02/`，不是交付产物。主任务仍41/53完成、12未完、57场景；正式参数/批准不改，本轮未改源码、测试、流水线或部署模板，也没有重跑单测。
