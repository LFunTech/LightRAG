# 协调器503与事件循环停顿：本地定位及修复（2026-10-23）

## 范围与结论

本轮只执行本地代码阅读、内存HTTP/模拟驱动实验、回归与文档同步；未连接集群或共享PG/图/S3/模型服务，未读取密钥。**本地可复现的阻塞路径已修复并通过回归，真实候选尚未复测或恢复；现场最初的数据库异常类型/SQLSTATE仍未知。**

用户明确要求保留故障现场：没有重启、recover、重放complete/查询、清理orphan/claim、改原test实例/数据/流水线、操作prod或Git提交/push/tag。此前故障Pod、已受理文档与1个orphan operation/processing claim以[原停止报告](2026-10-23-single-pod-ingestion-failure.md)为历史依据，本轮没有再次连接确认它们的实时状态。修复未部署，旧419文件源码overlay没有重新打包、上传或替换，也不再代表当前本地源码。

## 已证实与仍未知

1. 原现场异常发生在入库Stage 1 doc-status写入前的协调heartbeat。协调事务异常使对象永久`_broken`并终止pool/witness，后续协调接口因此持续503，durable operation/claim仍保留。Ready/通用health并未证明协调和polling可用。该安全隔离不是可用性恢复机制，不能清`_broken`、加超时或重放写入绕过。
2. 旧首异常日志和polling停止日志仅在release模式输出；候选关闭release，底层异常又被公共错误`from None`屏蔽，因此现场首因已丢，无法事后补回。12.407714788s事件循环lag、13.65229366s入库Embedding、11.98923363s provider_open与默认10s协调超时相邻，只能提出因果假设，不能断言PG故障、确定的TimeoutError或完整停顿来源。
3. 本地安装版本与候选记录一致：openai 2.54.0、httpx 0.28.1、asyncpg 0.31.0、pydantic 2.13.4。读取SDK源码确认`AsyncOpenAI.embeddings/chat`是同步cached_property，首次访问会同步导入资源树/构造模型。旧binding在事件循环中的provider调用lambda内首次访问这些属性；即使HTTP完全由MockTransport替代，该同步路径仍会阻塞loop。
4. 并行回归让真实协调器heartbeat/事务逻辑等待有界模拟驱动响应，同时触发SDK冷准备。在仅内存替换回旧同步准备逻辑时，Embedding和LLM两个用例均因协调超时进入quarantine；新实现两个用例通过且后续heartbeat可继续。此实验证明一条可能导致503的机制，**不是重放真实故障，不证明现场10s超时已被实际触发**。

## 实现

- `lightrag/llm/openai.py`：保留公共同步factory，两个实际async binding改用私有准备函数；每调用在工作线程完成client构造和embedding/chat lazy resource准备，ContextVars随任务复制。实际HTTP、流和关闭仍在调用方loop，沿用既有额度/计量/重试边界；不共享client、key或连接，不新增全局缓存。
- 取消准备时不遗弃worker；等待准备结束后关闭已创建client再传播取消。重复取消及慢关闭不能让调用方提前归还资源所有权；准备失败先关闭自有client再抛原异常。线程池等待、GIL竞争及其他同步路径仍需后续实际观测，不承诺所有loop lag已消除。
- `lightrag/distributed/coordinator.py`：release开/关均记录一次脱敏首异常，包含安全类型、有效SQLSTATE或`unknown`、用途、Pod/workspace/deployment/owner、实际连接PID、独立witness PID、command_timeout与UTC隔离时间。未知事务PID保留未知，不能用witness冒充；初始化pool与witness阶段各取自身PID。正常close不造故障，重复隔离不覆盖首因；日志handler失败仍先保持broken并确保两类transport均终止。
- `lightrag/distributed/pipeline.py`：release开/关均记录因协调错误停止的polling时间/安全scope/reason，和quarantine时间分开。Busy仍按原调度合同等待，不记为永久停止；永久错误仍停止，不自动重启polling/重试写入。
- 未修改协调SQL/checksum、超时、durable状态、恢复策略、路由、认证、base/test probes或部署配置。原Ready假绿现场并不会因本地改代码自动恢复，新的日志也不会出现在旧进程里。

注意指标口径：model-role总阶段仍包含SDK准备等待；`provider_open`在修复后不再包含被移出的lazy setup。不得仅比较这个子阶段声称模型服务提速、Query Embedding达标或总体容量改善。

## 本机冷启动对照（非SLO/容量验收）

独立冷进程、同一离线脚本、2次Embedding调用、固定2维Mock响应、2ms loop探针及cProfile；socket连接守卫记录外网尝试0。没有调用真实Embedding/LLM、入库或数据库。

| 单次实验 | 冷调用 | 热调用 | 最大loop lag | Mock HTTP dispatch |
| --- | ---: | ---: | ---: | ---: |
| 修复前 | 0.309000s | 0.000807s | 0.292174s | 2 |
| 修复后 | 0.219197s | 0.001006s | 0.011760s | 2 |

仅是本机少量样本的机制对照，不能外推远端12.4s、线上P95/P99、1000 QPS或4/8 Pod容量；不计算生产改善百分比。

## 回归与验证

TDD记录保留每轮失败而非覆写：初始19项12 failed/7 passed（日志/loop/cancel问题），实现后19 passed；uncertainty首因1项RED；模拟旧阻塞实现的并行协调用例2项RED；初始化witness PID 1项RED；polling日志2 failed/1 passed；未知事务PID与witness区分2项RED。最终两个新增测试文件共**33 passed，0.61s**。

初版两项事务日志测试在日志为空时触发StopIteration，随后改成先显式断言记录数量再取首条；根因仍是缺日志，未降低验收条件。后续初始子集376 passed与扩展中407 passed仅是中间记录，以下最终412项结果覆盖本轮全部代码，不叠加成独立总数。

最终相关子集（未运行全仓库/前端或真实集成后端）：

```bash
PYTHON=.venv/bin/python PYTHON_DOTENV_DISABLED=1 \
TIKTOKEN_CACHE_DIR="$PWD/docker/tiktoken-cache" ./scripts/test.sh \
  tests/distributed tests/llm/openai_impl tests/llm/test_query_accounting.py \
  tests/performance tests/api/routes/test_query_accounting_routes.py \
  tests/api/test_query_performance_health.py tests/ci/test_prod_workflows.py \
  tests/ci/test_prod_inputs.py tests/test_docstring_budget.py -q --tb=short
```

结果：**412 passed、116 skipped、2 warnings，14.76s**。116项集成用例未启用（无真实PG/后端/集群验收）；2条为既有websockets弃用警告。create_app测试另提示既有WebUI源码比构建新，本轮未改前端、未重建产物。已覆盖既有流式清理/usage/重试回归；新增覆盖慢同步构造/资源、重复取消、慢关闭、失败清理、并发ContextVars和独立client、OpenAI/Azure实际SDK内存HTTP仍在caller loop、并行协调事务、首因脱敏/一次性、logger失败fail-closed、正常close、uncertainty fallback、PID准确性及polling停止/Busy区别。

Ruff检查5个本轮Python文件通过；两个新测试文件全文件format检查通过，3个已有源码仅format/check本轮修改区间，未格式化其他既有改动。OpenSpec strict与变更边界核验结果见[配套JSON](2026-10-23-coordinator-local-fix.json)；原始离线日志在本地忽略目录`temp/coordinator-local-fix-20261023/`，JSON保存证据hash，不含密钥。

## 剩余门禁

本轮未新增/勾选主任务，仍41/53完成、12未完成、57场景；未知正式参数、隔离批准与prod关闭gate保持原样。未签署入库、普通/流式/多轮查询、并发、4+4、CLB、质量或任何性能SLO通过。

下一步需**单独确认真实复测的候选和新隔离数据域**，不可借本轮授权重启/修改现有故障Pod或重放已受理文档。当前现场的审计/恢复是另一项单独授权，不能与新候选复测混为一谈。先复核测试边界与停止条件，再用新修复验证低负载入库、逐段loop/模型/协调指标及查询；成功后才按顺序推进低并发、应用层4+4和另验CLB，1000 QPS仍须另行批准。
