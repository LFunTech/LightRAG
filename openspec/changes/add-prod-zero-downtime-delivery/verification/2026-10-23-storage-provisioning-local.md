# 首次存储迁移补全：本地验证

日期：2026-10-23。范围为本轮获准的代码和本地验证；**没有执行生产迁移、部署、切流、模型/压测、密钥读取/导入或 Git 发布**。原 test 流水线/配置/实例/数据和两个候选现场不变，生产 workflow 静态 gate 仍为 false。

## 实现

- `lightrag/provisioning/`：冻结 v1 的9业务表、3模型/维度向量表、7图定义，独立 `lightrag_storage` 版本/profile/checksum/结构账本。PG 事务及会话级 try-advisory-lock 串行化；拒绝无版本旧业务表和不兼容结构，不自动升级向量扩展/模型/索引。
- 独立调用原 coordination/release migration，原 SQL/checksum 不变；校验预置 runtime 角色与 operator 分离、无 DDL/迁移控制权限、业务 DML 完整，不创建角色/密码/数据库/图实例/bucket。
- 图 POST 前 autocommit 保存 EXECUTING 意图；未知 ACK 不重放，GET 必须证明精确匹配且 task success/CREATED 才结算。缺失/漂移/失败/超时保持拒绝，不删图、不伪造跨存储回滚。图请求不占 PG 写事务，session 迁移锁保留。
- 离线 plan + 受目标/hash/确认/身份/预算保护的 apply/verify/register CLI；DSN 显式传输策略、解码用户名并拒绝 query 改目标。scope 登记与结构迁移、release enroll 分开，不启动应用或模型。
- 初始化 Job 仅本地渲染固定 digest/已有 Secret 引用，无 Secret 生成、K8s 调用、应用/入口/RBAC/卷；预算/真实引用需单独确认。新资源打进 wheel 和 sdist。
- 结构校验明确拒绝本v1不支持的RLS、FORCE RLS、policy和用户trigger，不把受限身份看到的空集合当空库。
- 真实 release runtime 在 coordinator 准入前以 runtime 身份只读核对版本/结构/权限/显式 manifest 登记；生产普通和 maintenance 不补扩展/表/索引/图 schema。release 关闭时不添加旧 test/local 的迁移前置。

## 验证记录（最终结果）

- 本轮独立本地 PG18.6/pgvector0.8.7，仓库 `temp/provisioning-local-20261023/` 私有集群，动态回环端口，数据库 migration_test_*/release_test_*/coord_test_*。测试后自动停止；不复用共享本地服务，更不连接 test/prod。
- 迁移/发布/协调联合组：`sh temp/provisioning-local-20261023/run-tests.sh tests/provisioning tests/distributed/release tests/distributed/test_coordinator_integration.py tests/distributed/test_pipeline_control_integration.py tests/distributed/test_pipeline_scheduler_integration.py tests/distributed/test_api_guards_integration.py` → **211 passed，2现有websockets弃用警告，15.87s**（审查修复及完整MockHTTP登记闭环后的最终复跑）。其中图 HTTP 为 MockTransport，PG 事务/角色/目录/跨连接意图实际执行。
- 更早单组56 passed、离线后续21 passed、联合200/210中间结果仅为切片，与最终联合组不重复相加。
- 扩展第一次为199 passed/1 failed：旧测试写死coordination版本2但仓库已3，校正为完整(version,checksum)列表；不改迁移。失败日志保留，随后200通过，审查新增10用例后210通过，补显式端口拒绝后最终211通过。
- 原受影响目录 `tests/distributed tests/kg/postgres_impl tests/kg/hugegraph_impl tests/ci tests/test_docstring_budget.py` → **1070 passed，139 skipped，2现有warning，14.50s**。API配置/计量额外组 **158 passed，2.17s**；最终合并子集命令：`PYTHON_DOTENV_DISABLED=1 ./scripts/test.sh tests/provisioning tests/distributed tests/kg/postgres_impl tests/kg/hugegraph_impl tests/ci tests/api/config tests/api/routes/test_query_accounting_routes.py tests/api/test_query_performance_health.py tests/test_docstring_budget.py` → **1273 passed，166 skipped，2现有warning，11.40s**。其中72项provisioning用例在独立PG联合组全部执行；默认组跳过需显式外部fixture的integration，不把重叠次数累计为不同用例。
- `setuptools.build_meta.build_wheel/build_sdist` 本地无隔离/无下载构建成功；wheel、sdist均含4项迁移资源，未含 `.secrets`/kubeconfigs 路径。`python -m build` 不可用（本仓库 build 目录遮蔽模块），未为构建安装额外工具，改用项目声明的 setuptools PEP517 后端。
- Ruff check（全部本轮Python与接线/旧测试校正文件）、Ruff format check（本轮新目录及runtime，19文件）、`git diff --check`、change-scoped OpenSpec strict通过；62/62场景标题都有矩阵对应、278相对链接目标存在。原coordination迁移SQL无diff，原test交付基线/生产gate关闭的测试通过。
- [唯一独立审查](2026-10-23-storage-provisioning-review.md)发现4项Important，全部RED→GREEN后完成最终两组复跑：Gremlin vlabel/elabel、DSN双斜杠及缺省端口、图名点路径、RLS/policy/user-trigger语义漂移。无Critical、无递延Minor，不派第二次代理复审。
- 最终wheel/sdist的16项本轮源码/资源逐字节与工作区一致，hash见[机器记录](2026-10-23-storage-provisioning-local.json)；仅本地构建，不是正式发布镜像或tag。临时PG已停止，无遗留监听；未改共享本地PG。

## TDD 与决策

缺包/PG/图/CLI/Job及生产扩展/表/图门禁均观察到 RED 后实现；新增回归覆盖ledger写权限、scope/参数、DSN编码与 query 覆盖、runtime缺显式登记、图失联/已存在/缺失/失败/超时、另连接可见意图、caller事务拒绝、并发迁移与 release关闭兼容。测试本身误用静态方法产生的一次 AttributeError 不计作行为 RED。

详细 Ruling 与审查记录在 [implementation-ledger](implementation-ledger.md)；采用本仓库 Python/asyncpg，不引入其他项目的 Flyway/Alembic。初版仅接空目标或同版本重入，无版本旧数据另审；图失败可留部分已准备结构，不能自动回滚。图 GET 空域与 PG 登记不构成跨库事务，必须有外部维护排他，工具不能证明未知 writer 不存在。

## 仍需单独确认/授权

1. 生产 runtime/operator PG 与图角色/权限整改及现有凭据引用；具体迁移镜像、graph/job预算、独立维护/备份与已核验 profile/plan/manifest hash。未知值未填入正式参数。
2. 目标 PG17.6/pgvector0.8.0 与 HugeGraph1.7.0 的真实迁移/ACL/取消恢复演练；本地 PG18.6 和 Mock 图不能代签。
3. 按流程执行真实 `apply → verify → register → enroll`，随后独立 Blue4×1 与查询/写入、S3 GET/写删、4+4/CLB/质量和容量验收；未通过不得接生产流量。
4. 当前 CLB/权限隔离/工作目录与资源预算、模型配额等原12项真实待办仍未完；1000QPS/约10000在线完整 RAG、100/200/500ms、3s、20s及99.9%目标未签署。
5. 生产启用和 Git 发布需单独授权。**完成本地补全不等于现在可走生产流水线。**


## 任务状态

本轮新增11.1–11.6已完成本地实施/验证，当前 **47/59**，原 **12项真实待办**保持未完，62场景分层记录。未知正式参数及审批元数据未改；没有开启生产流水线。
