## Context

动机和外部行为见 `proposal.md` 与 `specs/s3-object-store-ingestion/spec.md`。当前文件摄取路径以 `INPUT_DIR` 中的本地文件为事实来源：`/documents/upload` 先把请求体写入输入目录，`/documents/scan` 枚举输入目录，`pending_parse` 记录在 parse worker 中再解析本地源文件；结构化解析结果以 `file://.../__parsed__/...` sidecar URI 记录。测试部署当前为多 Pod 共享这些文件挂载 RWX PVC，但向量、图、KV 与 doc-status 在分布式 profile 中已经由 PG/pgvector 与 HugeGraph 承载。

新增约束是：第三方应用仍需按官方 REST API 形态调用 `POST /documents/upload`，公开文档也只应描述外部如何调用，而不是暴露内部对象源、session manager、doc_status/full_docs、pipeline 调度或恢复实现。object-store-only profile 的目标不是移除 `/documents/upload` 这个官方入口，而是禁止它写入本地 `INPUT_DIR`。

这个 change 的设计重点是新增 object-backed 文档源，而不是把 S3 对象伪装成 `INPUT_DIR` 文件。现有 parser 多数仍以本地路径作为输入，因此第一版在 worker 本地 scratch 中 materialize 源对象和 sidecar mirror，再复用现有 parser/chunker/analyzer。

## Goals / Non-Goals

**Goals:**

- 新增原生 S3-compatible 对象摄取链路，并保持现有本地上传、目录扫描、SDK raw insert 和本地 sidecar 行为兼容。
- 保持官方兼容 `POST /documents/upload`：第三方无需改成内部接口；在 object-store-only profile 中该入口写入对象存储而不是 `INPUT_DIR`。
- 提供高性能 presigned direct-upload 路径：该路径让上传数据面绕过 LightRAG API，API 只签发上传会话、校验对象元数据、写入文档状态并触发 pipeline。
- 取消 object-backed 链路对共享 `INPUT_DIR` PVC 的依赖；多 Pod 只依赖相同对象存储、数据库和 HugeGraph 配置。
- 允许 object-store-only 部署显式关闭 `/documents/scan` 与所有本地 `INPUT_DIR` 写入入口，确保测试环境不会回退到本地文件系统。
- 明确对象源、解析 artifact、retry/delete/clear 的所有权与失败语义，避免把远程对象当成本地路径误删或误报。
- 面向第三方的公开文档只描述调用契约、请求/响应、状态轮询和错误码；内部实现细节只进入 OpenSpec/design/runbook。
- 使用可测试的抽象支持 S3-compatible 服务，测试中可用 in-memory/fake object store 覆盖错误路径。

**Non-Goals:**

- 不删除或重命名 `/documents/upload`、`/documents/scan`、`INPUT_DIR`、本地 parser hint 或本地 sidecar。
- 不把对象存储用作向量库、图存储、doc-status 或 KV 的替代；这些仍由既有 storage backend 决定。
- 不在第一版实现任意外部 bucket/key 导入。上传对象必须由 LightRAG 签发 session 并生成 key；后续如需 trusted import 另开 capability。
- 不承诺 parser 全链路 streaming。worker 本地 scratch 是第一版的解析边界，后续可逐步优化为流式读取。
- 不实现跨对象存储与 PG/HugeGraph 的 ACID 事务；沿用仓库既有“失败可诊断、可恢复、不伪成功”的一致性原则。
- 不把内部对象 key、object source metadata、upload session 存储格式或 pipeline 调度细节作为第三方公开 API 合同。

## Decisions

### 1. ObjectStore 是事实源；上传入口按部署 profile 选择写入后端

选择新增可选对象存储抽象，默认 `disabled`。对象摄取 API 只在配置启用且 preflight 通过时可用；本地上传/扫描默认继续使用原路径。这样满足“扩展而不是修改现有行为”，并让 K8s 测试部署可以选择只验收对象链路、逐步取消共享 input PVC。

`/documents/upload` 不再等价于“本地 INPUT_DIR 上传”。路由根据配置选择后端：

- 对象存储未启用且本地文件入口启用：保持既有行为，写入 `INPUT_DIR`。
- 对象存储启用且本地文件入口启用：默认保持既有本地行为；第三方需要对象直传时可用 presign/complete。
- 对象存储启用且 `ENABLE_LOCAL_FILE_INGESTION=false`：`/documents/upload` 保持官方 API 形态，但文件流写入对象存储并进入 object-backed pipeline；`/documents/scan` 继续 403。
- 对象存储未启用且 `ENABLE_LOCAL_FILE_INGESTION=false`：`/documents/upload` 与 `/documents/scan` 均 403，不能静默回退到本地。

ASGI 预 body guard 和路由 guard 的职责因此要区分“本地文件入口”与“官方 upload endpoint”。它们必须继续阻止会写 `INPUT_DIR` 的请求，但不能在 S3-backed upload 可用时提前拒绝 `POST /documents/upload`。`/documents/text` 和 `/documents/texts` 不属于本地文件入口，不受该开关影响。

备选方案 A 是把 `/documents/upload` 总是改成 S3-backed。这会改变未启用对象存储时的默认行为，也会影响依赖本地 `INPUT_DIR` 的单机用户。备选方案 B 是 object-store-only profile 直接删除官方 upload，只保留 presign/complete。这会迫使第三方应用偏离官方 API，已被用户否定。备选方案 C 是在上传完成后把 S3 对象下载回 `INPUT_DIR`，这能少改代码但仍依赖共享 PVC，本质是短期过渡方案，已被用户排除。

### 2. 上传会话使用现有 KV/doc-status 事实链，不新增必需业务数据库 schema

上传 session 存为一个小型持久记录，包含 workspace、upload_id、server-owned key、safe filename、canonical basename、declared size/content-type/checksum、expiry、status、created_at、completed_at 和清理状态。优先复用现有 KV storage 的独立 namespace；在分布式测试 profile 中该 KV 已经是 PGKVStorage，满足多 Pod 可见性。JSON KV 在单机模式中也能支持本地开发。

session 状态至少包含 `issued`、`completed`、`expired`、`aborted`。只有 `issued` 且未过期、object metadata 校验通过的 session 可以完成入队；完成后 session 变为 `completed`，重复 complete 返回幂等结果或明确冲突，不能创建第二个文档。

备选方案是新建专用 SQL 表。它在 PG profile 中更强，但会让非 PG 本地开发路径失去一致行为，并把对象摄取和 distributed coordination schema 绑定过深。第一版使用 KV namespace 足够表达行为；如果后续需要高效 listing/GC，再迁移到专用表。

### 3. `file_path` 继续表示文档业务来源名，新增显式 object source metadata

对象链路入队仍保留 canonical `file_path`，用于现有重复来源检测、引用显示和用户界面兼容；真实对象位置不写进 `file_path`，而是新增 source metadata，例如 `source_kind="s3_object"` 与 `object_source={bucket,key,etag,size,checksum_sha256,content_type,upload_id}`。本地上传/扫描不需要新增该字段；读取方以字段存在与否判断来源类型。

这样可以避免把 `s3://bucket/key` 当成 filename，破坏当前 basename dedup、parser hint、source-conflict 和 citation 逻辑。删除与 retry 也能明确区分“本地源文件”和“对象源文件”。

备选方案是把 `file_path` 改成 URI。它看似统一，但会触发大量兼容性风险：已有 API response、source conflict、query references 和文档列表都把它当用户可读来源名。

### 4. 官方 upload 是兼容入口，presigned URL 是高性能入口

保留两个外部上传形态：

- 官方兼容 upload：`POST /documents/upload` 接收 multipart `file`，返回既有 `InsertResponse` / `track_id`。在 object-store-only profile 中，服务端将请求体流式写入对象存储，完成后校验对象并入队；HTTP 响应只等待持久入队，不等待解析、LLM 抽取或索引完成。
- Presigned direct upload：创建上传 session，完成上传 session。创建 session 时只签发短 TTL、受 method/header/size/checksum 约束的 URL；完整凭据不返回客户端。客户端直传对象存储后，必须调用 complete；服务端通过 HEAD/metadata/checksum 校验对象，再写入 document storage 并触发受管理的后台 pipeline drive。

两个入口最终必须复用同一 object-backed enqueue 语义和同一异步 pipeline drive 行为，避免一个入口产生 `object_source`，另一个入口仍走本地路径或同步处理。官方 upload 可在服务端内部生成一次性 upload/session 或等价的 server-owned object record，但这些内部细节不是公开 API 合同。

如果对象存储支持校验头，签名时要求客户端按 session 带上 checksum header；否则 complete 阶段至少校验 size、etag/metadata 和可选应用层 sha256。第一版先支持单对象 PUT presign；对象大小上限沿用 `MAX_UPLOAD_SIZE`，因此不需要先实现 multipart upload。若未来提高到超大文件或不稳定网络，再扩展 multipart session，不改变 document source 语义。

备选方案是让所有第三方强制改用 presign/complete。它性能最好，但不是官方 API 兼容。另一个备选是 API 先写本地临时文件再上传 S3，它仍可能在 504/Pod 重启/磁盘限制时留下本地入口语义。官方 upload 的 S3-backed 实现必须尽量流式转发到对象存储，避免完整读入内存或落 `INPUT_DIR`。

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

K8s 通过 Secret 注入凭据，通过 env/configmap 注入非密配置。测试部署的目标形态是：PG/pgvector、HugeGraph、对象存储、`emptyDir` scratch、`ENABLE_LOCAL_FILE_INGESTION=false`；不再为 object-backed 链路创建共享 `INPUT_DIR` PVC，也不允许客户端通过 scan 或本地文件写入重新依赖该路径。保留 `WORKING_DIR` 是否仍需 PVC 要单独基于其他本地文件用途评估，不能因为对象摄取完成就误称所有 PVC 均可删除。

### 8. 公开文档只暴露外部调用契约

第三方对接文档应按“外部如何调用”组织，而不是按内部实现组织。公开文档可以说明：

- endpoint、method、auth header、content type；
- request/response 字段和 cURL/SDK 示例；
- `track_id` 状态轮询、错误码、重试建议；
- 官方兼容 `/documents/upload` 与高性能 presign/complete 两种外部流程的取舍。

公开文档不应说明 `object_source`、`full_docs`、`doc_status` 内部字段、对象 key 生成规则、upload session 存储 namespace、distributed operation、pipeline ingress、锁、fence 或恢复细节。上述内容只保留在 OpenSpec、内部 design/runbook 或代码级规则中。

## Risks / Trade-offs

- 对象存储故障会让 object-backed 文档无法解析或重试 → fail closed，文档进入 FAILED 并保留可诊断错误；本地上传链路不受影响。
- 本地 scratch 仍会占用 Pod 磁盘 → 通过 `emptyDir` sizeLimit、单文档大小限制和及时清理控制；这是换掉共享 PVC 的代价，不是长期事实存储。
- S3 ETag 不一定等于 MD5，特别是 multipart/SSE 场景 → session 支持显式 checksum；不能仅依赖 ETag 作为完整性证明。
- KV namespace 存 session 的 listing/GC 能力可能弱于专用 SQL 表 → 第一版 session 量通常与上传请求量同阶；若 GC 成为瓶颈，再迁移到专用表并保持 API 不变。
- 远程 sidecar resolver 影响 analyze/chunk 路径 → 先补回归测试覆盖 `file://` 与远程 URI 并保持本地路径不变，避免把现有文档重写为远程 sidecar。
- 完成上传与入队之间没有跨存储事务 → complete 只在对象校验通过后写 doc_status/full_docs；若后续写失败，session 保持未完成或失败可重试，不报告伪成功。
- 官方 upload 经由 LightRAG API 承载文件正文 → 该路径用于兼容官方 API，可能比 presigned direct upload 更占用 API Pod 带宽；通过 `MAX_UPLOAD_SIZE`、流式上传、异步入队和文档建议把大文件/高并发场景引导到 presign/complete 控制。

## Migration Plan

1. 默认发布为关闭状态：现有本地上传、scan、WebUI 上传和 SDK raw insert 不改变。
2. 在本地 fake object store 与 MinIO/COS 测试环境中启用对象摄取，验证 presign、direct PUT、complete、pipeline、retry、delete 和 clear。
3. Kubernetes 测试部署注入对象存储 Secret，并把 object-backed 验收加入 Woodpecker；该链路使用 `emptyDir` scratch，不挂载共享 `INPUT_DIR` PVC，并设置 `ENABLE_LOCAL_FILE_INGESTION=false`。
4. 确认对象链路验收稳定后，把测试环境常规文档输入切到官方兼容 `/documents/upload` 的 S3-backed 行为；大文件/高并发调用可使用 presigned flow。
5. 回退方式是关闭对象摄取配置并保留现有本地上传/scan；已完成的 object-backed 文档仍保留 metadata，可在对象存储恢复后 retry/delete，不需要把对象复制回 `INPUT_DIR`。
