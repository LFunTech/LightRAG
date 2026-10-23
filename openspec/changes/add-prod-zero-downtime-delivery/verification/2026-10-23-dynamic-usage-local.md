# 4+4动态usage与混合写停控：本地验证（2026-10-23）

范围：只实施本地代码、fake边界回归与文档。两个候选现场、原test实例/数据/流水线均未访问/变更；不重打包或部署overlay，不读取/导入密钥，不重启、recover、重放、操作prod或Git发布。主任务仍41/53、12项未完，57个场景；真实复测另行确认。

## 本轮交付

- `scripts/performance/dynamic_usage.py`：独立观察器覆盖Blue4→逐步加入Green→8→旧Blue关闭结算→Green4；UID/slot/digest/config/profile/epoch和角色计数固定验证。joining/closing保持未决和最后已知值，旧Pod消失不丢最终量；unknown计数与失败样本保留，不估零。
- `usage_control.py`复用既有operator只读Pod/状态/DB身份，阶段读取前后核对，只有真实向前变化重采。raw asyncpg连接短SQL串行，pool不串行，网络/模型等待不占该锁；采样超时先stop再join底层只读请求，不能遗弃thread-backed读取。
- 应用仅在显式opt-in时，正常排空+finalizer后写有界终止摘要，再closed ACK；写盘off-loop、重复取消join、失败不ACK；客户端journal/final持久化也off-loop，sample deadline先stop再join，状态只在事件循环提交，journal完整fsync后才标退休。认证status附compact计数，公开ready不暴露。renderer只对prod metrics=true配置exporter，原test/base/workflow不变，旧SQL/schema/权限不变。
- operator联合完整终止摘要、closed+retiring六项零、Pod UID/镜像/配置和exit0验证；四份最终证据与journal先fsync再放行原transport缩容。重启只接受即时上次exit0且restartCount=1；更多重启、截断、漂移或缺失停。文件和journal合计受预算限制，不把终止消息本身当trusted写权。
- rehearsal v2共享stop阻断新读写、依赖、质量探针和后续advance；已发业务按原有界超时自然收尾，仅允许必要GET追踪/读回。未知ACK保留UNCERTAIN不重放；重复取消也不取消已发mutation/长流，不自动abort/清owner/recover。
- 五项新正式预算`collection.dynamic_usage.*`全null，v2执行输入全部必填且无默认。参数v5共有196个待定叶子，原191个未知和12类false批准不变；原模型/负载/SLO已确认值不改。

## 验证与证据

TDD记录位于仓库私有临时目录`temp/local-dynamic-usage-20261023-01/`（日志0600、根目录0700）。原始RED/GREEN均保留；初始全链路fixture错误也保留，不冒充服务端故障。完整回归、静态检查、OpenSpec、最终审查与hash边界的最终结果见配套[JSON](2026-10-23-dynamic-usage-local.json)，最终相关目录回归1083 passed、60 skipped、2现有弃用警告（22.81s，包含16项文档约束）；新增文件中的62项单独通过（0.76s）。Ruff check/format、OpenSpec strict、git diff --check及246个相对链接核验通过。

已实际观察到：

1. 新增自然退出终止文件失败/取消/loop阻塞，deadline先stop、总证据预算、unknown失败证据与跨phase读取竞态的RED→GREEN。
2. 真实tool/control/orchestrator/exporter代码串联（仅PG/K8s/HTTP边界fake）通过4→8→4；Blue监听先关、随后产生late usage，四份最终证据先落盘再缩容；晚到28输入/12输出SDK报告Token保留，四个typed写最终CONFIRMED。不是8个真实Pod，也不是实际供应商费用或吞吐。
3. PREPARED及DRAINING注入unknown：前者已发写确认、未发依赖不发送；后者已发闭闸写的丢失ACK保留UNCERTAIN且只发送一次，未继续交权/删除，长流自然收尾，usage/SLO/演练均不通过。
4. 未解释成员消失/替换、重复、epoch/config/计数漂移、缺末量、盘满、禁用exporter、真实镜像不符、非零退出、截断/错误JSON、未排空等负例拒绝，不通过Ready/HTTP200补零。

## 明确未验证与上线前置

- 真实两个候选仍是历史overlay，未包含本轮代码；不以本地修复声称旧故障恢复。真实单/4/8 Pod、混合写、应用层4+4、CLB与1000 QPS/10000在线/SLO均未复测。
- 平台非root/read-only-root下终止文件可写性、kubelet完整捕获/保留、admission注入容器与原生退出链路必须另行验证。当前renderer单容器无init；3500B低于4096B单容器上限，但Pod总12KiB按含init的容器数平分，[Kubernetes官方说明](https://kubernetes.io/docs/tasks/debug/debug-application/determine-reason-pod-failure/)不等于目标平台已通过。
- 采样deadline停止新增工作，不强杀I/O。K8s/HTTP/PG底层有界超时、fsync/thread join、客户端尾部与费用余量/人工处置仍须预算。机器硬杀/磁盘丢失时只能保留未知，不支持自动resume。
- 动态计数不是跨Pod延迟分位、供应商账户总量/账单或严格计费authority；M01–M20实际来源、时钟/窗口、质量真值、后端/模型额度及所有原未定参数仍待确认。4Pod只是规划基线，没有容量承诺。

## 决策与代价

1. 终止摘要+既有closed行而非改SQL checksum/塞指标字段；代价是依赖真实平台可写/保留，缺失则停在缩容前，不自动恢复。
2. 新v2计划显式预算且unknown停所有新增混合工作；代价是旧v1计划拒绝、正常使用前必须补齐预算，失败run保留资源和已受理尾部。
3. 只对实际phase向前变化重采、单operator短SQL串行；代价是额外只读采样/DB等待需计资源预算，稳定phase失败不能自动重试掩盖。

## 最终审查与边界核验

一次fresh只读审查发现客户端同步持久化P2；两个慢盘/取消回归实际RED→GREEN后修复，最终子集全部通过，未做第二轮review。详见[审查与逐项处置](dynamic-usage-final-review.md)。无延期Minor。1604份起始文件仅发生允许的本轮变更；75份workflow/base/test部署/SQL/旧现场保护文件hash未变，master/HEAD和关闭的prod gate不变。正式参数JSON重排格式，仅新增五项null和版本号；没有把无法重建的原始格式hash冒充语义相等证明。原确认值和未知状态由限域更新及表单核验保留。

持久化修复补充：live/final末位成员出现unknown时，必须在任何慢fsync之前设置共享stop；两项新增用例实际2 failed/18 passed→20 passed。完整有界frame先校验unknown再持久化失败证据，避免为保存失败证据延迟停发。前次1065项/60个新增文件用例结果作为中间日志保留，最终为1083项/62个新增文件用例，计数不相加重复计算。
