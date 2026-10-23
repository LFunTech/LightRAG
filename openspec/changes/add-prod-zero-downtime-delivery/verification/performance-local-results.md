# 性能增量本地验证结果（2026-10-21）

## 结论与范围

本轮在用户“继续实施”授权内增加P0代码/工具并完成本地验证；**不是生产容量/质量验收**。
目标为 **1000 QPS 完整RAG（包含最终模型生成）/约10000在线用户**，不是10000在途请求。
4 Pod×1 worker/蓝绿8 Pod基线未改；现有test流水线、配置、Secret和运行环境未操作。
没有提交、push、tag、导入密钥、发真实负载、操作prod或现有PostgreSQL。

OpenSpec当前 **40/53完成、13未完成**：本轮10.3完成，10.2/10.7已有实现但整项仍部分完成；
10.1、10.4–10.6、10.8及原远端六项保持未勾选。不能把本文件或strict validation当启用批准。

## 已实现

- 新默认关闭的固定阶段/分类/桶观测，真实SDK query、模型独立角色队列、存储读、OpenAI SDK实际send/返回usage、cache、ASGI、协调调用和PG读池接线；不记录正文/凭据或高基数标签。
- 活跃查询/流跟随真实owner，异常/取消/未消费时不提前归还provider资源；未知close保留未关闭，业务异常原样传递。观测位于operation_guard内，不改变permit或执行权。
- 只有已有健康接口认证分支暴露指标。进程epoch、CPU累计/峰值RSS、event-loop lag和可脱敏离线区间报告；外部账户/锁/后端遥测不冒充本地已知。
- prod四个显式flag纳入不可变配置/hash/schema：metrics=true、完整答案cache=false、extract=true、keyword开关显式；test/local默认不变。旧答案不复用，关键词/抽取恢复缓存保留。
- 修正预算：query/keyword/extract/VLM各自独立队列，至少按`4×MAX_ASYNC+EMBEDDING_FUNC_MAX_ASYNC`/Pod核算当前profile，再聚合4/8 Pod及其他调用者；不是RPM/TPM额度证明。
- 只读open-loop普通/流式/多轮负载工具默认只离线校验；拒缺批准输入/预算截断/身份不符，统计client drop、调度lag、真实完成吞吐、分位上界和错误。200占位答案/无生成不算完整RAG成功；报告不保存正文/key，不覆盖文件。
- [draft workload](../../../../scripts/performance/workload-target.json)保留已确认目标，其余缺项不猜值；当前离线结果`ready=false`/退出2，未发网络。

## 实际执行结果

| 验证 | 实际结果 |
| --- | --- |
| 首次全后端 `DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib .venv/bin/python -m pytest tests -q` | 9367 passed / 355 skipped / **2 failed**，190.32s；两项旧dataclass位置兼容失败，已归入I6修复，未隐藏或改断言 |
| 唯一审查修复对应RED | 13 failed / 41 passed，覆盖6项Important；[审查及修复](performance-final-review.md) |
| 修复后performance/位置兼容/release query/OpenAI定向 | **119 passed**，1.10s |
| 最终同命令全后端fresh rerun | **9383 passed / 355 skipped / 0 failed**，177.65s；2项upstream websockets/uvicorn弃用warning |
| 独立临时PG18.6 `sh temp/prod-delivery-implementation/run-local-pg-tests.sh tests/distributed/release -q`（显式本地授权） | 最终 **68 passed**，3.14s，2项upstream warning；随机loopback/合成release_test数据库，trap停止，pg_ctl复核`no server running` |
| `./scripts/test.sh tests/performance tests/ci tests/api/test_query_performance_health.py tests/test_dataclass_positional_compatibility.py tests/test_docstring_budget.py -q` | **204 passed**，3.23s（与全套重叠，不相加） |
| Ruff/compile、JSON/shell检查、五workflow strict lint、离线test Kustomize | 通过；prod离线渲染/拒绝策略亦由本轮tests/ci覆盖，不连接集群 |
| wheel构建及字节核对 | 397 entries；新增performance.py与27项release/delivery Python/SQL一致；只含package/dist-info路径，无`.env`/`.secrets`/kubeconfigs/temp/node_modules路径；setuptools有既有license弃用警告 |
| 原交付/用户文件边界 | 35个原交付基线文件hash不变（原增量delivery-check单独排除）；原用户query_routes.py/query_accounting.py hash不变；server/OpenAI仅追加局部接线，未全文件format |
| OpenSpec scoped strict、52场景/任务/链接、diff --check | 最终校验见机器记录；strict不代替业务或容量验收 |

没有前端变更，不重建用户WebUI产物或执行无关frontend检查。
最终wheel SHA256：`6942c251cdd030fa93eb53f81abdab16778aa39a5beae5c93584ad170f3de3ef`。
机器结果与日志摘要/hash见 [performance-local-results.json](performance-local-results.json)；原9330/68/144历史报告未覆写。

## 未完成与上线前条件

- 批准P95/P99/TTFT/错误/等待SLO、流式/多轮/冷热热点比例、时长/在途上限、模式/token、数据规模/读写比、资源与模型RPM/TPM/并发，及独立隔离资源/ACL和发压权限。
- 逐Pod/提供商/PG/HugeGraph/CLB/K8s联合采集；SDK dispatch不包含显式SDK内部重试，usage缺失为未知、非账单，峰值RSS非当前值，coordination耗时非独立锁指标。
- 10.7尚缺混合写/4+4发布联动工具和语义/引用质量自动验证，依赖批准的写入/权限场景及ground truth；现有只读生成器不伪造这些能力。
- 单Pod/4Pod/4+4矩阵、知识/删除/权限正确性、真实发布连续性、同负载前后对照未运行；无证据不改Top-K/索引/队列、不选Embedding/L1/singleflight或新增Redis。
- 之后才评审N/2N和8.7/9.3；原生产gate仍`evaluate: 'false'`，实际接入、全部writer基线、必要计量可靠性、真实腾讯云CLB/ACL和首发仍须独立授权。

[性能工具与操作边界](../../../../scripts/performance/README.md)；[本轮裁定/代价](performance-final-review.md)；[唯一任务表](../tasks.md)。
