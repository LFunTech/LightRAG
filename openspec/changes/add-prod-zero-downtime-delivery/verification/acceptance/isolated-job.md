# 隔离作业接入单（不可执行草案）

**修复候选最新结果（2026-10-23）**：用户在上轮探针暂停后再次授权恢复；全部预检通过后已在全新namespace/数据域部署1×1修复候选，完成S3单文档processed、6次完整RAG/最多2并发和304.599秒只读观察。原test与旧故障现场保持；未复现503，但Embedding/冷检索样本超目标、3次流式usage未知，性能/计量未验收。见[复测报告](../2026-10-23-fixed-candidate-retest.md)。本段更新实际状态，不使下文正式operator/4+4/CLB/容量/生产草案获得执行批准，不回填正式未知参数，不授权旧现场恢复、清理或追加真实请求。

**最新现场（2026-10-23）**：用户进一步授权先建独立单实例、由执行者选择namespace和随机Secret。已创建test候选，复用既有服务，不执行4+4；启动因日志子目录缺失失败，异常即暂停，未开始业务或性能测试。见[单Pod现场](../2026-10-23-single-pod-attempt.md)。下文未部署/不可执行描述属于原准备阶段，不覆盖此后授权与实际资源；正式蓝绿/容量/生产门禁不因单实例尝试而解除。

随后经两次明确修正授权，候选已启动并执行1份合成文档的S3上传/读回/complete受理；后台入库协调503，已暂停，保留新scope的orphan/claim与未完成文档。查询/并发及4+4均未执行，见[最新停止记录](../2026-10-23-single-pod-ingestion-failure.md)。不得重放本run或把无pending mutation当作可直接重启/recover的依据。

本单只描述未来获准作业所需条件，**本轮不创建Job/namespace/Secret、不拉取或上传镜像、不连接任何集群**。所有目标、身份、资源、时间和审批在[参数底稿](parameters.json)为`null`或false；不提供带默认值的可apply manifest，不复用现有test五套实例或prod资源。

2026-10-23授权更新：用户已批准test只读预检及条件满足后的独立namespace/数据域候选部署与低负载功能、并发、4+4验证；以上“本轮不连接”描述的是本单初版，并非否认后续明确授权。当前已完成原10 Pod业务只读点检，尚未部署候选；真实qcloud/CLB入口与完整隔离门禁未核验，先暂停并确认测试路径，见[预检报告](../2026-10-23-test-preflight.md)。不放宽原test/prod/Git边界，不把低载授权当1000 QPS、破坏性故障、清理或整体参数批准。

用户随后选择test分层、CLB另验，后续状态以[分层准备记录及范围纠正](../2026-10-23-layered-test-preparation.md)为准。默认复用现有test PG/图/S3服务，候选应用独立namespace，业务数据通过经核验的数据域和权限隔离；不复用或变更原五套test应用实例，不自动重建后端组件。此前MinIO镜像阻断已撤回，不要求补镜像。已有本地overlay只是可选交付准备，不验证运行依赖/实际挂载；后文正式生产operator/CLB权限及产物链合同不因test分层而改变。

## 角色和最小权限边界

| 角色 | 必需能力 | 禁止/独立授权 |
| --- | --- | --- |
| 隔离资源/seed维护者 | 复用现有test PG/pgvector、HugeGraph、S3，在获准独立数据域准备必要schema/enroll/合成fixture，候选应用/RWX独立 | 不改原test业务数据/配置，不自动重建组件；DDL/enroll/seed按实际作用域核验批准，不调用test初始化/recover脚本，不隐含prod接入 |
| 应用Blue/Green | 原受限runtime数据库权限、专用模型/S3/图数据域，固定4×1与同scope/profile | 不接收operator DSN/CLB key/K8s token，不改release控制表或扩大特权，不靠slot建立两套协调身份 |
| 控制/混合演练operator Job | 原delivery_session的session锁、operator store、原K8s/Transport能力及受限CLB只读观测；业务动作仍用原actor权限 | 不把运维身份当业务授权；无权限创建cluster RBAC、强删Pod/Job、清owner/锁或recover。原foundation按批准RBAC单独安装 |
| 只读observer | 逐Pod认证health/release/ready、批准的云/PG/图/S3聚合；仅目标namespace及必要UID核验 | 不查SQL正文/对象正文、不做全表/全图扫描、不扩大DB权限、不修改采样配置给服务加负担 |
| read load作业 | 已批准入口和专用业务actor，按request/inflight/响应/时长预算发完整RAG | 不持operator权限、不访问其他环境，不擅自追加写/调用预算 |

物理共享服务不能仅凭namespace不同证明安全；需要实际ACL、独立数据域和资源/额度非干扰证据。不足时使用独立服务/账户并重新批准，不能通过压测影响test/prod来获取证据。当前没有批准任何新云资源或费用。

## 不可执行的Job规格约束

- 原控制路径使用`Kubernetes.in_cluster`，只在批准的集群内operator作业运行，不增加本机kubeconfig或test凭据兜底。实际cluster/namespace与两个UID必须绑定计划。
- 独立namespace/scope、ServiceAccount、网络出口、专用credential引用、输入/输出卷、CPU/内存、运行窗口/联系人全部待填；运行前需只读核验目标和最小权限。
- operator调用固定镜像digest与严格交付identity；`scripts/performance`不在普通runtime wheel/镜像中，作业须只读挂载同revision工具源码，并保存源码manifest SHA与包/镜像来源证明。不临时下载任意脚本，不将dirty工作区伪装成已验证commit。
- 实际源码/镜像交付若需要commit、push、tag、registry/COS上传或触发CI，逐项另取授权；本包不建立另一条绕过原产物链的生产通道。
- 控制作业采用单runner、`restartPolicy=Never`、`backoffLimit=0`，不让Job失败触发自动命令重放。无自动TTL删除现场，不设置用于强杀控制/业务进程的自动deadline；客户端请求和原控制调用仍有自己的有限预算。
- 私有输入/输出目录0700、文件0600，拒绝覆盖同run journal。证据卷需经核验可跨作业失联保留，实际位置/容量/保留期留空；空`emptyDir`不能被当作持久审计保证。
- Secret只在独立批准后以受限引用注入；数据actor、runtime、operator、CLB权限分开。禁止读取/source `.env`、`.secrets/test.secrets`、`.secrets/prod.secrets`，禁止argv/日志打印凭据。不为“准备”复制真实Secret到文档或测试fixture。
- 普通Pod、operator、observer和负载机的资源/连接/模型调用都计入总预算；8个应用Pod之外的Job和故障替换预留另计。无自动HPA/扩容、无缩Blue腾空间。

## 执行前签署项（目前全部未满足/未请求）

| 门禁 | 所需证据 | 当前证据/批准 |
| --- | --- | --- |
| 参数/停止方案 | 完整底稿、SLO、全部预算关系、阈值/时间窗、尾部消费余量和决策人 | |
| 隔离和入口 | cluster/namespace UID、PG/图/S3/RWX域、ACL/非干扰、qcloud实际HTTPS/直连profile/证书/传播与连接保持计划 | |
| 协议和数据 | 全部writer名单/兼容批准、verify-only基线、实际seed manifest、无未知owner/fence/待处理状态；必要计量可靠性 | |
| 身份和产物 | source/工具manifest、镜像digest/架构/revision、Blue/Green config/profile、初始epoch；真实来源而非手写passed | |
| 凭据与权限 | 仅按批准scope引用test凭据或受限test身份，不覆盖/扩大原身份权限、不使用prod凭据；operator/CLB权限按对应验收单独核验 | |
| 观测与质量 | 逐Pod和外部只读来源、负载机能力、时钟误差、golden与人工评审人、全部入口补用例计划 | |
| 破坏性/故障范围 | 只允许一次性fixture，具体Pod/数据/故障方法、禁止项与人工处置范围 | |
| 保留与清理 | 私有持久输出、保留期、责任人、后续清理独立审批；禁止失败后自动销毁 | |

## 分段作业顺序

1. 现在仅本地校验底稿/链接/入口覆盖，不生成可执行负载或Job。
2. 获准后先独立只读核验资源、权限和入口；该授权不包含seed、DDL、部署、发压。
3. 再按精确范围批准provision/初始化/enroll/seed和Blue基线，建立未变更配置与业务空闲证据；有积压先暂停，不因启动polling意外处理未知任务。
4. 验证源码/镜像/plan/golden/output身份后，先C03低负载正常连续性，再专项故障/拒绝用例。各类H/S/B/I入口缺适配需先补本地用例，不由任意shell/SQL写替代。
5. 观测齐备后单独批准C01/C02/C04的阶梯/持续容量负载；确认1Pod基线不会缩减任何真实test/prod。各单元重新记录资源、模型总额度和质量真值。
6. 每个run使用新输出目录/明确owner/epoch；失败遵循[停止预案](stop-plan.md)，不自动重试、恢复或删除作业。清理、条件中止/回退和扩大测试范围分别审批。

正式生产gate、test流水线和环境在以上所有隔离工作中均保持原样。隔离预演通过也不等于9.2/9.3授权；prod接入/Secret/初始化及正式发布仍分别确认。
