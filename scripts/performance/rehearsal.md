# 隔离混合写、4+4 发布和质量工具

仅为 OpenSpec 10.7 的可执行工具；不接入既有 test 流水线、不自动启用 prod。
本轮只跑本地合成/Mock及专用临时 PG 测试，未进行真实发压或集群操作。

## 输入与默认离线用法

从仓库根运行，使用新输出目录：

```sh
.venv/bin/python -m scripts.performance.rehearsal \
  --plan tests/performance/fixtures/mixed-plan.json \
  --scenario tests/performance/fixtures/mixed-scenario.json \
  --output-dir temp/my-offline-rehearsal
```

该命令仅验证输入并生成 `validation.json`，不连接 HTTP/Kubernetes/数据库。
fixtures 是纯合成单测数据，不是已批准的工作负载、生产参数或领域知识真值。
`workload-target.json` 仍缺 SLO/比例/预算，不能用上述合成值填充生产目标。

计划严格要求 `version=2`、`environment=isolated`、唯一 `run_id` / `approval_id`：

- `release`：scope/release_id、namespace 及 namespace/kube-system UID、profile、Blue/Green config hash、两镜像 digest、起始 epoch；稳态4、蓝绿4+4、最终Green4。必须匹配原交付会话，不能改为任意副本数。
- `read_plan`：沿用 [query_load 合同](README.md)，完整显式批准的目标/时长/在途/请求/响应/超时/SLO及隔离证据；profile/config与release一致。混合runner额外要求读请求超时≤600秒、单次持续计划≤24小时，故障可合作停止长调度等待。
- `usage`：显式`poll_seconds/sample_timeout_seconds/transition_timeout_seconds/max_samples/max_evidence_bytes`，全部必填、无默认；对应正式参数`collection.dynamic_usage`当前仍null。旧v1计划拒绝，不能无计量运行混合写。
- `limits`：HTTP整体超时≤600秒、轮询间隔小于超时、单响应≤16MiB、控制数据HTTP调用≤100000、并发写≤16；只读拓扑收敛每phase最多100次且受时间预算限制。独立查询负载使用 `read_plan` 自己的请求/缓冲预算，**不计入** `max_calls`，K8s/CLB调用使用原交付预算，须合并核算。
- `actions`：每个都有id/phase/actor/typed payload；只允许下表四种。必须覆盖闭闸写和交权后写。依赖只能引用此前声明、同run的已确认父动作；闭闸窗口父子依赖在连接/取得owner前拒绝，避免等待环。
- `checkpoints`：id/phase、`after`依赖动作、golden case IDs。闭闸时不能等待需要交权的写；必须使用全部case且包含最终检查点，case/依赖列表不能重复。可为变更前后定义不同问题、事实/引用/chunk/权限预期，不将旧版本真值套用于新版本。
- `scenario`：`quality` golden、`load_queries`原只读数据集、`long_stream_case`成功case ID。多轮显式提供 user/assistant history。

| kind | payload及完成条件 |
| --- | --- |
| `insert_text` | `text,file_source`；source必须为实际basename `perf-<run_id>-<safe-name>.txt`（ASCII字母/数字/下划线/点/横线，不含路径或parser hint）。原API会规范化来源，run身份必须留在basename。受理后按track核对source/id，直到processed；failed/未知不能当完成 |
| `create_entity` | `name,description`；name以 `PERF_<RUN_ID大写>_` 开头；成功响应后通过原graphs接口读回描述 |
| `edit_entity` | `create_action,description`；只能编辑本run已确认创建的实体，不rename/merge，读回描述 |
| `delete_document` | `insert_action`；只能删除本run已确认insert的确切doc/source，删除前重新核验；`delete_file=false,delete_llm_cache=false`，受理后等待原track查无文档 |

没有任意URL、SQL写、shell/hook、clear/recover或清缓存动作。创建的图实体/残留文档不会自动清理；演练失败后保留证据，由独立批准的处置决定下一步。

## 未来真实执行的额外前置（本轮未授权）

必须先单独批准隔离资源、数据ACL、全部writer协议基线、必要计量可靠性、预算/停止方案、质量真值和写入/发布演练；不得使用现有test/prod。
仅字符串 `isolated`/hash不能证明隔离。工具重用原受限operator会话、数据库session锁、权限预检与ProductionTransport，完整 qcloud 配置、入口只读观测权限、固定镜像与两slot配置均须可用。

执行需要同时提供 `--run --confirm-isolated-target --confirm-write-rehearsal` 和原生 `--delivery-config/--delivery-identity/--delivery-environment/--delivery-image` 四项；默认不提供。
专用隔离作业通过 `LIGHTRAG_PERF_BASE_URL` 和 `LIGHTRAG_PERF_ACTOR_KEYS`（actor到API key的JSON映射）注入凭据；operator/CLB使用原交付会话的专用环境变量，**不加载/source `.env`、test.secrets、prod.secrets，不在命令行传密钥**。
数据URL必须与原Transport验证的 qcloud HTTPS443入口完全相同。只支持该API-key profile，工具不替代认证实现/ACL评审。

真实控制CLI沿用 `Kubernetes.in_cluster`，因此只能在批准的**集群内operator作业**执行，不支持本机kubeconfig兜底。该作业还须提供固定同revision的只读仓库工具源码（`scripts`不在普通runtime wheel/镜像中），与同commit包及输入匹配；不能临时拉任意脚本。本轮不创建作业或修改镜像/流水线；作业身份/源码挂载与执行属于8.3独立授权接入。

混合写为有界阶段事件型动作（总数≤100），不是无限写压测机，也不会按未批准读写比无限循环；真实持续写负载的动作/强度、模型竞争和预算须显式批准。

执行时：

1. 核验原会话、入口服务profile/config和Blue四成员digest；由原store记录实际baseline，再取原生路由证据。
2. 原 `ReleaseOrchestrator` 逐phase推进，不伪造phase/Ready。逐Pod核验UID/config/profile/digest和原路由证明；原控制安全门禁全部保留。
3. 并行open-loop读负载、阶段普通/流式探针与typed写。闭闸写按scope/principal/idempotency key只读查询真实command QUEUED，最后必须同UUID SUCCEEDED；HTTP受理不是终态。
4. 一个真正持续消费的长流在交权前仍活跃，必须有旧Blue进程的活跃query证据，交权后正常结束；不暂停消费来伪造重叠。实际模型回答太短会保守失败，应批准合适case和时长，不能延迟客户端凑通过。
5. 已批准持续负载必须覆盖整个发布窗口；提前耗尽预算、错误或client drop立即停止新工作/后续推进，不能擅自补发。若错误发生在已开始的原子advance内，不取消该advance，完成后停止下一步并保留实际阶段。
6. 故障/调用者取消时冻结未发送依赖写和新查询到达，已发送请求按原预算自然收尾，HTTP保持打开。重复取消也不能把未知ACK记为未执行；收尾后保存报告并传播取消。不自动abort、回退、清owner或recover。硬杀进程/机器丢失仍可能只留下SENDING，必须当未知，不自动重跑。

输出新目录0700、文件0600、O_EXCL拒绝覆盖；`attempts.jsonl`每次发送前fsync写attempt hash，`input-identity.json`保存输入SHA。报告不保存正文、源路径、API key、业务doc ID或目标URL；command UUID、case/action ID及输入SHA仍属私有诊断信息。输入正文与响应仅在内存/私有输入中，但OS crash dump、swap及文件系统安全仍由环境保证。

未知ACK、重跑或已有owner全部拒绝自动resume/replay。即使重建同配置也不能撤销已发生的业务效果；应独立调查持久状态。磁盘写失败时停止并关闭资源，无法落盘时不得把缺失记录解释为未执行。

## 确定性质量验证

```sh
.venv/bin/python -m scripts.performance.quality \
  --cases <private-golden.json> --observations <private-observations.json> \
  --output <new-quality-report.json>
```

省略observations只离线校验golden；无网络。golden完整schema见 `mixed-scenario.json` 的 `quality`。
成功case必须提供 `required_facts`（每组允许多个字面变体）、预期引用和chunk真值；没有真值拒绝。可指定禁止事实/引用和最小chunk召回、引用precision。评分核验每case恰好一个观测、完整生成、事实字面变体、数字引用映射/唯一性、引用集合及召回；明确禁止项同时检查答案、引用正文与检索结果，不能因正文“没说”就忽略已泄漏内容。
预期401/403的case要求生成接口与 `/query/data` 都拒绝，且无生成/引用/禁止内容；权限撤销可用独立actor/case配置，工具不会自行修改权限。

在线采集调用真实普通/NDJSON/多轮接口及独立 `/query/data` 探针。后者不是生成那一次的同一检索快照，在持续写时可能跨版本；应使用checkpoint依赖保证真值稳定，不能据此声称原次生成的内部检索已精确重放。
这是可审计的**字面rubric**，不能判断所有否定/蕴涵/幻觉/领域语义，也不增加LLM裁判或冒充人审。

`rehearsal_checks_passed`只表示控制/持续性/写终态/配置规则门禁；`load.slo_met`独立报告。`production_accepted`、`capacity_accepted`始终false，`semantic_judgment_performed=false`。
完整RAG1000QPS、10000在线的容量和领域质量仍须真实1/4/8Pod矩阵、外部模型与后端遥测、人工质量评审及独立签署，不能用本地合成用例或工具退出0替代。

## 动态usage与停控合同v2

- 新run在取owner/发业务前先记录Blue4（可已有Green4）的干净基线；PREPARED允许Green逐个joining，之后必须Green4。固定每实例UID/config/profile/digest及进程epoch，拒绝未解释的消失、重建/重置、重复UID、阶段/身份漂移。并行HTTP读取前后复核控制面，只有真实向前阶段变化才重采，不把稳定阶段失败吞为0，也不因正常队列数量变化误停。
- 角色级SDK计数固定为`provider_calls,usage_reports,usage_unknown,usage_pending,input_tokens,output_tokens,provider_errors,provider_rate_limits`。原成员减干净基线，新Green从进程开始计；退休Blue末量不因Pod消失丢弃。pending是末值；joining/closing保留最后已知量和未决标记。未知量原样记录并停，不估价、不填零，不把计数相加当跨Pod延迟分位或供应商账单。
- 认证`/release/status`仅在query metrics启用时附加compact `query_usage/usage_final_supported`，公开ready不暴露；不新增公开接口/权限/DB列。生产renderer在metrics=true时显式配置`LIGHTRAG_USAGE_FINAL_PATH=/dev/termination-log`和File策略；默认其他路径/关闭模式不增加文件依赖，原test/base不变。旧实例未接入exporter则基线拒绝，不自动重启补配。
- native shutdown正常排空并完成finalizer后，线程写入已有regular终止文件、≤3500字节、fsync，随后才能closed ACK；重复取消也join I/O，失败不ACK。不在事件循环同步写盘。观察器须取得既有DB closed+retiring且六项全0、完整摘要身份/epoch、真实固定镜像及exit0；仅restartCount=1的即时上次成功退出可替代当前已退出状态。缺失/截断/非零退出/再次重启均失败。该选择依赖平台可写性及退出状态保留，必须另行实测，不能因本地Mock通过认定成立。Kubernetes单容器消息上限4096字节、Pod总12KiB且按含init的容器数平分，见[官方说明](https://kubernetes.io/docs/tasks/debug/debug-application/determine-reason-pod-failure/)；当前renderer无init/sidecar，未来注入须重新验预算。
- RETIRING缩容前，观察器先把4份`usage-final-<uid-sha256>.json`及`usage-observations.jsonl`落盘/fsync（目录也fsync），再允许原transport继续closed核验及缩容。0600文件/0700目录、禁止覆盖run；final和journal合计受`max_evidence_bytes`限制，报告保留final SHA。JSON仅固定元数据和计数，不含问题/答案/密钥/URL；UID、scope、hash及command ID仍为私有证据。
- 共享stop覆盖load、typed动作/依赖、阶段/质量探针和后续控制。unknown、缺样本、身份/计数异常、deadline/文件/预算错误均停；采样deadline先停新工作，再join原有有界只读请求（不遗弃线程）。已发送请求自然结束/原超时，不主动取消业务；仅GET可显式用于已受理写追踪。调用者重复取消同样等尾部再传播，未知ACK不重发，不自动abort/recover/清owner/清理。
- 同一operator raw asyncpg连接短DB段串行，不持连接等待HTTP/模型/gate；pool模式原样。采样最多8成员，最多8次相位收敛尝试，受总deadline约束。五项预算无默认，时间≤600秒、poll小于sample/transition、样本≤100000、usage总证据≤16MiB是输入安全上限，不是正式容量值；底层读取与线程/fsync尾部仍须资源/时长预算，deadline不强杀I/O。
- 最终必须COMPLETED、旧4全部final、新4无在途/unknown及任何stop未触发才usage.verified。任何部分证据只能partial，load.slo_met和rehearsal通过均不绕过它。原正常delivery runner不自动采用该演练gate；原写权/排空/路由保护保持。外部M01–M20、全入口、真实CLB和模型额度/账单仍须独立验收。

本轮真实复测未授权，两个候选现场及原test实例/数据/流水线保持不变；没有重打包或部署本地修改，无真实压测/重启/recover/重放/密钥导入/prod/Git发布。
