# 动态usage子切片：一次独立审查及修复

日期：2026-10-23。仅审查本轮本地闭环，不包括早先整棵未提交工作区、真实环境或性能签署。

独立只读reviewer发现1项Important/P2、0 Critical、0 Minor：DynamicUsage的journal/final fsync仍在负载/停控事件循环，慢盘可阻塞长流消费、deadline与stop，造成错误SLO和尾部保证失效。服务端exporter已off-loop不能代替该路径。

实施者先新增两个回归`test_slow_observer_fsync_keeps_stop_and_tail_live_and_joins_cancel[journal/final]`，实际观察2 failed/16 passed，再将journal写/fsync、final写/fsync/目录fsync/读回hash移到受控worker。状态在原事件循环更新，sample锁跨持久化，四份final及journal完整落盘后才标retired；deadline先stop、重复取消shield/join，写盘未完不放行缩容。不取消线程或业务来伪造完成。

修复后23项定向通过；最终相关子集**1083 passed、60 skipped、2现有弃用警告，22.81s（含16项文档约束）**，新增文件中的62项单独通过（0.76s）。日志位于本轮私有临时目录的review-red.log/review-green.log/delivery-final-suite.log/new-delivery-final.log。无第二轮review，修复依据为RED→GREEN与完整相关目录回归，不将第一次发现问题的审查冒充修复后再次签署。

## 审查保留判断的逐项处置

1. Ruling: 平台终止文件可写、完整保留、重启/退出行为 — 本地不替代现场；保留为另授权新目标的前置，异常/丢失一律停 — 若不支持，4+4不能放行，不能退回HTTP缺量补零。
2. Ruling: 真实4+4/CLB、SDK usage与账单准确性、吞吐/SLO/容量 — 均保持未签署，SDK报告计数不当账单或延迟分位；真实复测单独确认 — 代价是本轮无法回答已满足1000 QPS或指标，不能上线prod。
3. Ruling: 早先未提交改动不在本轮fresh review范围 — 以本轮起始1604文件hash限定改动，75项原workflow/base/test部署/SQL/旧现场报告保护文件保持相同，相关目录回归不替代整个历史功能审查 — 代价是不能将本次审查结论扩展为整条历史分支已获独立审查/可合并。

无延期Minor；本轮不提交/推送/打tag，不清理临时或候选现场。

持久化修复补充：live/final末位成员出现unknown时，必须在任何慢fsync之前设置共享stop；两项新增用例实际2 failed/18 passed→20 passed。完整有界frame先校验unknown再持久化失败证据，避免为保存失败证据延迟停发。前次1065项/60个新增文件用例结果作为中间日志保留，最终为1083项/62个新增文件用例，计数不相加重复计算。
