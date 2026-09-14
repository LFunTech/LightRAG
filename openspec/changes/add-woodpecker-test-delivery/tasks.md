## 0. 本机基础镜像同步（用户单独授权，其余实施仍待批准）

- [x] 0.1 盘点三个 Dockerfile 的全部外部 FROM/COPY 输入，固定源 index digest 与多架构清单；后续 Kaniko 切换移除了不再使用的 Dockerfile frontend parser directive。
- [x] 0.2 从本机同步完整基础镜像到 `docker-hub.f123.pub/base/` 独立标签，核对 index、子 manifest 及 blob 可读性，不覆盖共享标签。
- [x] 0.3 仅在目标验证成功后，将三个 Dockerfile 改为内部 digest-pinned 引用，生成对应镜像锁定清单。
- [x] 0.4 验证内部镜像读取与 Dockerfile 构建检查，运行镜像来源回归测试，完成操作文档与验证记录；本切片遵循用户要求提交推送到 master。

## 1. 评审与实施前置

- [x] 1.1 取得本 proposal/design/spec 的实施批准，记录 test 集群、`lightrag-test` namespace 和首次初始化维护边界。
- [ ] 1.2 核对 Woodpecker 仓库接入、受保护 tag、Secret 事件/镜像限制、COS/registry 权限及命名空间级部署凭据；只记录名称和验证结果。
  - 2026-09-14 只读核对：Woodpecker `LFunTech/LightRAG` 已启用，默认分支 `master`，仓库为 public；global secrets 存在 `DOCKER_USERNAME`、`DOCKER_PASSWORD`、`kubeconfig_test`（tag 事件可用），organization secrets 存在 `cos_storage_endpoint`、`cos_storage_bucket`、`cos_storage_secret_id`、`cos_storage_secret_key`（tag 事件可用），repo-local secrets 为空。GitHub rulesets 当前为空，`lightrag-test` namespace 当前不存在，因此受保护 tag 与命名空间级部署资源仍待管理员显式完成。
- [x] 1.3 验证 Kaniko 构建探针：内部工具镜像 digest、registry auth、amd64 构建参数、cache-repo 和临时存储预算；失败不自动提权或挂载宿主 Docker socket。

## 2. 本地回归基线与流水线静态门禁

- [x] 2.1 隔离本地配置复现 API prefix 两个失败，核对 API-key-only 与账号模式合同；仅在证据充分时修正测试 fixture/精确断言，补全匿名、错误密钥、合法密钥和两种 prefix 模式覆盖。
- [x] 2.2 保留可复用的本地后端检查入口，按 lock 安装依赖及必要系统库，运行完整非 integration 测试时保留统计和跳过原因；确认 Woodpecker 不调用该入口。
- [x] 2.3 保留本地前端检查入口，从正确目录执行 frozen install、完整 Bun 测试、typecheck、lint、build；确认 Woodpecker 不调用该入口。
- [x] 2.4 添加 tag-only 交付静态工作流，确保 master push/PR 不触发 Woodpecker、不接收发布/部署凭据、不运行仓库测试套件，并验证静态依赖失败阻止后续发布。

## 3. 发布身份与源码归档

- [x] 3.1 先添加身份校验回归测试，再实现仓库/tag/commit/master 来源校验，覆盖无效 tag、浅克隆、远端错误和移动 tag。
- [x] 3.2 实现 LightRAG 独立 COS 源码归档与发布记录，绑定 commit/pipeline 身份和校验和，不归档本地环境、秘密或业务数据。
- [x] 3.3 实现消费者身份/校验和验证与安全解包，覆盖篡改、错误记录、路径穿越、符号链接逃逸和外部命令失败；补齐 validate 工作流。

## 4. 镜像构建与验证

- [x] 4.1 使用已验证的 Kaniko 构建现有完整 Dockerfile，设置 amd64、资源预算、独立 registry cache 及 source/revision 标签；Dockerfile/Dockerfile.lite 不含 BuildKit-only 语法且不创建另一份生产 Dockerfile。
  - 2026-09-14 `v1.5.22-test` / pipeline #30 已进入完整 BuildKit 构建并实时输出日志，但旧 Dockerfile apt 阶段仍使用 `deb.debian.org`，构建下载速度过慢；已停止该过时流水线并按 haier-demo 模式将三个 Dockerfile 的 apt 默认源改为清华镜像。rootless 完整构建成功仍待新 tag 远端验证。
  - 2026-09-14 `v1.5.23-test` / pipeline #31 确认 `validate-release` 只 clone 一次、源码包上传到 COS 并由 `build-image` 下载校验；BuildKit rootless pod 成功启动且 apt 已命中清华源。排查时 Woodpecker build 日志停在 `#23 ... Fetched 80.6 MB in 45s`，但 Kubernetes 侧可见该层继续执行 apt 解包/配置并完成；真正问题是 rootless dpkg 阶段慢、旧 rustup 仍访问 `sh.rustup.rs` 且 `curl | sh` 会吞掉下载失败、后续 `uv sync` 仍默认走公网 PyPI。已停止该过时流水线，构建源修复后仍待新 tag 远端验证。
  - 2026-09-14 `v1.5.25-test` / pipeline #33 在 build 前的旧 `clone-source` 步骤超过十分钟未完成；Kubernetes 侧确认 `git reset --hard` 处于 `D` 状态，wait channel 为 `nfs_wait_bit_killable`，根因为在 Woodpecker NFS workspace 物化 Git worktree。已将 `validate-release` 改为单一 `release-source` 步骤，在容器本地 `/tmp/lightrag-release-source` 完成 clone、静态校验、归档、校验和上传，仍保持唯一 clone 与下游 MinIO/COS 下载模型；完整远端成功仍待新 tag 验证。
  - 2026-09-14 `v1.5.26-test` / pipeline #34 验证 `release-source` 在 `/tmp/lightrag-release-source` checkout 并上传源码包成功，`build-image` 随后进入 BuildKit 与清华 apt/rustup 阶段；继续排查发现 `uv sync --frozen` 仍通过 `uv.lock` 中的 `files.pythonhosted.org` artifact URL 连接 Fastly。已将 `uv.lock` 的 registry 与 artifact URL 改为 `https://mirror.f123.pub/repository/pypi/simple` / `https://mirror.f123.pub/repository/pypi/packages/` 并增加回归测试；完整远端成功仍待新 tag 验证。
  - 2026-09-14 `v1.5.27-test` / pipeline #35 验证 `release-source` 仍在 `/tmp/lightrag-release-source` 完成唯一 clone 并上传源码包；`build-image` 下载源码后进入 rootless BuildKit，但日志停在 `#23` apt 下载中段，step 运行约 11 分 46 秒后 exit 1，Woodpecker 未保存最终错误行。按 haier-demo 可用流水线改为内部 digest-pinned Kaniko，移除 Dockerfile/Dockerfile.lite 的 `RUN --mount`/`$BUILDPLATFORM`，并删除未使用的 BuildKit delivery 入口；完整远端成功仍待新 tag 验证。
  - 2026-09-14 `v1.5.28-test` / pipeline #36 远端证明 clone 修复：`release-source` 在 `/tmp/lightrag-release-source` 完成并约 69 秒结束，`build-image > download-source` 13 秒结束。Kaniko 构建随后显示 Cargo 来自 Dockerfile 历史兜底安装，仓库无 Rust 源码且 locked amd64 依赖已从 wheel 安装；新的无输出停顿点是 `lightrag-download-cache` 的 `pip download` 访问 GitHub spaCy wheel 且 capture 了 pip stdout。已停止该流水线并移除 Dockerfile/Dockerfile.lite 的 Rust/Cargo toolchain、build-essential/pkg-config 以及构建期 tiktoken/spaCy 缓存下载；完整远端成功仍待新 tag 验证。
  - 2026-09-14 `v1.5.29-test` / pipeline #38 验证删除 Rust/离线缓存后，Kaniko 构建进入 `uv sync` 并在无 Cargo/GCC/模型缓存下载下完成 193 个依赖安装；随后默认 full snapshot 在大型 `.venv` 层数分钟无输出但 Pod 仍有 CPU，根因为 Kaniko 内容 hash 快照成本。已增加 `--snapshot-mode=redo` 回归与 build 参数；完整远端成功仍待新 tag 验证。
  - 2026-09-14 `v1.5.30-test` / pipeline #39 验证 `--snapshot-mode=redo` 已进入 Kaniko 参数，但旧 Dockerfile 仍让 Kaniko 为后续 stage 保存 `root/.local`、`app/lightrag` 和大型 `app/.venv`，日志停在 `Saving file app/.venv for later use`。已删除 release Dockerfile/Dockerfile.lite 的 Python builder stage，改为 final stage 直接安装 locked Python 依赖，避免跨 stage 保存/复制 `.venv`；完整远端成功仍待新 tag 验证。
  - 2026-09-14 `v1.5.31-test` / pipeline #41 通过完整 Kaniko 构建：只剩前端产物跨 stage，final stage 直接安装 193 个 locked Python 依赖，apt 命中清华镜像，镜像成功推送并输出 digest `sha256:42df257a4bb8a244633bceb6b96fd7495a8ab262f43a89efa9ab0bbea927e8a9`；build-image 用时约 4 分 40 秒。
- [x] 4.2 安全生成与清理 registry 凭据，发布到 `docker-hub.f123.pub/lfun/lightrag`，实现既有版本冲突拒绝及来源一致的幂等重试。
- [x] 4.3 校验 manifest/index、平台、revision 和 digest，持久化发布记录，添加镜像身份不符和未知格式等拒绝路径测试。
  - 2026-09-14 pipeline #41 的 `pre-deploy > verify-image` 已使用 registry 记录解析并验证 `v1.5.31-test` 指向 commit `c4fe1e2241943bc9777eda7dccb151aeb73844ac`、digest `sha256:42df257a4bb8a244633bceb6b96fd7495a8ab262f43a89efa9ab0bbea927e8a9`。
- [x] 4.4 完成 build-image/pre-deploy 工作流，验证失败无后续副作用，并确认 pre/prod tag 不部署。

## 5. Chart 与测试部署配置

- [x] 5.1 先补 Kustomize 回归测试，再增加 digest replacement 及全程 Service 路由暂停能力；保留既有 Helm chart 默认 local 模式不作为 Woodpecker 部署入口。
- [x] 5.2 提供 test 专用分布式 values：两 Pod、每 Pod 一个进程、共享 PG/HugeGraph/工作区与路径、非 root、显式 Secret、连接池预算及足够的退出宽限期。
- [x] 5.3 提供命名空间级发布 RBAC、内部访问控制配套与前置检查，禁止公网 Service/Ingress，避免将模型/数据库凭据放入构建步骤或日志。
- [x] 5.4 提供首次环境准备与显式初始化操作文档，覆盖专属数据库/图域、两个 RWX PVC、真实百炼配置、鉴权、受控出站和已有 bootstrap 维护流程。

## 6. 保守自动升级与验收

- [x] 6.1 先建立发布状态机及命令替身测试，再实现环境身份/初始化/profile 检查、发布原子锁、旧发布拒绝与幂等重试。
- [ ] 6.2 实现旧流量撤下、缩容到零与优雅退出证据收集，拒绝超时强删、额外写入者和不确定退出，不用固定 sleep 充当完成证明。
- [ ] 6.3 实现受控检查 Job，验证协调及存储兼容状态；遇到 fence、orphan、active、pending、残留 claim/lock 或读取错误时停止，不自动 bootstrap/migrate/recover。
- [x] 6.4 实现已验证 digest 的新版本 Kustomize 启动，验收前保持 Service 路由关闭；失败保留现场，不调用自动回滚或自动启动旧版本。
- [ ] 6.5 实现逐 Pod imageID/健康/鉴权/分布式状态检查、唯一测试文档的跨 Pod 入库与查询、成功后恢复路由及 ClusterIP 验证。
- [ ] 6.6 补全拒绝路径测试：并发发布、遗留发布锁、旧版本覆盖、SIGKILL/超时、未知写入、profile 不兼容、错误 digest、验收失败、Secret 缺失与外部命令错误。

## 7. 验证与真实接入

- [x] 7.1 运行 Woodpecker strict lint、交付脚本本地测试/静态检查、Kustomize render 和相关 manifest 回归；保存命令、版本及结果。
  - 2026-09-14 本地验证：`./scripts/test.sh tests/setup/test_docker_base_images.py tests/ci` → 67 passed；`uv run ruff check scripts/ci/delivery.py tests/ci/test_workflows.py tests/ci/test_delivery.py tests/ci/test_source_artifact_script.py tests/setup/test_docker_base_images.py` → pass；`WOODPECKER_SERVER=https://woodpecker.f123.pub woodpecker-cli lint --strict .woodpecker/*.yml` → 4 个 workflow valid；`scripts/ci/delivery-check.sh` → pass，包含 shell/Python/JSON、Kustomize render、strict Woodpecker lint 和 OpenSpec strict validation；`openspec validate add-woodpecker-test-delivery --strict` → valid；`git diff --check` → pass；`docker buildx build --check --platform linux/amd64 -f Dockerfile .`、`Dockerfile.lite`、`Dockerfile.postgres` → Check complete, no warnings found。版本：woodpecker-cli 3.14.1、OpenSpec 1.9.0、kubectl client v1.36.1 / kustomize v5.8.1、uv Python 3.12.11、ruff 0.15.12。
  - 2026-09-14 构建源修复验证：Dockerfile package source 回归覆盖 PyPI 与 Bun/npm 镜像要求；uv/pip 默认源改为 `https://mirror.f123.pub/repository/pypi/simple`，防止回退到公网 PyPI。
  - 2026-09-14 clone/NFS 修复验证：新增 workflow 回归确保 `validate-release` 只有一个 `release-source` 步骤，clone worktree 使用容器本地 `/tmp/lightrag-release-source`，并且不会在 `/woodpecker/src` 执行 `git init`/`git reset`；`./scripts/test.sh tests/setup/test_docker_base_images.py tests/ci` → 68 passed；`uv run ruff check tests/ci/test_workflows.py`、`WOODPECKER_SERVER=https://woodpecker.f123.pub woodpecker-cli lint --strict .woodpecker/*.yml`、`scripts/ci/delivery-check.sh`、`openspec validate add-woodpecker-test-delivery --strict`、`git diff --check` 均通过。
  - 2026-09-14 uv.lock 镜像修复验证：新增回归测试先在公网 `uv.lock` 状态下失败，再改为 f123 mirror URL 后通过；`UV_DEFAULT_INDEX=https://mirror.f123.pub/repository/pypi/simple uv sync --frozen --dry-run --no-dev --extra api --extra offline --no-install-project --no-editable` 接受改写后的 lock；大 wheel 抽查显示目标镜像路径可返回，但 `pyarrow` mirror artifact 当前 GET 无响应，且该包来自 evaluation extra，不在当前 Docker `--extra api --extra offline` 安装集内。
  - 2026-09-14 Kaniko 切换本地验证：先新增 RED 回归覆盖 build-image step 使用 haier-demo 风格 Kaniko、Dockerfile 禁止 BuildKit-only `RUN --mount`/`$BUILDPLATFORM`/parser directive、delivery 模块不保留未使用 BuildKit 入口；随后改为 `docker-hub.f123.pub/devops/kaniko:v1.14.0-debug@sha256:1b282be1c4467618e9122d2ae457ffa3776a7caa52d7539e329123283bc71f79`、Kaniko cache repo `lfun/cache-lightrag`，并让 `scripts/ci/build-image.sh` 输出 context/cache/digest 进度。`docker buildx imagetools inspect` 确认上游 source index 为 `sha256:d1173d94ddd1092aaf88c929922efde0beee6b3ebfed53bbb86475128a41def9`，内部 mirror index 为 `sha256:660af157e453dfd327d5ded96a652d1e279cd3b0f3e68f047482f0510c288804`，pinned amd64 manifest 为 `sha256:1b282be1c4467618e9122d2ae457ffa3776a7caa52d7539e329123283bc71f79`；在 Kaniko debug 镜像内用 fake executor 执行 `sh scripts/ci/build-image.sh`，验证参数、digest 文件和 stdout 输出。`./scripts/test.sh tests/ci/test_workflows.py tests/setup/test_docker_base_images.py tests/ci/test_delivery.py` → 54 passed。
  - 2026-09-14 Rust/离线缓存删减验证：先新增 RED 回归要求 `Dockerfile` / `Dockerfile.lite` 不含 `rustup`、`RUSTUP_`、`.cargo/bin`、`build-essential`、`pkg-config`、`lightrag-download-cache`、`spacy_models`、`TIKTOKEN_CACHE_DIR`，确认失败后删除这些构建期输入；随后在无 `cargo`/无 `gcc` 的 `docker-hub.f123.pub/base/uv:python3.12-bookworm-slim-lightrag-e5b65587bce7@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58` 容器内执行 `uv sync --frozen --no-dev --extra api --extra offline --no-install-project --no-editable`，193 个 locked 依赖从 f123 PyPI wheel 安装成功，证明当前 amd64 release 依赖集不需要 Cargo/GCC 编译兜底；再新增 RED 回归要求 `scripts/ci/build-image.sh` 包含 `--snapshot-mode=redo` 且不启用实验性 `--use-new-run`，确认失败后添加该 Kaniko 参数；`./scripts/test.sh tests/setup/test_docker_base_images.py tests/ci` → 73 passed。
  - 2026-09-14 final-stage-only 验证：先新增 RED 回归要求 release Dockerfile 不含 ` AS builder`、`--from=builder`、`COPY --from=builder /app/.venv`、`COPY --from=builder /root/.local`，确认旧结构失败；随后删除 Python builder stage，并移除不再使用的 `ghcr.io/astral-sh/uv:python3.12-bookworm-slim` 基础镜像锁定项。`./scripts/test.sh tests/setup/test_docker_base_images.py tests/ci` → 75 passed。
  - 2026-09-14 deploy env 传播修复验证：pipeline #41 的 `deploy-test` 失败于 `. build/release/image.env && sh scripts/ci/deploy-test.sh` 后子进程无法读取 `LIGHTRAG_IMAGE_DIGEST`；新增 RED 回归证明未 export 的 env 文件不能传递到 child shell，随后改为 `resolve-image --env-output` 写出 `export LIGHTRAG_IMAGE_DIGEST=...` 等变量。
- [ ] 7.2 本地运行完整非 integration 后端测试和完整前端检查，分项记录 pass/skip/fail；确认这些测试结果不被写成 Woodpecker 门禁。
- [ ] 7.3 在批准的 test 集群创建独立测试资源、完成跨节点 RWX 实测和显式初始化；记录目标与证据，不修改本机/Kind 服务或其他应用资源。
- [ ] 7.4 在获得触发授权后用测试 tag 跑通真实 Woodpecker 构建、镜像验证和初次双 Pod 部署，保存 pipeline/tag/commit/digest、imageID 及业务验收结果。
  - 2026-09-14 `v1.5.31-test` / pipeline #41 已跑通真实 Woodpecker 源码归档、镜像构建和 pre-deploy 镜像验证；`deploy-test` 已进入真实部署步骤，但因 `image.env` 未 export 导致脚本入参缺失而失败。修复后仍需新测试 tag 验证初次双 Pod 部署。
- [ ] 7.5 通过后续兼容测试版本验证自动升级，以及至少一个不破坏存储的拒绝发布场景，确认旧流量不会在验收前恢复。
- [ ] 7.6 完成 CI 接入、Secret/RBAC、失败处理与人工回退 runbook；明确未验证项目，不用静态验证替代远端成功。
- [ ] 7.7 对照 spec 自检全部场景，执行 OpenSpec strict validation，提交并推送到 master，确认 main 未改变；不自动归档或创建生产 release。
