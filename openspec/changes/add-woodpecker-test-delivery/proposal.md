## Why

当前 fork 没有 Woodpecker 发布链路；直接复制 `../ai-center` 或 `../haier-demo` 的单实例部署会与 HugeGraph 分布式写入和维护合同冲突，但镜像构建与发布应复用 haier-demo 已验证的 Kaniko/registry/cache 组织模式。需要一条从不运行仓库测试套件的交付静态校验、源码归档、镜像发布到 Kubernetes 测试部署的可追溯链路。

用户已确认包含 `*-test` 标签后的自动部署，目标为 ai-center 所在 **test 集群**，使用独立 **`lightrag-test` namespace**。用户另外明确授权先在本机完成基础镜像同步和 Dockerfile 引用替换，并要求测试环境所需 Kubernetes namespace、运行 Secret、pull Secret、PVC 和环境快照由流水线处理；数据库、HugeGraph 与百炼连接值来自仓库本地 `.secrets/test.secrets` 同步到 Woodpecker repo secrets 后再被部署步骤引用。

## What Changes

- 新增 tag-only `.woodpecker/` 工作流：交付静态校验、发布身份校验与源码归档、镜像构建发布、镜像校验、测试部署和部署验收；Woodpecker 不运行 pytest、Bun test 等仓库测试套件，也不响应 `master` push/PR。保留现有 GitHub Actions。
- 符合规范且来自 `master` 历史的发布 tag 执行完整发布门禁；`master` 普通更新不触发 Woodpecker，`main` 不修改、不作为 fork 发布来源。
- 沿用参考仓库的 `vX.Y.Z-test`、`vX.Y.Z-pre`、`vX.Y.Z` 约定。只有 `-test` 自动部署，pre/prod 仅发布并校验镜像。
- 使用内部 digest-pinned Kaniko 构建现有完整 Dockerfile，目标 `linux/amd64`、镜像仓库 `docker-hub.f123.pub/lfun/lightrag`，并将 Dockerfile/Dockerfile.lite 改为不依赖 BuildKit-only `RUN --mount` 或 `$BUILDPLATFORM`。唯一源码工作流 clone 后将源码包、校验和与记录上传到独立 COS/MinIO 路径，后续工作流 `skip_clone` 并从该路径下载源码；源码与发布记录绑定 commit、pipeline 身份和校验和；部署使用已校验的镜像 digest。
- 全部 Dockerfile 的外部基础镜像先由本机同步到 `docker-hub.f123.pub/base/`，验证完整多架构 manifest 与 digest 后再替换引用，包括外部 `COPY --from`；Kaniko 构建路径不再保留 Dockerfile frontend parser directive。使用独立标签及 digest pin，不覆盖其他项目共享标签，不由流水线自动补镜像或回退公网镜像源。
- 新增测试环境 Kustomize overlay 和命名空间级发布配套，复用现有分布式部署合同，运行两个 Pod、每 Pod 一个进程；使用 `.secrets/test.secrets` 派生的 Woodpecker repo secrets 生成运行配置，目标为 PG/pgvector、HugeGraph 和真实共享持久存储，不使用本机测试模型替身。
- 自动发布先执行幂等测试环境准备：创建/更新 `lightrag-test` namespace、registry pull Secret、包含 `LIGHTRAG_COORDINATION_DSN` 的 `lightrag-runtime`、使用同一 PostgreSQL 连接执行显式 coordination schema migration、运行显式 storage bootstrap Job 准备 PG/vector/status 与 HugeGraph profile、写入 profile snapshot 和创建两个 `syno-nfs` RWX PVC；应用 workspace/deployment id 使用合法的 `lightrag_test`。随后再串行发布、旧副本优雅退出、持久化状态检查、新副本启动及验收。故障恢复和回滚仍是显式维护操作，不加入发布自动补偿。
- 测试环境 Service 仍为 ClusterIP，但由流水线创建 test-only nginx Ingress 对外暴露 API、`/webui` 与 `/workspace` 以便调试；默认 host 为 `lightrag-test.f123.pub`，可通过 Woodpecker secret `lightrag_test_public_host` 覆盖。明确 API 鉴权、Ingress controller 来源放行及数据库/模型受控出站要求，不创建 NodePort/LoadBalancer。
- 增加工作流、发布脚本及 Kustomize manifest 本地回归覆盖；全量非 integration 后端测试和完整前端检查仅作为本地/外部 CI 验证责任，不在 Woodpecker 流水线执行，也不作为 Woodpecker 发布门禁。
- 提供首次接入、Secret/RBAC 配置、测试基础设施初始化、正常发布、失败处理及受控回滚文档。真实远端发布验收与静态检查分别记录，不以 YAML lint 代替部署成功。

## Capabilities

### New Capabilities

- `woodpecker-test-delivery`: fork 的交付静态门禁、可信源码与镜像交付，以及遵守分布式写入合同的私有测试环境自动部署。

### Modified Capabilities

无已归档正式 spec。本 change 不改变 HugeGraph 存储语义、分布式写入与恢复合同、默认 local 部署模式或对外业务 API。

## Impact

计划涉及 `.woodpecker/`、`scripts/ci/`、`tests/ci/`、`k8s-deploy/lightrag-kustomize/` 的 digest/测试部署配套及对应 `tests/setup/`、部署文档与本 change artifacts。根据追加要求，`Dockerfile`、`Dockerfile.lite` 和 `Dockerfile.postgres` 的镜像来源统一改为 `docker-hub.f123.pub/base/`；保留原镜像内容、构建阶段及已有多架构能力，不创建分叉的生产 Dockerfile。开发者与现有 CI 需要能够访问该 registry；这不是完整离线构建承诺，但 Dockerfile 的默认 apt 与 Bun/npm 源已按目标网络改为清华/npmmirror 镜像，PyPI 源改为 f123 内部镜像。为适配 Kaniko，Dockerfile/Dockerfile.lite 不再使用 BuildKit cache/bind mount 语法；为避免把不必要组件加入流水线，它们也不再安装 Rust/Cargo toolchain，且不运行会访问 GitHub/spaCy 的离线缓存下载助手；API 启动必需的 tiktoken BPE 缓存以已校验文件随源码提交并复制进镜像，避免运行时访问 OpenAI blob。镜像内容仍来自同一生产 Dockerfile 路径，不创建分叉 Dockerfile。

外部依赖为已有 Woodpecker 3.18.1 Kubernetes agent、COS、私有镜像仓库，test 集群部署 kubeconfig，以及从 `.secrets/test.secrets` 同步到 Woodpecker repo secrets 的百炼、PG/pgvector、HugeGraph 连接信息。部署步骤负责在目标集群创建/更新专属命名空间、运行 Secret、pull Secret、coordination migration Job、storage bootstrap Job、profile snapshot 和两个 RWX PVC；连接 secret 缺失、后端不可用、profile 不兼容或存储状态不安全时发布必须明确失败，不降级为文件后端或无认证单 Pod。

不修改 `../ai-center`、`main`、Woodpecker server/agent 版本或全局权限，也不使用其他应用的数据库、身份密钥或生产凭据。不改动当前本机 `.env`、`.secrets`、原生服务和既有 Kind 并发测试环境。
