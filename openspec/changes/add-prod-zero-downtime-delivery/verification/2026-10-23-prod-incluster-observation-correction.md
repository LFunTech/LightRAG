# prod 预检观察点与工作卷范围纠正（2026-10-23）

本记录补充并纠正[上一份只读报告](2026-10-23-prod-readonly-preflight.md)的执行方法和前置条件解释，不覆盖历史结果。用户明确要求内网 PG、图服务和 S3 从目标 TKE 内手动校验，入口由 Kubernetes qcloud Ingress 管理。仍不部署应用、不切流、不发压、不导入密钥，不改变原 test、两个候选或 Git 发布状态。

## 1. 正确的观察位置与当前实际进度

- 本机到 PG/图私网超时只代表本机路径未通，不构成服务故障或 TKE 内不可达的结论。此前本机 S3 HEAD/List 成功也不算 S3 内网链路通过；后续必须从目标 TKE 验证实际 DNS、路由、TLS、认证与只读访问，不开放公网、不重建基础服务。
- 本轮只读确认目标仍为 `cls-px392o6p`，`kube-system` UID 为 `83ba4335-60be-495e-ad3d-9d4fed2567c6`。所有本轮 kubectl 均显式选择 prod，并将客户端 cache 放在仓库私有临时目录，不改全局 context。
- 经事先说明，仅在现有 `eduplus/ep-search-79b8ddbd8b-pwq7l` 容器执行一次固定 `command -v` 能力检查。只有 `/usr/bin/openssl`；没有 `python3`、`python`、`psql`、`curl` 或 `node`。未安装依赖、复制文件、读取该业务配置或凭据，也未向 PG/图/S3 发送请求。
- 本次 exec 前后对象 UID 均为 `1750fe99-ddbc-420d-96f6-3407f4e13fbd`，Running、Ready、restart=0。此相等只适用于本次检查前后，不把上一次缓存对象的身份沿用到现在。
- 工具不足后停止，不继续遍历其他业务 Pod 执行，不创建诊断 Pod/Job 或注入 ephemeral container。**PG/图/S3 内网认证与权限校验仍未完成**；需要指定已有具备客户端的运维入口，若无现成入口，临时诊断 Pod 需单独授权。凭据不得放入命令参数、输出、远端文件或擅自导入 Secret。

## 2. Ingress 管理入口，观察完成不等于修改 CLB

北京时间 15:40:29 后的只读 GET 取得两个现有业务 qcloud Ingress：

| namespace / Ingress | generation | 控制器回写条件 | status.observedGeneration |
| --- | --- | --- | --- |
| ep-study-mate-prod / ep-study-mate | 5 | Ready=True，reason=Success | 无 |
| eduplus / eduplus-backend-ingress | 4 | Ready=True，reason=Success | 无 |

两者均存在 loadBalancer status 和 tke-service-config annotation。本轮仅投影条件的 type/status/reason/时间等非敏感字段；不读取 last-applied、证书、Secret 或业务配置正文。条件最后变化时间均为 `2026-10-22T08:11:24Z`，条件本身也未见 observedGeneration。

- 用户所述管理路径正确：声明 Ingress/Service，由 qcloud 控制器管理 CLB；不应人工维护后端，不应把本机缺 CLB API 权限说成创建或使用 Ingress 的普遍前置条件。
- 当前发布器实现以 Service selector CAS 发起路由变化，以只读 `DescribeListeners/Targets/TargetHealth` 观察真实后端和健康状态，不直接修改 CLB。之前的 UnauthorizedOperation 是**该观测实现的权限缺口**，不是控制器未工作或入口不可用的证据。
- 已有业务的静态 Ready 条件既没有本次目标 LightRAG 路由，也没有逐候选 Pod 身份/健康及切流代际回执，不能替代真实切流完成证据。EndpointSlice 也只证明集群内端点，不独自证明外部 CLB 已收敛。
- 如果后续改为仅 Kubernetes 观测，须验证控制器提供的实际回执与真实入口探针能覆盖上述证据后再修改门禁；本轮不改发布器、不放宽旧 Pod 退休条件，也不在别的业务路由上试切流。

## 3. volume 的用途与 RWX 纠偏

以下是当前本地候选源码的实际路径，不冒充生产已部署：

| volume / 挂载点 | 当前用途 | 是否主数据存储 |
| --- | --- | --- |
| working / `/app/data` | `WORKING_DIR=/app/data/rag_storage`、`INPUT_DIR=/app/data/inputs`，原件下载 scratch、解析工作目录及本地 sidecar；如 LOG_DIR 指向该目录，也承载文件日志 | 在 PG/HugeGraph/S3 profile 下不是文档原件或结构化主数据的存储 |
| tmp / `/tmp` | Pod 本地普通临时文件，当前为 emptyDir | 否 |
| release / `/app/release` | 只读 ConfigMap 内的发布身份 | 否，不是共享工作盘 |

源码证据：`lightrag_delivery/render.py::render_slot/render_foundation`；`lightrag/pipeline.py::_prepare_object_source_for_parse/_upload_object_sidecar_artifacts`；`lightrag/parser/base.py::ParseContext.sidecar_blocks_path`。

- 对象入库路径先从 S3 下载原件供解析，解析后将本地 sidecar 上传 S3，full_docs 中写入 s3:// 引用；复用解析支持把该引用下载到本地 scratch。PG/pgvector、图服务和 S3 承担该 profile 的持久化职责，不因解析需要本地路径而改回本地文件后端。
- 只读根文件系统下需要可写 scratch，**可写不等于必须共享或持久化**。当前 renderer 为全部蓝绿 Pod 绑定同一个 RWX `lightrag-working` PVC，是现有模板选择；仅此不足以证明 S3 模式的业务必须新增 CFS/NFS。
- 因此撤回“先提供 RWX 存储就是必需业务前置”的解释。准确状态是：**现有模板与目标存储方案仍需对齐**，本轮不新增基础存储，也不擅自移除 PVC。
- 按 Pod 独立 emptyDir 是应评估的替代，不是本轮已实现/批准方案。移除共享卷前须覆盖跨 Pod 解析复用、重试/重建恢复、上传尚未持久化时的中间态、所有启用的本地文件入口、日志可写路径以及 ephemeral-storage 限额/清理；不能仅凭正常 S3 入库成功就宣称共享依赖已消除。

## 4. 状态与验证

本轮只新增观察证据与范围纠正，没有修改应用、流水线、存储模板、运行配置或正式参数。不勾真实功能、4+4、性能或生产验收任务；仍为 41/53 完成、12 待办、57 场景，196 个未知参数叶子及 12 类整体批准不变。低载写、PUT/DELETE 和 1000 QPS 均未执行。

原始脱敏投影和固定 GET 脚本保留在 `temp/prod-incluster-readonly-20261023-01/`；该目录不是交付产物或生产完成凭证。文档核验不冒充后端测试，不复用历史单测通过数作为内网验收。

本地验证：`openspec validate add-prod-zero-downtime-delivery --strict`、`git diff --check` 通过；250 个相对链接存在。1619 个既有非敏感文件 hash 对比仅 tasks、ledger 和旧报告的纠正入口变化，另新增本报告；源码、测试、流水线、部署模板、正式参数与 HEAD/master 均不变，prod gate 仍关闭。未知参数统计排除审批元数据中的 24 个空日期/引用字段，196 个参数未知和 12 类 false 批准保持原样；本次只读授权单独记录，不受整体表单状态否定。
