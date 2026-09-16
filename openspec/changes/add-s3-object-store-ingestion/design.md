## Context

动机和外部行为见 `proposal.md` 与 `specs/s3-object-store-ingestion/spec.md`。当前文件摄取路径以 `INPUT_DIR` 中的本地文件为事实来源：`/documents/upload` 先把请求体写入输入目录，`/documents/scan` 枚举输入目录，`pending_parse` 记录在 parse worker 中再解析本地源文件；结构化解析结果以 `file://.../__parsed__/...` sidecar URI 记录。测试部署当前为多 Pod 共享这些文件挂载 RWX PVC，但向量、图、KV 与 doc-status 在分布式 profile 中已经由 PG/pgvector 与 HugeGraph 承载。

这个 change 的设计重点是新增 object-backed 文档源，而不是把 S3 对象伪装成 `INPUT_DIR` 文件。现有 parser 多数仍以本地路径作为输入，因此第一版在 worker 本地 scratch 中 materialize 源对象和 sidecar mirror，再复用现有 parser/chunker/analyzer。

## Goals / Non-Goals

**Goals:**

- 新增原生 S3-compatible 对象摄取链路，并保持现有本地上传、目录扫描、SDK raw insert 和本地 sidecar 行为兼容。
- 让上传数据面绕过 LightRAG API：API 只签发上传会话、校验对象元数据、写入文档状态并触发 pipeline。
- 取消 object-backed 链路对共享 `INPUT_DIR` PVC 的依赖；多 Pod 只依赖相同对象存储、数据库和 HugeGraph 配置。
- 允许 object-store-only 部署显式关闭本地 `/documents/upload` 与 `/documents/scan` 入口，确保测试环境不会回退到本地 `INPUT_DIR`。
- 明确对象源、解析 artifact、retry/delete/clear 的所有权与失败语义，避免把远程对象当成本地路径误删或误报。
- 使用可测试的抽象支持 S3-compatible 服务，测试中可用 in-memory/fake object store 覆盖错误路径。

**Non-Goals:**

- 不删除或重命名 `/documents/upload`、`/documents/scan`、`INPUT_DIR`、本地 parser hint 或本地 sidecar。
- 不把对象存储用作向量库、图存储、doc-status 或 KV 的替代；这些仍由既有 storage backend 决定。
- 不在第一版实现任意外部 bucket/key 导入。上传对象必须由 LightRAG 签发 session 并生成 key；后续如需 trusted import 另开 capability。
- 不承诺 parser 全链路 streaming。worker 本地 scratch 是第一版的解析边界，后续可逐步优化为流式读取。
- 不实现跨对象存储与 PG/HugeGraph 的 ACID 事务；沿用仓库既有“失败可诊断、可恢复、不伪成功”的一致性原则。

## Decisions

### 1. 新增 ObjectStore 抽象，而不是改造本地上传为 S3

选择新增可选对象存储抽象，默认 `disabled`。对象摄取 API 只在配置启用且 preflight 通过时可用；本地上传/扫描默认继续使用原路径。这样满足“扩展而不是修改现有行为”，并让 K8s 测试部署可以选择只验收对象链路、逐步取消共享 input PVC。object-store-only profile 额外设置 `ENABLE_LOCAL_FILE_INGESTION=false`，由 ASGI 预 body guard 和路由 guard 同时拒绝本地 `/documents/upload` 与 `/documents/scan`，但不影响 presign/complete、`/documents/text` 和 `/documents/texts`。

备选方案 A 是直接把 `/documents/upload` 写入 S3，并让 `/documents/scan` 扫 S3 prefix。这会改变现有客户端和文件运维习惯，且 scan 的排序、冲突修复、手工放文件流程都会变成破坏性迁移。备选方案 B 是在上传完成后把 S3 对象下载回 `INPUT_DIR`，这能少改代码但仍依赖共享 PVC，本质是短期过渡方案，已被用户排除。

### 2. 上传会话使用现有 KV/doc-status 事实链，不新增必需业务数据库 schema

上传 session 存为一个小型持久记录，包含 workspace、upload_id、server-owned key、safe filename、canonical basename、declared size/content-type/checksum、expiry、status、created_at、completed_at 和清理状态。优先复用现有 KV storage 的独立 namespace；在分布式测试 profile 中该 KV 已经是 PGKVStorage，满足多 Pod 可见性。JSON KV 在单机模式中也能支持本地开发。

session 状态至少包含 `issued`、`completed`、`expired`、`aborted`。只有 `issued` 且未过期、object metadata 校验通过的 session 可以完成入队；完成后 session 变为 `completed`，重复 complete 返回幂等结果或明确冲突，不能创建第二个文档。

备选方案是新建专用 SQL 表。它在 PG profile 中更强，但会让非 PG 本地开发路径失去一致行为，并把对象摄取和 distributed coordination schema 绑定过深。第一版使用 KV namespace 足够表达行为；如果后续需要高效 listing/GC，再迁移到专用表。

### 3. `file_path` 继续表示文档业务来源名，新增显式 object source metadata

对象链路入队仍保留 canonical `file_path`，用于现有重复来源检测、引用显示和用户界面兼容；真实对象位置不写进 `file_path`，而是新增 source metadata，例如 `source_kind="s3_object"` 与 `object_source={bucket,key,etag,size,checksum_sha256,content_type,upload_id}`。本地上传/扫描不需要新增该字段；读取方以字段存在与否判断来源类型。

这样可以避免把 `s3://bucket/key` 当成 filename，破坏当前 basename dedup、parser hint、source-conflict 和 citation 逻辑。删除与 retry 也能明确区分“本地源文件”和“对象源文件”。

备选方案是把 `file_path` 改成 URI。它看似统一，但会触发大量兼容性风险：已有 API response、source conflict、query references 和文档列表都把它当用户可读来源名。

### 4. Presigned URL 是上传数据面，LightRAG complete 是控制面

新增两个控制面入口：创建上传 session，完成上传 session。创建 session 时只签发短 TTL、受 method/header/size/checksum 约束的 URL；完整凭据不返回客户端。客户端直传对象存储后，必须调用 complete；服务端通过 HEAD/metadata/checksum 校验对象，再写入 document storage 并触发 pipeline。

如果对象存储支持校验头，签名时要求客户端按 session 带上 checksum header；否则 complete 阶段至少校验 size、etag/metadata 和可选应用层 sha256。第一版先支持单对象 PUT presign；对象大小上限沿用 `MAX_UPLOAD_SIZE`，因此不需要先实现 multipart upload。若未来提高到超大文件或不稳定网络，再扩展 multipart session，不改变 document source 语义。

备选方案是 API proxy upload 到 S3。它不能解决 API 流量和内存瓶颈，也不能减少大文件上传对 Pod 的压力。

### 5. Parser 仍消费本地 scratch，sidecar 远程化

object-backed pending-parse 文档被 worker 领取后，source resolver 将对象下载到本地 scratch 目录，使用安全 basename 和 session metadata 生成临时路径，然后调用现有 parser。parser 产生的 sidecar 先落在同一 scratch 树，parse 成功后上传到对象存储 artifact prefix，并把 `sidecar_location` 写为 `s3://bucket/prefix/.../` 或等价远程 URI。

后续 analyze/chunk/retry 通过 sidecar resolver 解析 `file://` 或远程 URI。远程 URI 会在处理生命周期内下载/同步到 scratch mirror；本地文档仍直接读取 `file://`。scratch 不是事实来源，Pod 重启后可丢弃。

备选方案是逐个 parser 支持 S3 stream。它长期更省磁盘，但现有 native/mineru/docling/legacy 路径和 sidecar 消费点较多，一次性流式改造风险高，也不是移除共享 PVC 的必要条件。

### 6. 删除、clear 和 GC 分离：已提交对象与未完成上传分别处理

完成入队的 object source/artifact 由文档生命周期管理：按 delete/clear 参数决定是否删除源对象和 parsed artifact，删除失败必须在响应或状态中可见，不能宣称已全量清理。未完成 session 由 upload-session GC 管理，只删除 pending prefix，不触碰 completed 文档 prefix。

对象 key 前缀按 workspace 和 upload/doc id 分层，删除只允许作用于服务端生成并记录为 owned 的 prefix。这样可以避免误删第三方 bucket 中的非 LightRAG 对象，也为后续 lifecycle policy 提供稳定前缀。

备选方案是完全依赖 bucket lifecycle 自动过期。它可以作为辅助，但无法替代应用层对 completed 文档 artifact 的删除语义和错误报告。

### 7. 配置与部署失败关闭

新增配置项覆盖 provider enable、endpoint URL、bucket、region、path-style、access key/secret、presign TTL、upload/session TTL、scratch dir、可选 SSE、最大上传大小和 object prefix。S3 实现延迟导入客户端库；未启用时缺失依赖不影响现有服务。启用后 preflight 必须验证 bucket 可访问、head/put/delete 权限满足最小需求，失败则对象摄取不可用或启动失败，不能静默回落成本地文件上传。

K8s 通过 Secret 注入凭据，通过 env/configmap 注入非密配置。测试部署的目标形态是：PG/pgvector、HugeGraph、对象存储、`emptyDir` scratch、`ENABLE_LOCAL_FILE_INGESTION=false`；不再为 object-backed 链路创建共享 `INPUT_DIR` PVC，也不允许客户端通过本地上传/scan 重新依赖该路径。保留 `WORKING_DIR` 是否仍需 PVC 要单独基于其他本地文件用途评估，不能因为对象摄取完成就误称所有 PVC 均可删除。

## Risks / Trade-offs

- 对象存储故障会让 object-backed 文档无法解析或重试 → fail closed，文档进入 FAILED 并保留可诊断错误；本地上传链路不受影响。
- 本地 scratch 仍会占用 Pod 磁盘 → 通过 `emptyDir` sizeLimit、单文档大小限制和及时清理控制；这是换掉共享 PVC 的代价，不是长期事实存储。
- S3 ETag 不一定等于 MD5，特别是 multipart/SSE 场景 → session 支持显式 checksum；不能仅依赖 ETag 作为完整性证明。
- KV namespace 存 session 的 listing/GC 能力可能弱于专用 SQL 表 → 第一版 session 量通常与上传请求量同阶；若 GC 成为瓶颈，再迁移到专用表并保持 API 不变。
- 远程 sidecar resolver 影响 analyze/chunk 路径 → 先补回归测试覆盖 `file://` 与远程 URI 并保持本地路径不变，避免把现有文档重写为远程 sidecar。
- 完成上传与入队之间没有跨存储事务 → complete 只在对象校验通过后写 doc_status/full_docs；若后续写失败，session 保持未完成或失败可重试，不报告伪成功。

## Migration Plan

1. 默认发布为关闭状态：现有本地上传、scan、WebUI 上传和 SDK raw insert 不改变。
2. 在本地 fake object store 与 MinIO/COS 测试环境中启用对象摄取，验证 presign、direct PUT、complete、pipeline、retry、delete 和 clear。
3. Kubernetes 测试部署注入对象存储 Secret，并把 object-backed 验收加入 Woodpecker；该链路使用 `emptyDir` scratch，不挂载共享 `INPUT_DIR` PVC，并设置 `ENABLE_LOCAL_FILE_INGESTION=false`。
4. 确认对象链路验收稳定后，再把测试环境常规文档输入切到 presigned flow；保留旧 `/documents/upload` 作为兼容入口。
5. 回退方式是关闭对象摄取配置并保留现有本地上传/scan；已完成的 object-backed 文档仍保留 metadata，可在对象存储恢复后 retry/delete，不需要把对象复制回 `INPUT_DIR`。
