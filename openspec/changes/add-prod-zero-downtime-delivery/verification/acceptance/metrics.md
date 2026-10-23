# 指标采集清单与证据口径

状态：方案准备完成，**尚未在真实隔离环境采集**。采样频率、窗口、阈值、时钟误差与总负责人在[参数底稿](parameters.json)的`collection`/`stop_thresholds`中为`null`；逐项来源/责任人按下文的映射材料登记，批准前不连接任何环境。

状态补记（2026-10-23）：后来已获准的单候选低载结果见[复测](../2026-10-23-fixed-candidate-retest.md)，不等于本清单完整正式采集/验收。本轮仅本地补齐v2边界与固定成员watch，见[本地验证](../2026-10-23-usage-timing-local.md)；未重新访问两个候选或原test，不将历史粗口径样本填入新阶段，参数底稿保持不变。

代码依据：[performance.py](../../../../../lightrag/performance.py)、[离线区间报告](../../../../../scripts/performance/report.py)、[release排空证据](../../../../../lightrag/distributed/release/evidence.py)、[现有工具说明](../../../../../scripts/performance/README.md)。下表区分“已有接线”与“待外部来源”，不把指标清单冒充新增采集器实现。

## 用户确认的性能目标（2026-10-22）

以下是用户给定的**目标值，不是实测结果**。数值来源为[参数底稿](parameters.json)的`slo`，耗时以秒保存，成功率按0–1比例保存；`target_confirmation`记录原7项数值，`acceptance_decisions`记录后续成功口径/Rerank选择及授权代选的写排队基线，全部参数及12类执行批准仍未通过。

| 指标 | 用户目标 | `slo`字段 | 采集关联 |
| --- | --- | --- | --- |
| Query Embedding耗时 | P95 ≤100ms | `query_embedding_p95_seconds=0.1` | M05；v2查询`embedding_total`包含角色队列/输入/SDK准备/提供商与解码，`queue_embedding`/`model_embedding`及准备分项嵌套保留；一次批调用与每请求聚合单位仍待审 |
| 向量检索延迟 | P95 ≤200ms | `vector_retrieval_p95_seconds=0.2` | M04/M09；v2仅PG的`vector_backend`排除Embedding但包含池/SQL/读取；一次调用与多路检索请求级单位仍待审，不能用旧vector混算 |
| Rerank重排序延迟（非必选） | 若启用则P95 ≤200ms | `rerank_p95_seconds=0.2` | M04/M05；用户确认非必需，当前profile保持关闭，只有核验实际未启用后该阶段才记not_applicable，不记0ms通过 |
| 检索全链路耗时 | P95 ≤500ms | `retrieval_total_p95_seconds=0.5` | M04/M05；v2 `retrieval_total`从关键词（naive从向量路径）到context就绪，含关键词/Embedding/图/chunk/启用rerank及等待，不含最终生成和答案cache；正式验收映射/窗口仍待审 |
| RAG首Token延迟 | P95 ≤3s | `stream_ttft_p95_seconds=3` | M02为客户端首个非空生成文本；M03/提供商首Token仅作内部定位，不替代端到端证据 |
| RAG完整响应时间 | P95 ≤20s | `ordinary_p95_seconds=20`、`stream_completion_p95_seconds=20` | M02；同一完整响应目标记录在普通/流式完成字段，分类样本、token限制与多轮适用范围仍待确认 |
| RAG检索技术成功率 | ≥99.9% | `retrieval_success_rate_min=0.999` | M01/M04；用户确认技术与相关性分别验收，合法空结果计技术成功；窗口、分母/异常分类与样本量仍待确认 |
| 写入最大排队时长 | ≤30s | `write_queue_max_seconds=30` | M13；用户授权代选的初始规划值，非执行时间、HTTP超时或已生效配置；可靠排队起止证据仍需核验 |

**已明确的成功与质量边界：**用户要求技术成功与相关性两项分别验收，正常完成检索的合法空结果计技术成功。技术异常、权限/协调失败不得伪装为空结果成功。相关性另由M19的标注问题/期望来源与chunk/引用/人工评审验收；空结果按该case真值判断，不自动算相关性通过或失败。当前将99.9%记录为技术成功目标，**不自动给相关性套99.9%阈值**，召回/引用/语义阈值仍为空待确认。合法空结果也不代表完成了包含真实模型生成的完整RAG吞吐，容量报告须分别列出实际生成、合法空结果未生成、缓存/旁路等，不能用空库请求证明1000完整RAG QPS。

**尚未确认的验收口径：**阶段计时是否含队列/网络、一次调用或单请求聚合、全链路起止、成功率分母/窗口/最少样本量与缓存命中/超时/取消/拒绝分类、相关性真值与阈值、Token实际样本/模型兼容及多轮SLO适用范围。流式/多轮比例和生成Token分布已有用户授权代选的[规划基线](README.md#授权代选的负载基线与模型清单核对2026-10-22)，不是实际usage或执行批准。`slo.measurement_contract_reference`保持`null`，不能因部分口径已明确就生成可执行负载。首Token不得用响应头、心跳或引用元数据代替。技术成功率不得直接填入整体RAG错误率、召回率或答案正确率。

### 写排队30秒的规划依据与边界

30秒是本方案在用户授权代选下采用的**初始业务排队上限**：为短暂切换留出十秒级缓冲，同时不把“短暂排队”默认为分钟级等待。它不是行业标准、腾讯云默认值或已证明的发布耗时；后续隔离演练若不能满足，应报告不达标并重新评审预算，不自行放宽。

- 排队时间从发布命令持久受理进入QUEUED起，到有权执行者实际开始执行止；仅被claim、状态改EXECUTING或updated_at变化不单独证明真正开始。M13仍需核验可靠起止证据，不能声称已新增采集器或自动30秒处置。
- `write_queue_max_seconds=30`不直接填写`request_wait_seconds`、`drain_seconds`、`cutover_seconds`、`proxy_timeout_seconds`或`client_timeout_seconds`。同步写还要给实际执行和响应留余量，异步写保持原持久受理语义；20秒查询响应目标不用于写操作。
- 原排空/切流/响应/保留预算不等式仍须满足，且规划的最坏排队必须不超过30秒；若长写排空或入口传播无法满足，不得靠强杀旧任务、放弃已受理写、绕过路由证明换取达标。交权前按原权限和安全条件决定不启动/停止推进或条件中止，交权后保留实际owner和未知义务。
- 已受理写超过30秒是SLO未达标，不等于未执行、自动取消或可重放；仍须保存稳定身份、终态和源引用。停止新工作不冻结服务器已受理队列，见[停止预案](stop-plan.md)。队列容量按真实写入频率/突发另算，1000查询QPS不等于1000写QPS。
- CLB的连接保持、读/发超时是不同配置项，不能拿连接保持时间代替整个写请求的可用预算；具体目标配置仍须现场核验。[腾讯云七层个性化配置](https://intl.cloud.tencent.com/zh/document/product/214/32427)区分这些超时，本次只查公开文档，未读取或修改目标环境。

**当前实现与目标的差距：**

- 用户已确认Rerank非必需，与当前无reranker的profile一致；当前不新增provider或额度依赖。真实验收仍需保存解析配置/hash并证明未执行，再将重排阶段记not_applicable；不能只凭本地意向填写现场通过。以后若显式启用，200ms目标及provider/revision/额度/预算/兼容验证重新适用。
- 2026-10-22按用户授权补齐边界并本地验证：应用QueryMetrics新增0.2/3/20秒，客户端Histogram新增0.2/3秒（原有20秒保留），其余原桶不变。精确命中进入该桶、略超进入更高桶；累计差值、P95/P99分位秩、空样本/溢出、默认关闭和旧布局拒绝均有回归，见[本轮结果](../histogram-local-results.md)。上界仍不是精确分位；区间快照须与工具布局/epoch一致，历史缺桶不能补零或插值。跨Pod合并、真实边界/窗口与外部采集仍未完成，10.2不勾整项，也不据合成样本声称延迟达标。
- 阶段P95不能直接相加为全链路P95；100+200+200ms不能证明或否定500ms目标。须对同一请求的全链路直接计时，同时分别满足适用阶段目标。
- P99、整体RAG错误/超时/drop预算、写排队P95和其他停止阈值仍未知，保留`null`；30s仅为写排队最大值，不代替客户端硬超时或发布排空预算。未修改现有test或prod配置，未连接模型或真实环境。

## 采集项目

| ID | 内容/单位 | 来源与现状 | 验收使用及缺口 |
| --- | --- | --- | --- |
| M01 | 到达/完成QPS、attempt/completed/errors/drop、在途、调度lag、负载机CPU/网络 | 已有query_load客户端聚合；负载机资源需外部采集 | 按全部批准持续时间并包含尾部drain报告吞吐；少发、调度落后、client drop不当作服务容量 |
| M02 | 普通/流式/多轮完成时间、TTFT分位，秒 | 客户端JSON/NDJSON完整性和计时已有；按工作负载拆独立计划/样本 | TTFT指首个非空生成文本，不是响应头/引用；流式正常EOF/错误/取消分别统计，不能只看HTTP200 |
| M03 | query_total/http_total、first_response/http_first_response | 每Pod认证health中的query_performance已有 | query_total在真实业务owner内，不含全部外层准入；http_total含ASGI等待，两者不可相加 |
| M04 | keywords/retrieval/vector/graph/chunk/kv/context/rerank阶段，秒 | 固定stage词汇已有，支持profile无reranker | 嵌套阶段不相加；缺样本标unknown/not_applicable，不能报0耗时。未经支持provider不能冒充全链路覆盖 |
| M05 | queue_query/keyword/extract/vlm/embedding/rerank及model_*耗时 | 角色队列/实际调用路径已有 | query与后台process拆分；每Pod限额不是账户/集群硬隔离或全局额度 |
| M06 | cache_hit/miss/bypass/skip/error/fill、cache_read/write | query/keywords/extract固定分类已有；没有通用Embedding缓存接线 | 命中后仍量检索；记实际开关。条目数/字节/驱逐及收益需另取只读来源，不伪造为已有counter |
| M07 | provider_calls/errors/rate_limits、input/output_tokens、usage_unknown/pending/reports | v2 OpenAI-compatible SDK dispatch/完整末尾usage，pending与terminal unknown分开 | 不含隐藏内部HTTP重试、未插桩provider或权威账单；旧版unknown包含在途，v2末值不能与其混算或当0 Token。固定成员watch异常停新增到达，动态4+4最终快照/混合写联调仍待 |
| M08 | 全账户RPM/输入输出TPM/并发、429/重试、其他调用者/额度余量/费用 | 模型提供商只读遥测/账户报表，**待确认来源** | 与M07时间窗核对，不反推为应用实际生成量；先批准消费上限和尾部预留，不记录prompt/账单密钥 |
| M09 | pg_pool等待、业务池/coordination池/witness/其他连接总量 | 应用读pool等待/coordination调用耗时已有；PG服务端统计待授权只读采集 | pool耗时不是DB锁等待。4/8Pod合计所有池及独立witness；记录max/used/headroom，不能只报单Pod |
| M10 | PG锁/长事务/查询计划/CPU/IO/连接异常 | 服务端/云监控只读来源待确认 | 仅有界聚合，不收SQL正文/参数；不能以采集为名DDL、重启、清锁、开启高负载追踪或扩大DB权限 |
| M11 | HugeGraph并发、池、请求延迟/错误、服务CPU/内存/IO | 应用graph阶段是局部耗时；后端/云监控**待接入** | 同库其他调用者计入，不将应用耗时当锁持有时间。禁止全图扫描作采样 |
| M12 | S3请求/错误/吞吐、源与会话/结果保留、清理状态 | 原会话/保留逻辑已有；云端请求指标待只读来源 | 队列引用源必须保留；不采对象正文、预签名URL或凭据，不遍历整个共享bucket作压测采样 |
| M13 | release phase/owner/epoch、QUEUED/EXECUTING/终态、队列最老年龄/等待 | 原store/status、精确command及created_at/updated_at已有；最老年龄/等待分位无现成汇总，需批准的有界只读/客户端时间线关联 | QUEUED不是业务受理/完成；按scope/principal/key关联同UUID，业务终态另验；不能将updated_at直接当执行起点。ID只进入受控证据，不做metric label |
| M14 | tickets/uncertain_tickets/writers/unknown_owners/orphans/pending/claims/locks/uncertain_commands/cleaning_sources | 原collect_drain_evidence真实活跃索引路径已有 | **仅原控制面可作排空权威**；观测旁路不得提交手写零值。读取失败是unknown，不是空闲，不扫全历史 |
| M15 | readiness、coordinator首异常、SQLSTATE/用途、witness、polling/executor状态 | 原ready/release状态及脱敏诊断已有 | /health200不证明协调可用；standby合法不polling和故障死亡区分，不重启/清_broken掩盖问题 |
| M16 | 每Pod HTTP/query/writer/waiter/backend/background生命周期 | 原release实例lifecycle已有 | 旧纯查询允许跨交权；backend关闭未知不能记0，所有旧实例均closed才可回收 |
| M17 | Pod/进程CPU、当前内存/峰值RSS、event-loop lag、throttle/OOM/restarts、节点/RWX | 进程CPU累计/峰值RSS/event-loop lag已有；当前working set/节点/PVC由外部监控提供 | 峰值RSS不是当前值；CPU时间差不是直接百分比。当前内存/节点证据不可用时不能以本地峰值替代 |
| M18 | Service/EndpointSlice、逐Pod UID/imageID/config/profile、CLB目标/健康、HTTPS候选身份、流连接尾部 | 原ProductionTransport/qcloud只读验证路径已有，现场未验 | 按每phase保存真实证明；patch成功、8个Ready或单个入口探针不等于全部CLB后端已传播 |
| M19 | 事实/引用/chunk召回/precision、禁止泄漏、删除/版本/撤权正确性 | quality字面rubric已有；领域人工评审待安排 | 独立/query/data探针不是原生成的同次上下文。反例、语义否定/幻觉仍需人工评审；结果缺失即未验，不当作通过 |
| M20 | 计量/审计事件保存、真实调用确认、取消后迟到usage | 受控authority与release接入门禁见[计量合同](../../../../../docs/design/QueryAccounting.md)；可靠性未代为验收 | 必要副作用无法证明可靠就阻止接入；不借性能观测代替账务、不静默丢弃、不修改原用户authority |

## 来源与责任人登记（待填写）

已按下表合同建立[20项来源登记底稿](metric-source-map.json)，填写规则见[登记说明](records-guide.md)。它目前只是空白底稿，并未获得mapping_id、批准或现场来源确认。

`parameters.json.collection.metric_source_mapping_id`当前为`null`，指向未来**受控的逐项来源映射材料**，不是任意填一个非空字符串就通过。该材料必须恰好覆盖M01–M20，每项记录以下字段：

| 字段 | 要求/当前值 |
| --- | --- |
| metric_id | 对应上述M编号，不缺失/不重复 |
| actual_source_reference | 实际认证接口/只读报表或数据源及口径，当前空白，不含凭据/原始SQL |
| wiring_status | 区分已有接线、待适配、待外部联调和有证据的不适用；真实采集结果另记，当前空白 |
| collection_owner | 具体采集责任人，不能只填泛称“运维”，当前空白 |
| acceptance_reviewer | 指标口径/证据签署确认人，当前空白 |
| read_only_permission_evidence | 最小权限/目标范围核验引用，当前空白 |
| interval_and_window | 采样频率、区间/时钟基准、observer预算，当前空白 |
| unavailable_action | 缺失或不可达时的停止/unknown处理及通知对象；不能静默补零，当前空白 |
| evidence_location | 私有输出位置、保留期、manifest和脱敏方案，当前空白 |

总采集负责人和总确认人分别填`collection.collection_owner`/`collection.acceptance_reviewer`。未提交完整逐项映射、未核验来源可用性/权限或未明确不可用处置，**观测门禁不通过**。本包仅规定材料合同，不新增采集程序或冒充已取得现场证据。

## 区间与聚合规则

1. 每个单Pod/4Pod/4+4的冷/热、随机/热点、普通/流式/多轮、读写混合单元，都有独立run、输入SHA、数据manifest和资源/模型版本。保留目标与实测，不按峰值一项外推1000QPS。
2. 每个Pod**直连且认证**采集，不经混合Service随机抽样。baseline前、各phase边界和尾部收尾保存快照；周期与来源在底稿确认后执行。观测作业自身CPU/网络/PG连接计入总预算。
3. 受控索引关联UTC时间、run/phase、slot、Pod UID、imageID、config/profile、`observation_epoch`。epoch变更拆区间；跨进程重启不相减。不把这些ID加到应用固定指标标签中。
4. 原始/汇总仅提取`query_performance`及批准字段，不能完整打印health配置。metric标签沿用固定stage/kind/mode/outcome/category；command/doc/用户ID、问题/答案、原始URL/DSN/SQL/令牌禁止作为标签或公开日志。
5. 固定直方图报分位上界；上界溢出为unknown。多Pod应合并同口径bucket后算分位，**不能平均P95/P99**；现有report仅处理单进程差值，跨Pod合并/外部关联未被本包声称已实现。
6. 全部8Pod与其他调用者的账户/后端遥测必须同窗核对；缺失、时钟偏差、观测器错误或输入不一致时暂停并标证据不完整。频率/完整率未知时不启动容量验收。

## 每单元交付物

保留私有manifest、每进程before/after、客户端统计、phase/队列时间线、外部聚合、质量/人工评审、异常与停止记录；公共摘要仅保留脱敏分类和聚合。路径、权限、保留期与责任人在底稿待确认。未提供或未采集项目逐项标`not_run`/`unknown`，不复制历史本地结果填空。

### 动态4+4 SDK usage证据（本地已实现，现场未验）

rehearsal v2逐UID/epoch记录角色级SDK调用、报告/unknown/pending及输入输出Token；旧Blue以终止摘要+closed行+exit0联合验证，先保存四份最终文件及journal再允许缩容，丢失不补0。详见[工具合同](../../../../../scripts/performance/rehearsal.md#动态usage与停控合同v2)。这仅补齐本地动态计数连续性和停控路径，不是跨Pod延迟分位、同次请求内部阶段关联、模型账户总量/账单或外部遥测来源联调。M01–M20实际来源/负责人/权限/窗口仍未填；新采样/证据预算五项null，合法空检索技术成功和相关性分验、全部已确认SLO保持原样。
