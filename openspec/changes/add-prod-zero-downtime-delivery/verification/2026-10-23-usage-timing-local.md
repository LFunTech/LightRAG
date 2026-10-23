# 流式usage、计时边界及冷路径：本地验证

日期：2026-10-23。唯一计划为本change第10节；本轮仅本地实施/验证/文档。**未部署、未真实复测、未通过生产或容量/SLO验收。** 机器摘要及源码指纹见[JSON](2026-10-23-usage-timing-local.json)。

## 已实现的切片

1. OpenAI-compatible流式默认请求include_usage，合并options、保留显式false；非流式不添加字段，不删除参数重发/自动降级。仅接受完整、一致的非负整数末尾总量；usage-only空choices可读，中间/部分/冲突不当完整报告，重复相同末量仅计一次。token_tracker同步拒绝不完整流式总量，不估算/补零。原网络/限流重试策略保留，仍逐SDK dispatch计数；严格query-accounting依旧拒绝stream，未放宽authority或生产gate。
2. 原子更新SDK dispatch/在途/结算，`usage_pending`不再被误报成terminal `usage_unknown`。成功、异常、关闭和未消费流均有结算；显式关闭仍交给真实owner，原许可/并发槽/客户端清理回归保留。合法0/0/0与缺失区分，缺失不会变成已知零。
3. `measurement_version=2`增加KG/naive的`retrieval_total`（关键词/Embedding到context就绪，不含最终生成），查询角色`embedding_total`（含队列与解码）、输入/SDK准备分项、仅PG且排除Embedding的`vector_backend`。原retrieval/vector定义保留，不混算嵌套/单调用/每请求。报告拒绝混版本、跨epoch、消失/倒退counter及非整数计数；未执行阶段不补0。
4. query_load CLI接入固定成员UsageWatch：完整直连UID/profile/config与干净基线，认证只读GET、独立有界HTTP池、并行逐实例采样、1s默认轮间隔/10s每轮总超时/8MiB响应/100000 GET上限。unknown≥1、指标/身份/epoch/计数异常停止新增到达；在途pending不误停，末端未结算不通过。已发请求按原有超时自然收尾，不取消/重放、不控制服务端；无watch的API诊断不得给usage/SLO通过。这些实现上限不是正式获批的资源/观测预算。
5. Embedding的prefix/Tokenizer/截断移出事件循环，重复取消仍join后再退出且不dispatch；同批重复文本仅编码一次，同次KG批量Embedding对完全相同文本去重后按用途还原。无跨请求缓存、共享HTTP客户端、模型/prefix/维度更换。
6. SDK `addon_params.parallel_retrieval=true`可在hybrid/mix内并行最多三路独立只读分支，**默认关闭**，有progress_callback仍按原顺序；合并顺序不变，异常/取消等待同组读收尾后原样传播。无新增DDL/索引、Top-K/连接池/线上开关修改；没有自动启用API/prod配置。

精确边界和接口见[工具合同](../../../../scripts/performance/README.md#v2计时边界与流式usage本地实现未部署)、[指标清单](acceptance/metrics.md)及[停止预案](acceptance/stop-plan.md)。

流式协议依据为已实际查阅的[OpenAI Chat Completions创建文档](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)和[流式事件文档](https://developers.openai.com/api/reference/resources/chat/subresources/completions/streaming-events)：usage末块可没有choices，断流可能收不到末量。未调用模型或账户API，不将兼容接口等同于提供商账单。

## 回归与定位证据

新增47个本地用例：流式/Embedding usage 16、停测/报告15、计时边界8、冷路径/取消/进度回调6、输入准备2。使用真实OpenAI SDK加MockTransport SSE、实际KG/naive/PGVector代码加fake驱动、可控时钟/线程/事件；不依赖真实PG、图、S3或模型。

- RED→GREEN日志：初始usage缺请求参数/在途误判10失败；边界/去重/并行/收尾10失败；输入阻塞/取消后dispatch 2失败；停测未实现/混版本11失败；参数合并和counter消失2失败；4实例串行采样1失败；部分/冲突tracker 2失败；中间/冲突末量2失败；浮点计数2失败。进度回调顺序额外用仅内存变异去掉fallback条件捕获1失败（3通过/2未选），未改磁盘源码；兼容性/后续扩展用例也纳入最终验证，不把首次即绿的兼容测试冒称红转绿。
- 本地等待对照：每查询3路各30ms合成只读等待，串/并各5轮，中位数 **93.156ms / 31.097ms**；均3次调用、输出顺序一致、真实后端调用0。它只验证独立等待可重叠，**不是冷RAG、Embedding、PG或1000 QPS提升结果**。
- 历史真实低载证据仍是首次检索1.753s、关键词1.326s，查询Embedding7样本平均约255ms且全>100ms。关键词约占首次总耗时76%，不能把冷路径都归咎向量；旧Embedding样本不能拆出网络/CPU。新增分项只为下次授权复测定位，不反填旧快照或声称已达100/200/500ms。

### 最终实际命令

```bash
PYTHON=.venv/bin/python PYTHON_DOTENV_DISABLED=1 \
TIKTOKEN_CACHE_DIR="$PWD/docker/tiktoken-cache" ./scripts/test.sh \
  tests/performance tests/llm tests/kg/postgres_impl tests/api/routes \
  tests/distributed/release tests/ci tests/test_operate_vector_context.py \
  tests/test_docstring_budget.py -q \
  --basetemp=temp/local-usage-boundaries-20261023/pytest-final3
```

**2212 passed、66 skipped、2 warnings，32.1s，exit=0**。覆盖实际改动镜像目录和查询/发布/原test CI非回归子集，非全仓套件；未开启integration或live服务。两个warning为本地Uvicorn测试依赖的websockets弃用提示；该测试使用临时本地socket，不是候选/集群操作。

- Ruff：4个核心文件、performance工具目录及相关新增/修改测试check通过；6个全新文件format check通过，既有文件仅格式化本轮相关片段，不整树重排既有用户改动。
- `openspec validate add-prod-zero-downtime-delivery --strict`通过，`git diff --check`通过。
- HEAD前后均`beb4d64681c06d393255c577cdf0d988fdeecbfb`，分支master；25项保护文件（原workflows/test脚本/base/test overlay、参数底稿及两次现场历史报告等）hash不变。未知参数不补默认值/批准，不创建平行计划、提交/push/tag或新任务。
- 私有逐次日志/基线/合成对照位于仓库`temp/local-usage-boundaries-20261023/`，本轮目录0700、根日志/JSON0600；未覆盖以前故障/复测材料或重新打包overlay。

## 未验证与保留边界

- 两候选`lightrag-loadtest-20261023`、`lightrag-retest-20261023-0915`及其数据/源码现场未访问或修改；没有重启、recover、重放或清理。原test实例/数据/流水线、基础组件及prod未操作，未读取/导入真实密钥。
- 本轮源码尚未进入任一候选；不把旧419文件overlay身份套用到新代码。任何真实复测必须另行确认，不自动部署或续发请求，1000 QPS授权仍独立。
- 固定成员watch不是动态4+4退休聚合器或混合写全程停控联调：逐Pod最终结算、拓扑变更窗口/身份连续性与共享停止联动**仍需后续本地补齐及授权环境验证**，不能把成员消失当零或用现有rehearsal功能检查签署usage通过。
- 提供商实际usage支持/配额、Embedding和冷关键词外部时延、PG/图真实等待、线程池和三路并行的4/8Pod峰值/质量及正式统计窗口/单位仍未验。优化默认不增加跨请求缓存/请求合并，不以牺牲召回、许可或审计换延迟。
- 主任务保持 **41/53完成、12未完成、57场景**。10.2/10.4只是本地子成果；10.5/10.6采用评审、10.8同条件真实对照及生产接入/启用门禁没有被本报告替代。
