# S3-compatible 对象存储摄取运维手册

对象存储摄取是可选文档来源，适用于希望客户端把大文件直接上传到 S3-compatible 存储的部署。它是增量能力：未启用时，本地 `/documents/upload`、`/documents/scan`、SDK 文本写入、`INPUT_DIR` 和本地 parsed sidecar 行为保持不变。运维也可以额外设置 `ENABLE_LOCAL_FILE_INGESTION=false` 进入 object-store-only 模式：保留 presign/complete 与文本摄取接口，但拒绝 `/documents/upload` 和 `/documents/scan`。

面向第三方应用的公开对接文档通过 WebUI 静态路径发布：
`/webui/docs/third-party-object-upload-integration/`。该页面只包含调用流程与示例占位符，不包含真实密钥、Secret 名称或环境专用连接信息。

## 运行时配置

```sh
LIGHTRAG_OBJECT_STORAGE=s3
S3_ENDPOINT_URL=https://minio-or-cos.example.com
S3_BUCKET=lightrag-documents
S3_ACCESS_KEY_ID=...
S3_SECRET_ACCESS_KEY=...
# 可选
S3_REGION=
S3_FORCE_PATH_STYLE=true
S3_OBJECT_PREFIX=lightrag/documents
S3_PRESIGN_TTL_SECONDS=900
S3_UPLOAD_SESSION_TTL_SECONDS=3600
S3_SCRATCH_DIR=/tmp/lightrag-object-scratch
# object-store-only 部署选项
ENABLE_LOCAL_FILE_INGESTION=false
```

访问凭据必须来自 Secret 管理器或 Kubernetes Secret，不要写入提交到仓库的 `.env`、命令行、日志、OpenSpec artifact 或证据记录。

启用后，服务启动会初始化 upload session KV namespace 并执行对象存储 preflight。缺少 endpoint、bucket、access key、secret key，或 bucket 权限不可用时会 fail closed，不会把 object-backed 请求静默回退为本地上传。

内置 WebUI 的上传弹窗会在对象存储摄取已配置时自动使用该流程。只有服务端明确返回“对象存储文档摄取未配置”且本地文件摄取仍启用时，WebUI 才保留既有的本地 `/documents/upload` 路径；object-store-only 部署会收到服务端 403 本地入口拒绝。其它 presign 或对象存储错误会直接暴露，不会被本地上传 fallback 掩盖。

## 客户端流程

1. 带认证调用 `POST /documents/uploads/presign`，请求体包含 `filename`、`content_type`、`size`，以及可选 `checksum_sha256`。
2. 使用响应里的 method 和 headers，把文件字节直接上传到 `upload_url`。
3. 调用 `POST /documents/uploads/complete`，提交 `upload_id` 与 `object_key`。

对象 key 由服务端生成，并绑定到 upload session 与 workspace。创建 session 时客户端提供的 object key 会被忽略；complete 时如果 key 与签发记录不一致会被拒绝。complete 会先对对象执行 `HEAD` 校验，确认 size/content type/checksum 等元数据匹配后才入队。

`file_path` 仍然是面向用户展示的来源文件名；S3 bucket、key、size、content type、checksum、ETag、upload id 和 parsed-artifact prefix 都写入 `object_source` metadata。

## 处理与存储边界

worker 会把源对象 materialize 到 `S3_SCRATCH_DIR`，把该本地 scratch 路径传给现有 parser，然后把 parsed sidecar 上传回对象存储 artifact prefix。后续分析、分块和 retry 会把远程 `s3://bucket/prefix/` sidecar mirror 到本地 scratch 后再读取。

对象存储只保存源文件和解析产物，不替代 PG/pgvector、HugeGraph、KV/doc-status、向量存储、图存储或 LLM cache。

## Kubernetes 注意事项

object-backed 分布式 profile 使用：

- 共享 PG/pgvector、HugeGraph、KV/doc-status 配置；
- `LIGHTRAG_OBJECT_STORAGE=s3`；
- 针对输入文件链路设置 `LIGHTRAG_SHARED_STORAGE=false`；
- Kubernetes Secret 注入 S3-compatible 凭据；
- `S3_SCRATCH_DIR` 使用每 Pod 的 `emptyDir` 等临时存储。

本地 `/documents/upload` 与 `/documents/scan` 仍需要处理该本地文件的 Pod 可见对应文件系统。只有验收路径完全使用 object-backed API 时，移除共享 `INPUT_DIR` PVC 才是安全的；此时应设置 `ENABLE_LOCAL_FILE_INGESTION=false`，避免客户端误入本地 `INPUT_DIR` 链路。

## 清理与重试

- `DELETE /documents/delete_document?delete_file=true` 会删除记录中的服务端 owned 源对象和 parsed-artifact prefix，不会把 object-backed 文档的 `file_path` 当作本地路径删除。
- 清理逻辑拒绝 LightRAG owned upload/artifact prefix 之外的 key，避免误删 bucket 中的其它对象。
- `POST /documents/reprocess_failed` 会复用记录中的对象源和远程 sidecar metadata，不要求原客户端重新上传。
- 过期且未完成的 upload session 可由 session manager 识别并清理；清理只触碰未完成上传 prefix，不触碰已完成文档对象。
