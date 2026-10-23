# 本地实现验证（非生产上线验收）

日期：2026-10-21。工作区原地实施，HEAD 仍为
`beb4d64681c06d393255c577cdf0d988fdeecbfb`，未提交/推送/tag。
没有导入密钥、访问或操作 test/prod、recover、改变既有 PostgreSQL 或触发流水线。

**快照边界**：本文件及 `2026-10-21-local-results.json` 记录性能方案补充前的原 45 项/48 场景交付验证。
后续用户仅授权补充缓存/性能文档，新增 8 项任务及 4 个场景均未实施；当前 39/53 的进度见
[tasks.md](../tasks.md)，52 场景状态见[矩阵](scenario-evidence.md)。下述测试数字保持历史事实，
不能作为新增性能能力、指标接线或真实压测通过的证据。

## 最终实际执行结果

| 检查 | 实际命令/范围 | 结果 |
| --- | --- | --- |
| 全后端里程碑 | `DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib .venv/bin/python -m pytest tests -q` | **9330 passed, 355 skipped, 0 failed**, 206.41s；2 条 upstream websockets 弃用警告 |
| 真实隔离 PG18.6 | `sh temp/prod-delivery-implementation/run-local-pg-tests.sh tests/distributed/release -q` | **68 passed**, 3.32s；2 条同类警告；自动停止服务 |
| 现有/新增 CI、docstring | `./scripts/test.sh tests/ci tests/test_docstring_budget.py -q` | **144 passed**, 2.57s |
| 最终 unused-import 清理后保留策略复跑 | `./scripts/test.sh tests/object_storage/test_release_retention.py -q` | **2 passed** |
| 静态交付 | `bash scripts/ci/delivery-check.sh` | **5 workflow strict lint**、Python/shell/JSON 检查、test Kustomize render 全部通过；没有执行/新增 CI 仓库测试步骤 |
| Python lint | `.venv/bin/ruff check` 全部本次变更的后端/交付 Python 及新增测试 | **All checks passed** |
| 编译 | `.venv/bin/python -m compileall -q` release 包、delivery 包、release API/lifecycle | 通过 |
| prod 离线 render | `tests/ci/test_prod_kustomize.py`（包含于上述 CI） | 真实 `kubectl kustomize`，两个独立 Deployment 共 8 Pod、无 NGINX/test 默认残留、私有输出权限通过 |
| 源码/目标基线 | SHA256 对照实施前 baseline | 除获准增量 `delivery-check.sh` 外 **35 文件完全一致**；包含原 4 workflow/test 脚本/全部 base/test overlay 和原 CI 测试 |
| 打包 | `.venv/bin/python setup.py build --build-base temp/prod-delivery-implementation/final-wheel-build bdist_wheel --bdist-dir temp/prod-delivery-implementation/final-wheel-bdist --dist-dir temp/prod-delivery-implementation/final-dist` | 成功；396 entries；27 个 release/delivery Python/SQL 文件与当前源码逐字节一致；无 node_modules/密钥/kubeconfig/temp 路径 |
| 差异检查 | `git diff --check` | 通过 |
| OpenSpec | `openspec validate add-prod-zero-downtime-delivery --strict`；48 场景与文档链接逐项审计 | **valid**；39/45 本地完成、6 项远端保留未完成 |

上述检查有重叠，**不能累加为独立测试总数**。全量默认跳过的 integration 已如实保留；
仅新 release integration 在隔离本地 PG 下另行开启，不声称其余跳过项通过。
没有前端修改，因此未跑 Bun/TypeScript/ESLint。API 启动提示本地 WebUI build 比源码旧，
未为本任务重建/变更用户前端产物；生产仍走原 Dockerfile 的 WebUI 构建链。

全量运行早期曾有本机 Cairo 搜索路径失败和原 HEAD docstring 预算失败；前者只设置上述
**进程环境**，后者压缩文档并定向复测，没有删除/skip 用例。扩大 lint 发现的新测试
unused import 已删除并复跑；不隐藏初次失败。原有 query-accounting 模块和 query routes
hash 与实施前相同；server/OpenAI 文件仅在保留用户原改动基础上加 release-only 接入/流关闭。

## 验证层级

- **真实本地**：独立 PostgreSQL 18.6 的 SQL migration、schema/权限、scope 行锁、原
  coordination pending/ACK 与 release ticket/owner/epoch/队列；原 FastAPI handlers、
  auth/ASGI、OpenAI helper 生命周期，以及 loopback 真 Uvicorn 长流退出。
- **合成或 fake**：业务测试表/文档、外部模型、HugeGraph、S3、Kubernetes API、CLB API
  与入口应答。可以证明代码拒绝/排序/身份校验，不证明云端传播、真实业务一致性或吞吐。
- **未运行**：真实腾讯云 TKE/CLB/网络/多节点 RWX、prod/test ACL/Secrets、真实构建镜像
  smoke、4/8 Pod 持续零停机演练、容量/SLO、所有真实 writer 基线、第一次 prod 发布。

PG 数据、socket、日志都在仓库 `temp/prod-delivery-implementation/pg/`，随机 loopback
端口和 `release_test_*` 合成数据库；fixture 拒绝非 loopback/非测试库。结束时实际执行
`pg_ctl status` 返回 **no server running**，没有停止或修改原本存在的数据库服务。

## 依赖与完成度

- `add-woodpecker-test-delivery` 仍有 CI 权限/真实 test tag/远端存储与失败验收未勾选。
- `add-s3-object-store-ingestion` 的真实 test tag/API smoke 未勾选。
- `add-distributed-workspace-writes` 原任务已完成；这不等于本次蓝绿验收完成。
- 本 change 只勾选本地实现/验证项，保留 8.3、8.6、8.7、9.2、9.3、9.4 待独立授权。
- [48 场景逐项对照](scenario-evidence.md)、[最终审查修复](final-review.md)、
  [操作手册](../../../../docs/ProdDelivery.md) 是实现与接入边界的共同记录；不自动归档。

## 上线前硬前置

1. 确认首次/已有环境路径、全部 writer/SDK 的协议基线及实际读写兼容批准；必要计量
   authority 未证明可靠性时当前会阻止启用。
2. 核对真实目标 UID/endpoint、PG/图/S3 ACL、RWX/调度/资源与 8 Pod 聚合后端/模型预算，
   审批有限排队/排空/切流/超时链；当前 4 Pod 不是高并发容量结论。
3. 确认 qcloud HTTPS/direct controller、CLB listener/location/cert、所有超时/断流属性、
   实际 imageID 格式与受限网络出口；完成真实持久在线/流式切流与故障演练。
4. 单独授予/导入 prod kubeconfig、operator DB 与 CLB 只读权限、应用 Secret/registry 引用；
   `.secrets/prod.secrets` 不代表这些权限已经具备，不能借 test/COS 凭据补缺。
5. 另行批准维护初始化/迁移/接入、Git 发布、解除 workflow 静态 gate 和首次正式 tag 发布。

## 证据文件

完整 stdout/初次失败留在 ignored `temp/prod-delivery-implementation/`，最终结果摘要由
本文件持久保存。最终 wheel SHA256：
`ad7dd6967592cce5892bdfe8f2933d765feb539b384acfa1661a9f4bc3a2f1c5`。
最终产物审计另存 `final-artifact-audit.json`；OpenSpec scoped strict 与 48 场景矩阵/本地文档链接
审计均已通过，最终结构化结果见 [2026-10-21-local-results.json](2026-10-21-local-results.json)。
校验有效不等于远端验收通过。
