# 性能准备与隔离压测工具

本目录不是发布步骤，不被现有 test 流水线调用。当前只运行 fake HTTP/本地回归，**没有发起真实负载**。

## 已确认与待确认

- 目标：**1000 QPS 完整 RAG（包含最终模型生成）**、约 **10000 在线用户**。
- 在线人数不是在途请求数。需结合实际延迟/思考时间/流持续时间确定 `max_inflight`；不能用 Pod 数反推。
- `workload-target.json` 是明确不完整的审批输入，不是可执行生产配置。流式/多轮比例、持续时间、SLO、响应/在途/总请求上限和隔离证据留空，校验返回 `ready=false` 和退出码 2，不能擅自补“合理默认值”。
- 还需在验收记录批准：模式/Top-K/token、知识库/chunk 规模、冷热与热点分布、读写比例、模型账户 RPM/TPM/并发、所有共享资源预算、隔离 ACL、测试停止预案和质量数据集。
- 无答案缓存时，1000 次最终生成/秒即 **60000 次/分钟**，还不含关键词、抽取和重试；输入/输出 Token 预算需实测。此为算术需求，不是提供商额度或系统吞吐证明。

## 默认离线；真实发压必须另获授权

从仓库根目录运行（使用项目 Python 及 httpx）：

```sh
.venv/bin/python -m scripts.performance.query_load --plan scripts/performance/workload-target.json
```

另行授权后，用**专门的隔离资源与凭据**准备批准 plan/dataset；不加载 `.env`、`test.secrets` 或 `prod.secrets`。
`LIGHTRAG_PERF_BASE_URL` / `LIGHTRAG_PERF_API_KEY` 仅由批准的隔离作业环境注入，不把凭据放命令行。
只有同时传入 `--run --confirm-isolated-target --dataset <private.json> --output <new-report.json>` 才会发请求。
首次 GET `/release/status` 核对 profile/config hash；此核对**不能证明 ACL、所有 Pod 身份或数据域隔离**，不能代替人工接入核验，也不能据此批准向 prod/test 发压。无 release 身份的环境拒绝运行。

数据集仅为 `[{"query": "...", "mode": "mix", "history": [{"role": "user", "content": "..."}]}]`，不接受任意 URL、写入请求或 system 消息。
流式/多轮比例采用确定性序列，报告实际数量；短测的比例不一定精确等于目标。普通/流式请求分别调用 `/query` 与 `/query/stream`，校验完整 JSON/NDJSON；200 中的 error、`llm_generated=false`占位答案、非文本/空内容、超时、空流均失败；普通响应还须显式`llm_generated=true`，检查JSON/NDJSON content-type。

生成器是有界 **open-loop**：按目标时刻投递，不因 semaphore 等待而暗中降低目标 QPS；在途达到上限时计 client drop。
总请求预算不得截断 `ceil(QPS×duration)`；单响应≤16MiB、在途响应缓冲预算≤256MiB、总请求≤1000万/在途≤20000，超限拒绝。
整个请求有硬超时，取消会等待 httpx 正常关闭，不无限积攒任务。调度落后会体现在 `schedule_lag`，须与负载机 CPU/网络一起审查；发生明显调度滞后时不能用该结果评价服务容量。

报告不保存问题、答案、历史、key 或目标 URL，仅数据集 SHA、身份和聚合值。输出 0600、拒绝覆盖已有文件；输入/响应正文仍可能含敏感信息，需由操作者私下保护。
`achieved_completion_qps` 使用**至少批准持续时间且包含尾部 drain**的总时长，`throughput_met` 要求实际完成吞吐达到目标；延迟/错误/drop 全达标且吞吐达标才 `slo_met=true`，不满足则 CLI 退出 2。
这是保守的全程判据，不是剔除预热/尾部的稳态推算；阶梯/预热/持续应分计划独立记录。
固定直方图提供分位**上界**（溢出为 null，不算通过）。TTFT 只计首个非空 `response` 内容，不计响应头或引用事件。
客户端和应用阶段直方图均包含 **0.2s、3s、20s** 边界，精确命中边界计入该桶，略微超出必须进入更高桶；不会因旧粗桶把3s直接报成5s。其他原有桶保留，客户端桶计数为非累计，应用快照为累计，不能混用。
无论 SLO 是否满足，`production_accepted/provider_capacity_verified/knowledge_quality_verified/topology_verified` 始终 false；工具不代替验收人员签署。

## 实例级阶段采集（默认离线）

启用 `LIGHTRAG_QUERY_METRICS=true` 后，已有 `/health` 的**认证分支**提供 `query_performance`；不新开公开 metrics 路由。
在获准环境中逐 Pod 直连保存区间前后 JSON，而非通过 Service 随机采样；使用已有认证，采集脚本不得打印健康响应中的其他配置。
每个进程有 `observation_epoch`；进程替换必须拆区间。阶段嵌套，不可把耗时相加成端到端；query_total 是 operation_guard 内侧、真实release owner中的 SDK业务执行（不含外层准入等待），http_total 是包含外层等待的 ASGI生命周期。

```sh
.venv/bin/python -m scripts.performance.report --before <private-before.json> --after <private-after.json> --output <new-interval.json>
```

离线工具只接受单进程快照或带 `query_performance` 的健康响应，校验固定指标词汇、累计桶与进程 epoch，输出差值/P95/P99 上界；丢弃其他字段。
区间前后必须匹配当前工具的完整桶布局，缺少0.2/3/20s的旧快照或混合布局会被拒绝，不能补零/插值伪造细分计数。历史快照须使用与采集版本匹配的历史工具；新验收使用新进程/新布局的完整区间，仍不得跨epoch相减。新增边界不实现跨Pod分位合并，也不代替统计窗口、样本量、真实环境或SLO验收。
`measurement_version=2` 将 `usage_pending`（仍在途）与 `usage_unknown`（结束后仍无有效末尾总量）分开；区间报告输出两者末值，不把它们做普通累计差值。旧版本未区分在途，不能与v2相减。缺失usage绝不计作零Token；提供商真实返回且校验通过的0/0/0则是已知零。
OpenAI-compatible provider_calls 计 SDK dispatch（默认 SDK retries=0 时包含外层每次重试）；显式开启 SDK 内部重试时不包含其隐藏 HTTP 尝试。
Token 仅计返回的 usage，不是账单；限流后的重试/账户额度须与提供商侧遥测核对。其他 provider 尚无这一调用/Token 插桩，不可用零值声称没有调用。
后台模型角色以 `kind=process` 单列；它们不增加 active_queries/query_total。CPU 是进程累计时间，RSS 是峰值而非当前值；PG pool 为读侧 acquire 等待，coordination/release 为调用耗时而非独立锁持有量。
原有 release、协调器/池队列健康信息和云端 PG/HugeGraph/CLB/K8s 指标仍需联合采集，不把这些观测缓存用作写权/Ready/排空判断。

### v2计时边界与流式usage（本地实现，未部署）

| stage | 精确范围 / 不包含的成本 |
| --- | --- |
| `retrieval_total` | KG从关键词开始到完整context/raw_data就绪，含关键词缓存/模型、Embedding、图/向量/chunk、已启用rerank及内层等待；naive从向量路径开始到context就绪。含合法无结果、异常和取消；不含最终生成、答案cache lookup/fill及外层准入。空输入/bypass未执行则无样本，不填0。 |
| `embedding_total` | 查询Embedding角色的队列入口到向量结果返回，含排队、输入预处理、SDK准备、提供商与解码；`model_embedding`仍是队列内执行时间。进程后台角色仍单列，不和查询样本混用。 |
| `embedding_input` / `embedding_prepare` | 前者含prefix/Tokenizer/截断及线程等待；后者含每调用SDK构造/lazy resource准备及线程等待。均不含HTTP调用。`llm_prepare`同理。 |
| `vector_backend` | **仅PGVectorStorage**在Embedding就绪后实际`db.query`，含PG池等待、SQL/已有重试及结果读取，不含Embedding。单次后端调用，不冒充每请求多路总耗时或纯数据库执行时间。 |
| 原`retrieval` / `vector` | 保留旧含义：KG context构建子段；通用VDB.query外包络（naive可能含Embedding）。不得用旧名字代替上两项或相加嵌套阶段。 |

应用观测仍默认关闭，无新公开路由；新旧measurement版本或epoch混合直接拒绝。报告只给桶分位上界；验收的每请求/每调用单位、窗口和跨Pod汇总仍需确认，未改变任何已批准SLO数值。

流式OpenAI-compatible请求默认`stream_options.include_usage=true`，合并已有options而保留显式false；非流式不附加该字段。不支持该参数时明确失败，不删参数重发或自动降级。只接受usage-only末chunk或带finish_reason的末chunk中完整非负整数且prompt+completion=total的总量；中间累计/部分/冲突总量不算完整usage，重复相同末量只计一次。缺失、断流、未消费关闭或调用异常结算为unknown，资源仍正常收尾。`token_tracker`同样不补零伪造完整流式总量。严格query-accounting依旧拒绝stream，本观测不是生产计费/审计的放行依据。

### usage_unknown停测门禁（默认不执行）

`query_load --run`现在还必须显式传`--usage-targets <private-targets.json>`。该JSON是有界列表，每项仅含`base_url`、`instance_id`、`config_hash`；均须使用获准逐Pod直连地址与真实UID/配置，不能用Service随机采样，本文不代填目标。正常计划仍缺值、默认仅离线校验，新增参数不构成发压授权。

执行时在任何查询前核对每个目标的`/release/status`身份及认证`/health` v2快照，要求干净且无在途基线；用独立最多16连接的只读HTTP池采样，不与长流抢负载连接。工具采样默认1s、每轮总超时10s、每响应8MiB、最多100000次GET；这些是实现的安全上限，不是已批准的正式采样/资源预算。缺失/禁用指标、身份/epoch漂移、counter消失/重置、非法数值、观测错误、结束后unknown≥1均停止新增到达；pending在途中不误停，末端仍有pending则不通过。已经发出的请求按原有有界超时自然收尾，不主动取消、重试、重放或操作服务端。

报告含`usage.verified/error_class/samples`；API未配置watch仅可作基础诊断，`slo_met=false`，不能默认为usage通过。单独`query_load --usage-targets`的watch仍是固定成员工具；动态4+4须使用rehearsal v2接入的DynamicUsage，不能用Service随机采样或把Pod消失当0。新动态工具已完成本地闭环，详见[动态合同](rehearsal.md#动态usage与停控合同v2)。它不是跨Pod延迟分位聚合、账单或真实容量签署；正式4+4计量连续性仍是10.2/10.8真实验收前置。

### 本地优化开关与限制

Embedding输入的prefix/Tokenizer/截断移出事件循环并在取消时join；同批同文本仅编码一次。同一次KG批量Embedding对完全相同文本去重并按用途重建顺序，不跨请求/权限域复用，不改变prefix、维度或返回向量归属。

SDK可显式设置`LightRAG(addon_params={"parallel_retrieval": True})`启用hybrid/mix内最多三路独立只读分支（local/global/chunks）的并行等待；默认关闭，有progress_callback时保留原顺序。结果仍按原local/global顺序合并；异常/取消先取消并等待同组任务结束，再原样传播，不释放仍有在途读的调用方许可。API/prod配置没有自动打开它，启用前还须审核每请求峰值、4/8 Pod及共享后端预算和质量；本轮不改Top-K、索引、池或任何线上开关，不加跨请求Embedding缓存/Redis/singleflight。

## 尚待真实隔离验收的矩阵

1. 同数据/资源/参数/模型版本的冷/热、随机/热点；独立数据域，不清抽取恢复缓存。
2. 普通/流式/多轮分类；单 Pod、4 Pod、4+4 分阶段采集每个实例。
3. 并行受控入库/编辑/删除/权限撤销，核对最终存储、幂等/未知 ACK、引用与答案质量；现有本地回归并非真实数据验收。
4. 发布全阶段持续查询、写入排队、旧流正常结束；与既有 release runbook 联合演练。
5. 同负载优化前后对照与精确缓存/singleflight采用评审。

## 混合写、4+4 联动与质量工具

10.7已补齐严格输入的typed混合写、原生发布编排联动、实时旧流/持续负载覆盖及确定性质量rubric，见[完整工具合同与离线示例](rehearsal.md)。默认离线，未来真实写/发布需要独立授权和双确认；不会被test流水线调用，不支持自动recover/未知写重放。

本地工具通过不等于真实4+4拓扑、领域语义或1000QPS验收；8.3/8.6/10.8仍未运行。production/capacity accepted保持false，不改变prod gate。
# 现有 test 的分层验证与本地候选快照

用户已批准在原 test 之外的独立 namespace/数据域验证功能、低并发和应用层
4+4；qcloud/CLB 的真实入口传播与长流保持另在腾讯云非生产环境验收。
这不是生产 Transport 的新模式，不更改既有 test 流程，也不授予 1000 QPS 发压、
生产、Git 发布或扩大 registry 权限的许可。

没有 Git/registry 发布授权时，可先生成**仅用于隔离 test 的本地源码 overlay**：

```bash
PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m scripts.performance.candidate_bundle \
  --repository /Users/minwang/Projects/LightRAG \
  --output /Users/minwang/Projects/LightRAG/temp/isolated-candidate-new-attempt \
  --runtime-image "$VERIFIED_TEST_RUNTIME_IMAGE" \
  --base-commit "$(git rev-parse HEAD)"
```

`VERIFIED_TEST_RUNTIME_IMAGE` 必须显式包含基础镜像 `@sha256:...`；不从 prod、
`.env`、Secret 或 CI 取默认值。命令不访问网络/集群，不执行 Git 写入、上传或部署。
输出目录必须在当前仓库且不存在，文件为0600、输出目录0700；失败不覆盖/清理旧尝试。

工具只读取 `lightrag/`、`lightrag_delivery/`、`scripts/performance/` 以及
`pyproject.toml`，排除隐藏文件、bytecode，拒绝 symlink、凭据类文件名和未知文件类型。
不读取 `.secrets/*` 或 `.env`。逐文件 SHA256、可复现 `bundle.tgz`、512 KiB 分片及其
hash 写入 `manifest.json`；写盘前复读源码检测观察到的并发修改，写盘后验证归档/分片/
文件集合与内容。**不能据此保证未获审查的源码从未内嵌凭据，也不能把双读当文件系统
原子快照；打包期间必须停止其他源码编辑者。**

产物固定标记 `source_kind=working_tree_snapshot`、`production_eligible=false`。
`base_commit` 只是所据 HEAD 的说明，不证明这些未提交字节属于该 commit；基础镜像
digest 也不证明 overlay 已装入或运行时依赖兼容。部署前仍需逐实例验证固定镜像、
只读挂载的完整源码/分片和实际配置，运行依赖预检以及独立后端/ACL/预算检查。
未完成上述门禁不得启动候选；该工具本身没有部署、解包到运行实例或控制发布能力。

本地验证通过不代表真实 RAG、应用层 4+4 或 CLB 验收通过。test 分层验证应优先复用
已有 PG、图服务与 S3，仅隔离候选应用及业务数据域；不得把独立数据域理解为必须重建
物理组件。先核验实际连接/权限/数据域和共享资源预算，确有缺口再报告必要增量，
不以 mock 后端或直接访问原业务数据凑通过，也不要求用户为未采用的组件补镜像。
