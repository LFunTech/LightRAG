# K8s-only 首次部署：本地验证与执行准备

## 授权与实现

用户明确要求流水线仅部署K8s、不再校验CLB/EdgeOne；沿用必要commit/push及正式prod tag授权。原test流水线/实例/数据和两个候选不动。禁止K8s应用TLS保持最高规则。

新增显式initial settings分支，复用原源码归档、镜像revision/platform/digest校验链；固定digest初始化Job依次实际调用migration apply→verify→register→enroll，成功后创建4×1 Blue、等待协调器感知的readiness，再建立HTTP Service/Ingress。scope执行claim防跨pipeline重放；失败/未知/超时保留现场。没有自动recover、rollback、重启或迁移重试，不覆盖已有应用，不创建基础组件或集群RBAC。

数据持久化仍用prod现有PG/图/S3。无RWX时使用每Pod有界10Gi emptyDir，仅scratch、不建PVC；禁用本地文件入库并拒绝文件storage。显式空network表示不新增NetworkPolicy，不猜CLB来源或用0/0绕过旧规则。旧cloud-aware profile不变。

## 新鲜验证

- `PYTHON_DOTENV_DISABLED=1 PYTHON="$PWD/.venv/bin/python" ./scripts/test.sh tests/ci tests/provisioning tests/distributed tests/api/config`：**678 passed /146 skipped /2既有DeprecationWarning**。
- 仓库temp独立PG、随机loopback端口：`sh temp/provisioning-local-20261023/run-tests.sh tests/provisioning tests/distributed/release tests/ci/test_prod_initial.py`：**247 passed /1 skipped /2既有warning**；退出确认server stopped。包含真实PG迁移/验证/登记/enroll及重复幂等；图为MockHTTP，不冒充真实HugeGraph DDL。
- 独立临时uv/jsonschema工具（不改项目依赖）执行`tests/ci/test_prod_initial.py`：**29 passed**；实际私有prod JSON及Draft202012 schema校验通过。以上三组重叠，不相加。
- 11个受影响Python文件Ruff/format、sh/bash语法、5个workflow strict lint通过；本change OpenSpec strict通过。
- 干净源码导出构建wheel/sdist，每包**11项**受影响生产模块/迁移资源逐字节一致，无私有输入/工作站目录。scripts不在wheel中，CI仍使用可信源码归档执行它们。
- 原4份workflow、test部署脚本/base/test overlay、旧迁移SQL/checksum与基线diff为空；原test和两个候选未访问。未运行完整后端suite，本轮只运行上述受影响目录；历史9661不是本轮结果。
- [唯一审查及一次修复](2026-10-23-kubernetes-initial-review.md)：0 Critical /2 Important均已修复；无第二审。

原始本地日志在忽略目录`temp/prod-initial-delivery-20261023/`，不提交私有输入。

## 真实输入准备（不是部署结果）

仅解析`.secrets/prod.secrets`，未读取test或改写源清单。prod/kube-system UID与目标TKE一致；prod namespace未发现LightRAG Deployment/Service/Ingress。复用现有prod/lfun、PG lightrag、HugeGraph lightrag001、COS bucket及独立S3 prefix，显式使用已批准首次共用专用账号例外；不需要CREATEROLE。首次物理profile/tag绑定v1.5.9，尚未创建tag时不填commit/digest/Pod验收结果。

百炼专属Host的错误`/v1/models`返回404；修正`/compatible-mode/v1/models`后GET 200，178个模型，qwen-plus可见，未生成token。基线生成qwen-plus，用户授权百炼Embedding选型为text-embedding-v4/1024/8192；rerank关闭，额度未确认，不作容量结论。

已向Woodpecker仓库57导入**23个lightrag_prod_*** CI输入，仅tag事件；原26个secret元数据完全未变。最初带点tag的image限制被服务器422拒绝，确认不存在后修正：官方[Secrets文档](https://woodpecker-ci.org/docs/usage/secrets)说明image限制仅适用plugin，当前部署为commands，故沿用repo/tag限定、不加image限制；未放宽正式tag的workflow/step限制。所有值仅在私有文件/内存和受保护API中使用，未写入Git或输出。此时尚无生产Job/应用/后端写，后续tag/流水线真实结果另记。

## 明确未验

本次成功结果只允许称`KUBERNETES_READY`；外部入口、SSE、真实S3 ACL、查询/入库功能、4+4交接和1000QPS/SLO未通过验收，不能因Pod Ready认领。首次共用owner有DDL/控制表写权风险已获批准，普通Green/升级仍拒绝。未知模型额度、P99等留空；原12项真实任务保持未勾选。
