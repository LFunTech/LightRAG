# 阶段、容量与故障场景准备矩阵

全部真实用例当前为`not_run`，执行人/时间/结果/证据空白。按[完整入口ID](write-entrypoints.md)填覆盖表，保留[原57个capability场景](../scenario-evidence.md)的历史证据；本表是运行单元/故障case细化，不新增或勾选capability场景。

## 正常矩阵与执行先后

| 单元 | 拓扑/负载 | 必须保存 | 当前结果/证据 |
| --- | --- | --- | --- |
| C01 | 单Pod×1 worker，先低负载后批准阶梯/持续 | 冷/热、随机/热点、普通/流式/多轮、模型/后端与负载机基线 | |
| C02 | 隔离4Pod稳态，同数据/模型/参数对照C01 | 扩副本收益/竞争、逐Pod与共享总额度、读写混合 | |
| C03 | Blue4+Green4，先低负载正常发布 | 全phase持续查询/长流、闭闸排队、交权/旧实例收尾 | |
| C04 | 获准压力下Blue4+Green4阶梯/持续 | C03连续性与各类SLO/资源/写等待同时满足，含尾部drain | |
| C05 | 仅获准优化后重复C01–C04 | 同一数据/目标/资源/模型的前后对照、质量/权限/缓存/计量 | |

每单元的时长/阶梯/重复次数/模式与分布在参数底稿为空，不能自行生成可执行压力值。完整RAG必须真实生成，不用`/query/data`、占位答案、假模型或热答案缓存替代。合成/mock可证明本地合同，不能作为上述单元实测。

冷态使用新隔离数据域/批准命名空间；不清test/prod或抽取恢复cache。热态保留同样规模、模型/参数和明确预热记录，不把缓存收益套给多轮/流式。更新、删除、撤权前后使用不同golden版本和checkpoint依赖；规则只负责明确事实/引用/chunk/禁止项，领域语义必须安排人工评审。

## 发布阶段检查

| 阶段 | 必须观察的读/流 | 写/控制与资源证据 |
| --- | --- | --- |
| 启动前 | 无负载，直测Blue身份/协调及真实入口 | 初始Blue4、Green0或已审查standby4；无未知owner/fence/orphan/claim/锁/不确定写；分开核对release队列和doc-status待处理任务。存在积压先暂停评估，不能因pending mutation=0就声称空队列 |
| PREPARED | Blue持续查询，Green私有查询合法；旧长流实际开始 | 固定Blue baseline digest，Green待命不DDL/不polling/不补写；8Pod及所有后端/模型预算可用 |
| DRAINING | 普通/流式/多轮不等写闸门，持续负载仍在 | 旧ticket继续、新请求真实QUEUED；等待无业务锁；后台不新claim，H/S/B/I各类覆盖到此边界 |
| READ_SWITCHED | 扩展双slot后收窄Green的真实CLB/HTTPS证明；旧Blue长流仍活跃并持续消费 | 仅原控制路径判定排空；逐UID/digest/config/profile核验；队列不提前执行、查询不算业务writer |
| WRITER_SWITCHED | Green继续读，Blue旧流可自然结束 | 同scope epoch单调增加，Green唯一新写权；排队同UUID终态与真实业务完成；Blue迟到写/缓存不能越权 |
| RETIRING | Green持续服务；旧HTTP/长流/waiter/backend自然收尾 | Blue不新领取；不能先关witness/pool或SIGKILL。真实closed后才4→0，不强占workspace独占阻断Green |
| COMPLETED | Green4，Blue0；完整负载窗口/尾部统计与质量终检 | 每条已接收命令有终态或明确未知；新旧业务及归因/计量证据齐全后评审，不用Ready/health替代 |

现有rehearsal固定4+4，不负责单Pod控制切换，也不是无限写入压测机；其≤100个阶段事件型动作之外的持续写压力/全入口适配必须另外准备并批准。不能自动延长负载凑覆盖，预算提前耗尽即停止并报告覆盖失败。

## 故障与拒绝用例

每项运行记录都必须列：目标fixture、H/S/B/I/U覆盖ID、触发phase、预期拒绝/停止范围、实际查询影响、命令/业务/归因状态、[停止预案](stop-plan.md)分支、输入/产物hash、执行授权和私有证据引用。故障注入须专项批准，不能对现有test/prod重启Pod、断网或操作数据库。

| ID | 条件/注入点 | 预期可验证结果 | 当前结果/证据 |
| --- | --- | --- | --- |
| F01 | 缺参数/双确认/授权、原journal已存在 | 连接/owner/新写前拒绝，不把空值填默认，不自动续跑 | |
| F02 | cluster/namespace UID、数据域/ACL指向test/prod或身份不符 | 首次资源/业务变更前拒绝，无跨环境读取/Secret兜底 | |
| F03 | 未登记writer/SDK无上下文、service未授权、不可JSON参数/临时路径 | 首次mutation前拒绝；U01/U02明确不受支持 | |
| F04 | Green部分不可用、readiness假绿、digest/profile不符或格式不兼容 | 不关闭正常Blue写/不切流，不以4/8个Ready放行 | |
| F05 | 8Pod不可调度、总PG/模型/HugeGraph预算不足 | 开始交接前拒绝；不先停/缩Blue腾资源 | |
| F06 | 写请求与关闸同刻到达、后台新批次/迟到Blue写 | ticket或QUEUED二选一；无漏统计、无双writer，后台不新claim | |
| F07 | 长旧写/抽取/模型调用超过排空或等待预算 | 不强杀；工具停推进保留owner。交权前安全中止需另行判断/批准，不自动abort | |
| F08 | 队列条数/字节耗尽、闭闸依赖环 | 受理前限流，已接收命令不丢；确定依赖环预检拒绝，不能用删队列腾位 | |
| F09 | 相同主体/scope/key同payload重复及异payload冲突 | 同command/一次业务效果，冲突拒绝；不将未知ACK重发作为“重试验证” | |
| F10 | 排队期间主体撤销/过期、续期token或越权查command | 执行前复核，拒绝无业务副作用；仅原主体可读结果，不持久raw token | |
| F11 | 接收者/客户端断开或Pod消失 | durable command/源/结果仍可追踪；等待者断开不删除命令、不宣称未执行 | |
| F12 | executor在物理写后消失、终态ACK丢失/网络超时 | 保留EXECUTING/UNCERTAIN与原mutation/ticket，停止后续推进，不重投 | |
| F13 | CI/control Job在任一phase前后失联/重复runner | 持久owner不按TTL释放，第二runner拒绝；对同owner也不在工具中自动resume/replay | |
| F14 | EndpointSlice/CLB传播慢、部分目标健康、HTTPS身份不符 | 不接受patch成功作为完成；保持可用端点并停止下一步，不能强删Blue | |
| F15 | 协调PG连接/witness丢失、pool broken、polling/executor停止 | /ready及控制面识别不可用，采集首因；不靠/health200、重启/清_broken/recover处理 | |
| F16 | 排空读失败、未知writer/orphan/fence/claim/锁/pending或backend关闭未知 | unknown不是空闲，不交权/退休；纯查询不误判writer，保留恢复现场 | |
| F17 | H01/H02跨会话过期、对象/结果清理与入队竞态 | 已接收引用保护、无丢源/重复文档；关闭/错误不扩大FAILED重试次数 | |
| F18 | 正常EOF/未消费/异常/取消/重复取消、writer transfer后旧长流完成 | permit/模型槽/HTTP/backend/witness生命周期正确，旧slot不补写；未发写冻结，已发写收尾 | |
| F19 | 普通业务身份试图改owner/epoch/gate，H11或U03试图恢复 | 权限/原distributed路由拒绝，控制与恢复屏障不被清除；无正向recover测试 | |
| F20 | 新数据/删除/撤权、模型/prefix/维度或参数变更后旧cache/旧答案 | 禁止内容/引用/召回规则拒绝；不兼容物理profile在发布前拒绝，切模型需新隔离域/独立批准 | |
| F21 | 必要计量无法保存/结算、迟到usage或取消 | release接入拒绝或保留真实未知义务，不能跳过必要审计换吞吐 | |
| F22 | 负载HTTP错误/占位答案/空流、drop/调度滞后、预算提前耗尽 | 不把HTTP200或少发算完整RAG；停新到达/新写和后续advance，保留尾部，未覆盖窗口不通过 | |
| F23 | CPU/内存/后端/模型/费用越预算或观测缺口/时钟偏差 | 按批准阈值人工/观测侧停发，不加Pod、无限排队或隐藏重试；缺遥测不签容量通过 | |
| F24 | journal/证据盘满、日志暴露敏感字段 | 无可用证据先停新工作；已发结果未知不记FAILED未执行；按停止预案完成有界客户端收尾并限制泄漏面，保留服务端资源/持久状态，不删证据“修复” | |
| F25 | 交权前中止、交权后条件回退、已开始退休后尝试回退 | 与普通停止分开审批；原条件满足才操作，未知写或已退休拒绝；无自动undo/recover | |
| F26 | 清空/清cache/rename/merge/冲突修复、外部修复脚本误用 | 只在额外获准一次性fixture验证适用入口；维护/全域副作用无授权拒绝，不把工具清单变成执行许可 | |

故障负例的“通过”仅指正确拒绝/保留状态，并不代表故障期间无查询错误或容量达标。负例、故障run和正常容量run分报告；预期401/403等只在预先声明case中判定，不能从总体错误率静默剔除真实服务错误。

## 真实结项门禁

8.3必须包含持续普通/流式查询、全入口覆盖或有证据的不适用结论、各phase真实路由/写权/终态及旧流保持；8.6还需完整批准矩阵/各类SLO/资源与外部模型后端观测；10.8需同负载对照和领域质量评审。缺任一项保留未完成，不把测试工具退出0、文档strict或历史本地测试当现场结论。
