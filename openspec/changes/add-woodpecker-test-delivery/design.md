## Context

动机与范围见 [proposal.md](proposal.md)。以下为 2026-09-14 的只读检查结果，不代表部署验收：

- `../haier-demo/.woodpecker/system-deploy.yml` 是当前可用的本机对照：它使用单 commit 自定义 clone、tag-only list-form `when`、内部 CI 工具镜像、镜像发布后 manifest 校验，以及脚本化 Kubernetes apply/status 输出。LightRAG 必须复用这些已验证的流水线组织模式；其业务 Secret、Ingress、单副本和启动迁移不能直接移植。目标 Woodpecker strict lint 未将 haier 的自定义 clone 镜像列入 clone allowlist，所以 LightRAG 将同一段单 commit/no-tags clone 脚本作为 `skip_clone: true` 后的普通步骤执行，而不是使用顶层 `clone:`；该步骤把 Git worktree 放在容器本地 `/tmp`，避免 Kubernetes backend 的 NFS workspace 在 `git reset --hard` checkout 时长时间阻塞。
- 本仓库原 `Dockerfile`/`Dockerfile.lite` 使用 BuildKit-only cache/bind `RUN --mount` 和 `$BUILDPLATFORM`。远端 #35 显示 rootless BuildKit 在 apt 阶段长时间无最终错误日志并以 exit 1 结束；为复用 haier-demo 可用模式，Woodpecker 构建改用内部 digest-pinned Kaniko，同时将 Dockerfile 改造为 Kaniko 可解析的多阶段构建。现有 GitHub Actions 不调整。
- test 集群为 Kubernetes `v1.32.13+rke2r1`、amd64 节点；Woodpecker server/agent 均为 `v3.18.1`，agent 使用 Kubernetes backend、`woodpecker-pipelines` namespace 和 `syno-nfs` RWX workspace。
- `lightrag-test` namespace 尚不存在。发现 `syno-nfs` StorageClass 不等于证明应用共享文件语义；流水线负责创建 namespace/PVC/Secret/profile snapshot，仍必须跨节点验证共享存储。
- 新增 Kustomize 环境 overlay 提供两个 Pod、共享持久卷、非 root、显式运行 Secret 引用和默认暂停路由；部署步骤从 Woodpecker repo secrets 生成 `lightrag-runtime`，这些 repo secrets 来自本仓库 `.secrets/test.secrets`。其镜像引用通过 Kustomize replacement 注入已验证 digest。
- 定向重跑两个已有 API prefix 测试，仍为 `2 failed, 32 deselected`：API-key-only 配置未携带凭据时实际 403，断言期待 401。当前鉴权代码明确区分 API key 的 403 和账号登录的 401；实施时应在干净配置下进一步验证测试合同，不能先标记跳过。

参考合同：[分布式部署](../../../docs/DistributedDeployment.md)、[运行时合同](../../../docs/design/DistributedRuntimeContract.md)、[pipeline 合同](../../../docs/design/DistributedPipelineContract.md)。

## Goals / Non-Goals

**Goals**

- 发布一个已验证 commit 对应的完整镜像，部署的 digest 与验证产物一致，失败步骤不能被误报成功。
- 自动测试发布针对专属、初始化完毕、仅由受控应用写入的 workspace；兼容升级允许停机，不允许新旧版本混写。
- 自动测试发布在流水线内幂等准备 Kubernetes 资源：namespace、pull Secret、runtime Secret、coordination migration Job、storage bootstrap Job、profile snapshot 和 RWX PVC；连接值只来自 Woodpecker secrets。
- 凭据按质量检查、源码/镜像发布、测试部署分别隔离，部署控制权限限定在 `lightrag-test`。
- 用真实 Woodpecker 和 test 集群验证完整链路，并明确区分模板验证、环境准备和实际发布证据。

**Non-Goals**

- 不提供生产/pre 自动部署，不修改 `main` 或其他仓库，不升级 CI 基础设施。
- 不改变业务鉴权接口、分布式协议、存储 schema 或失败恢复语义；不引入零停机、自动接管或自动数据回滚承诺。
- 不在每次发布时创建数据库服务、清空测试数据、执行故障 recover、破坏性数据修复，或用本机服务替代目标环境；Kubernetes runtime Secret、pull Secret、coordination-only schema migration、显式 storage bootstrap、snapshot、namespace 和 PVC 的幂等创建属于部署准备。
- amd64 是此次已确认测试集群的交付平台；本 change 不撤销现有 GitHub Actions 的多架构能力，也不声称 Woodpecker 已验证 arm64。

## Decisions

### 1. 触发规则与工作流依赖

新增不运行仓库测试套件的 tag-only 交付工作流。`validate-release` 是唯一源码 clone 工作流，按 `../haier-demo` 的单 commit 自定义 clone 模式只拉取 `CI_COMMIT_SHA`，不克隆 LFS/submodule/全量 tag；发布校验再只获取当前 release tag 与 `master` 历史证明来源。为同时满足 strict lint，所有 workflow 均声明 `skip_clone: true`，`validate-release` 的单一 `release-source` 普通步骤在容器本地 `/tmp` 执行 clone、交付静态检查、发布身份校验、源码归档和上传，避免后续步骤重新 clone 或在 Woodpecker NFS workspace 上物化 worktree；`build-image`、`pre-deploy`、`deploy-test` 从 LightRAG 的 MinIO/COS 源码包路径下载并校验后再执行构建、镜像校验或部署。复杂行为放入可本地测试的 `scripts/ci/`，YAML 负责声明触发、依赖、资源和 Secret。

| 事件 | 质量检查 | 源码归档/镜像发布 | 自动部署 |
| --- | --- | --- | --- |
| `master` push | 否（不触发 Woodpecker） | 否 | 否 |
| 目标为 `master` 的 PR | 否（不触发 Woodpecker） | 否 | 否 |
| `vX.Y.Z-test` tag | 是（唯一 clone 的 `release-source` 步骤内执行） | 是；后续 workflow 从 MinIO/COS 下载源码 | `lightrag-test` |
| `vX.Y.Z-pre` / `vX.Y.Z` tag | 是（唯一 clone 的 `release-source` 步骤内执行） | 是；后续 workflow 从 MinIO/COS 下载源码 | 否 |
| 其他分支/tag | 不发布；无效发布 tag 明确拒绝 | 否 | 否 |

tag 事件不能依靠分支过滤证明来源。归档前核对仓库身份、tag 解析后的 commit、`CI_COMMIT_SHA` 和远端 `master` 历史；浅克隆或网络故障导致无法证明时失败。重试同样校验 tag 未移动。保护发布标签和配置文件属于 forge/CI 接入前置条件；仓库管理员或持有任意发布 Secret 的恶意维护者不在本流水线的隔离承诺内。

tag 发布必须等待同一 commit 的交付静态校验成功，不借用旧流水线结果。无效 tag、静态校验失败或任一依赖失败均不能上传可发布产物或部署。仓库测试套件不是 Woodpecker 发布门禁。普通 `master` 更新不再提供“静态检查流水线”；相关验证保留为本地或外部 CI 证据。

### 2. 本地验证与流水线静态门禁

- Woodpecker 交付流水线不运行仓库测试套件：不调用 `./scripts/test.sh`、pytest、`bun test`、前端 typecheck/lint/build 检查入口，也不为测试安装额外模型或依赖。此前后端/前端检查脚本保留为本地或外部 CI 可手动执行的验证入口，不能从 `.woodpecker/` 引用。
- `validate-release` 的 `release-source` 步骤在源码 clone 后执行交付静态校验：shell/Python 语法检查、基础镜像锁文件 JSON 校验，以及在工具可用时执行 Woodpecker lint、Kustomize render 和 OpenSpec strict validation。工具缺失时报告“未在该镜像内执行”，不伪装为测试通过。该步骤只在 release tag 事件运行。
- 发布 tag 仍必须经过同一 commit 的交付静态校验、源码身份校验、镜像构建/校验和部署前置检查；但仓库单元/集成测试失败不由 Woodpecker 直接判定，避免把交付流水线变成长时间、外部下载和 runner CPU 差异敏感的测试平台。
- 本地实施验证仍要运行与改动相关的 pytest/Bun/OpenSpec/manifest 回归，并记录哪些检查未由 Woodpecker 执行。外部服务集成测试显式 opt-in，不将跳过它们写成已验证。发布后验收使用真实目标服务；本地测试模型 fixture 不进入应用镜像或测试环境运行配置。

### 3. 构建器选择与权限

采用 **Kaniko**，直接构建现有 Dockerfile。远端 #35 的 rootless BuildKit 运行中，Woodpecker step 日志停在 `#23` apt 下载中段，之后约 11 分钟以 exit 1 结束且没有最终错误行；该路径已经不满足“实时输出日志到 stdio”和可诊断性要求。haier-demo 的可用系统发布链路使用 Kaniko、registry auth、cache-repo、镜像发布后 manifest 校验和脚本化部署状态输出；LightRAG 改为复用这一构建模式，但不照搬它的业务 Secret、单副本部署或启动迁移。

为避免分叉生产镜像路径，Dockerfile/Dockerfile.lite 被改造为 Kaniko 可解析：删除 BuildKit-only `RUN --mount=type=cache`、`RUN --mount=type=bind` 和 `$BUILDPLATFORM` 依赖；删除 Python builder stage，改为 final stage 直接用 uv 安装 locked wheel 依赖，避免 Kaniko 跨 stage 保存大型 `.venv`。三个 Dockerfile 的 apt 阶段默认使用 `http://mirrors.tuna.tsinghua.edu.cn/debian` 与 `http://mirrors.tuna.tsinghua.edu.cn/debian-security`，执行 `apt-get update` 前先改写 `/etc/apt/sources.list*` 并设置 apt retry/timeout，避免 Woodpecker 构建落回 Debian 默认源。

构建入口必须向 stdout/stderr 输出可持续刷新的非敏感进度：脚本在调用 Kaniko 前打印 tag、commit、context dir、cache repo、digest 文件位置和 Kaniko 版本；Kaniko 的 Dockerfile command 输出直接进入 step 日志，避免 Woodpecker 只能看到一个长时间运行但无上下文的步骤。不得打印 registry 密码、COS 密钥或完整运行 Secret。

Kaniko 工具镜像也必须由内部 registry 提供，不能让 Kubernetes agent 从上游 registry 拉取。当前 test runner 为 `linux/amd64`，因此工具镜像固定为 `docker-hub.f123.pub/devops/kaniko:v1.14.0-debug@sha256:1b282be1c4467618e9122d2ae457ffa3776a7caa52d7539e329123283bc71f79`，并由 `scripts/ci/tool-images.lock.json` 记录 source index、mirror index、平台 manifest 和内部镜像引用；这不放宽 Dockerfile 基础镜像保留完整平台集的要求。step 继续声明 CPU、内存和临时磁盘预算，构建失败不自动改为 privileged 或挂载宿主 Docker socket。

#### 本机预同步基础镜像（用户单独授权）

所有 Dockerfile 的外部镜像输入均只引用 `docker-hub.f123.pub/base/`。包括每个外部 FROM，以及最终阶段提取 uv 二进制的外部 COPY；内部 build-stage 别名不改。Kaniko 构建路径不需要 Dockerfile frontend parser directive，且 release Dockerfile 不再需要 Python builder stage，因此当前共有四个独立 Dockerfile 来源：oven/bun、Python runtime、uv binary 和 pgvector/pgvector。

在本机使用 registry-to-registry 工具先解析并固定源 manifest/index digest，再按该 digest 同步完整内容和多架构子 manifest，校验目标 digest 后才修改 Dockerfile。不使用普通本机 `docker pull/tag/push` 缩减为单个 arm64 平台，不通过 Kubernetes 或 Woodpecker 执行同步。目标标签形如 `<upstream-tag>-lightrag-<digest-prefix>`，避免覆盖其他应用的公共标签；Dockerfile 还固定完整 digest。

`scripts/ci/base-images.lock.json` 记录源、目标、digest 和平台，[操作文档](../../../docs/ContainerBaseImages.md)记录本机同步、校验与更新方式；本次实际结果见[验证记录](base-images-verification.md)。后续更新必须重走“先推送验证、后修改引用”，CI 不自动从公网补齐缺失镜像，也不能用 build args 绕过基础镜像来源要求。已验证 registry 拒绝匿名读取，既有 CI 和开发环境需要专用读取权限，GHCR 登录不能替代它；发布写权限不授予 PR。该改动收敛的是镜像来源；后续流水线排查又将 Dockerfile 默认 apt 与 Bun/npm 源分别收敛到清华/npmmirror 镜像，PyPI 源收敛到 f123 内部镜像。因为 `uv sync --frozen` 会按 `uv.lock` 内的 distribution URL 下载，`uv.lock` 也必须使用 f123 mirror 的 registry 与 artifact URL，不能仅依赖 Dockerfile 的 `UV_DEFAULT_INDEX` / `PIP_INDEX_URL`。`v1.5.28-test` / #36 进一步证明 Rust/Cargo toolchain 和 broad `lightrag-download-cache` 属于不必要流水线输入：仓库没有 Rust 源码，locked amd64 依赖集从 wheel 安装，而 broad cache helper 会访问 OpenAI blob/GitHub 并吞掉 pip 实时输出；因此 release Dockerfile/Dockerfile.lite 删除 Rust/Cargo 和该 helper。`v1.5.39-test` / #46 又证明默认 API tokenizer 在启动时仍必须读取 tiktoken BPE 数据，且 test 集群到 OpenAI blob 会卡住；因此只将已校验的 `o200k_base` / `cl100k_base` tiktoken cache 文件提交到 `docker/tiktoken-cache/` 并复制进镜像，仍不下载或烘焙 spaCy 模型。`v1.5.29-test` / #38 随后定位到 Kaniko 默认 full snapshot 对大型 `.venv` 层会长时间无日志，build script 改用 `--snapshot-mode=redo` 降低 snapshot 成本，并删除 Python builder stage，避免跨 stage 保存/复制 `.venv`。未镜像的上游服务和 Dockerfile.postgres 的 AGE 源码仍不是离线化承诺。

实施前先以最小构建验证 Kaniko 工具镜像可拉取、registry auth 文件可用、amd64 构建参数和 registry cache 配置正确，再构建完整镜像。步骤须设置 CPU、内存和临时磁盘预算，构建失败不自动改为 privileged、不全局放宽 agent，也不挂载宿主 Docker socket。`v1.5.21-test` 和 #35 记录保留为 BuildKit 方案被淘汰的证据。

备选方案：

- **rootless BuildKit**：最初用于保留 BuildKit cache/bind mount 语义，但远端 #35 出现长时间无最终错误日志后 exit 1，诊断性不足，不再作为 Woodpecker 默认方案。
- **Docker-in-Docker/buildx**：兼容性好，但在现有 Kubernetes backend 中增加 daemon、证书、存储与特权边界，不作为默认方案。Kaniko 无法运行时报告实际限制，再评审，而非静默退回此方案。

资料：[Woodpecker Kubernetes backend](https://woodpecker-ci.org/docs/administration/configuration/backends/kubernetes)、[Kaniko executor flags](https://github.com/GoogleContainerTools/kaniko)。

### 4. 源码、镜像及发布记录

COS 使用 `lightrag/ci-source/<commit>/<pipeline-id>/` 和对应 release-record 命名空间，不能覆盖 ai-center 路径。归档仅由 `validate-release` 的 `release-source` 唯一 clone 产出，包含受审源码；该 clone worktree 位于容器本地 `/tmp`，不依赖 Woodpecker NFS workspace 写入性能。归档拒绝纳入 `.env`、`.secrets`、私钥、开发数据和构建凭据。生成 SHA-256 校验和及记录，包含 repo、tag、commit、pipeline 身份；下游 workflow 先从 MinIO/COS 下载源码包并校验 SHA-256，再解包使用。消费者验证全部身份与校验和，拒绝路径穿越、符号链接逃逸和异常归档。

镜像仓库固定为 `docker-hub.f123.pub/lfun/lightrag`，不调用默认发布到上游的 `docker-build-push.sh`。镜像附带 commit/source/tag 标识，发布记录包含最终 digest、平台和源码包 hash。版本 tag 需不可变；遇到已经存在但来源不同的版本拒绝覆盖。相同版本重试复核已有 digest 和来源，不假定重复构建字节相同。

镜像校验不能仅测试 manifest 非空：解析受支持的 manifest/index、确认 amd64 镜像、revision 与预期 commit、记录 digest，并使部署只消费该 digest。Kustomize overlay 通过 `release-image` ConfigMap replacement 注入 `image` digest；部署脚本校验 digest 格式，manifest 回归测试保证应用镜像只使用不可变 digest。

构建凭据在临时目录中生成合法 JSON、权限 0600，使用后清理；不使用会把密码写入日志的调试输出。COS/registry 权限仅覆盖 LightRAG 自己的路径和仓库。参考项目的 Secret 名称可在管理员授权下复用，但权限范围和事件过滤必须实际确认，不能假定全局 Secret 自动可用。

### 5. 测试环境与首次接入

目标为 `~/.kube/test-config` 对应的 test 集群；所有工具命令显式指定 kubeconfig/context，不改变本机默认上下文。namespace 为 `lightrag-test`，应用 release/deployment 为 `lightrag`。

使用 Kustomize 测试 overlay：两副本、`WORKERS=1`、PGKV/PGDocStatus/PGVector/HugeGraph、稳定且一致的 deployment ID/workspace、两条共同 RWX 路径、非 root UID/GID 1000，禁用 Service links。HugeGraph 连接池按整个两副本部署预算配置，测试起点为每 Pod 1 个连接。Kubernetes 执行方式参考 haier 的 `deploy-test.sh`/`deploy.sh`/`status.sh`：从全局 `kubeconfig_test` 注入 kubeconfig、脚本化前置检查、运行 coordination schema migration Job、运行 storage bootstrap Job、应用受审 manifest、等待 rollout，并在步骤结尾输出 Deployment/Pod/Service 状态；LightRAG 不创建公网 Ingress，也不在发布脚本里执行故障 recover。

首次接入由流水线执行 Kubernetes 侧幂等准备，不要求人工预先创建 namespace、运行 Secret、pull Secret、profile ConfigMap 或 PVC：

1. Woodpecker 优先引用已有 global `kubeconfig_test` 注入为 `LIGHTRAG_TEST_KUBECONFIG`，若该凭据权限越界再改为专用 LightRAG kubeconfig，而非复制 ai-center 的集群管理员 kubeconfig。部署脚本用该 kubeconfig 创建/更新 `lightrag-test` namespace 和命名空间内资源。
2. 本仓库 `.secrets/test.secrets` 是 test 连接值来源；其中百炼、PostgreSQL 和 HugeGraph 条目同步为 `LFunTech/LightRAG` repo secrets，workflow 只通过 `from_secret` 注入 `deploy-test`。全局 `DOCKER_USERNAME`/`DOCKER_PASSWORD` 继续用于 registry 与 pull Secret，缺失或空值立即失败且不打印明文。
3. `deploy-test.sh` 参考 haier 的 `ensure_namespace` / `apply_runtime_secret` / `apply_registry_secret` 模式，用 `kubectl create ... --dry-run=client -o yaml | kubectl apply -f -` 创建/更新 `lightrag-registry-pull`、`lightrag-runtime` 和 `lightrag-test-environment`。`lightrag-runtime` 包含 API key、由 PG 连接字段生成的 `LIGHTRAG_COORDINATION_DSN`、百炼/OpenAI-compatible host/key、DashScope workspace header、PG 连接字段、HugeGraph REST/Gremlin/graphspace/graph/Basic 凭据及认证方式元数据；Deployment 只保留非敏感 profile 常量，连接字段全部来自 Secret。Kubernetes namespace 固定为 `lightrag-test`，应用 `WORKSPACE`、`POSTGRES_WORKSPACE` 和 `LIGHTRAG_DEPLOYMENT_ID` 固定为合法标识 `lightrag_test`，避免启动时清洗后与 PG workspace 不一致。
4. 在新副本启动前，`deploy-test.sh` 使用已验证 release image 创建 `lightrag-coordination-migrate` Job，并通过 `lightrag-runtime` 注入同一个 `.secrets/test.secrets` 派生的 PG DSN 执行 `python -m lightrag.distributed migrate`。该 migration 只初始化/验证 `lightrag_coordination` schema，是分布式写入运行前置。
5. 随后创建 `lightrag-storage-bootstrap` Job，使用同一个 release image、runtime Secret 和与 Deployment 一致的非敏感 profile env 执行 `python -m lightrag.distributed bootstrap --actor woodpecker --confirm-writers-stopped --confirm-inflight-finished`。该命令在打开 durable maintenance 操作前先用 `.secrets/test.secrets` 派生的连接做 PG `vector` extension 权限与 HugeGraph 鉴权/图路径只读预检，避免可预期的权限缺失留下 fence。该步骤在旧 Pod 已停止、Service 已暂停的显式维护窗口内准备 PG/vector/status 表、vector extension、索引与 HugeGraph schema；失败保留现场，不触发 recover。
6. Kustomize overlay 创建 ServiceAccount/RBAC、ClusterIP Service、NetworkPolicy 和两个 `syno-nfs` RWX PVC；发布脚本在 rollout 前等待 PVC Bound，并在验收阶段通过双 Pod 行为验证共享存储和分布式后端。数据库底层存储按其自身要求选择，不把应用 RWX 文件卷充当数据库持久化方案。
7. 使用经授权的 test 基础设施中 LightRAG 专属 PG/pgvector 数据库、协调库/权限及 HugeGraph 数据域，不共享其他应用的数据。普通 tag 部署可让 HugeGraphStorage 补齐自己的兼容 schema，但不会创建 HugeGraph 服务、清空图、执行故障 recover 或 rollback。
8. 配置内部访问控制：Service 为 ClusterIP，不创建公网入口；DNS、数据库、HugeGraph 端口和模型 HTTPS 出站分别放行。不能宣称 ClusterIP 本身提供访问控制，也不能把 HTTPS 任意出站称为域名级白名单。对可创建 Pod/Job 的发布身份，不能声称 namespace 内的 Secret 对该身份不可读：其命名空间级权限边界必须在接入文档中明示。

CI 会取得部署所需模型/数据库明文以生成 Kubernetes Secret，但仅限 `deploy-test` 步骤；源码归档、镜像构建和镜像校验步骤不接收这些运行时 secret。

### 6. 自动升级状态机

采用允许停机的保守发布；不使用自动回滚，也不依赖 RollingUpdate 避免混写。Kustomize base 默认使 Service selector 指向不存在的暂停标签，整个 `kubectl apply -k` 和验收期间流量保持关闭；恢复路由必须通过受审 manifest/patch 步骤完成，不能让后续 apply 意外恢复。

1. **发布前检查**：校验 tag/commit/digest、目标集群/namespace 身份、Secret/PVC/profile、已批准的自动升级接入状态，以及只有该应用使用测试 workspace 的运维约束。数据模型、schema 或运行 profile 变化进入显式维护，不由普通 tag 自动处理。
2. **串行化**：用 namespace 内的原子发布锁记录 pipeline/tag/digest；同时到达的发布拒绝竞争而不是互相覆盖。发布记录阻止较老 pipeline 覆盖已成功发布的新版本。中断留下的发布锁不能因 TTL 到期自动抢占；人工核对旧发布是否仍在运行后才能释放。此锁仅防部署脚本竞争，不替代应用持久化协调协议。
3. **停止旧实例**：保存原镜像和发布标识，撤下 Service selector 的业务流量；将当前 Deployment 缩容至 0，等待优雅退出，不强删 Pod。独立 SDK/导入器/修复工具不得使用此专属 workspace，发现额外写入者则拒绝发布。600 秒退出预算是初始值，不是请求完成证明。
4. **核验写入状态**：等待旧 Pod 消失之后，再通过受控检查 Job 检查协调状态及新镜像的存储/profile 兼容性。必须确认没有 fenced/orphaned/active operation、pending mutation、残留 claim/lock 或其他未完成状态；错误、无数据或超时不是“空闲”。不输出包含业务正文的完整 inspect 快照到公共构建日志。
5. **启动新实例**：仅在正常优雅退出且持久化写入确认完整时，使用已校验的 digest 通过 Kustomize overlay 更新 Deployment、恢复两副本；普通 API 启动仍 verify-only。任何 schema 不兼容或不确定状态保留现场并失败，不能通过 bootstrap/recover 消除。
6. **验收并恢复路由**：在 Service 业务流量仍关闭时逐 Pod 校验实际 imageID、健康、鉴权、分布式状态与共享配置，并用唯一标识的测试文档验证双 Pod 入库和跨 Pod 查询；验收成功后恢复 Service selector，校验 ClusterIP 路径，再记录发布成功。测试文档可识别并保留，清理遵循既有 purge 合同，不在失败现场强行删除。

如果旧实例被 SIGKILL、未知请求可能迟到提交、检查不确定或新版本验收失败，发布失败并保留原始证据，业务路由不恢复；不把“不再有 Pod”当作后端请求已结束。失败后自动启动旧版本同样不安全，因此只提供显式停止写入、检查兼容性与必要恢复后的人工回退步骤。

### 7. 验证与交付证据

- 本地脚本测试覆盖输入身份、tag/分支矩阵、缺失凭据、归档篡改/穿越、manifest/index、digest、旧发布覆盖、新旧并发发布、外部命令非零及超时；这些测试不在 Woodpecker 流水线执行。
- 本地 Kustomize manifest 测试覆盖 digest replacement、RWX/非 root/双副本、禁止公开 Service 类型、运行 Secret 引用和默认暂停路由。既有 Helm chart 仍由原有 chart 回归测试覆盖，但不再作为 Woodpecker 测试部署入口。
- 首次环境验收包括 Kaniko 构建探针、实际 RWX 跨节点验证、显式 bootstrap 和 Secret/RBAC 边界验证；不能用本机 Kind 结果替代 test 集群结果。
- `v1.5.23-test` / Woodpecker pipeline #31 证实源码包从 COS 下载并通过校验、rootless BuildKit 可启动且 apt 已改写到清华源；同次排查显示 Woodpecker 保存的 build 日志停在 `#23 ... Fetched 80.6 MB` 不是最终状态，Kubernetes 侧可见 apt 解包/安装继续执行。该运行暴露了 rootless dpkg 阶段较慢、旧 rustup `curl | sh` 会吞掉 DNS/下载失败，以及 `uv sync` 仍默认走公网 PyPI。`v1.5.27-test` / pipeline #35 进一步显示 BuildKit 日志停在 apt 下载中段并以 exit 1 结束，无最终错误行；Woodpecker 构建方案因此改为 haier-demo 风格 Kaniko。`v1.5.28-test` / pipeline #36 证明 clone/NFS 修复有效，`release-source` 约 69 秒完成且 `download-source` 13 秒完成；同次 Kaniko 日志显示 Cargo 来自 Dockerfile 历史兜底安装而非仓库代码，且新的停顿点是构建期下载 GitHub spaCy 模型 wheel。`v1.5.29-test` / #38 进一步显示依赖层安装已无需 Cargo/GCC/模型缓存，但 Kaniko full snapshot `.venv` 层仍长时间无输出；已改用 `--snapshot-mode=redo`，完整远端成功仍需新 tag 验证。
- 在获得发布触发授权并配置好凭据后，以受保护测试 tag 跑通远端流水线；记录 commit/tag/pipeline ID、各阶段结果、镜像 digest、两个 Pod 的 imageID 和测试文档/查询结果。再用兼容的新测试版本验证升级路径及至少一个不修改存储的拒绝发布场景。
- 本地测试、前端检查、manifest 检查和远端验收分别报告；其中本地测试和前端检查不由 Woodpecker 执行。尚未取得 CI 凭据、测试资源或触发授权时，明确列为未完成项，不把 proposal/脚本完成等同于用户已可测试。

## Risks / Trade-offs

- **Kaniko 兼容性依赖 Dockerfile 语法** → 以回归测试禁止 BuildKit-only `RUN --mount` 和 `$BUILDPLATFORM`；失败不自动提权、不修改 agent 全局安全设置。
- **完整镜像较大，模型依赖下载耗时** → 固定工具/模型输入、使用 registry cache、设置磁盘和超时预算；不清理其他项目镜像或卷。
- **默认基础镜像改为内部 registry** → 在本机验证目标内容及读取权限，记录现有 CI 的接入要求；不借此覆盖其他项目标签或删除旧镜像。
- **跨存储不提供事务及自动 HA** → 只自动执行兼容升级和正常 drain；任何未确认状态保留并交由既有审计恢复路径。
- **流水线获得运行时 secret 后会创建 namespace/PVC/Secret** → 这是 test 环境部署准备，不是数据库服务创建或故障恢复；缺失 secret、后端不可达或不安全存储状态仍失败并保留证据。
- **同一提交重建不保证 digest 相同** → 已发布版本不可变，重试验证来源并复用已确认 artifact；漂移需新 tag，而非覆盖。
- **共享 RWX 不保证共享文件正确性** → test 集群跨节点验证真实语义；PVC 声明只是一项检查。
- **仓库测试不属于 Woodpecker 门禁** → 测试失败仍按本地/外部 CI 规则修复，但 Woodpecker 不运行 pytest/Bun test，也不把跳过或失败写成发布门禁结果。
- **有意保留停止服务状态** → 明确显示发布失败阶段和恢复手册；可用性让位于不丢数据、不混写。

## Migration Plan

1. 评审本 proposal 后实现并验证工作流、脚本、Kustomize 配套、测试和运行手册，不修改现有本地服务。
2. 合并/提交到 fork `master` 并 push，保持 `main` 原始引用；不自动创建 release 或触发生产/pre 发布。
3. 管理员接入 Woodpecker 仓库、受保护 tag 和按事件限制的 Secret；将 `.secrets/test.secrets` 同步到 LightRAG repo secrets，并确认 test 后端连接只指向专属 PG/HugeGraph 域。
4. 在授权后触发测试 tag，完成初次部署、兼容升级及拒绝路径验收，再宣布自动测试交付链路可用。
5. 失败回退时停止写入并按分布式部署手册核查，不自动降级为 local writer、不删除协调历史、不恢复跨存储不一致的局部备份。
