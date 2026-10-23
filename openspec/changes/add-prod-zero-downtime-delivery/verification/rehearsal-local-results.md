# 10.7混合写/4+4/质量工具本地验证（2026-10-21）

## 本轮结论

已完成用户授权的**代码实现与本地验证**，10.7工具任务完成，当前 **41/53完成、12未完成**。
目标仍为1000QPS完整RAG（包含最终生成）/约10000在线，SLO/比例/规模/额度/隔离授权未补齐，**没有真实发压或容量/领域质量结论**。

现有test流水线/配置/运行环境保持不变；不操作prod/既有PostgreSQL，不导入或读取真实密钥，不提交/push/tag。
HEAD/master仍为 `beb4d64681c06d393255c577cdf0d988fdeecbfb`，生产gate仍false。

## 实现范围

- typed文本入库、图创建/编辑及仅本run文档删除；规范化basename、父动作依赖、异步处理/删除终态和图读回；不清cache/file、没有任意脚本/SQL写/recover。
- 复用原交付operator锁/权限预检/ProductionTransport与ReleaseOrchestrator，先捕获真实Blue baseline，再逐阶段验证4+4/最终4成员及qcloud路由；闭闸command按精确scope/principal/key核对QUEUED与同UUID SUCCEEDED。
- 持续读负载覆盖发布窗口、阶段普通/流式探针、真实消费的长流/旧slot query跨交权；故障、负载错误或预算提前耗尽停止后续推进，冻结未发送依赖写，已发请求保持有界收尾，重复取消不提前关HTTP。
- golden事实变体/禁止上下文、引用、chunk召回/precision、权限拒绝及多轮/NDJSON采集；缺失/重复证据拒绝。规则不是全面语义裁判，`/query/data`是独立探针而非原生成的同次快照。
- 默认CLI离线；未来真实运行要求专用隔离资源、完整批准输入、同入口身份、双确认和私有新journal；不自动resume/replay。所有production/capacity accepted为false。
- 原 `deliver` 仅提取同一上下文管理器以供复用，默认advance/abort/rollback不变；query_load默认行为不变，新增可选合作停止。

完整合同/示例见[工具说明](../../../../scripts/performance/rehearsal.md)，审查与修复见[唯一最终审查](rehearsal-final-review.md)。

## 实际执行

| 命令/核验 | 真实结果 |
| --- | --- |
| `./scripts/test.sh tests/performance tests/distributed/release tests/ci -q` | **254 passed /33 skipped /2 warnings**，10.60s；integration默认跳过，另跑临时PG |
| `sh temp/prod-delivery-implementation/run-local-pg-tests.sh tests/distributed/release -q` | **70 passed /2 warnings**，3.47s；仓库temp专用PG18、随机loopback端口/新合成库；trap停止，`pg_ctl status`再次确认no server running |
| `DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib .venv/bin/python -m pytest tests -q` | **9420 passed /357 skipped /0 failed /2 warnings**，190.27s；本轮fresh，不复用上轮9383；DYLD仅本进程提供已有Cairo库 |
| `ruff check scripts/performance tests/performance tests/distributed/release/test_rehearsal_integration.py lightrag/distributed/release/delivery.py`、compileall | 通过 |
| 五workflow `woodpecker-cli lint --strict .woodpecker/*.yml` | 五份Config is valid；只本地lint，未触发CI |
| `kubectl kustomize .../overlays/test`及tests/ci中的prod渲染 | 本地离线通过；不连接集群 |
| wheel构建/源码逐字节审计 | **397 entries，28个本轮相关Python/SQL文件一致**，其余已有package源码亦一致；无.env/.secrets/kubeconfigs/temp/node_modules路径 |
| 原交付/本轮边界hash | **35原交付文件不变**；295旧文件快照中仅delivery.py、query_load.py、test_load.py改变；用户query_routes/query_accounting不变 |
| 默认rehearsal与quality离线CLI | 合成计划valid=true/network_run=false；合成质量rule_gate_passed=true、semantic_judgment_performed=false。默认目标plan仍ready=false/退出2，缺项未伪填 |
| scoped OpenSpec strict、57场景唯一映射、53任务/相对链接和diff whitespace | 最终机器审计见[JSON记录](rehearsal-local-results.json) |

两项warning来自上游websockets/uvicorn弃用，与本轮业务无关。各测试集合重叠，不能相加；未改前端，不运行无关前端检查。
wheel SHA256：`59da6d362ced065bd061c17c538f786ff41b5694648e0e0df2d12e0e4da1277e`。
所有日志在ignored `temp/prod-delivery-implementation/rehearsal-*`，精简摘要/hash持久化到同目录的本JSON记录；历史报告不覆盖。

## 仍需确认和另行授权

1. 批准P95/P99/TTFT/错误/写等待SLO，流式/多轮/冷热热点比例、时长/并发/响应预算、数据/读写规模、模型及后端共享总额度；10000在线不能当10000在途。
2. 独立隔离数据域/ACL、协议基线与全部writer、必要计量可靠性；实际qcloud入口/CLB/TKE只读证据和最低权限。真实密钥只能在独立授权接入时注入。
3. 真实1Pod/4Pod/4+4矩阵、长流保持、并行写/故障场景、逐Pod与提供商/PG/HugeGraph/CLB联合观测、领域质量/删除与撤权验证。
4. 根据同负载前后证据选择10.4–10.6优化，补10.2外部遥测与10.8对照，再评审N/2N及生产启用。无证据不新增Redis/L1/singleflight、不放开prod gate。
5. 工具失败保留owner/queued/UNCERTAIN证据，不自动abort/recover/replay；硬杀或磁盘丢失可能只留SENDING，须人工只读调查，不可当未执行。
