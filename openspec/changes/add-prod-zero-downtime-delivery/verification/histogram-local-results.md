# 指标直方图边界本地验证（2026-10-22）

本轮仅实施用户明确授权的200ms、3s、20s分桶子切片，属于10.2的本地准备，不完成其外部遥测/跨Pod联调。主任务仍41/53完成、12未完成，57场景证据状态未提升。没有真实发压、目标环境连接、test/prod操作、密钥导入或Git提交/push/tag。

## 实现与兼容边界

- `lightrag/performance.py`的固定BOUNDS新增0.2、3.0、20.0秒，应用累计快照与离线report共用此布局；默认开关、指标词汇、鉴权与写权均未改。
- `scripts/performance/query_load.py`的独立客户端Histogram新增0.2、3.0秒，原20.0秒及全部其他边界保留。客户端桶为非累计，应用快照桶为累计，不强行统一两套布局或混用计数。
- 精确命中边界归入该桶，略超进入更高桶；P95/P99仍是分位上界，不做线性插值，也不把溢出/空样本当0或通过。
- 离线区间报告继续严格检查布局/epoch。旧快照（包括前后均为旧布局）和新旧混合布局均拒绝；不补零恢复不存在的细分样本。历史证据保留，分析时使用对应历史工具；新验收重新形成匹配新布局的完整区间。
- 参数底稿不修改：195个待定叶子仍null、12类批准false。现有test流水线/base和test overlay、prod配置/gate、用户原有计量代码保持原样；不新增采集器、provider、采样参数或任何环境操作。

## 实际验证

所有pytest命令均设置`PYTHON=.venv/bin/python PYTHON_DOTENV_DISABLED=1`，使用仓库测试入口；不加载真实.env，外部调用仅使用既有mock/fake，不连接已有服务。测试向直方图直接提交合成数值，不实际等待3秒/20秒，也不是吞吐压测。

| 验证 | 实际结果 |
| --- | --- |
| 首次定向RED：3个测试文件，筛选`slo_histogram or slo_quantile or legacy_bucket or interval_cli_exports or client_histogram` | 20 failed / 10 passed / 38 deselected；0.2→0.25、3→5、20→30的粗上界及未拒绝旧布局均先复现 |
| 同一命令GREEN | 30 passed / 38 deselected；包含客户端原20秒边界和默认关闭/溢出既有语义保护 |
| `./scripts/test.sh tests/performance tests/ci tests/api/test_query_performance_health.py tests/api/test_health_auth.py -q --tb=short` | 257 passed，17.00s；包含关联性能工具、CI静态/拒绝与健康接口认证回归，不运行远程流水线或真实Kubernetes操作 |
| Ruff check / format --check | 6个相关源码/测试文件通过；没有全仓库format |
| `python3 temp/prod-delivery-implementation/verify-histogram-delivery.py` | 参数/来源/执行台账hash不变；195 null/12 false；只允许两处BOUNDS的运行源码AST变化，旧测试函数AST不变；1137既有文件边界、57场景/53任务与相对文件链接核对通过 |
| `openspec validate add-prod-zero-downtime-delivery --strict`、`git diff --check` | 通过；不会由strict通过推导真实验收或启用授权 |
| 不完整计划默认CLI（未带`--run`） | ready=false、退出2；未加载真实.env/Secret或发送请求 |
| 本轮唯一fresh review | 0 Critical / 0 Important / 1非阻塞Minor，下面明确保留；审查只读源码/日志，未重跑测试或访问环境 |

新增回归覆盖相邻浮点值的包含/排除、累计桶、95/99分位秩、快照累计差值而非生命周期样本、溢出/空样本、旧布局拒绝、CLI私有输出和默认关闭。日志保留于仓库ignored `temp/prod-delivery-implementation/histogram-{red,green,regression}.log`，不覆盖历史9420/70/254结果。

机器结果见[histogram-local-results.json](histogram-local-results.json)，裁定及代价见[实施账本](implementation-ledger.md)。这些检查只能证明本地子切片，不代替真实模型/存储/入口/质量/容量验收。

## 审查保留项与范围

- **历史Minor（现已补齐）**：原审查延期的客户端混合分布P95/P99秩、report零增量/分位真正落在溢出桶用例，已按用户后续授权完成，见下节。原机器记录`final_review.minor_deferred=1`保留当轮历史事实，新结果单独记入`follow_up_quantile_tests`，不覆写原30/257项测试结果。
- 裁定：沿用既有工作区和OpenSpec账本，不创建Git工作树/分支/提交；以本轮快照限定差异，代价是不依赖提交范围作为审查证据。
- 裁定：真实SLO/计时窗口/跨Pod与外部来源不签署，其他既有未提交功能不背书，全面鉴权/写入正确性不重新签署。原因是本片没有这些实现/环境授权，且未改变对应路径；代价是相关专项与真实验收仍须另补，不能用257项回归或一次审查放行生产。

## 延期分位测试补齐（2026-10-22）

本次只扩展`tests/performance/test_load.py`及`test_report.py`的真实消费者测试，**不改生产代码或分位算法**；所有原有测试函数和断言保留。被测实现当前行为正确，因此新测试首次即通过，再通过独立进程中的临时内存变异验证测试辨识力，不伪造“先修复了生产缺陷”的RED记录。

| 覆盖项 | 新增用例与独立期望 |
| --- | --- |
| 客户端混合分位秩 | 3组101样本，95/4/2分布；P95/P99秩为96/100，分别命中不同桶。覆盖0.2/3/20秒目标、ceil而非floor、P99不可借用P95及非累计桶计数 |
| 报告零增量 | 2例：没有任何直方图时结果为空；已有历史样本但区间无新增时count/sum/所有桶为0、P95/P99为None，不返回历史分位或0 |
| 报告溢出分位 | 4例：P95有限而P99溢出、两者溢出、全部样本溢出、300秒有限边界与301秒溢出并存；未知分位保持None/JSON null，不夹到最大有限桶或0 |

实际验证（pytest继续设置`PYTHON=.venv/bin/python PYTHON_DOTENV_DISABLED=1`）：

- 新增用例定向运行：**9 passed / 46 deselected**。
- 4种临时内存变异均按预期被捕获：客户端floor秩3 failed；P99误用P95 3 failed；零增量分位返回0为1 failed/1 passed；溢出封顶300秒为4 failed。每种独立进程，未修改磁盘上的生产文件；失败均为断言识别到注入错误，不是导入/环境错误。
- 变异进程结束后，`./scripts/test.sh tests/performance -q --tb=short`：**123 passed，9.58s**，其中包含上述9例；不与旧257项相加。Ruff check/format --check的2个测试文件通过。
- 只做本地测试增强与OpenSpec记录，不重跑无关前端/全后端/PG/远端CI或重新审查其他实现。详细机器记录及最终文档/参数/文件边界核验见同一JSON的`follow_up_quantile_tests`。

这项历史Minor现已关闭，不新增主任务或场景；10.2外部遥测/跨Pod及真实验收状态不变，仍41/53完成、12未完成。未知参数/来源/执行台账、运行代码与原test交付配置保持原样，不导入密钥、真实发压、操作test/prod或提交/push/tag。

## 仍未验证

外部逐Pod遥测/跨Pod直方图合并、真实计时边界/窗口/样本量、相关性阈值/真值、P99/负载/资源/模型额度等未确认项保持原状态。分桶精度补齐不证明任何P95目标实际达到，也不授权隔离作业或生产启用。
