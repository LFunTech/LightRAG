# 复用 test 服务的单 Pod 候选尝试（2026-10-23）

本报告保留首次日志目录失败的历史。用户随后批准修正、重建及低负载验证，最新结果见[重建与入库停止记录](2026-10-23-single-pod-ingestion-failure.md)：候选已能启动、真实S3上传通过，但入库期间协调503，仍未完成功能/性能验收，不覆盖本报告中的历史0请求读数。

## 当前结论

用户将当前阶段收敛为一个独立测试实例，授权执行者选择 namespace、生成随机 Secret，优先尽快测试；原 test/prod/Git 边界和异常即暂停不变。已创建候选，但**启动失败，尚未完成任何业务或性能测试**。未自动重建、重启或 recover，保留失败现场。完整脱敏结果见 [JSON](2026-10-23-single-pod-attempt.json)。

这不是重建后端服务，也不是绕过发布协议的安全检查：本阶段明确关闭 `LIGHTRAG_RELEASE_ENABLED`，仅准备单实例功能/低并发基线；原 4×1、4+4 生产规划不变。后续发布验收仍须准备 operator/runtime 分离，不能将本次关闭协议的实例称为蓝绿验收通过。1000 QPS、CLB 及生产启用均未获本轮执行授权。

## 只读核对与隔离方式

- 原五套应用共享现有 PG、HugeGraph 和 S3。PG/coordination 新连接与现有 schema 校验正常，五 workspace 无 fence、待处理文档或 pending mutation；原五套 Deployment、原运行 Secret 及十个 Pod 的身份/配置前后相同。
- 现有 PG 账号与 test 清单的 PG 凭据相同，无 CREATEDB/CREATEROLE，但可注册新的协调 workspace。没有发现独立 operator 引用，不擅自创建角色、扩大原账号权限或创建发布控制 schema。独立数据库不是本阶段前置。
- 图服务现有 schema 只读校验通过，拟用图 scope 无顶点；S3 bucket HEAD 为 200，新 prefix 的有界列表为空。未在只读阶段调用会 PUT/DELETE 的对象存储 preflight。
- 这些 workspace、图 scope、S3 prefix 是代码约束的逻辑隔离，**不是已证明的数据库 RLS、图租户 ACL 或 S3 IAM 强隔离**；不能据此允许任意压力或宣称共享资源不受影响。

## 实际创建范围

| 项目 | 实际值 |
| --- | --- |
| 环境 / namespace | test / `lightrag-loadtest-20261023` |
| 应用 | 单个 `candidate` Pod，1 worker，`restartPolicy=Never` |
| workspace / deployment id | `lightrag_lab_20261023` |
| S3 prefix | `lightrag/release-lab-20261023/object-ingestion` |
| API 密钥 | 新随机 384-bit 值，仅保存在新 namespace 的不可变 Secret，不输出/保存到本地证据 |
| 数据连接 | 只复用必要的 test 后端/模型字段；未读取或导入 prod 凭据 |
| 入口及资源 | 私有 ClusterIP、入站 NetworkPolicy、ResourceQuota、无 Ingress、无 K8s token；应用 requests 500m/1Gi、limits 2 CPU/4Gi |
| 运行约束 | 发布协议关闭、分布式协调开启、S3 开启、本地文件入库关闭、图 schema 自动创建关闭、查询指标开启、答案缓存关闭 |

共创建18个新资源，包括 namespace、配额、运行/拉取 Secret、源码分片及 manifest ConfigMap、私有 Service/NetworkPolicy 和唯一候选 Pod。未创建数据库、图或 S3 基础组件，没有改原 test 资源/流水线。

固定基础镜像 digest 为 `sha256:82be927d0d3e8123179587311f2d12c5f8292fd5ac7fe43cd836d8f65a8614ca`。当前 dirty 源码以非生产只读 overlay 加载，源清单 SHA256 为 `742e8bb3435c93045f854d8fcaf6f2e5cc6a93817fae1ad52556ccfdff907e5b`。init container 验证全部419个文件与分片/归档 hash，并完成选定依赖导入；这不等于完整应用启动或依赖验收，不冒充已提交构建镜像。

文件上传原件仍按 **presign → 直传 S3 → complete → 后台物化/解析** 的既有链路设计。PG 保存文档状态及检索数据，图服务保存图；Pod 的 scratch 是临时工作空间，运行日志不是业务文档存储。本次没有实际上传任何文档。

## 启动失败与停止现场

- init 校验成功，应用进程于 UTC `00:12:44` 启动、`00:14:18` 以 exit code 1 结束，Pod `Failed`、重启0次；120秒 Ready 等待未通过，不把 TCP readiness 当作业务验收。
- 首个明确异常为 `configure_logging()` 的 `ValueError: Unable to configure handler 'file'`，根因 `FileNotFoundError`：`/app/data/logs/lightrag.log`。
- 执行者给新候选设置了尚不存在的 `LOG_DIR=/app/data/logs`。当前函数执行 `os.makedirs(os.path.dirname(log_dir))`，只创建父目录，不创建 `logs` 本身；候选直接运行 Python，未由其他初始化步骤准备子目录。这是候选运行目录前置遗漏，不能归因于 PG、图服务或 S3 不可用。
- 此处位于 `create_app()` 和 lifespan **之前**。随后只读核验新协调 scope、doc-status 数据域仍不存在；API 请求、fixture 上传、RAG 查询、模型调用均为0。没有 schema/角色迁移、bootstrap、recover 或业务写入。
- 离线提取并执行真实 `configure_logging()`：缺少子目录时复现 FileNotFoundError；将 LOG_DIR 指向已存在的可写根目录后日志配置成功。首次本地复现 harness 缺少 `logging.config` 导入，仅是本地观察器错误，补齐导入后完成该验证，未因此做远端变更。

下一步建议仅将**该候选**的日志目录改为已有可写挂载目录 `/app/data`，确认继续后重建失败候选，再开始协调/入库/查询与有界低并发验收。未应用此修正、未删除失败 Pod，也未追加负载或自动恢复。

主任务仍 **41/53完成、12未完成**，8.3/8.6/10.1/10.2/10.8等不勾选，未知正式参数继续留空。没有性能、质量、4+4、CLB 或生产通过结论；未提交、推送、打 tag，prod workflow gate 保持关闭。

记录核验：OpenSpec scoped strict、`git diff --check`、原四 workflow/test 脚本及 base/test overlay 对 HEAD 无差异检查通过；部署记录、当前419文件与 overlay hash、41/53任务、57场景及本轮文档链接核对通过。本轮没有修改应用源码或运行仓库测试套件，离线日志前置复现不替代远端功能/性能测试。
