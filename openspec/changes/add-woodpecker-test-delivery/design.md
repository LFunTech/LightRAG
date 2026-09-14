## Context

动机与范围见 [proposal.md](proposal.md)。以下为 2026-09-14 的只读检查结果，不代表部署验收：

- `../ai-center/.woodpecker/` 使用 tag 校验、COS 源码归档、Kaniko 构建、镜像 manifest 校验和 `-test` 自动部署；其业务 Secret、Ingress、单副本及启动迁移不能移植。
- 本仓库 `Dockerfile` 使用 cache/bind `RUN --mount` 和多阶段前端构建，需要兼容这些语义的构建器。现有 GitHub Actions 不调整。
- test 集群为 Kubernetes `v1.32.13+rke2r1`、amd64 节点；Woodpecker server/agent 均为 `v3.18.1`，agent 使用 Kubernetes backend、`woodpecker-pipelines` namespace 和 `syno-nfs` RWX workspace。
- `lightrag-test` namespace 尚不存在。发现 `syno-nfs` StorageClass 不等于证明应用共享文件语义；必须跨节点验证。
- 新增 Kustomize 环境 overlay 提供两个 Pod、共享持久卷、非 root、显式运行 Secret 引用和默认暂停路由；普通启动只校验存储。其镜像引用通过 Kustomize replacement 注入已验证 digest。
- 定向重跑两个已有 API prefix 测试，仍为 `2 failed, 32 deselected`：API-key-only 配置未携带凭据时实际 403，断言期待 401。当前鉴权代码明确区分 API key 的 403 和账号登录的 401；实施时应在干净配置下进一步验证测试合同，不能先标记跳过。

参考合同：[分布式部署](../../../docs/DistributedDeployment.md)、[运行时合同](../../../docs/design/DistributedRuntimeContract.md)、[pipeline 合同](../../../docs/design/DistributedPipelineContract.md)。

## Goals / Non-Goals

**Goals**

- 发布一个已验证 commit 对应的完整镜像，部署的 digest 与验证产物一致，失败步骤不能被误报成功。
- 自动测试发布针对专属、初始化完毕、仅由受控应用写入的 workspace；兼容升级允许停机，不允许新旧版本混写。
- 凭据按质量检查、源码/镜像发布、测试部署分别隔离，部署控制权限限定在 `lightrag-test`。
- 用真实 Woodpecker 和 test 集群验证完整链路，并明确区分模板验证、环境准备和实际发布证据。

**Non-Goals**

- 不提供生产/pre 自动部署，不修改 `main` 或其他仓库，不升级 CI 基础设施。
- 不改变业务鉴权接口、分布式协议、存储 schema 或失败恢复语义；不引入零停机、自动接管或自动数据回滚承诺。
- 不在每次发布时创建数据库、清空测试数据、执行 bootstrap/migrate/recover，或用本机服务替代目标环境。
- amd64 是此次已确认测试集群的交付平台；本 change 不撤销现有 GitHub Actions 的多架构能力，也不声称 Woodpecker 已验证 arm64。

## Decisions

### 1. 触发规则与工作流依赖

新增质量工作流和参考项目风格的 `validate`、`build-image`、`pre-deploy`、`deploy-test` 工作流，复杂行为放入可测试的 `scripts/ci/`，YAML 负责声明触发、依赖、资源和 Secret。

| 事件 | 质量检查 | 源码归档/镜像发布 | 自动部署 |
| --- | --- | --- | --- |
| `master` push | 是 | 否 | 否 |
| 目标为 `master` 的 PR | 是，无发布/部署 Secret | 否 | 否 |
| `vX.Y.Z-test` tag | 是 | 是 | `lightrag-test` |
| `vX.Y.Z-pre` / `vX.Y.Z` tag | 是 | 是 | 否 |
| 其他分支/tag | 不发布；无效发布 tag 明确拒绝 | 否 | 否 |

tag 事件不能依靠分支过滤证明来源。归档前核对仓库身份、tag 解析后的 commit、`CI_COMMIT_SHA` 和远端 `master` 历史；浅克隆或网络故障导致无法证明时失败。重试同样校验 tag 未移动。保护发布标签和配置文件属于 forge/CI 接入前置条件；仓库管理员或持有任意发布 Secret 的恶意维护者不在本流水线的隔离承诺内。

tag 发布必须等待同一 commit 的质量检查成功，不借用旧流水线结果。无效 tag、测试失败或任一依赖失败均不能上传可发布产物或部署。

### 2. 质量门禁

- Python 使用 `uv.lock` 固定的 API、offline-storage、offline-llm、pytest extras，执行完整非 integration 测试。安装必要系统库；CI 不动态下载 spaCy 模型，缺少模型时使用测试套件既有 skip 机制记录跳过原因，避免代理环境把质量门禁变成外部下载测试；对 `faiss-cpu` 先做导入探针，若 lock 版本在当前 runner CPU 上触发 SIGILL，则在项目声明范围内降级到可导入 wheel 后继续运行 Faiss 测试；与开发者 `.env` 隔离，记录 pass/skip/fail 原因。
- 前端在 `lightrag_webui/` 执行 frozen install、全部 Bun 测试、`tsc --noEmit`、lint、build，保留现有构建体积门禁。
- 工作流 strict lint、CI 脚本测试、shell/Python 静态检查、Kustomize render 和分布式 manifest 回归测试均需运行。
- 既有两个失败单独验证：若为测试配置/状态码合同错误，修正 fixture 和精确断言，同时保留匿名拒绝、错误密钥拒绝、合法密钥通过鉴权及两个 prefix 转发模式的回归覆盖；不将 `401` 简单放宽为“任意非 200”。若确需改变生产鉴权行为，停止该修复并单独评审，不混入 CI 改动。
- 外部服务集成测试显式 opt-in，不将跳过它们写成已验证。发布后验收使用真实目标服务；本地测试模型 fixture 不进入应用镜像或测试环境运行配置。

### 3. 构建器选择与权限

推荐 **rootless BuildKit**，直接构建现有 Dockerfile。固定工具版本和可验证镜像 digest，使用 amd64 原生构建与独立 registry cache；BuildKit 状态置于单次构建的本地临时目录，不放在共享 NFS workspace 上。构建输入仍来自校验后的源码包。

#### 本机预同步基础镜像（用户单独授权）

所有 Dockerfile 的外部镜像输入均只引用 `docker-hub.f123.pub/base/`。包括 Dockerfile frontend、每个外部 FROM，以及最终阶段提取 uv 二进制的外部 COPY；内部 build-stage 别名不改。当前共有六个独立来源：docker/dockerfile、oven/bun、uv Python builder、Python runtime、uv binary 和 pgvector/pgvector。

在本机使用 registry-to-registry 工具先解析并固定源 manifest/index digest，再按该 digest 同步完整内容和多架构子 manifest，校验目标 digest 后才修改 Dockerfile。不使用普通本机 `docker pull/tag/push` 缩减为单个 arm64 平台，不通过 Kubernetes 或 Woodpecker 执行同步。目标标签形如 `<upstream-tag>-lightrag-<digest-prefix>`，避免覆盖其他应用的公共标签；Dockerfile 还固定完整 digest。

`scripts/ci/base-images.lock.json` 记录源、目标、digest 和平台，[操作文档](../../../docs/ContainerBaseImages.md)记录本机同步、校验与更新方式；本次实际结果见[验证记录](base-images-verification.md)。后续更新必须重走“先推送验证、后修改引用”，CI 不自动从公网补齐缺失镜像，也不能用 build args 绕过基础镜像来源要求。已验证 registry 拒绝匿名读取，既有 CI 和开发环境需要专用读取权限，GHCR 登录不能替代它；发布写权限不授予 PR。该改动仅收敛镜像来源，apt、Rust、PyPI、npm 和模型下载仍有构建出站依赖。

实施前先以最小构建验证 Kubernetes step 的非 root UID、user namespace、seccomp/AppArmor 配置及 cache/bind mount 支持，再构建完整镜像。rootless 不等于不需要安全配置：仅构建步骤允许必要的 unconfined profile / no-process-sandbox 设置，不能全局放宽 agent 或挂载宿主 Docker socket。步骤须设置 CPU、内存和临时磁盘预算，构建失败不自动改为 privileged。

备选方案：

- **保留 Kaniko，另写或改造 Dockerfile**：与现有缓存和离线模型 bind mount 语义存在差异，增加两个生产构建路径漂移风险，不采用。
- **Docker-in-Docker/buildx**：兼容性好，但在现有 Kubernetes backend 中增加 daemon、证书、存储与特权边界，不作为默认方案。rootless 无法运行时报告实际限制，再评审，而非静默退回此方案。

资料：[Woodpecker Kubernetes backend](https://woodpecker-ci.org/docs/administration/configuration/backends/kubernetes)、[BuildKit rootless 限制](https://github.com/moby/buildkit/blob/master/docs/rootless.md)。

### 4. 源码、镜像及发布记录

COS 使用 `lightrag/ci-source/<commit>/<pipeline-id>/` 和对应 release-record 命名空间，不能覆盖 ai-center 路径。归档仅包含已跟踪的受审源码；拒绝纳入 `.env`、`.secrets`、私钥、开发数据和构建凭据。生成 SHA-256 校验和及记录，包含 repo、tag、commit、pipeline 身份；消费者验证全部身份与校验和，拒绝路径穿越、符号链接逃逸和异常归档。

镜像仓库固定为 `docker-hub.f123.pub/lfun/lightrag`，不调用默认发布到上游的 `docker-build-push.sh`。镜像附带 commit/source/tag 标识，发布记录包含最终 digest、平台和源码包 hash。版本 tag 需不可变；遇到已经存在但来源不同的版本拒绝覆盖。相同版本重试复核已有 digest 和来源，不假定重复构建字节相同。

镜像校验不能仅测试 manifest 非空：解析受支持的 manifest/index、确认 amd64 镜像、revision 与预期 commit、记录 digest，并使部署只消费该 digest。Kustomize overlay 通过 `release-image` ConfigMap replacement 注入 `image` digest；部署脚本校验 digest 格式，manifest 回归测试保证应用镜像只使用不可变 digest。

构建凭据在临时目录中生成合法 JSON、权限 0600，使用后清理；不使用会把密码写入日志的调试输出。COS/registry 权限仅覆盖 LightRAG 自己的路径和仓库。参考项目的 Secret 名称可在管理员授权下复用，但权限范围和事件过滤必须实际确认，不能假定全局 Secret 自动可用。

### 5. 测试环境与首次接入

目标为 `~/.kube/test-config` 对应的 test 集群；所有工具命令显式指定 kubeconfig/context，不改变本机默认上下文。namespace 为 `lightrag-test`，应用 release/deployment 为 `lightrag`。

使用 Kustomize 测试 overlay：两副本、`WORKERS=1`、PGKV/PGDocStatus/PGVector/HugeGraph、稳定且一致的 deployment ID/workspace、两条共同 RWX 路径、非 root UID/GID 1000，禁用 Service links。HugeGraph 连接池按整个两副本部署预算配置，测试起点为每 Pod 1 个连接。

首次接入是单独的显式环境准备，不属于每个 tag 自动部署：

1. 创建 namespace 和命名空间限定的发布 ServiceAccount/RBAC，向 CI 提供专用 `lightrag_test_kubeconfig`，而非复制 ai-center 的集群管理员 kubeconfig。
2. 预置 LightRAG 专属 PG/pgvector 数据库、协调库/权限及 HugeGraph 数据域。可以使用经授权的 test 基础设施，但不共享其他应用的数据库或图数据；不猜测任何现有 Service 就是可用目标。
3. 预置两个 `syno-nfs` RWX PVC，跨不同节点实际验证 UID 1000 的创建、读取、原子 rename 和排他创建操作，保留证据。数据库底层存储按其自身要求选择，不把应用 RWX 文件卷充当数据库持久化方案。
4. 创建外部运行 Secret 与 pull Secret，固定服务器地址、workspace、embedding 模型/维度及共享路径。继续使用已选的真实百炼模型 `qwen-plus` / `text-embedding-v4`（1024 维）；地址与密钥通过环境输入，不写入源码。API key 必须设置，账号模式另需一致且强随机的 TOKEN_SECRET。
5. 配置内部访问控制，仅允许明确的内部调用方和验收作业访问应用端口；Service 为 ClusterIP，不创建公网入口。DNS、数据库、HugeGraph 和模型 HTTPS 出站分别放行；不能宣称 ClusterIP 本身提供访问控制，也不能把 HTTPS 任意出站称为域名级白名单。
6. 完成备份、停止所有写入者并确认后端请求结束后，操作员显式执行已有 migrate/bootstrap 流程。保留日志与实际检查记录，不能由 CI 自动填写确认开关。新环境也不省略 schema/profile 验证。

运行 Secret 预置在 Kubernetes，发布只引用它；CI 不需要取得模型/数据库明文用于构建或打印完整运行环境。对可创建 Pod/Job 的发布身份，不能声称 namespace 内的 Secret 对该身份不可读：其命名空间级权限边界必须在接入文档中明示。

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

- 脚本测试覆盖输入身份、tag/分支矩阵、缺失凭据、归档篡改/穿越、manifest/index、digest、旧发布覆盖、新旧并发发布、外部命令非零及超时；执行假后端测试证明失败时没有后续发布副作用。
- Kustomize manifest 测试覆盖 digest replacement、RWX/非 root/双副本、禁止公开 Service 类型、运行 Secret 引用和默认暂停路由。既有 Helm chart 仍由原有 chart 回归测试覆盖，但不再作为 Woodpecker 测试部署入口。
- 首次环境验收包括 rootless 构建探针、实际 RWX 跨节点验证、显式 bootstrap 和 Secret/RBAC 边界验证；不能用本机 Kind 结果替代 test 集群结果。
- 在获得发布触发授权并配置好凭据后，以受保护测试 tag 跑通远端流水线；记录 commit/tag/pipeline ID、各阶段结果、镜像 digest、两个 Pod 的 imageID 和测试文档/查询结果。再用兼容的新测试版本验证升级路径及至少一个不修改存储的拒绝发布场景。
- 全量测试、前端检查、manifest 检查和远端验收分别报告。尚未取得 CI 凭据、测试资源或触发授权时，明确列为未完成项，不把 proposal/脚本完成等同于用户已可测试。

## Risks / Trade-offs

- **rootless 仍需容器运行时能力** → 先探针验证；失败不自动提权、不修改 agent 全局安全设置。
- **完整镜像较大，模型依赖下载耗时** → 固定工具/模型输入、使用 registry cache、设置磁盘和超时预算；不清理其他项目镜像或卷。
- **默认基础镜像改为内部 registry** → 在本机验证目标内容及读取权限，记录现有 CI 的接入要求；不借此覆盖其他项目标签或删除旧镜像。
- **跨存储不提供事务及自动 HA** → 只自动执行兼容升级和正常 drain；任何未确认状态保留并交由既有审计恢复路径。
- **首次初始化需要人工维护，环境未就绪的首个 tag 会失败** → 文档明确“先构建镜像，再准备/初始化环境，再重试部署”；不把初始化权限塞入每次部署。
- **同一提交重建不保证 digest 相同** → 已发布版本不可变，重试验证来源并复用已确认 artifact；漂移需新 tag，而非覆盖。
- **共享 RWX 不保证共享文件正确性** → test 集群跨节点验证真实语义；PVC 声明只是一项检查。
- **既有测试失败阻止发布** → 隔离复现并按现行合同修复，不跳过、不将失败改为允许失败。
- **有意保留停止服务状态** → 明确显示发布失败阶段和恢复手册；可用性让位于不丢数据、不混写。

## Migration Plan

1. 评审本 proposal 后实现并验证工作流、脚本、Kustomize 配套、测试和运行手册，不修改现有本地服务。
2. 合并/提交到 fork `master` 并 push，保持 `main` 原始引用；不自动创建 release 或触发生产/pre 发布。
3. 管理员接入 Woodpecker 仓库、受保护 tag 和按事件/镜像限制的 Secret；完成独立测试环境准备及显式初始化。
4. 在授权后触发测试 tag，完成初次部署、兼容升级及拒绝路径验收，再宣布自动测试交付链路可用。
5. 失败回退时停止写入并按分布式部署手册核查，不自动降级为 local writer、不删除协调历史、不恢复跨存储不一致的局部备份。
