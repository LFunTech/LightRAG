# 10.7工具补全唯一最终审查（2026-10-21）

一个fresh-context reviewer只读审查本轮工具、query_load合作停止和delivery_session提取；不重审此前发布/P0增量或用户计量业务。
结果：**0 Critical、7 Important、0 Minor**。七项接受分级，全部在同一轮由主agent按RED→GREEN修复；没有二次review。

| 项 | 实际影响 | 修复与回归 |
| --- | --- | --- |
| I1 | 工具强制路径来源，原API取basename后永远无法确认insert | run身份移到受限basename `perf-<run>-<name>.txt`，不改原API/parser；真实 `InsertTextRequest` 规范化测试RED→GREEN，同步fixture/native边界 |
| I2 | 调用者取消会传入已发送写，UNKNOWN被记FAILED且资源不关闭 | shield整个owner执行，取消仅发送停止信号；禁止新工作、已发写自然受限收尾后落报告并传播取消；重复取消/资源关闭回归RED→GREEN |
| I3 | load HTTP错误只计数，仍推进到COMPLETED | 可选合作stop_event即时接收错误/drop，推进/发送前核验；默认独立readload不变；全500回归RED→GREEN |
| I4 | 故障收尾时尚在等parent的edit仍会发送 | 发送前检查共享停止信号，未发送写为NOT_SENT，已发送写不取消；慢create+失败query回归RED→GREEN |
| I5 | 正文合法但引用正文/检索返回明确禁止事实，规则仍通过 | 成功/拒绝case都检查引用和retrieval的禁止事实/引用，报告仅failure code；两种内容位置RED→GREEN |
| I6 | load只覆盖PREPARED也能通过“持续演练” | 负载预算提前耗尽即停止，不自动追加批准预算；报告连续窗口覆盖；提前耗尽回归RED→GREEN |
| I7 | 闭闸父子动作都等交权，推进却等子QUEUED，确定性死锁 | 验证阶段拒绝闭闸窗口父子依赖，在owner/网络前失败；RED→GREEN |

## 同一轮纳入的主agent自查

- **Baseline接线**：PREPARED真实路由需要原store.previous.digest，原工具只begin未capture。先验证Blue四成员pinned digest，再用原capture_baseline记录，不提前applyGreen；真实PG + `ProductionTransport.expected_slot` 回归先失败（Old artifact was not captured），后通过。
- **重定向未知写**：3xx不能当成功受理，不跟随、不重试、保留UNCERTAIN；HTTP302合成回归RED→GREEN。
- **停止时长**：读负载长调度sleep必须可被合作停止唤醒，不能在故障后仍等下一次到达；低QPS/100秒间隔回归先超时后通过，不取消已有请求。
- **证据磁盘失败**：终态journal写失败不能跳过其他任务/HTTP收尾；gather收集异常并保守记证据未知、停止推进，finally关闭资源。磁盘满注入RED→GREEN；无法写report的硬磁盘故障仍抛错，不宣称记录成功或未执行。

- **输入门禁完整性**：混合runner拒绝超过600秒的读请求/24小时运行寿命，checkpoint重复case/依赖预检拒绝，不等owner后才评分失败；三项输入回归RED→GREEN。

对应RED日志：`rehearsal-review-red.log`（7失败）、`rehearsal-coverage-red.log`（2失败）、`rehearsal-cleanup-red.log`（2失败）、`rehearsal-pg-red.log`（1失败/1通过）、`rehearsal-input-bounds-red.log`（3失败）。
最终合并定向 **254 passed/33 skipped**，真实临时PG **70 passed**；全后端及构建/边界审计见[本轮验证](rehearsal-local-results.md)。不是容量或真实CLB证据。

## 对declined-to-judge的裁定

1. **TKE/CLB、权限、ACL、容量、领域质量**：本轮不联网/不发压，不签署通过；代价是8.3/8.6/10.8与生产启用保持未完成。
2. **旧发布/P0/用户计量**：不重审或改其authority；本轮复用默认交付路径并跑原回归/哈希检查；代价是不能以本轮审查声称所有历史业务都获重新验收。
3. **native baseline缺口**：接受为本轮Important，自查项纳入真实PG RED→GREEN；代价是不在reviewer七项中重复计数，并非遗漏修复。
4. **自然语言否定/蕴涵**：字面rubric不承诺全面语义，不加未经批准LLM裁判；明确禁止项不能漏检，已按I5修复；代价是仍需真实领域数据人工评审。
5. **跨存储删除/恢复合同**：不改原存储流程，typed删除只针对本run确认来源且不清cache/file，不把doc消失提升为跨库原子性证明；代价是原跨存储合同/恢复证据和真实数据验收仍适用。

无deferred minors；没有把Important降级/留作TODO，不执行Git/Secret/真实环境动作。
