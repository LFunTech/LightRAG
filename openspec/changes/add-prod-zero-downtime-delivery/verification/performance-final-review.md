# 性能增量唯一最终审查与修复（2026-10-21）

本轮使用一个 fresh-context reviewer，只读审查**整个性能增量**，不重新审查已独立验收的原发布实现或用户 query-accounting 业务。
结果：0 Critical、6 Important、0 Minor；全部 Important 进入同一轮 TDD 修复，不安排第二轮 reviewer。

| 项 | 可复现影响 | 修复与证据 |
| --- | --- | --- |
| I1 | ObservedStream 把异常/耗尽与底层关闭混为一谈，可能使未知关闭的 provider 被视为 lease 已关闭并归还模型槽 | 分离观测终态/failed/source_closed，异常不冒充关闭成功，显式 aclose 仍传到底层；`test_failed_iteration_does_not_hide_explicit_source_cleanup`、`test_exhausted_stream_still_forwards_explicit_close`、真实 OwnedProviderStream→ObservedStream→ProviderStreamLease 的 `test_release_provider_unknown_close_cannot_be_hidden_by_observation` RED→GREEN |
| I2 | HTTP200/no-context `llm_generated=false` 占位内容被当成完成生成，可能假达标 | 普通响应要求 true、非空文本/JSON；流式拒绝 false/错误类型/空内容、检查 NDJSON content-type；`test_full_rag_never_counts_placeholder_or_invalid_answer_as_generation` 真实httpx transport参数化 RED→GREEN |
| I3 | aclose 未恢复 role，提前关闭时返回的 usage 归other、query假未知 | close 像 anext 一样设置/恢复 role；真实OpenAI adapter normal/early-close `test_real_openai_stream_path_records_terminal_usage_and_keeps_close` RED→GREEN |
| I4 | 首响应前调用者取消，shield owner仍执行而trace提前结束、迟到usage漏记 | query观测移到原operation_guard内侧，在真实owner中创建/结束，不改permit或run_query；真实LightRAG.aquery+run_query的 `test_cancel_before_response_follows_real_release_owner_not_waiter` RED→GREEN |
| I5 | provider_call 包在accounting准入之外，未发送的拒绝也计SDK dispatch，结算失败也误报provider失败 | 仅在account_openai_call既有send回调内插桩，业务authority完全不改；真实accounting拒绝+真实LLM/embedding adapter `test_real_accounting_rejection_before_dispatch_does_not_count_sdk_call` RED→GREEN |
| I6 | 新dataclass字段插在旧字段中，SDK旧位置参数静默错位 | 追加到全部既有字段后；原 `tests/test_dataclass_positional_compatibility.py` 两项全套RED→GREEN，不改旧顺序断言 |

## 实际验证

- RED汇总：13 failed / 41 passed；包含六项对应的失败，不是仅 import/setup 错误。日志 `performance-review-red.log`。
- 修复后定向：performance、旧位置兼容、release query生命周期和OpenAI adapter **119 passed**。日志 `performance-review-green.log`。
- 最终全后端、隔离PG、静态与构建结果见 [性能本地验证](performance-local-results.md)，不以119项替代全套；未完成真实容量/质量验收。

## 对 reviewer declined-to-judge 的逐项裁定

1. **容量/SLO/Pod数**：保持1000完整RAG QPS/10000在线为目标，缺SLO/数据/隔离压测不判断通过。成本：不能给出保证Pod数或启用生产。
2. **外部重试/账单/账户额度/后端遥测**：SDK dispatch/返回usage/读pool/调用耗时是准确本地边界，缺失为未知，不作权威或账单。成本：仍需提供商、PG/HugeGraph/CLB/K8s联合采集，10.2部分完成。
3. **10.4–10.6优化采用**：无基线不盲装Embedding/L1/singleflight/索引优化。成本：尚未消除冷热点放大或承诺性能收益，任务不勾选。
4. **10.7/10.8混合/质量/对照**：只读负载与离线差值不是混合写/发布联动或语义质量工具，也不是容量验收。成本：完整工具和隔离演练仍需补齐，不伪造完成。
5. **旧协议/用户计量业务**：不重审/改动其业务，只修本轮观测接线；原必要计量不可靠时拒绝release接入门禁保留。成本：不宣布用户计量功能已验收。
6. **含尾部drain的吞吐**：采用保守全程完成吞吐并明确非稳态；调度lag另列。成本：短测可能保守不通过，应以批准持续计划另测，不外推生产。

无 deferred minors；没有将任一 Important 降级或绕过。没有触发线上/Git/Secret操作。
