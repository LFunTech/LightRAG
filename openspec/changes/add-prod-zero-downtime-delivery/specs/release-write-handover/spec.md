## Purpose

为兼容生产升级提供可选的查询连续与写入交接合同：通过实例待命、持久化准入、短暂有界排队和旧实例排空，在不丢失请求、不重放不确定存储写入且不削弱现有协调保护的前提下完成新旧版本切换。

## ADDED Requirements

### Requirement: Make the release protocol opt-in and scoped

发布协议 MUST 默认关闭，启用后 SHALL 作用于明确的生产数据 scope 及受控参与者。两个版本访问相同数据时 MUST 使用同一个稳定协调身份，MUST NOT 按颜色建立两套互不协调的写入身份。未启用的 test/local 实例 SHALL 保持既有准入、迁移和退出行为。

#### Scenario: Two slots access one production workspace
- **WHEN** Blue 和 Green 同时连接相同生产数据
- **THEN** 使用一致的 workspace、协调身份和兼容运行 profile，仅 slot/发布角色不同

#### Scenario: Existing test does not enable handover
- **WHEN** 原 test 配置启动或退出应用
- **THEN** 不要求发布元数据或新的接入操作，不改变其现有后台调度与写入语义

### Requirement: Keep standby instances free of production mutations

待命实例 SHALL 验证运行依赖并允许受控查询验收，但 MUST NOT 自动领取后台文档、执行生产业务写入、补 schema 或因查询触发生产缓存写入。待命检查通过不等于获得写执行权。

#### Scenario: A standby starts while documents are pending
- **WHEN** Green 待命启动且生产队列已有待处理文档
- **THEN** 不领取或处理这些文档，既有 Blue 调度不被抢占

### Requirement: Order write admission against the durable release gate

关闭写闸门与新写操作获准 SHALL 有持久化、可验证的先后关系。闸门关闭前已获准的操作 SHALL 正常完成；之后到达的业务写及后台领取 MUST 排队或保持未领取，MUST NOT 在等待闸门时持有业务锁或共享存储事务。控制范围 SHALL 包含 HTTP、SDK、后台任务和隐式 mutation。

#### Scenario: A write races with gate closure
- **WHEN** 一次图编辑与关闭写闸门并发
- **THEN** 该操作要么先取得有效许可并被纳入排空，要么在首次业务变更前排队，不能漏入统计后继续写

#### Scenario: A background worker observes a closed gate
- **WHEN** 闸门关闭后后台 worker 发现新文档或人工重试意图
- **THEN** 不领取新的执行任务，不取消已获准任务，不改变 FAILED 文档一次人工重试的原有语义

### Requirement: Durably track queued requests without false success

已接收的排队写入 SHALL 具有稳定命令身份、版本化参数、授权主体、顺序和可查询状态，并跨实例切流及进程重启保留。同步操作 MUST 保持原最终响应语义，异步操作 MUST 仅在原有受理事实持久化后确认成功；进入发布队列 MUST NOT 被报告为业务完成。原连接仍存在时 SHALL 返回真实执行结果，客户端断开 MUST NOT 删除已接收命令。

detached 后台任务 MUST 在交接前取得独立 durable ticket；父命令结束 MUST NOT 使其后续写失去授权。文本/文件异步受理 MUST 等待原 enqueue 持久化点，不能仅以“后台函数已开始”确认。

#### Scenario: A synchronous delete waits during handover
- **WHEN** 删除请求在闸门关闭期间到达且被接收
- **THEN** 请求等待执行结果，不能提前返回删除成功或未经约定的 `202`；切换后由有权实例执行并向等待方返回原有响应结构

#### Scenario: The accepting instance is replaced
- **WHEN** 接收排队请求的实例在响应完成前丢失
- **THEN** 命令及已确认结果仍可通过稳定身份追踪，不因实例替换丢失或重新创建一份业务操作

### Requirement: Bound queue capacity and release waiting time

生产发布 SHALL 显式配置有限排队容量、请求等待预算、排空与切换预算以及代理/客户端超时余量；配置缺失或无法满足预算 MUST 阻止开始发布。在声明负载内已接收请求 MUST 在预算内被释放执行或得到明确可追踪结果。排队溢出 SHALL 在接收前限流，MUST NOT 丢弃已接收请求或无限等待以换取“无错误”指标。

#### Scenario: A long existing task threatens the wait budget
- **WHEN** 交权前旧写任务不能在预算内结束
- **THEN** 在安全条件成立时中止本次发布并让 Blue 恢复处理，不中断查询或强杀该任务

#### Scenario: Queue capacity is exhausted
- **WHEN** 新请求将超过已批准排队容量
- **THEN** 在创建执行义务前返回明确限流及重试提示，已有命令保持完整且可追踪

### Requirement: Preserve authorization and avoid duplicate command effects

命令执行 SHALL 重新核对生产作用域、当前授权和输入有效性，不保存明文认证 token。相同幂等身份与相同负载 SHALL 关联同一命令/结果，冲突负载 MUST 拒绝；命令状态 MUST 区分排队、执行中、已确认终态和不确定状态。系统 MUST NOT 将跨存储未知结果当作未执行而自动重放。

#### Scenario: A client repeats the same queued command
- **WHEN** 客户端使用相同幂等身份重发相同负载
- **THEN** 返回或等待同一命令，不重复发起业务 mutation

#### Scenario: Authorization is revoked while waiting
- **WHEN** 一个排队命令执行前，其主体权限已失效
- **THEN** 以明确授权失败结束该命令且不执行业务变更，不沿用发布运维身份替用户越权执行

#### Scenario: The executor loses a mutation acknowledgement
- **WHEN** 某条命令可能已向 PG/HugeGraph 提交但没有确认结果
- **THEN** 保留不确定状态和原恢复证据，不因重新投递或进程重启再次发送相同 mutation

### Requirement: Preserve queued source and recovery references

命令引用的对象源、上传会话、结果记录及恢复证据 SHALL 保留到相关操作确认完成且满足保留策略；清理 MUST NOT 删除被未完成命令引用的对象。已持久化文档任务 MUST NOT 在交接时重复创建或扩大失败重试次数。

#### Scenario: Upload completion is queued across session expiry
- **WHEN** 上传对象已完成而 complete 请求处于发布队列
- **THEN** 发布采用的保留策略保证命令执行时对象及必要会话仍可验证，不把正常排队变成源文件丢失

### Requirement: Keep queries independent of the write gate

正常查询和流式查询 SHALL 不等待发布写闸门且继续受到原有数据/权限保护。系统 MUST 明确识别查询中的附带写入：可选缓存填充在发送前无权时可显式跳过，已发送写入须排空；必要计费/审计事件 MUST 可靠保留，MUST NOT 静默丢弃。系统 MUST NOT 关闭全局分布式保护、吞掉存储异常或伪报查询结果来满足连续性。

流式 permit/模型并发槽 SHALL 保留至真实消费或关闭；未消费但已打开的 upstream response/client MUST 被显式关闭。HTTP 断开清理 MUST 等待在途 provider read，关闭未知时保留 backend 活跃证据并阻止实例退休，不能提前归还并发槽。

#### Scenario: A cache miss occurs during write drain
- **WHEN** 闸门关闭期间查询未命中缓存
- **THEN** 正常检索及生成响应，不等待发布写闸门；无权缓存填充在发送前被明确跳过，后续可补齐

#### Scenario: An old streaming query finishes after writer transfer
- **WHEN** Blue 上已有流式查询在 Green 获得写权后才结束
- **THEN** 流式响应正常完成，Blue 不再向生产存储补写缓存，也不绕过必要审计可靠性要求

#### Scenario: A required query side effect cannot be preserved reliably
- **WHEN** 候选版本不能在不阻塞查询的前提下可靠保存必要计费或审计事件
- **THEN** 该版本不能通过生产发布准入，不通过跳过必要数据实现查询连续

### Requirement: Transfer writer authority only after confirmed drain

写执行权 SHALL 在旧版已准入业务写、缓存 mutation、任务领取和后端在途请求均确认结束后转移，并以持久化 owner/epoch 校验阻止旧 slot 再获准新写。只读查询的存续 MUST NOT 要求关闭全部查询才允许交权。Pod 消失、固定等待或发布 epoch 变化 MUST NOT 被当作后端写已结束的证明。

#### Scenario: A stale old instance attempts a new write
- **WHEN** Green 获得新 epoch 后 Blue 又收到写请求或尝试领取任务
- **THEN** Blue 不能执行该新写，所接收请求进入同一受控队列或结果等待路径，不能形成双 writer 版本

#### Scenario: Read-only queries remain active during handover
- **WHEN** 旧业务写已确认排空但 Blue 仍有长查询
- **THEN** 查询继续完成且不被视为未排空业务写，切流及安全写权交接不要求取消这些查询

### Requirement: Retire individual instances without disrupting peers

旧实例 SHALL 先停止接收新的执行工作，再等待本实例 HTTP/流式连接、排队等待连接、后台任务和存储请求完成，最后安全释放本实例资源。退出 MUST NOT 为取得 workspace 独占而阻断新版正常查询，MUST NOT 提前关闭活跃 permit 的连接/witness、清除共享锁或以 SIGKILL 充当正常排空。后续获准的手动或自动缩容 MUST 遵守相同实例排空合同，伸缩控制 MUST NOT 与发布 owner 的待命/交接副本控制竞争。

#### Scenario: Old connections outlive cutover
- **WHEN** 正式入口及写执行权已转移但 Blue 仍有流式响应或队列结果等待连接
- **THEN** Blue 保持这些连接直到完成，Green 持续服务；仅在本实例资源可安全关闭后退出

#### Scenario: A later scale-down selects a busy instance
- **WHEN** 获准缩容选中的实例仍有流式查询、排队等待连接或已准入写入
- **THEN** 先停止新执行工作并按原合同等待本实例排空，不以低 CPU、伸缩期限或目标副本数代替完成证明；发布交接期间由发布 owner 协调副本变化

### Requirement: Fail closed on uncertain handover without automatic recovery

闸门、队列或协调状态不可读取、出现未知写入者、不确定 mutation、遗留 owner 或 fence 时，发布 SHALL 停止推进并保留证据，不自动解除恢复屏障或重放命令。正常发布路径的查询连续性 MUST 与后端故障下的可用性承诺区分，不以健康探针或重启掩盖错误。

#### Scenario: Storage inspection fails during drain
- **WHEN** 无法完整读取 active/pending/claim 等必要证据
- **THEN** 状态被视为未知而非空闲，不转移写权、不强删旧实例，并明确报告发布未完成

### Requirement: Restrict release control and expose safe progress

发布控制 SHALL 仅对获准运维身份开放，普通业务认证不能自行切换 slot、打开闸门、转移写权或清除记录。进度 SHALL 提供不含凭据或业务正文的阶段、owner、队列数量、等待时长和错误分类，支持持续查询与写入演练的可验证结论。

#### Scenario: An ordinary API client attempts to control release
- **WHEN** 普通业务客户端试图修改发布状态或取得写 owner 身份
- **THEN** 拒绝该操作且不改变路由、准入、队列或协调状态
