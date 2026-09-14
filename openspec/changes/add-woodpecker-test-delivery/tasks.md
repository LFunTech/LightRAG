## 0. 本机基础镜像同步（用户单独授权，其余实施仍待批准）

- [x] 0.1 盘点三个 Dockerfile 的全部外部 FROM、COPY 和 frontend，固定六个源 index digest 与多架构清单。
- [x] 0.2 从本机同步完整基础镜像到 `docker-hub.f123.pub/base/` 独立标签，核对 index、子 manifest 及 blob 可读性，不覆盖共享标签。
- [x] 0.3 仅在目标验证成功后，将三个 Dockerfile 改为内部 digest-pinned 引用，生成对应镜像锁定清单。
- [x] 0.4 验证内部镜像读取与 Dockerfile 构建检查，运行镜像来源回归测试，完成操作文档与验证记录；本切片遵循用户要求提交推送到 master。

## 1. 评审与实施前置

- [x] 1.1 取得本 proposal/design/spec 的实施批准，记录 test 集群、`lightrag-test` namespace 和首次初始化维护边界。
- [ ] 1.2 核对 Woodpecker 仓库接入、受保护 tag、Secret 事件/镜像限制、COS/registry 权限及命名空间级部署凭据；只记录名称和验证结果。
- [ ] 1.3 验证 rootless BuildKit 探针：非 root、所需安全上下文、cache/bind mount、amd64 构建和临时存储预算，固定工具镜像版本/digest；失败不自动提权。

## 2. 本地回归基线与流水线静态门禁

- [x] 2.1 隔离本地配置复现 API prefix 两个失败，核对 API-key-only 与账号模式合同；仅在证据充分时修正测试 fixture/精确断言，补全匿名、错误密钥、合法密钥和两种 prefix 模式覆盖。
- [x] 2.2 保留可复用的本地后端检查入口，按 lock 安装依赖及必要系统库，运行完整非 integration 测试时保留统计和跳过原因；确认 Woodpecker 不调用该入口。
- [x] 2.3 保留本地前端检查入口，从正确目录执行 frozen install、完整 Bun 测试、typecheck、lint、build；确认 Woodpecker 不调用该入口。
- [x] 2.4 添加 master push/PR 和发布 tag 的交付静态工作流，确保 PR 不接收发布/部署凭据、不运行仓库测试套件，并验证静态依赖失败阻止后续发布。

## 3. 发布身份与源码归档

- [x] 3.1 先添加身份校验回归测试，再实现仓库/tag/commit/master 来源校验，覆盖无效 tag、浅克隆、远端错误和移动 tag。
- [ ] 3.2 实现 LightRAG 独立 COS 源码归档与发布记录，绑定 commit/pipeline 身份和校验和，不归档本地环境、秘密或业务数据。
- [x] 3.3 实现消费者身份/校验和验证与安全解包，覆盖篡改、错误记录、路径穿越、符号链接逃逸和外部命令失败；补齐 validate 工作流。

## 4. 镜像构建与验证

- [ ] 4.1 使用已验证的 rootless BuildKit 构建现有完整 Dockerfile，设置 amd64、资源预算、独立 registry cache 及 source/revision 标签，不创建另一份生产 Dockerfile。
- [ ] 4.2 安全生成与清理 registry 凭据，发布到 `docker-hub.f123.pub/lfun/lightrag`，实现既有版本冲突拒绝及来源一致的幂等重试。
- [ ] 4.3 校验 manifest/index、平台、revision 和 digest，持久化发布记录，添加镜像身份不符和未知格式等拒绝路径测试。
- [ ] 4.4 完成 build-image/pre-deploy 工作流，验证失败无后续副作用，并确认 pre/prod tag 不部署。

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

- [ ] 7.1 运行 Woodpecker strict lint、交付脚本本地测试/静态检查、Kustomize render 和相关 manifest 回归；保存命令、版本及结果。
- [ ] 7.2 本地运行完整非 integration 后端测试和完整前端检查，分项记录 pass/skip/fail；确认这些测试结果不被写成 Woodpecker 门禁。
- [ ] 7.3 在批准的 test 集群创建独立测试资源、完成跨节点 RWX 实测和显式初始化；记录目标与证据，不修改本机/Kind 服务或其他应用资源。
- [ ] 7.4 在获得触发授权后用测试 tag 跑通真实 Woodpecker 构建、镜像验证和初次双 Pod 部署，保存 pipeline/tag/commit/digest、imageID 及业务验收结果。
- [ ] 7.5 通过后续兼容测试版本验证自动升级，以及至少一个不破坏存储的拒绝发布场景，确认旧流量不会在验收前恢复。
- [ ] 7.6 完成 CI 接入、Secret/RBAC、失败处理与人工回退 runbook；明确未验证项目，不用静态验证替代远端成功。
- [ ] 7.7 对照 spec 自检全部场景，执行 OpenSpec strict validation，提交并推送到 master，确认 main 未改变；不自动归档或创建生产 release。
