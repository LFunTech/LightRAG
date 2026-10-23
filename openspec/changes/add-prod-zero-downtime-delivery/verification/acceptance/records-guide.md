# 指标登记与执行台账填写说明

日期：2026-10-22。本文把已有验收清单展开为可填写的结构化材料，**不新增运行工具、执行权限或测试结果**；主任务仍41/53、12项未完成。

## 三份底稿各自负责什么

| 文件 | 用途 | 当前状态 |
| --- | --- | --- |
| [parameters.json](parameters.json) | 用户/责任方确认的目标、SLO、资源/额度、数据/隔离、预算及分段审批 | 已记录7项目标及后续口径/30s写排队；现按授权代选80%流式、30%多轮及输入/输出Token分布，prod清单未提供明确模型/额度字段。当前196个待定叶子仍null（本轮新增5项动态usage预算，全部留空）、12类批准false；旧198/196/195项为历史快照 |
| [metric-source-map.json](metric-source-map.json) | 对应metrics.md的M01–M20，登记实际来源、接线状态、采集人/确认人、只读权限、窗口、缺失处理及证据位置 | 20条记录完整列出；所有现场字段为空，collection_result=not_run，external_sources_verified=false |
| [execution-ledger.json](execution-ledger.json) | 全入口、故障、容量单元、phase和停止记录的执行台账 | 51入口、26故障、5单元、6阶段及停止记录全部not_run；无run/批准/现场证据 |

这些文件都是`not_executable=true`的文档底稿，不符合load/rehearsal/Job输入schema，不会自动生成请求、部署、读取Secret或连接任何环境。`definition_label`、ID和phase只是从既有文档/源码核对过的**清单元数据**，不是实测值、适用性决定或授权。

[执行前缺项报告](preflight-gaps.md)及[JSON快照](preflight-gaps.json)只列当前缺项，不能由“格式校验通过”推导执行就绪。代码/参数/批准或现场记录变化后须重新核验，不能沿用旧报告；2026-10-21的准备报告仍是历史快照。

## 指标来源表

1. 每项`actual_source_reference`必须是专用隔离目标的受控只读来源引用，不能保存DSN、密码、Bearer、预签名URL、原始SQL或业务正文。表中`definition_source`只指清单来源，不是已经可采集的数据源。
2. `wiring_status`填写已实现/待适配/待外部联调/有证据的不适用等明确状态；即使已有应用插桩，也不能据此将`collection_result`填passed。真实来源、最小权限、采样窗口/时钟和缺失处置需逐项确认。
3. `collection_owner`与`acceptance_reviewer`填具体责任人；`unavailable_action`要说明缺失时停哪一类run、如何保留unknown及通知谁。引用编号非空不等于证据真实，签署人必须核实受控材料。
4. 顶层mapping_id、批准引用、总负责人/确认人与参数底稿的collection映射必须一致。**本轮不为它们生成批准号或将文件名自动填入metric_source_mapping_id**；空值明确表示尚未交接/确认。
5. 单进程阶段统计、外部遥测和多Pod合并分开；不能填“已全链路覆盖”掩盖后端锁/全账户额度/账单/当前内存或同次检索快照的缺口。口径仍以[metrics.md](metrics.md)为准。

## 全入口与场景台账

入口ID与[write-entrypoints.md](write-entrypoints.md)一一对应：H19+S17+B7+I5+U3，共51项。四类typed工具只覆盖其中有限动作；`adapter_reference`为空时就是未提供受控执行适配，不会自动生成hook或SQL来补覆盖。

- `planning_constraint`是既有代码/profile约束，不是批准。当前profile拒绝的本地上传/扫描、distributed force_reset或未登记外部writer不能被改成正向生产验收；清空/cache清理和维护类操作需额外限定一次性fixture，当前仍禁止执行。
- `coverage_evidence`的normal_open/gate_boundary/post_transfer/expected_rejection是**证据填写位置**，不是要求把每个禁止入口都执行四遍。先由适用性/批准运行单明确应测试的上下文，正常可写入口按原开闸/闭闸/交权合同取证，拒绝入口按无副作用负例取证。
- 单独保存业务受理、command终态与`business_terminal_evidence`、`attribution_evidence`；QUEUED、HTTP200、track受理、doc消失均不能被自动提升为业务及跨库存储全成功。
- fault_records对应F01–F26；entry_reference_ids/phase/fixture/适配/审批为空时不能假设已有故障脚本。预期拒绝是否观察到单独记录，故障负例通过不表示容量/连续性SLO通过。
- capacity_units对应C01–C05；C01/C02/C04是8.6必须的1/4/4+4容量证据，C03负责低负载功能连续性。C05仅在获准采用优化时做同负载复测；不采用须有基线、采用评审和明确理由，不能用空结果伪造前后提升。
- phase_records是原release阶段的**只读观测台账**，不能手工写入release数据库或用它替代原Transport/store的路由/排空证明。epoch、active_slot、实例身份和流证据当前全空。

## 状态与证据规则

| 状态 | 使用条件 |
| --- | --- |
| not_run | 尚未真实运行；当前所有记录均属此状态，不因表单行齐全变为通过 |
| passed | 只有获准真实执行、所需输入/身份/适配/私有证据齐备且责任人完成签署时才能记录；仍不是生产启用授权 |
| failed | 有真实未满足项/业务错误，保留原终态和归因；不能将失败改not_applicable隐藏 |
| unknown | 已发送或证据不足，无法确定真实结果；保留已受理义务、owner/原mutation，不自动重试 |
| not_applicable | 需实际配置/协议证明、理由和明确评审；不能把必需单元或禁止操作的拒绝证明要求整体豁免 |

`passed`与`not_applicable`都不能只改一个字符串：必须有审批范围、fixture/输入与产物身份、可核验的证据引用、对应结果或不适用理由及签署。未来形式校验最多证明字段齐全，不能鉴别凭据/ACL、外部证据真伪或替代人工验收。保留原始失败/未知记录，新run另建记录，不覆盖历史。

## 停止记录

stop_record是[停止预案](stop-plan.md)要求的人工/观测侧记录，不是一个新release phase或控制命令。停止新到达/未发动作/后续advance不等于服务器全部停写；已受理命令与后台尾部继续可见，费用/资源和未决义务必须记录。

不得用填写台账替代真实停止信号，也不能为“收尾”改gate、清owner、杀Job/Pod、关witness/pool、重启PG或recover。控制动作在途/Job失联/ACK未知的处理仍按停止预案；本轮没有实际事件，因此停止记录保持not_run/空白。

## 确认参数的优先顺序

数值及后续口径确认详情见[metrics.md](metrics.md#用户确认的性能目标2026-10-22)。Rerank非必选只说明接入选择，现场not_applicable仍需配置证据；合法空结果计技术成功不替代相关性验收或真实生成容量。30s为用户授权代选的业务排队基线，不是操作/超时配置批准。不得把这些局部确认改写为`approvals.parameters.approved=true`或环境执行权；旧SHA与198/196项空值计数仅代表各自历史快照。

用户后续授权的[负载规划基线](README.md#授权代选的负载基线与模型清单核对2026-10-22)只填流式/多轮及两项Token分布，不是观测值或执行许可；模型/额度未在prod清单明确列出，仍保持空白。不将“secrets中有”扩大为读取test、导入密钥或使用凭据查询账户的授权；新参数SHA/191个空值以当前缺项报告为准，旧195项为上一版事实。

先确认**业务SLO、负载组成、模型/额度、数据与真值**四组，而不是要求用户一次填写198项。运维再补**资源/隔离/入口/全部writer与计量依赖、采集来源、作业与停止预算/联系人**；最后按目标/窗口申请只读、资源/seed、混合写/发布、容量、故障及破坏性fixture等独立授权。

任何组仍空就保留对应门禁阻断；不猜一套性能参数跑起来，也不为了推进任务直接新增Redis/缓存/singleflight、启用prod或改变test。

## 动态4+4本地工具补记（2026-10-23）

新增`collection.dynamic_usage`五项预算，全部为null；参数v5共196个待定叶子，既有191项未知及12类false批准未变。rehearsal v2必须显式提供同名`usage`预算；合成fixture不是正式数值来源。逐UID/epoch计数、旧实例最终usage与共享停止联动已完成本地回归，见[本轮记录](../2026-10-23-dynamic-usage-local.md)。不回填真实指标/执行台账，不表示两个保留候选已更新，不改变真实复测须另行确认的当前边界。
