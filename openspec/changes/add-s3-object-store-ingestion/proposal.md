## Why

当前测试部署为了让多个 Pod 共享上传源文件和解析 sidecar，仍依赖 RWX PVC；这对分布式部署、跨节点调度、故障恢复和后续环境扩展都有较大运维挑战。用户明确要求不要短期 PVC 过渡方案，而是在保留官方 REST API 兼容性的同时，新增原生 S3-compatible object storage 文档摄取链路。

第三方应用当前预期按官方 LightRAG API 调用 `POST /documents/upload` 上传 multipart 文件。测试环境又需要完全禁止本地 `INPUT_DIR` 文件入口，避免 WebUI 或第三方调用重新落回本地磁盘。因此本 change 需要把“官方兼容上传入口”和“S3-backed 持久源”统一起来：第三方仍调用官方形态的 `/documents/upload`，服务端在 object-store-only profile 下把文件写入对象存储并异步入队，而不是写入 `INPUT_DIR`。

## What Changes

- 新增可选的 S3-compatible Object Store 能力，作为独立文档源与解析 artifact 存储后端；默认关闭，未配置时现有 `/documents/upload`、`/documents/scan`、`INPUT_DIR` 和本地 sidecar 行为完全不变。
- 新增 presigned direct-upload API：第三方应用先向 LightRAG 申请上传会话和预签名 URL，再直接上传到 S3/MinIO，最后提交 upload/session 信息完成校验与入队；LightRAG API 不再承载该链路的大文件正文。
- 扩展官方兼容 `POST /documents/upload`：在 object-store-only profile 且对象存储可用时，multipart 文件由 LightRAG API 流式写入对象存储并复用 object-backed 入队语义，返回官方兼容的 `InsertResponse` / `track_id`；该路径不得写入 `INPUT_DIR`，也不得同步等待解析、抽取或索引完成。
- 新增 object-backed document source 语义：pipeline/doc_status/full_docs 显式保存对象源元数据、checksum、size、etag、bucket/key、upload session，而不是把对象伪装成本地路径。
- 新增 object source resolver：解析 worker 在处理 object-backed 文档时，从对象存储下载到 Pod 本地 scratch 目录供现有 parser 使用；解析输出和 sidecar 上传回对象存储并以远程 URI 记录。
- 新增删除、重试、重复检测、清理 orphan upload、可观测状态与错误处理规则，保证对象引用和 doc_status/KV 状态一致可恢复。
- 新增 Kubernetes/Woodpecker 配套：测试环境可选择原生 S3 链路并移除共享 `INPUT_DIR` PVC；Pod 只需要本地临时 scratch，不以 RWX PVC 作为多副本文档源同步机制。
- 新增可选的 `ENABLE_LOCAL_FILE_INGESTION=false` 运行时开关；测试环境使用 object-store-only profile 时显式拒绝 `/documents/scan` 和所有本地 `INPUT_DIR` 写入，但在对象存储可用时继续允许官方兼容 `/documents/upload` 走 S3-backed 路径。
- 调整公开对接文档边界：面向第三方的文档只描述外部 API 调用契约、请求/响应、状态轮询和错误码；内部对象源字段、session 存储、pipeline metadata、锁和恢复细节只保留在 OpenSpec/design 或内部 runbook 中。
- 不删除现有文件上传、目录扫描、parser hint、local sidecar 或 SDK raw insert 路径；本 change 是 additive extension，不做破坏性替换。

## Capabilities

### New Capabilities

- `s3-object-store-ingestion`: 原生 S3-compatible 对象存储文档摄取、官方兼容 S3-backed upload、presigned direct upload、object-backed pipeline source 与远程解析 artifact 管理。

### Modified Capabilities

无已归档正式 spec。本 change 通过新增 capability 约束新链路，并明确现有本地上传/扫描行为保持兼容。

## Impact

涉及 `lightrag/` 对象存储抽象与 S3 实现，`lightrag/api/routers/document_routes.py`、本地文件入口 middleware、API 配置与启动校验、pipeline source resolver 与 parser sidecar URI 处理、doc_status/full_docs metadata 合同、删除/clear/retry/source-conflict 相关逻辑、Kubernetes Kustomize 测试部署、Woodpecker 部署验收、环境变量文档和针对 API/pipeline/object-store 的回归测试。

外部依赖为 S3-compatible 服务（测试环境可复用 MinIO/COS）、对应访问凭据、bucket/prefix 生命周期策略和可选 multipart upload 支持。凭据必须通过 Secret 注入，不进入日志、发布记录或 OpenSpec artifact。对象存储不替代 PG/pgvector、HugeGraph、KV/doc-status 后端；它只负责原始文件和解析 artifact，不存储向量、图或文档状态事实。
