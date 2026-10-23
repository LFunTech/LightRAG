# 修复候选真实复测：预检暂停（2026-10-23）

## 结果与边界

用户本次授权在test的新namespace/全新数据域部署修复候选，复用现有PG、图服务和S3，进行单文档、查询及最多2并发复测，并明确异常立即暂停。**本次停在只读预检，未部署候选、未发起单文档入库或查询，因此尚不能评价协调503修复的真实效果。**

计划目标为`lightrag-retest-20261023-0915`，workspace/deployment为`lightrag_retest_20261023_0915`，S3前缀为`lightrag/retest-20261023-0915/object-ingestion`。这些是本run选定的隔离标识，不回填正式生产/容量验收的未知参数。新namespace经只读确认不存在，资源创建、业务/模型请求、重启、recover、重放、prod及Git发布操作均为0。

## 只读通过的部分

- test cluster UID与预期相同；11个节点top读取成功，只作为当前资源观测，不作为容量承诺。
- 原五套Deployment/10个Pod清单均为预期2副本/Running/Ready；UID、spec hash、Secret UID/RV及重启数在诊断前后未变，也与上次保存的原test快照一致。**本轮尚未执行10个Pod逐一HTTP/协调接口点检**，不能拿前次20个200代替本次结果。
- PG以`default_transaction_read_only=on`及只读repeatable-read事务核验协调schema/checksum。原五scope无fence、待处理文档、pending mutation、claim、资源锁、待执行retry或orphan；新scope未注册，所有已列出的public `lightrag_*` workspace表均未见新scope数据。
- PG只读观测`max_connections=1000`、superuser reserve=3、已用623，候选预计最多约10连接及1个观察连接；这不是持续余量或4/8 Pod预算验收。
- 原故障Pod仍是UID `0f32b028-3279-412b-a841-181566917176`、Running、重启0，spec hash在只读诊断前后相同。故障scope仍generation1/fenced=false，原文档analyzing、同一active pipeline operation和processing claim。只读SELECT未触发应用inspect/恢复，不把这些遗留义务当作空闲。

## 停止原因：本次自编图域探针收到400

最初只读图校验抛`HugeGraphClientError`后立即暂停，尚未检查S3、完成原Pod HTTP点检、打包修复源码、运行候选配置构造或创建任何资源。

暂停后的有界只读诊断区分了阶段：HugeGraph的版本和7项schema GET均通过；对新图scope的顶点存在性Gremlin只读查询返回HTTP400。安全错误信封为`groovy.lang.MissingMethodException`，非transient；未保存服务器错误正文、正文业务数据或凭据。诊断不是重放已受理的业务请求，没有DDL、图写、S3写或模型调用。

这是**本次执行者编写的预检表达式被拒绝**，不是新候选已发生503，也不能据此报告“图服务不可用”。探针使用`hasLabel(label)`和名为`label`的binding，与实际storage使用的`vlabel`/`elabel`不同；这是待验证的参数/方法解析冲突假设，当前错误信封不足以证明完整调用解析细节。责任在本次预检实现，未将它归因PG或原test。

已仅在本地将探针参数改为与实际storage相同的`vlabel`/`elabel`，保留失败脚本及诊断文件。**修正后的只读请求没有再发出，尚未验证修正有效，更未自动恢复部署。** 应用SDK/协调器修复源码未再修改，原测试流水线/base/test配置和prod关闭gate不变。

## 验证与下一步

脱敏结果与范围证据见[JSON](2026-10-23-retest-preflight-stop.json)。本轮不重复运行未改动应用的单元测试，不把此前412 passed当作本轮真实复测结果；只做记录/链接、OpenSpec strict及文件变更边界核验。主任务仍41/53完成、12未完成、57场景，不勾8.3/8.6/10.2等真实验收，不改变未知正式参数。

后续恢复推进时，先验证修正后的只读图域探针，再补S3空前缀、原Pod HTTP和完整配置预检；所有门禁通过后才在新namespace部署修复候选并按既定低负载预算测试。仍不得修改原故障Pod/已受理文档、重启/recover/重放，或将当前授权扩大到4+4、1000 QPS、CLB、prod、密钥导入和Git发布。按用户“异常立即暂停汇报”要求，本run当前保持暂停，不自行重跑。
