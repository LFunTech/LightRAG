# 当前代码的多 Pod 部署风险实测

日期：2026-10-21，时区 Asia/Shanghai。范围：用户单独授权的代码审查及 test 隔离测试，不是生产部署或完整蓝绿发布验收。

后续状态：用户先后单独授权单 Pod 金丝雀和剩余九个实例的顺序恢复，[最新恢复记录](/Users/minwang/Projects/LightRAG/openspec/changes/add-prod-zero-downtime-delivery/test-sequential-recovery.md)显示最终 10 个 Pod 的协调接口及 polling 均恢复正常。下文“10 个 Pod 全部 503”是恢复前的审计快照；readiness 和失效处置代码尚未修复。

## 结论

**当前代码具备同版本多 Pod 分布式读写的基础，4 Pod × 1 worker 的小规模正常流程可以工作；但不能直接认定满足生产高并发、故障高可用或查询不中断的蓝绿发布要求。**

尤其需要区分“4 个单 worker Pod”和“单 Pod 运行 4 个 Gunicorn worker”。本次验证前者，不外推后者或任意存储组合。受测配置启用了分布式协调，使用 PGKV / PGDocStatus / PGVector / HugeGraph 和 S3；本地锁或文件型存储不能代替这些条件。

发现两类实际问题：

1. 原五套 test 的 10 个 Pod 虽然全部 Ready、`/health` 为 200，但 `/documents/pipeline_status` 全部为 503，错误为 `CoordinationUnavailableError: Coordinator is not available`。停摆迹象早于本轮隔离故障注入：原 scope 的最后一条 pipeline operation 均停在北京时间 **2026-10-18 21:29:24 左右**，早于本轮测试；原 scope 没有 fence 或活跃 operation。本轮没有变更这些 scope，最初失效原因尚未确定，未擅自重启或 recover。
2. 隔离四 Pod 中复现了流式并发槽和 operation 生命周期提前结束、实例清理争抢 workspace 独占，以及失去 operation owner 后查询拒绝但 readiness 仍为绿等生产相关边界。

原五套 test 的 Deployment UID、完整 spec 哈希、Pod UID 和副本数在测试前后相同，10 个 Pod 均无重启。这只能证明结构未被改动，**不能证明既有业务正常或 test 发布验收通过**。

脱敏机器证据：[2026-10-21-test-risk.json](/Users/minwang/Projects/LightRAG/openspec/changes/add-prod-zero-downtime-delivery/verification/2026-10-21-test-risk.json)。

## 测试对象与边界

- 代码基线 HEAD：`beb4d64681c06d393255c577cdf0d988fdeecbfb`，同时包含用户当前未提交的查询计量相关代码；本次未修改这些文件。
- 底座镜像 digest：`sha256:82be927d0d3e8123179587311f2d12c5f8292fd5ac7fe43cd836d8f65a8614ca`。通过只读 ConfigMap 覆盖与镜像不同的 4 个 Python 文件；不是声称该 registry 镜像本身已包含未提交代码。
- 受测容器全部 **191 个包内 Python 文件**与工作区逐文件一致，聚合 SHA-256：`6ab861671f015c8bb89cf0b2051513ab7bc90a119d96178c2f9afc04238ec8b1`。该证据不等于重新构建并验证了完整生产镜像依赖。
- 独立 namespace：`lightrag-risk-20261021`；deployment/workspace：`lightrag_risk_20261021`；S3 prefix：`lightrag/risk-audit-20261021/object-ingestion`。仅使用 test 连接与凭据，未访问 prod，未更改 prod/test 密钥文件或 CI Secret。
- 四个应用 Pod 分布于四个 test worker 节点，每 Pod 1 worker；额外一个确定性模型 HTTP/SSE fixture，不计入应用 Pod。PG、pgvector、HugeGraph、S3 均为真实 test 后端。
- 模型请求使用 fixture 而非真实模型，避免额度消耗及结果随机性。真实模型 QPM/TPM、长上下文生成能力、质量和生产性能均未验证。
- 为定位边界，设置每 Pod query 并发 1、embedding 并发 2、PG 业务池上限 4、HugeGraph 连接上限 1、polling 间隔 2 秒，关闭 LLM cache。不能将此参数和小型请求耗时当作生产推荐值或压测结果；部分请求被刻意 hold。
- 没有创建公网 Ingress；通过绑定 `127.0.0.1` 的临时 port-forward 检查。只对隔离 scope 写测试数据及注入故障，没有对原五套 test 做业务写入、扩缩容、重启或恢复。

## 实测矩阵

| 检查 | 实际结果 | 能证明的边界 |
| --- | --- | --- |
| 四 Pod 启动 | 4/4 Ready，每 Pod 1 worker | 同版本分布式配置能够启动；不是业务持续可用证明 |
| 单 Pod 非流式 query，限额 1，3 请求 | 全部 200；provider 真实最大并发 1 | 非流式单实例限额在此请求路径生效 |
| 四 Pod，各限额 1，各发 1 请求 | 全部 200；provider 最大并发 4；活跃 query operation 4 | 参数不是整个 Kubernetes 部署的总限额 |
| 单 Pod 流式 query，限额 1，4 请求 | 同时保持 4 条 provider 流；流活跃时 query operation 为 0；4 请求均 200 | **复现流生命周期未被并发槽和 operation 完整覆盖** |
| 独立空闲 SDK 实例 finalize，同伴存在未完成 query | 约 1.007 秒后 `CoordinationBusyError`；同伴 query 200 | 实例清理竞争全 workspace 独占。探针刻意设等待 1 秒；未证明默认 30 秒退出或真实 Pod 终止必然失败 |
| 四 Pod 混合入库和查询 | 4 次文本入库全部 processed；并行 8 次 bypass query 全部 200 | 小型并发正常流程可用；不是高并发容量结论 |
| 跨 Pod 真实 S3 上传 | Pod A presign → S3 PUT → Pod D complete 均 200；另一 Pod 查任务为 processed | 会话及对象源不依赖同一接收 Pod |
| 四 Pod 真检索 | 每 Pod 一次 mix query 均 200、有 fixture 答案 | 实际 PG/vector/HugeGraph 检索链路可走通；模型输出质量不在范围内 |
| 五文档归因审计 | 5 processed；实体/关系真实来源均 5；关系 weight=5；tracking、实体/关系锚点各 5；向量存在；pending mutation=0 | 此小样本未发现贡献覆盖丢失，不外推删除、重命名、合并或高压并发正确性 |
| owner 消失 | 独立 SDK 进程持 durable operation、物理业务 mutation 前 `os._exit(73)`；scope fenced、1 orphan、0 pending；四 Pod query 均 503，但 health 均 200、仍 4 Ready | fail-closed 正常生效；**多副本不能消除此类 workspace 级停服，readiness 有假绿** |
| embedding profile | model/dimension 相同，仅 document prefix 不同，manifest hash 仍相同；未初始化业务存储、未发 embedding 请求 | 兼容性指纹不足；没有故意写入混合向量 |
| 原五套 test 只读业务诊断 | 10/10 health 200；10/10 pipeline_status 503；scope 未 fenced、无 active operation | 健康探针不能表示协调依赖和业务能力正常 |

故障注入在保存正常流程证据后单独执行；被强制结束的是额外 SDK 探针进程，不是应用 Pod、节点或实际后端写请求。未执行自动恢复，未把 fence 当作可自动清理的超时锁。

## 风险与代码证据

### R1 / P1：readiness 与实际业务能力脱节，原 test 已出现实际异常

Kustomize readiness 指向 `/health`，但该端点的合同是进程 liveness，不检查 coordinator 可用性、workspace fence 或 polling 是否已停止。隔离故障和既有 test 都证明此差异是真实的，而非仅代码推测。单纯扩到 4 或 8 Pod 不会改变共同依赖失效的后果。

代码：[Deployment:44–51](/Users/minwang/Projects/LightRAG/k8s-deploy/lightrag-kustomize/base/deployment.yaml)、[health:3016–3093](/Users/minwang/Projects/LightRAG/lightrag/api/lightrag_server.py)、[polling:219–229](/Users/minwang/Projects/LightRAG/lightrag/distributed/pipeline.py)、[coordinator:224–274](/Users/minwang/Projects/LightRAG/lightrag/distributed/coordinator.py)。coordinator 的 broken/closed 状态不会在原对象上自动重连，这是明确的 fail-closed 合同。

后续需单独设计低成本的服务能力/readiness 检查及告警，区分可读、可写、standby 和 fence，保留 liveness 的进程语义；**不能把 fence 改成 liveness 失败并依赖无限重启或自动 recover**。原 test 初始失效原因需另行调查，恢复操作须先核查当前 writer/在途状态并取得授权。

### R2 / P1：流式请求绕过实际并发上限，且排空依据提前消失

限流 wrapper 在模型函数返回 async iterator 时即设置结果、释放队列任务；`operation_guard` 同样在返回 iterator 后结束 durable operation，真正的流消费随后才由 HTTP generator 执行。实测限额 1 却有 4 个活跃 provider 流，且协调器认为活跃 query operation 为 0。

代码：[utils.py:1597–1640、1850–1892](/Users/minwang/Projects/LightRAG/lightrag/utils.py)、[operation_guard:282–301](/Users/minwang/Projects/LightRAG/lightrag/distributed/runtime.py)、[流返回:5288–5299](/Users/minwang/Projects/LightRAG/lightrag/lightrag.py)、[API 消费:1130–1140](/Users/minwang/Projects/LightRAG/lightrag/api/routers/query_routes.py)。

后果是模型配额/在途连接失控，以及仅查 operation 无法证明旧实例的流已经排空。修复需覆盖正常耗尽、异常、客户端断开和取消时的完整生命周期。此证据**不证明流结束后必然发生非法 graph mutation 或数据损坏**，也不能把可选 query accounting 当作已有的集群限流器。

### R3 / P1：实例退出依赖 workspace 独占，不适合直接滚动或蓝绿回收

`finalization_guard` 先停止本实例 polling，再以 `exclusive=True` 申请 `finalize`。持续进入的同伴 shared query 会与此独占竞争；取得独占后又会影响同伴准入。实测已复现竞争导致的有限等待拒绝，未实测 Kubernetes 默认退出期限被耗尽或 SIGKILL 后果。

代码：[finalization_guard:487–506](/Users/minwang/Projects/LightRAG/lightrag/distributed/runtime.py)、[默认等待与准入:93、328–388](/Users/minwang/Projects/LightRAG/lightrag/distributed/coordinator.py)。后续须按提案 5.5 区分本实例资源 drain/close 与 workspace 维护独占，不能只改 `strategy` 或加 `preStop sleep`。

### R4 / P2：embedding 配置一致性校验未覆盖实际向量语义

manifest 只记录 embedding model 和 dimension，没有完整覆盖 binding/endpoint、document/query prefix、asymmetric 等影响向量含义的配置。实测不同 document prefix 仍得到同一 manifest hash。因此“能加入同一 workspace”不等于向量配置一定兼容；多 Pod 配置漂移或蓝绿差异可能混用不兼容向量。

代码：[manifest:434–451](/Users/minwang/Projects/LightRAG/lightrag/distributed/runtime.py)、[embedding builder:1079–1142](/Users/minwang/Projects/LightRAG/lightrag/api/lightrag_server.py)。应在生产接入预检中规范化并比对实际语义配置；不能把 credential 本身纳入公开指纹，也不能更改已有 manifest/schema 后不处理兼容性。

### R5 / P2：副本放大共享资源消耗，配置中的 global 不是跨 Pod 总限额

现有 `global_concurrency_limits` 通过 Gunicorn master 的 `multiprocessing.Manager` 在同一实例 worker 间共享，不跨 Kubernetes Pod。角色队列还按 `llm:<role>` 分组。同一模型/账户被 query、keyword、extract 等共同使用时，需要合并预算。四 Pod 实测每 Pod query 限 1 得到总并发 4，不能把配置 1 当作部署总上限；流式请求还叠加 R2。

代码：[共享范围:1534–1553](/Users/minwang/Projects/LightRAG/lightrag/kg/shared_storage.py)、[角色分组:189–193](/Users/minwang/Projects/LightRAG/lightrag/llm_roles.py)、[PG 池默认值:2976–2978](/Users/minwang/Projects/LightRAG/lightrag/kg/postgres_impl.py)、[HugeGraph 池:195](/Users/minwang/Projects/LightRAG/lightrag/kg/hugegraph_client.py)、[协调池/witness:93–99、169–200](/Users/minwang/Projects/LightRAG/lightrag/distributed/coordinator.py)。

按每 Pod 一个 worker、每个实例一个 runtime 估算：若 PG 业务池上限为 P，另有协调池默认 4 与 witness 1，则 N Pod 理论连接预算约为 `N × (P + 5)`，还未包含维护进程及其他调用者。P=50 时，4/8 Pod 分别约 220/440；这是配置上限预算，**不是启动立即占满的实测值**。HugeGraph 默认每客户端池 10 时，4/8 实例池预算为 40/80。实际 HTTP/DB 资源还须核查最终配置和调用者数量。

运行期角色更新仅修改本实例 `_role_llm_states`，没有跨 Pod 广播；生产应使用受控一致配置，不能用一次随机路由的热更新证明全部副本已经同步。上述容量与漂移边界属于接入要求，不是已经发生数据覆盖的证据。

### R6 / P2：空闲 polling 历史持续增长，全量诊断存在可操作性风险

每轮 polling 即使没有待处理文档仍产生 durable operation，结束时改为 completed，不自动归档。原五套 test 的 pipeline operation 累计 **26,878,517 条**。整个共享 `operations` relation（包含其他 scope）的实测表大小为 5,096,701,952 bytes，索引为 1,514,217,472 bytes，总大小 **6,612,336,640 bytes，约 6.16 GiB**。

另外，五个 scope 的新只读 coordinator 完整 `inspect()` 均返回 `CoordinationUnavailableError`。补充的 test-01 分阶段检查显示：新只读 coordinator 初始化约 0.08 秒成功，有限 workspace 读取成功、fenced=false。因此不能将完整诊断失败解释成“数据库此刻无法连接”。`_snapshot` 确实无分页读取该 scope 全部 operation/mutation/历史；在每 scope 五百多万条历史时风险很大，但**本次未确认完整快照失败的具体底层异常，也未证明历史增长导致了 10 月 18 日的原始失效**。

代码：[polling:148–160、219–235](/Users/minwang/Projects/LightRAG/lightrag/distributed/pipeline.py)、[完成记录:415–423、全量快照:653–699](/Users/minwang/Projects/LightRAG/lightrag/distributed/coordinator.py)。默认每秒、无额外处理耗时的理想估算，4/8 Pod 空闲轮询可产生约 345,600/691,200 operation/日；不是本轮吞吐实测。后续需设计有界活跃状态诊断、历史分页与安全保留/归档，不能删除 active/pending、恢复证据或未经审计清历史以求上线。

## 与 OpenSpec 实施任务的关系

- R1 是生产业务验收/待命预检的阻断项，关联 5.1、6.4、7.2、8.3、9.1。原 test 的故障调查与恢复需独立受控处理，不能悄悄修改既有 test 发布合同。
- R2/R3 关联 5.2、5.3、5.5、7.2、7.3，须补完整流生命周期与实例 drain 回归后再做蓝绿。
- R4 关联 1.4、2.4、3.1、9.1 的配置/兼容性接入预检。
- R5/R6 关联 1.4、6.6、8.6、8.7、9.1 的容量、运维和长期运行要求。
- 发布 owner、standby、持久化写闸门/命令队列及实例级安全回收尚未实施；不能把本次多 Pod 正常流程测试视作这些能力已存在。原 **45 个实施任务全部保持未勾选**。

## 本地验证与尚未验证项

实际执行：

```sh
./scripts/test.sh tests/distributed tests/ci tests/setup/test_distributed_chart.py tests/llm/test_global_concurrency_queue.py tests/llm/test_llm_role_runtime.py tests/llm/test_query_accounting.py tests/api/routes/test_query_accounting_routes.py tests/llm/openai_impl/test_query_usage_source.py -q
```

结果：**282 passed，83 skipped，0 failed，8.10 秒**。跳过项不计入真实后端集成通过；上述 test 集群实验是单独证据。未运行全仓库测试或前端检查，本次没有修改产品代码或前端。

尚未验证：真实模型配额/高压流控、代表性生产负载与 P95/P99/SLO、8 个应用 Pod、新旧版本混跑、真实入口切流、缓存开启后的查询附带写完整路径、发布期间持久化写排队和幂等结果、真实应用 Pod/节点死亡、丢失 backend ACK、删除/清空/图合并/重命名在多 Pod 下的破坏性竞争。不能宣称高并发容量或正常发布零停机已经通过。

## 清理与安全边界

- 审计 namespace 已删除，应用、fixture、临时 Secret/ConfigMap、维护 Job 和本地 port-forward 已退出；收尾重新确认 namespace 不存在。
- StorageClass 使用 Retain，留下 Released 的 **2Gi PV `pvc-6bbfb014-83b6-4bd3-92cf-2fa4111ccb4e`**；未删除底层数据。
- 独立审计 workspace 的五文档、图/向量/锚点、S3 源及协调 fence/orphan 历史保留，用于审查；未 recover、清锁或删除恢复归因。后续清理应单独确认保留需要，不复用该 fenced scope 继续测试。
- 原始一次性探针留在 `/Users/minwang/Projects/LightRAG/temp/prod-risk-audit-20261021/`，属于忽略的临时目录，不保证随 Git 保存；持久化 JSON 记录探针哈希和脱敏结果。复测必须使用新 namespace/workspace/S3 prefix，不直接重跑同名初始化脚本。
- 未触发 test/prod 流水线、未提交/push/tag，未同步或展示密钥。新增内容仅为风险文档和脱敏证据，不表示已批准或完成生产实施。
