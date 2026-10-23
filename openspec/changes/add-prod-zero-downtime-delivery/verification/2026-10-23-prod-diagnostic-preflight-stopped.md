# prod 临时诊断 Pod：镜像预检阻断，未创建（2026-10-23）

用户单独授权在目标 TKE 创建一个临时诊断 Pod，检查 PG、图和 S3 的内网只读连接/认证，结束后删除；不重建基础组件、不部署应用、不切流、不发压、不导入密钥，不操作原 test、两个候选或 Git 发布，异常立即暂停。

## 实际执行与停止位置

1. 只读核对 prod `cls-px392o6p`，`kube-system` UID 为 `83ba4335-60be-495e-ad3d-9d4fed2567c6`；拟使用已有 `default` namespace，UID 为 `b623a094-ec55-43b2-b843-37215233006d`，不另建 namespace。只读投影现有 Pod 身份和 Service selector，未接触 Secret payload 或业务配置。
2. 诊断镜像暂选仓库已记录的固定 `docker-hub.f123.pub/lfun/lightrag@sha256:82be927d0d3e8123179587311f2d12c5f8292fd5ac7fe43cd836d8f65a8614ca`，计划只使用 Python 客户端、不运行应用入口。该选择不是业务或部署硬要求，不要求用户专门开放此仓库或发布新镜像。
3. 在本机对该 manifest 做只读可拉取性预检。初次 401 携带标准 Bearer challenge；按其同域 realm/service/repository pull scope 完成匿名鉴权尝试后，token 或 manifest 阶段最终仍返回 401，未获得匿名读取证明。未读取 registry 密码、集群拉取 Secret 或后端凭据，也未转用其他身份兜底。
4. 按异常即暂停要求，**未创建诊断 Pod**，未尝试集群拉取、未安装依赖或执行后端探针。PG/图/S3 内网请求均为 0，不能写作内网验证通过或服务故障；此前本机网络与 S3 结果仍不替代内网验收。

## 清理与下一步

- 本轮创建 Pod=0、删除 Pod=0，无诊断资源需要清理；不把“尚未创建”写成“已部署并清理”。原 test 和两个候选未访问，未改变基础服务、工作负载、Ingress、Secret 或流水线。
- 继续需要一个**目标 TKE 可拉取且具备 PG/S3 客户端的诊断镜像**，或可复用的既有 imagePullSecret namespace/name 引用；只需引用，不需要聊天提供密钥正文，不擅自创建/复制/导入 Secret。也可明确指定现有可用运维客户端。未把所选私有镜像鉴权问题扩大成 PG/图/S3、CLB 或 TKE 网络故障。
- 停止记录见[脱敏 JSON](2026-10-23-prod-diagnostic-preflight-stopped.json)。固定预检脚本及元数据投影仅在仓库 `temp/prod-diag-20261023-01/`，不保存 Bearer token、密码、Secret 或业务正文。
- 本轮只有只读预检与证据文档，未做真实后端测试；任务仍 41/53 完成、12 待办、57 场景，不签署 4+4、SLO 或生产放行，不修改正式未知参数。
