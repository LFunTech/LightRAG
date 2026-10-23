# 单实例重建及低负载入库停止记录（2026-10-23）

## 结论

**候选启动已通过，但低负载入库未通过；普通/流式/多轮及并发查询尚未执行。** 1份258字节合成原文件已真实存入现有test S3，读回SHA256匹配，complete返回受理；随后协调接口503，脚本按异常即暂停停止。候选仍Running/Ready、重启0，不能将它或 `/health=200` 认作功能通过。

原test的10个Pod逐一认证直连检查，`/health`与`/documents/pipeline_status`均200、polling正常；5个Deployment、原运行Secret及10个Pod身份/配置前后不变，原文档计数仍为5/0/1/3/0。未重建基础组件、改流水线、操作prod、导入prod凭据、提交/push/tag或发起1000 QPS压测。详细脱敏结果见[JSON](2026-10-23-single-pod-ingestion-failure.json)。

## 两次获准的候选修正

1. 用户首先授权只将失败候选的`LOG_DIR`改为已有可写挂载根`/app/data`，以UID前置条件删除原失败Pod并重建；镜像、源码、Secret、资源和其他配置不变。日志问题消除，但候选构造报 `Distributed writes require kv_storage=PGKVStorage`，立即暂停，未发业务/模型请求。
2. 根因是执行者创建的候选清单遗漏四个存储选择字段，不是test缺少服务。真实配置解析得到JsonKV/JsonDocStatus/NanoVector/NetworkX默认值，分布式保护正确拒绝，没有降级或关闭保护。
3. 用户随后单独批准只补齐 `LIGHTRAG_KV_STORAGE=PGKVStorage`、`LIGHTRAG_DOC_STATUS_STORAGE=PGDocStatusStorage`、`LIGHTRAG_VECTOR_STORAGE=PGVectorStorage`、`LIGHTRAG_GRAPH_STORAGE=HugeGraphStorage` 后再重建。先完成真实配置解析及完整`create_app()`离线构造，网络连接被拒绝的检测器记录0次尝试，未初始化后端，再仅修改四字段重建。原有LOG_DIR修正保留，其余配置/产物/Secret不变。

本地构造预检初轮未合并镜像已有的`tiktoken`缓存环境，下载尝试被离线守卫拒绝；一次诊断还错误解析venv解释器符号链接导致使用系统Python。这些均是本地预检harness问题，未部署或影响后端。最终保留venv路径，并使用仓库中与镜像同源的预置Tokenizer缓存，完整构造通过；不以这些本地错误解释后续真实协调故障。

候选仍是test namespace `lightrag-loadtest-20261023`、workspace/deployment `lightrag_lab_20261023`、1 Pod×1 worker、restartPolicy Never、发布协议明确关闭。它不构成4+4验收，也不改变生产4×1/蓝绿8 Pod规划或未来operator/runtime权限分离要求。基础镜像digest与419文件非生产overlay沿用[首次尝试](2026-10-23-single-pod-attempt.md)，运行前重新核对了源码、不可变ConfigMap分片与manifest；没有将dirty字节冒充已提交镜像。

## 实际执行的低负载流程

预先固定预算：1份合成fixture、6次完整RAG查询、最多2并发、160次API调用上限、单响应2MiB、入库等待300秒；非1000 QPS或容量压测。按顺序验收，不通过入库便不进入查询阶段。

| 步骤 | 实测 |
| --- | --- |
| 直接连接候选Pod，认证health/协调接口 | 起初均200，polling active、无fence/orphan/claim、文档域为空 |
| 未认证访问受保护文档接口 | 按预期拒绝，未执行写入 |
| 创建上传会话、直传现有S3 | presign与PUT成功，严格核对目标为配置的test S3、新prefix；未将API key发送给S3 |
| 原文件独立读回 | HEAD 200、258字节、内容SHA256一致；只读取本run自己的对象 |
| complete | 北京时间08:30:51返回HTTP 200及track id，仅确认已受理，不称已处理 |
| 后台处理跟踪 | 仍未到processed，08:31:05的pipeline_status返回503，试验立即停止 |
| 查询/并发 | 计划的6次查询全部未发送；没有压测吞吐/TTFT/完整响应结果 |

S3原文件SHA256为`1188768935d4e18c802b9668899995844c939fd7e87529c3d350a81b8f5c47a4`，位于本run新prefix `lightrag/release-lab-20261023/object-ingestion`。PG已有1条full-doc与1条doc-status，doc-status停留analyzing；该scope的text-chunks、full-entities、full-relations均0，图scope只读有界查询未见顶点。未据此推断所有未检查的向量表都为空。原文档仍走S3，Pod本地只用于scratch及运行日志，未改回本地持久化。

## 故障与证据边界

首次**可见**异常路径：`process_single_document`的Stage 1并行持久化 → `_upsert_doc_status_transition` → storage_write准入heartbeat → coordinator `_transaction`，抛出`CoordinationUnavailableError: Coordination transaction failed`。随后失败状态收尾也因`Coordinator is not available`失败，不能将错误处理未完成当作安全终态。

只读协调快照：

- 新scope：generation 1，数据库中fenced=false，但**1个active pipeline operation、1个processing claim、1个无witness的orphan**。
- resource lock、pending mutation、pending retry均0。它们不抵消orphan/claim，也不证明可安全重放或仅重启解决。
- 真实Pod继续Running/Ready，`/health=200 healthy`，`/documents/pipeline_status=503 CoordinationUnavailableError`。该单实例未开启release，候选沿用TCP readiness；进程/端口和通用health不核验完整协调可用性，所以会出现假绿。生产release readiness合同不能由本次结果签署。

`_transaction`会在未知事务异常后quarantine并终止pool/witness、保留操作/claim证据；这解释了进程仍活着却协调不可用及orphan的形成。但**底层异常类型与SQLSTATE尚不能确定**：当前首因日志只在存在`_release_context`时输出，本次release=false未保留它，异常随后`raise ... from None`。没有把上层通用异常猜成数据库重启、连接不足、死锁或确定超时。

新增关联线索：入库前事件循环最大lag约2.34ms，失败窗口后为**12.408秒**；唯一一次入库Embedding包围耗时13.652秒、provider_open包围耗时11.989秒，协调器默认command_timeout为10秒。观测到1次Embedding SDK dispatch、报告52 input tokens、unknown usage为0，无最终生成/关键词/实体抽取dispatch。**事件循环阻塞与超时相互作用是优先验证假设，不是已证明首因**；SDK dispatch也不等于厂商账单或账户额度。这些都是`kind=process`入库观测，不是Query Embedding P95，更不是查询SLO或生产容量结论。

## 停止状态与下一步

已停止新上传、入库、模型和查询请求，关闭本轮本地port-forward/客户端；保留候选、S3源、部分PG数据、operation/claim及诊断证据。未重启候选、重放complete、清claim/锁、修改generation/fence、recover、修改PG服务/权限/schema或删除fixture。现有待处理文档不能自动改成failed/processed来方便重测。

下一步先本地定位事件循环/协调异常交互，并为非release模式补首异常脱敏观测及对应回归；不能仅调大超时、清`_broken`或预热掩盖未知事务结果。需要再做远端复现时另确认精确候选/新隔离数据域，保留本次现场、不重放已受理文档；本次scope的恢复须按真实后端状态审计后单独授权。单实例全链路通过后，才继续低并发、4+4及独立CLB验收，1000 QPS仍另行批准。

主任务仍**41/53完成、12未完成**；8.3/8.6/10.1/10.2/10.8等均不勾选。正式参数未知项不代填，生产gate不变。真实S3上传子步骤通过不等于入库、功能、性能、质量、零停机或生产验收通过。

记录检查已运行：OpenSpec change-scoped strict、`git diff --check`、原四workflow/test部署脚本/base与test overlay相对HEAD无差异检查通过；47个相对文件链接、53任务/57场景、脱敏JSON与现场证据投影以及419文件源码overlay当前hash核对通过。本轮应用源码未修改，未运行仓库后端/前端测试套件；本地构造与静态检查不替代失败的真实入库验收。
