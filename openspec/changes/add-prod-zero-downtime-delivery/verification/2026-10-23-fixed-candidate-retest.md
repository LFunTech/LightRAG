# 修复候选：新隔离数据域低负载复测（2026-10-23）

## 结论

**本轮单文档入库、6次完整RAG和最多2并发功能检查通过，协调503与12.4秒停顿未复现；性能、计量和容量验收未通过。** 用户授权范围内的真实低载复测及末端只读核对已完成，不继续4+4、CLB或1000 QPS。

3次流式生成均缺provider token usage，已按`usage_unknown`保留未知，不能补零或据此签署TPM/费用预算。Query Embedding与首次冷检索已有超出既定目标的样本，不能因为响应正确或本轮未503就声称性能达标。

原五套test/10个Pod的UID、spec、Secret UID/RV、重启数及文档计数前后不变；前后各逐Pod认证检查health和协调接口均200、polling正常。旧故障namespace、Pod、源码overlay和原文档/operation/claim均保留，未重启、recover、重放或清理。没有新建PG/图/S3、执行后端schema/角色变更、操作prod、读取/导入prod密钥或提交/push/tag。

## 目标、身份与前置

- test namespace：`lightrag-retest-20261023-0915`；workspace/deployment：`lightrag_retest_20261023_0915`；S3 prefix：`lightrag/retest-20261023-0915/object-ingestion`。上一轮仅预检失败、未创建资源，用户再次授权后才恢复本run；未重用旧已受理请求或数据域。
- 1 Pod × 1 worker，Pod UID `49bfb694-8ace-4f2a-b0af-43546b585ae2`，`restartPolicy=Never`、无自动TTL清理；发布协议关闭，分布式协调与S3原件存储保持开启。原test和prod的配置不改。
- 基础镜像固定为 `docker-hub.f123.pub/lfun/lightrag@sha256:82be927d0d3e8123179587311f2d12c5f8292fd5ac7fe43cd836d8f65a8614ca`。当前dirty源码仅作为明确非生产的419文件overlay，source SHA256 `8fd612dd9534eb9e48ce3a934d0214e3b84cdb907a242434ff0caa00e14a5e09`；不是已提交镜像或生产交付身份。
- init逐文件/归档/分片校验通过；新overlay中的3份修复源码与[本地验证记录](2026-10-23-coordinator-local-fix.json)的hash一致，实际依赖openai 2.54.0、httpx 0.28.1、asyncpg 0.31.0。旧故障overlay仍是其历史SHA，未被替换。
- 用户授权后，修正的`vlabel`/`elabel`只读探针已返回新图域V/E均0；PG新scope/列出业务workspace表为空，S3新prefix为空。此前`hasLabel(label)`探针400不再阻断，不把该探针错误称为图服务故障。
- 完整`create_app`离线构造通过、网络尝试0、后端未初始化；显式选择PGKV/PGDocStatus/PGVector/HugeGraph，LOG_DIR指向已有可写挂载根。之后才创建18项新namespace资源：1 Namespace、1 ResourceQuota、2 Secret、11 ConfigMap、1 Pod、1 ClusterIP Service、1私有Ingress NetworkPolicy，无基础组件或公共入口。
- 仅在内存读取已有test运行/pull凭据，随机384bit API密钥仅用于新Secret，无凭据值落盘/入文档。这里验证逻辑workspace/图scope/S3prefix隔离，**未证明共享后端强ACL隔离或发布runtime/operator权限分离**，不能外推到release-enabled 4+4/prod验收。

## 已执行的功能检查

1. 一份全新260字节合成原件通过S3 presign/PUT；HEAD与自身对象读回确认SHA256 `075627aafbf47a25a0eb08771aead3eba10f45394167bc565dd481b3b5784a2b`。原件留在S3，Pod只保留运行日志/临时目录，不改为本地原件持久化。
2. complete只发送一次，随后观察到真正的`processed`终态和1 chunk，而非仅以HTTP200受理作为通过。track `upload_20261023_014027_c61e150e`，doc `doc-2685126d401f3f39c6315dcf6f65dd9d`。PG末端full-doc/doc-status/chunk/两类归因anchor各1，向量chunk/entity/relation分别1/5/4，图5个顶点/4条边，S3该prefix仍有1个260B对象；没有清理fixture。
3. 一次`/query/data`返回5 entities、4 relationships、1 chunk和1 reference，包含合成事实。接着6次完整RAG全部由最终模型生成、每次均含正确合成代码及1个引用；关闭答案缓存，不能将keyword cache命中称为完整答案缓存。
4. 先普通mix和流式mix，再一组普通mix/流式naive双并发，最后一组带history的普通/流式mix双并发。两组客户端区间均重叠、峰值2。未为采样额外发送第三个并发HTTP观察请求，**没有直接采样服务端峰值活跃数**，该值保留未知。
5. 查询完成后观察304.599秒、31次health/协调检查，polling保持active、无错误/fence/orphan；此窗口覆盖多个配置5秒轮询间隔，不把它写成逐次执行了多少个实际poll的计数。新scope末端generation1/fenced=false，active operation、claim、锁、pending mutation、retry和orphan均0，active query=0。
6. 候选API共104次（含只读观察和1次预设未认证拒绝），未出现正常请求错误；末端有界日志检查0条ERROR/CRITICAL/Traceback/quarantine/polling stop匹配。旧故障scope仍原generation1、analyzing文档及原active operation/claim，heartbeat/状态快照未变；旧Pod重启0。

## 小样本耗时与必须保留的缺口

| 检查 | 本轮观测 | 可以得出的结论 |
| --- | --- | --- |
| 6次完整RAG客户端总耗时 | 1.797–2.131s | 仅本合成数据、低载下的样本；不能签署P95≤20s或容量 |
| 3次流式首token | 0.716–1.046s | 仅3个成功样本；不能签署P95≤3s |
| Query Embedding，含独立检索请求共7样本 | 均值254.979ms、最大296.633ms，≤100ms样本0，P95桶上界500ms | **这些观测未满足既定100ms目标**；不能直接靠增Pod推断改善 |
| 首次冷`/query/data`全链路 | 1.752686s，其中keyword阶段1.326175s | **该冷样本超过500ms目标**；后续5次keyword命中不能代表冷查询 |
| 名为`retrieval`的子阶段 | 6样本、最大425.541ms | 不含上面的keyword阶段；不能冒充检索全链路≤500ms |
| `vector`阶段 | 19个单调用样本、最大212.757ms、混合桶P95上界250ms | mix的多个小调用与naive内嵌embedding计时混合，不能直接当纯向量/每请求P95，计时拆分仍欠缺 |
| event loop lag | 入库前最大2.345ms；末端进程累计最大221.304ms，561样本、P95桶上界5ms | 本轮未重现旧12.407715s；不是严格同fixture/节点/负载对照，不报生产改善百分比 |
| 协调阶段 | 查询上下文中4个样本、最大9.571ms | 不是所有入库/polling事务的全覆盖指标；其他上下文不能补零 |

以上桶分位按累积桶混合后取rank对应上界，不平均各组分位、不插值。固定目标200ms/3s/20s桶仍保留，未知P99/正式负载参数不代填。重复同一合成问题、先做一次冷检索、无rerank、单chunk、低并发，是明显有利样本；1000 QPS/10000在线用户、真实知识规模、多种冷问题和质量成功率均未验收。

模型计数为SDK dispatch口径：Embedding17（入库10、查询/独立检索7）、extract2、keyword1、最终query6，共26；**不是原始HTTP次数或最终账单**。6个最终query都执行，三次非流式有usage，三次流式记`usage_unknown=1`，合计3；不填入/输出token估值。provider error/rate-limit/stream-close-error非零计数未见、observation_errors=0，不等于usage完整。

本次停止检查覆盖HTTP/协调、provider error/rate limit/stream close和观测异常，但未把`usage_unknown`设为自动停止项，缺口在收尾汇总时识别。识别后不追加业务/压测、不重放流式请求；这是后续工具本地补强与计量验收的明确待办，不能把本轮功能通过用于绕过必要生产计量门禁。

## 范围核验及下一步

前后均只读核验原10 Pod/Secret/Deployment投影与文档计数5/0/1/3/0；旧故障Pod/overlay/文档/operation/claim前后相同。新候选和合成数据保留运行，无自动缩容、退出、清理或后台取消。控制命令、recover、旧请求重放、4+4、CLB及1000 QPS均未运行。

本轮不修改应用、测试或部署脚本，不复用此前412单测作为本轮结果；实际证据是上述预检、真实低载、304.599秒观察和末端只读核对。记录检查、源码hash、链接/OpenSpec strict及1397既有文件的变更边界结果见[配套JSON](2026-10-23-fixed-candidate-retest.json)。主任务仍41/53完成、12未完成、57场景；不能据单实例功能检查勾选8.3/8.6/10.2/10.8或启用prod。

建议下一片先在本地补流式usage请求/记录与未知usage停止策略，并拆清naive向量和检索全链路计时，再依据255ms Embedding/冷keyword基线评估调用与缓存优化。若需要新增真实请求、扩大并发、4+4、CLB或压测，按相应范围单独确认；当前不直接扩Pod或启动1000 QPS。旧故障scope恢复仍是独立审计/授权事项，新的成功候选不替它清状态。
