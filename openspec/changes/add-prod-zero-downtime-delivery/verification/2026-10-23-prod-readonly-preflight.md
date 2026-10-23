# prod 只读预检：不能部署或切流（2026-10-23）

> 后续范围纠正见[目标 TKE 内网观察与 volume 用途](2026-10-23-prod-incluster-observation-correction.md)：本机网络结果不代表 TKE 内网验收；CLB 由 Ingress 管理，云 API 权限仅是当前观测实现依赖；模板绑定 RWX 不等于业务必须新增共享存储。下文保留为当时检查记录，涉及这些前置条件的解释以纠正记录为准。

本次用户仅授权生产只读核验集群、CLB、实例、PG/图/S3、权限隔离和4+4资源余量；明确禁止部署、切流、发压、导入密钥、Git发布。观测窗口北京时间 **15:06:36–15:20:31**。结论：**只读预检已执行，但部署/切流前置存在阻断，未签署生产可用性或性能。**

实际证据见[脱敏JSON](2026-10-23-prod-readonly-preflight.json)。原始投影/脚本仅保留在仓库私有临时目录`temp/prod-readonly-preflight-20261023-01/`，0700/0600；不保存API原始对象、连接密码、Secret/kubeconfig正文、问题/文档/对象正文或模型响应。

## 1. 集群与现有应用

- 按本机prod专用配置/context固定为TKE `cls-px392o6p`，kube-system UID `83ba4335-60be-495e-ad3d-9d4fed2567c6`；API endpoint仅保存SHA。服务端v1.34.1-tke.4，10/10节点Ready，均amd64、当前无Memory/Disk/PID压力或unschedulable/taint。10节点同一zone，不等于可用区级容灾。
- 列出27 namespace、176 Pod、41 Deployment/StatefulSet、21 Ingress；未发现名称/镜像/环境变量键匹配LightRAG的Pod，也未识别对应namespace/入口或release专用RBAC。**当前观察范围没有可确认的旧Blue，因此不能按已有Blue直接升级切流。** 这不是完整外部writer审计，不排除另一个集群、外部服务或镜像内隐藏SDK调用者。
- 首尾41个工作负载的UID、镜像、replicas一致。未调用任何应用业务接口，未请求原test或两个候选；本结论只说明本轮没有操作它们，不声称已重新验证它们的实时健康。
- 本地kubectl v1.36.1比API高两个minor，超出官方±1支持范围；本轮GET/LIST成功不消除该工具链风险，发布前应使用获准的匹配版本，不升级集群来迎合本机工具。[Kubernetes版本偏差政策](https://kubernetes.io/releases/version-skew-policy/)

## 2. qcloud / CLB

- IngressClass实际为qcloud；发现现有多个业务共用`lb-ao2qcdid`。这只是共享入口发现，不是LightRAG专属路由或HTTPS/direct/长流证据，不能覆盖现有规则。
- 仅使用prod文件中显式`TENCENT_CLOUD_SECRET_ID/KEY`执行一次`DescribeListeners`，region使用清单COS_REGION且与节点sh位置一致，**仅供只读发现，不冒充批准的CLB接入配置**。返回`UnauthorizedOperation`，RequestId已保留。不改用COS密钥兜底，不追加权限，不修改CLB。
- 因此listener/location/certificate、应用域名、真实后端/健康、会话/断流设置、传播时限和控制器版本均未完成核验；没有发送DescribeTargets/TargetHealth或入口业务探针来猜测目标。K8s中无目标Ingress，并不证明整个CLB没有HTTPS。

## 3. PG / HugeGraph

- 只从`.secrets/prod.secrets`解析连接值，未source/执行，未读取test清单。PG/图REST/Gremlin均解析到私网地址，本机对5432/8080/8182各一次有界TCP诊断均超时。PG只读连接尝试失败，图versions请求连接超时，未进入SQL事务或读取图数据。
- **这是本机到内网的观测路径失败，不是PG/图服务故障或Pod到后端不可达的证明。** 不创建诊断Job，不exec/port-forward，不改白名单、路由、VPN或服务配置，不重启数据库。
- PG准备的探针仅允许系统元数据/权限与schema清单、read-only repeatable-read、5s语句/1s锁超时、要求加密且不回退明文；但连接未成功，故server version/pgvector、协调/release schema、账号权限/分离、fence/owner/未决写和连接余量全部未知，不能填0或passed。
- 图版本、七项schema、scope/ACL、已知未决状态与连接/容量均未知。配置REST为HTTP，仅作现状记录；私网访问和鉴权/TLS边界仍需在获准观察位置核验，不因私网IP认为ACL安全。

## 4. S3

- 使用同一prod清单的AWS access/secret，显式S3_ENDPOINT_URL/S3_BUCKET别名仅供本地只读探针，不修改正式解析器或导入Secret。HTTPS HEAD 200；`ListObjectsV2(MaxKeys=1)` 200、返回0、无截断，未保存任何对象键/正文。
- `GetBucketAcl`/`GetBucketPolicy`均403 AccessDenied；`GetBucketLocation`返回403 PathStyleDomainForbidden。后者是区域查询/地址风格兼容性缺口，不冒充整个S3读写失败；没有换URL/密钥重试。
- 只能确认该身份在本次入口可HEAD/列举。**不能确认可写/删除、最小权限、实际prefix隔离或其他principal的可见域；空列举不是IAM隔离证据。** 没有PUT/DELETE、预签名上传、原件读回或CORS写入验证；不调用test对象或生产客户对象。

## 5. 4+4余量与部署模板

- CPU allocatable **55.16 vCPU**、已有requests **20.65**，requests口径余量约**34.51 vCPU /117.44 GiB**；152个非终态Pod，无未调度非终态Pod。按regular/init/原生sidecar和overhead计算，未发现Pod-level资源字段。
- 14个容器未申报CPU/内存request，因此上述余量不是峰值/性能保证；top仅为瞬时使用率，不作为容量依据。当前待部署Pod资源、limits、控制/观测预留、ENI实际空闲IP和namespace quota未批准/验证。
- **仅作资源算例，不是参数批准或模拟调度通过**：若每Pod requests=2vCPU/4GiB，忽略额外约束与安全余量，逐节点可容纳槽位合计11；若4vCPU/8GiB则只有4槽，虽然总量相加看似够，碎片化仍无法同时8Pod。还需按每slot分散约束、挂载、IP及其他工作负载变化验证真实放置。
- 8 Pod连接预算必须核算每Pod业务池+协调/witness/release、operator/observer与其他调用者；当前PG不可达、HugeGraph额度及模型RPM/TPM/并发未确认，**不批准4+4或1000QPS**。
- 当前renderer显式申请`ReadWriteMany`的`lightrag-working` PVC；集群仅发现CBS StorageClass和2份RWO claim，无已核验RWX资源。**S3原件可读不改变现有模板的工作卷合同。** 不能擅自新增CFS/NFS；应选择经批准的RWX接入，或另行设计/回归验证使用独立Pod scratch以移除RWX依赖。本轮不变更模板，不重建PG/图/S3。

## 6. 权限、接入与产物

- 当前检查身份在default namespace显示通配权限，不应直接作为应用/日常发布凭据；未识别LightRAG专用runtime/operator/observer身份和schema权限分离。未枚举/读取集群Secret payload，不靠通配管理员权限替代ACL核验。
- 生产清单存在历史`lightrag_test_api_key`名称，尚未选定显式prod别名映射；这不代表必须读test清单，也不应将名称误读为可以导入test凭据。应用模型/Embedding具体配置、限额、数据域/prefix、备份与首次bootstrap/enroll证据尚未核验。
- 当前最新实现仍在未提交工作区，正式版本/commit/digest与已审批onboarding未绑定；流水线静态gate继续关闭。只读授权不包含提交/推送/tag、Secret导入、schema迁移或初始化。

## 7. 阻断项与下一步顺序

| 优先级 | 阻断 | 最小下一步（仍需按操作范围授权） |
| --- | --- | --- |
| 1 | PG/图不可从当前观察位置核验 | 提供现成、可达的只读运维入口，或明确授权从指定已有观察客户端运行固定探针；不新建组件、不开放公网、不自动exec/建Job |
| 1 | CLB只读API权限不足 | 提供指定LB的受限只读凭据引用或运维签署的DescribeListeners/Targets/Health结果；不要粘贴密钥，不扩大现有COS权限 |
| 1 | LightRAG目标/旧Blue未识别 | 确认首次接入的namespace/域名/数据域，或给出现有外部基线引用；先完成协议/全部writer/备份审计，不凭Ready接管未知writer |
| 2 | 当前工作卷模板无RWX证据 | 明确获准存储方案，或单独评审去除RWX的Pod scratch改造，不因为有S3就绕过挂载门禁 |
| 2 | S3 IAM/prefix与地址风格未验 | 运维提供最小权限策略及确切bucket/prefix/endpoint约定，先只读核验，不用写客户对象来试权限 |
| 2 | 4+4与模型/后端预算未确认 | 确认Pod资源、IP、8Pod+辅助作业、PG/图连接和模型额度/尾部预算；保留未知值，不把算例填入批准参数 |
| 3 | 产物/生产接入未绑定 | 单独批准最小权限接入、必要初始化与不可变产物发布；先不接生产流量的候选低载验证，再决定真实4+4/入口演练和切流 |

**现在不进入部署、切流或任何压测。** 本轮不要求重建PG/图/S3，不修改原test及两个候选，不执行恢复/重放/清理。1000QPS、故障与破坏性测试仍须明确范围/预算另行授权，不能借这次只读授权启动。

## 记录与验证边界

- OpenSpec主任务仍41/53、12待办、57场景。9.2仅有本次只读子步骤实际证据，不勾整项；正式参数/来源表/执行台账及12类整体批准保持原样，本次只读授权在本报告单独记录，不被表单false否定，也不升级为部署授权。
- 只做真实只读检查及文档核验；没有修改应用/测试代码，因此未重跑单测，不借历史1083项本地通过宣称本轮生产验收通过。scoped strict、链接/边界/任务计数核验见JSON最终追加。

本轮最终本地核验：OpenSpec strict与git diff --check通过，246相对文件链接存在，41/53任务与57场景不变。文档更新前1615份非敏感仓库文件的hash对比仅tasks/implementation-ledger两份既有文档变化，另新增本报告/JSON；应用/测试/流水线/base/overlay/参数与HEAD/master均保持。prod gate仍关闭，敏感值扫描和探针AST只读检查通过（准备9条SELECT，实际PG执行0条）。首次本地验证脚本遗漏仓库import路径已在本地纠正，不涉及生产请求/配置；未把该脚本错误归因服务故障。没有重跑单测，也没有宣称生产测试通过。
