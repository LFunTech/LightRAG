## 1. 评审与实现前置

- [x] 1.1 取得本 proposal/design/spec 的实施批准，确认这是新增 object-backed 链路而非替换 `/documents/upload` 与 `/documents/scan`。
- [x] 1.2 盘点现有文件摄取、pending_parse、parser sidecar、delete/clear/retry/source-conflict 入口，列出必须保持兼容的行为和测试基线。
- [x] 1.3 确认目标 S3-compatible 服务、bucket/prefix、凭据注入方式、测试环境 Secret 名称和对象生命周期策略；只记录名称和能力，不记录密钥值。

## 2. Object Store 配置与核心抽象

- [x] 2.1 先写配置和 preflight 失败用例：未启用时不影响启动，启用但缺少 endpoint/bucket/credential/权限时对象摄取不可用且不静默回退。
- [x] 2.2 新增对象存储配置解析、启动/运行时 preflight、secret-safe 配置导出和禁用态 no-op 行为。
- [x] 2.3 新增 Object Store 协议、S3-compatible 实现和测试 fake，实现 head/get/put/delete/list-prefix、presign upload、prefix 删除与错误分类。
- [x] 2.4 新增 upload session 持久化 namespace，覆盖 issued/completed/expired/aborted 状态、TTL、幂等 complete、并发 complete 冲突和 orphan session 查询。

## 3. Presigned 上传 API

- [x] 3.1 先写 API RED 测试：认证、文件名校验、大小限制、对象 key 不可由客户端指定、签名响应不泄露密钥。
- [x] 3.2 实现创建 upload session 的 API，请求包含 filename/content_type/size/checksum/workspace，响应包含 upload_id/object_key/url/headers/expires_at/max_size。
- [x] 3.3 实现 complete API：HEAD 对象、校验 key/session/workspace/size/content_type/checksum、复用现有 canonical basename 冲突规则，并在校验通过后入队。
- [x] 3.4 覆盖 missing object、metadata mismatch、expired session、foreign key、重复 complete、对象存储暂时不可用等错误响应。

## 4. Object-backed pipeline 与远程 sidecar

- [x] 4.1 先写 pipeline RED 测试：object-backed pending_parse 不读取 `INPUT_DIR`，任意 Pod 可通过对象存储 claim 并解析同一文档源。
- [x] 4.2 扩展 full_docs/doc_status metadata，保留 `file_path` 作为业务来源名，新增显式 object source 元数据并保持本地文档记录不变。
- [x] 4.3 实现 object source resolver：worker 下载源对象到本地 scratch，校验 metadata/checksum，parser 使用 scratch 文件，失败写入可重试的 FAILED 状态。
- [x] 4.4 扩展 sidecar URI resolver，支持 object-backed parsed artifacts 上传到对象存储并在 analyze/chunk/retry 生命周期内下载为 scratch mirror。
- [x] 4.5 覆盖 native、legacy、外部 parser 的 object-backed 路径，确保 parser hint、process_options、chunk_options 和 duplicate content hash 语义保持一致。

## 5. 删除、重试、清理与一致性

- [x] 5.1 先写删除/clear/retry RED 测试：对象源不被当作本地路径，retry 使用已记录对象源，删除失败不伪报全清理。
- [x] 5.2 接入 per-document delete 和 clear：按 owned object prefix 删除源对象与 parsed artifacts，失败时返回/记录可诊断结果并保留恢复线索。
- [x] 5.3 接入 FAILED retry、scan/manual retry/source-conflict 相关逻辑，确保 object-backed 文档不会要求重新上传源文件或被目录扫描错误接管。
- [x] 5.4 实现 upload session GC/cleanup 命令或后台维护入口，仅清理过期未完成 upload prefix，不触碰 completed 文档对象。

## 6. 部署、流水线与文档

- [x] 6.1 更新环境变量示例、API 文档、File Processing Pipeline 文档、对象存储 runbook，以及不含密钥的 WebUI 公开第三方对接页，明确本地上传/扫描兼容与 object-backed 推荐流。
- [x] 6.2 更新 Kustomize 测试部署：通过 Secret 注入对象存储凭据，object-backed 链路使用 `emptyDir` scratch，不创建共享 `INPUT_DIR` PVC。
- [x] 6.3 更新 Woodpecker 部署验收：在 test 集群通过 presigned upload 或等价控制面直传对象、complete、处理、查询/状态、retry/delete 的最小真实链路。
- [x] 6.4 确认 pipeline 日志不输出 presigned URL、access key、secret key、checksum 明文以外的敏感材料；失败日志只包含安全对象标识。
- [x] 6.5 测试环境切换为 object-store-only profile：设置 `ENABLE_LOCAL_FILE_INGESTION=false`，并在服务端拒绝本地 `/documents/upload` 与 `/documents/scan`。

## 7. 验证与收口

- [x] 7.1 运行对象存储、API routes、pipeline、parser、delete/clear/retry 的相关 mirror 测试子集并记录通过数。
- [x] 7.2 运行 ruff、OpenSpec strict validation、必要 shell/YAML 静态检查和 secret-value 扫描。
- [x] 7.3 在本机或隔离 MinIO/COS 环境执行 object-backed 端到端 smoke，验证 API 不承载文件正文且 worker 从对象存储解析。
- [ ] 7.4 触发 Woodpecker test tag，验证测试部署无共享 `INPUT_DIR` PVC 的 object-backed 摄取链路可用，并记录 pipeline/build/deploy 证据。
- [x] 7.5 对照 spec 逐项更新任务和验证记录，列出仍未验证的环境限制或运维事项。

## 验证记录

- `./scripts/test.sh ... -q`（对象存储、API routes、pipeline、parser、delete/clear/retry、CI/Kustomize、PostgreSQL 相关子集）：273 passed。
- `uv run ruff check ...`：All checks passed。
- `sh -n scripts/ci/deploy-test.sh`：通过。
- Kustomize 与 Woodpecker YAML 解析：`yaml ok`。
- `openspec validate add-s3-object-store-ingestion --strict`：通过。
- `openspec validate --all --strict`：4 passed, 0 failed。
- 本机 MinIO smoke：presign 控制面、direct PUT 数据面、complete 入队、worker scratch 下载均通过；未向日志输出 presigned URL 或密钥值。
- 敏感日志扫描：只发现配置字段名/API 字段名/脚本变量名；未发现 `upload_url` 被 logger/print/echo 直接输出，direct PUT 异常路径只输出 HTTP 状态或异常类型。
- WebUI 公开对接页：`/webui/docs/third-party-object-upload-integration/` 作为 Vite public 静态页面发布；页面已升级为“LightRAG 第三方 API 对接指南”，左侧提供目录树并可在总览、认证约定、对象上传、查询问答、图谱接口、状态/重试/删除、错误处理和端到端示例之间切换；对象上传序列图继续作为文档接入章节核心图。静态检查确认页面不包含真实凭据、Secret 文件名或环境 Secret 字段。
- WebUI 公开对接页 API 详解：文档接入、查询/Ollama 兼容、图谱读写、状态/重试/删除/运维章节中的每个 endpoint 均改为独立 `endpoint-detail` 块，逐项说明用途、何时调用、请求、成功响应、常见状态码和第三方接入建议；静态回归测试覆盖 40 个公开对接 endpoint，防止退回一句话 API 卡片。
- WebUI 公开对接页 API 文档格式：40 个公开对接 endpoint 均按标准 API 文档结构呈现“基本信息、请求参数（Request）、响应结果（Response）、状态码与异常说明、代码示例”；每个 API 的请求参数、响应字段和错误码均表格化，并保留可复制的 cURL 示例。
- WebUI 公开对接页 API 索引与版式：左侧目录树扩展为“章节 + 具体 endpoint”两级索引，40 个公开对接 endpoint 均可通过 hash 直达；脚本会根据目标 API 自动切换隐藏章节并滚动到对应条目。正文 API 详解改为标准分节、定义列表和表格化参考，避免到处呈现重复小方块；窄屏下代码示例自动退化为单列，长表格只在表格容器内横向滚动，防止页面级横向溢出。
- WebUI 公开对接页布局：根容器取消桌面固定宽度上限，改为全宽视口布局与自适应 gutter；卡片网格使用容器感知的最小列宽。Playwright CLI 在 1440px 与 390px 视口确认 `main` 与 viewport 等宽，安全约定区域未变形，且页面级无横向溢出。
- WebUI 公开对接页序列图：页面与图都使用可用宽度，取消序列图桌面最大宽度上限；SVG 设计画布由 1180px 拓宽到 1500px，泳道中心距由约 235px 拉开到 318px，避免仅靠整体缩放模拟变宽；图内基础字号控制为标题 18px、消息主标签 10.5px、备注 8px，并隐藏大块标签背景，改用文字描边保证可读性，避免文本框遮挡图中连线。Playwright CLI 在 1440px、1920px 与 390px 视口确认 `main` 全宽，桌面图随容器展开，窄屏只在图容器内部滚动，页面级无横向溢出。
- WebUI 公开对接页交互：Playwright CLI 在 1440px 视口确认文档中心标题为“LightRAG 第三方 API 对接指南”、左侧目录树有 8 个文档入口、默认显示总览；点击“知识检索与问答”后只显示 `query-api` 文档并包含 `POST /query` / `POST /query/stream`，点击“对象上传流程”后切回 `sequence` 文档并显示 1500px SVG；390px 视口下目录改为单列静态位置，页面级无横向溢出。
- 2026-09-15 分布式 object upload session runtime 修复验证：先新增 RED 回归，确认 `_build_object_upload_components` 未给 `upload_sessions` KV storage 绑定 distributed runtime、启动初始化 helper 缺失，且 `/documents/uploads/presign` / `/documents/uploads/complete` 的 session 写入会因缺少 active operation 失败（`./scripts/test.sh tests/api/config/test_object_storage_server.py tests/api/routes/test_object_upload_routes.py -q` → 4 failed, 14 passed）；修复后同一目标测试 18 passed，扩大到 `./scripts/test.sh tests/api/routes tests/api/config/test_object_storage_server.py tests/api/test_distributed_lifespan.py -q` → 653 passed, 27 skipped；`uv run ruff check lightrag/api/lightrag_server.py lightrag/api/routers/document_routes.py tests/api/config/test_object_storage_server.py tests/api/routes/test_object_upload_routes.py`、`openspec validate add-s3-object-store-ingestion --strict`、`openspec validate add-woodpecker-test-delivery --strict`、`git diff --check` 均通过。
- 2026-09-16 test 环境 object-store-only local ingress 禁用验证：先新增 RED 回归，确认缺少 `lightrag.api.local_file_ingestion_middleware` 且本地入口无禁用开关；修复后 `./scripts/test.sh tests/api/config/test_api_config_local_file_ingestion.py tests/api/test_local_file_ingestion_middleware.py tests/api/routes/test_local_file_ingestion_gate.py tests/api/routes/test_object_upload_routes.py tests/ci/test_kustomize_delivery.py tests/ci/test_test_environment_assets.py tests/ci/test_workflows.py` → 75 passed；扩大到 `./scripts/test.sh tests/api` → 1325 passed, 37 skipped；`./scripts/test.sh tests/ci` → 81 passed；`bun install --frozen-lockfile && bun test && bunx tsc --noEmit && bun run lint` → 638 frontend tests passed、typecheck passed、lint 0 errors/1 existing warning；`uv run ruff check ...`、`sh -n scripts/ci/deploy-test.sh`、`openspec validate add-s3-object-store-ingestion --strict`、`openspec validate --all --strict`、`git diff --check` 均通过。
- 2026-09-16 deploy-test stale coordination fence 修复验证：pipeline #69 在旧 deploy acceptance 仍实际调用对象上传/重试业务 API，instance `03` 的 retry presign 返回 `CoordinationUnavailableError` 并遗留 workspace fence；pipeline #70 使用健康检查式 deploy acceptance 后，instance `03` 在 bootstrap 阶段因同一 stale fence 返回 `WorkspaceFencedError`。先新增 RED 回归 `test_deploy_test_script_recovers_fenced_workspace_before_bootstrap`，确认 `deploy-test.sh` 未在 `migrate_coordination_schema` 与 `bootstrap_storage_profile` 之间执行 recovery（1 failed）；修复后同一目标测试 1 passed，`./scripts/test.sh tests/ci` → 82 passed，`sh -n scripts/ci/deploy-test.sh` 通过。本次只在本地修复脚本与文档；7.4 仍需新 test tag 在远端验证 scoped audited recovery 和全实例 rollout。
- 2026-09-16 deploy-test cold image pull timeout 修复验证：pipeline #71 证明 instance `01` 的 coordination inspect/recovery skip 和 bootstrap 均成功，但 rollout 因第二个 Pod 冷拉新镜像约 9m19s，超过脚本 `kubectl rollout status --timeout=600s` 而失败；失败后只读 K8s 检查显示 Deployment 已达到 `2/2 Ready` 且运行期望 digest，说明这是 rollout 等待窗口偏短导致的假失败。先新增 RED 回归 `test_deploy_test_script_allows_cold_image_pull_during_rollout`（1 failed），修复为 `LIGHTRAG_TEST_ROLLOUT_TIMEOUT_SECONDS` 可配置且默认 1200 秒后同一目标测试 1 passed；完整验证见本次后续记录。

## 未完成/待远端验证

- 2026-09-15 `v1.5.47-test` / pipeline #57 已使用 S3 object-store 配置进入测试部署，但在 `lightrag-storage-bootstrap` 阶段失败，尚未到达 object-backed 摄取验收。失败根因为已有 PostgreSQL 业务表缺少 S3 元数据列 `object_source`，而分布式 bootstrap 未先执行业务表兼容迁移；本次修复使显式 maintenance/bootstrap 路径先运行既有 PG `check_tables()` 迁移，再执行 verify。7.4 仍未完成，需新 tag 验证 test 集群完整 rollout、双 Pod object-backed 摄取、retry/delete 和对外 Ingress。
- 2026-09-15 `v1.5.49-test` / pipeline #60 已通过源码归档、Kaniko build、pre-deploy image verification、storage preflight、coordination migration 和 storage bootstrap，随后在 Deployment rollout 阶段失败。Pod startup 日志显示 object upload session 的 `upload_sessions` KV storage 是在 LightRAG 主 storage 列表之外创建的，未绑定 distributed runtime，且初始化没有处于 runtime operation 中；同源问题也会影响 presign/complete 路由的 session 写入。已修复为构建 object upload components 时绑定 runtime，并在 startup 与上传路由中进入显式 distributed operation；7.4 仍需新 test tag 在远端重新验证完整 rollout、对外 Ingress 和 object-backed 摄取链路。
- 2026-09-16 `v1.5.57-test` / pipeline #70 已确认 deploy-test 不再需要实际业务调用即可发现运行状态，但因 pipeline #69 遗留的 fenced workspace 在 instance `03` bootstrap 停止。已补充 deploy-test 的 drained test workspace audited recovery；7.4 仍需新 tag 验证该 recovery 后能继续 bootstrap、rollout 与健康检查。
- 2026-09-16 `v1.5.58-test` / pipeline #71 已确认 scoped audited recovery 不再阻塞 instance `01`，但仍因冷拉镜像超过 600 秒 rollout timeout 停止；已将 rollout/Pod Ready 等待默认提高到 1200 秒并保留环境变量覆盖。7.4 仍需新 tag 验证全实例 rollout、Service route restore 和公共 `/health`。
