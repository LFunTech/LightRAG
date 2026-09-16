## Purpose

提供一条可选的原生 S3-compatible 文档摄取链路，使外部应用既可以按官方 `POST /documents/upload` API 上传文件，也可以通过 presigned flow 直传大文件到对象存储，并让 LightRAG 以对象引用驱动后续解析、索引、重试和删除，同时保持未启用对象存储时的既有本地上传与目录扫描行为兼容。

## ADDED Requirements

### Requirement: Additive object-store ingestion
The system SHALL provide object-store ingestion as an opt-in capability without changing the observable behavior of existing local file upload, local input directory scan, SDK raw text insertion, or local parsed-artifact workflows when object-store ingestion is disabled.

#### Scenario: Object-store ingestion disabled
- **WHEN** the server starts without object-store ingestion configured
- **THEN** existing `/documents/upload`, `/documents/scan`, text insertion, parser hints, local `INPUT_DIR`, and local sidecar behavior remain available with their current request and response semantics

#### Scenario: Object-store ingestion enabled alongside local upload
- **WHEN** the server starts with object-store ingestion configured
- **THEN** clients can use the new object-store upload flow while existing local upload and scan clients continue to work unless an operator separately disables those legacy entry points

#### Scenario: Operator disables local file entry points
- **WHEN** the server starts with object-store ingestion configured and `ENABLE_LOCAL_FILE_INGESTION=false`
- **THEN** `POST /documents/scan` is rejected with a forbidden response without scheduling an input-directory scan
- **AND** `POST /documents/upload` remains available as an official-compatible S3-backed upload endpoint that does not write to `INPUT_DIR`
- **AND** `POST /documents/uploads/presign`, `POST /documents/uploads/complete`, and text insertion remain available according to their own configuration and validation rules

#### Scenario: Local file entry points disabled without object store
- **WHEN** the server starts without object-store ingestion configured and `ENABLE_LOCAL_FILE_INGESTION=false`
- **THEN** `POST /documents/upload` and `POST /documents/scan` are rejected instead of silently writing to local storage

### Requirement: Official-compatible S3-backed upload
The system SHALL preserve the official `POST /documents/upload` multipart API shape for third-party clients while using object storage as the durable file source whenever local file ingestion is disabled and object-store ingestion is available.

#### Scenario: Multipart upload succeeds in object-store-only profile
- **WHEN** an authenticated client posts a valid multipart `file` to `POST /documents/upload` while object-store ingestion is configured and `ENABLE_LOCAL_FILE_INGESTION=false`
- **THEN** the system stores the uploaded file in object storage under a server-owned object record
- **AND** it enqueues the document through the object-backed pipeline
- **AND** it returns the standard insert response containing a `track_id`

#### Scenario: Official upload does not create an input-dir file
- **WHEN** `POST /documents/upload` succeeds in object-store-only profile
- **THEN** no source file is written to `INPUT_DIR`
- **AND** a later worker can process the document without relying on the upload-handling Pod's local filesystem

#### Scenario: Official upload remains asynchronous
- **WHEN** `POST /documents/upload` has stored the object and durably enqueued the document
- **THEN** the HTTP response is returned without waiting for parsing, LLM extraction, graph indexing, or vector indexing to finish
- **AND** clients use the returned `track_id` and existing status APIs to observe final processing state

#### Scenario: Official upload fails closed when object storage is unavailable
- **WHEN** local file ingestion is disabled and object-store ingestion is configured but temporarily unavailable or fails preflight
- **THEN** `POST /documents/upload` fails with a clear error
- **AND** the system does not fall back to writing the file into `INPUT_DIR`

#### Scenario: Official upload validation stays compatible
- **WHEN** a multipart upload uses an unsafe filename, unsupported extension, oversized body, duplicate active source name, or invalid parser/chunk options
- **THEN** the system rejects the request with the same externally observable validation semantics used by the official upload API
- **AND** it does not leave a committed document record for the rejected upload

### Requirement: Presigned upload sessions
The system SHALL allow an authenticated client to create a bounded upload session for one document object and receive a presigned S3-compatible upload URL plus the headers and expiry needed to upload directly to object storage.

#### Scenario: Successful session creation
- **WHEN** an authenticated client requests an upload session with a safe filename, declared content type, declared size, workspace, and optional checksum
- **THEN** the system returns an upload session identifier, a server-generated object key, a presigned upload URL, required upload headers, expiry time, and the maximum accepted size for that session

#### Scenario: Unsafe filename rejected
- **WHEN** a client requests an upload session with a filename that would be rejected by the existing document upload filename rules
- **THEN** the system rejects the request before issuing any object-store credential or presigned URL

#### Scenario: Size limit rejected before signing
- **WHEN** a client requests an upload session whose declared size exceeds the configured document upload limit
- **THEN** the system rejects the request before issuing any object-store credential or presigned URL

### Requirement: Server-owned object keys and tenant isolation
The system SHALL generate and validate object keys so a client cannot upload, complete, read, delete, or enqueue objects outside the authenticated workspace and upload session it was granted.

#### Scenario: Client-supplied key ignored for new uploads
- **WHEN** a client creates an upload session
- **THEN** the authoritative object key is generated by the server and is constrained to the target workspace/session prefix rather than accepted from arbitrary client input

#### Scenario: Completion with foreign key rejected
- **WHEN** a client tries to complete an upload using an object key that was not issued for that authenticated workspace and upload session
- **THEN** the system rejects the completion and does not enqueue a document

### Requirement: Direct upload completion verifies the object
The system SHALL require a completion request after the client uploads the file, verify the object store state, and enqueue the document only when the object metadata matches the upload session contract.

#### Scenario: Successful completion enqueues document
- **WHEN** a client completes an issued upload session after the object exists with matching size, content type, and checksum when a checksum was required
- **THEN** the system records the object source metadata and enqueues the document for normal asynchronous processing

#### Scenario: Missing object on completion
- **WHEN** a client completes an upload session before the object exists in object storage
- **THEN** the system rejects the completion without writing a pending document record

#### Scenario: Metadata mismatch on completion
- **WHEN** the object exists but its size, content type, checksum, or issued key does not match the upload session
- **THEN** the system rejects the completion without enqueueing the document and leaves the upload session auditable for cleanup

#### Scenario: Duplicate source name rejected consistently
- **WHEN** completing an object upload would conflict with an existing active document source for the workspace under the same canonical basename rules as local upload
- **THEN** the system rejects the completion with a conflict response and does not create a second active document for the same source name

### Requirement: Object-backed document processing
The system SHALL process completed object-backed documents through the normal document pipeline while preserving explicit object-source metadata and without requiring a shared input-directory PVC for source-file visibility across Pods.

#### Scenario: Parser receives a local scratch copy
- **WHEN** a worker processes an object-backed pending-parse document
- **THEN** the worker obtains the source object from object storage into writable local scratch space and invokes the parser using that scratch file while preserving the original source metadata for status and citation

#### Scenario: Source object unavailable during processing
- **WHEN** a worker cannot read a completed source object due to missing object, authorization failure, checksum mismatch, or object-store error
- **THEN** the document transitions to a failed state with an actionable error and remains eligible for the existing explicit retry flow after the operator fixes the object-store problem

#### Scenario: Multi-Pod source visibility
- **WHEN** two server Pods share the same database and object-store configuration but do not share an `INPUT_DIR` filesystem
- **THEN** any Pod that claims an object-backed document can process it by reading the source from object storage rather than relying on another Pod's local filesystem

### Requirement: Remote parsed artifacts
The system SHALL store parsed sidecar artifacts for object-backed documents in object storage and record a remote sidecar URI that downstream parsing, multimodal analysis, chunking, retry, and deletion flows can resolve.

#### Scenario: Successful parse stores remote artifacts
- **WHEN** an object-backed document is parsed by an engine that emits sidecar artifacts
- **THEN** the parsed artifacts are persisted under the document's object-store artifact prefix and the document record stores a resolvable remote sidecar URI

#### Scenario: Existing local sidecars remain supported
- **WHEN** a document was ingested by the existing local upload or scan path
- **THEN** local `file://` sidecar records remain valid and are not rewritten to object-store URIs by this capability

### Requirement: Retry, deletion, and clear honor object sources
The system SHALL make explicit decisions for object-backed source files and parsed artifacts during retry, per-document deletion, and workspace clear operations without treating object-store references as local filesystem paths.

#### Scenario: Retry reuses durable object source
- **WHEN** an object-backed document fails during parsing or later processing and the user invokes the existing explicit retry path
- **THEN** the retry uses the recorded object source and remote sidecar metadata rather than requiring the original client to upload the file again

#### Scenario: Delete removes object-backed artifacts when requested
- **WHEN** a user deletes an object-backed document with source/artifact deletion enabled
- **THEN** the system removes the document's owned object-store source and parsed artifacts only after preserving the same fail-closed storage consistency guarantees used by existing document deletion

#### Scenario: Clear reports object-store cleanup failures
- **WHEN** workspace clear attempts to delete object-backed sources or parsed artifacts and object-store cleanup fails
- **THEN** the response and logs identify that object-store cleanup was incomplete without claiming that all document artifacts were removed

### Requirement: Upload session lifecycle and cleanup
The system SHALL keep upload sessions observable and expire or clean up abandoned object uploads without enqueueing documents that were never completed.

#### Scenario: Expired session cannot complete
- **WHEN** a client attempts to complete an upload session after its expiry window
- **THEN** the system rejects completion unless the object is explicitly revalidated through a new session or supported recovery path

#### Scenario: Abandoned upload cleanup
- **WHEN** an upload session expires without successful completion
- **THEN** the system can identify and remove the abandoned pending object prefix without touching committed document sources

### Requirement: Object-store deployment contract
The system SHALL expose object-store configuration as explicit runtime configuration and Kubernetes Secret-driven deployment settings, and it SHALL fail closed when object-store ingestion is enabled but required configuration or access verification fails.

#### Scenario: Missing object-store configuration
- **WHEN** object-store ingestion is enabled but required endpoint, bucket, region/path-style setting, or credentials are missing
- **THEN** server startup or object-store preflight fails clearly instead of silently falling back to local file ingestion for object-backed requests

#### Scenario: Test deployment without shared input PVC
- **WHEN** the Kubernetes test deployment is configured to exercise object-store ingestion
- **THEN** it can run multiple Pods without a shared `INPUT_DIR` PVC, using object storage for source files and parsed artifacts and local ephemeral storage only for scratch processing
- **AND** `/documents/scan` and all local `INPUT_DIR` writes are disabled for that test profile
- **AND** official-compatible `POST /documents/upload` remains available by writing to object storage

### Requirement: Third-party API documentation boundary
The system SHALL document third-party integration in terms of external API contracts rather than exposing internal object-store, pipeline, or storage implementation details.

#### Scenario: Public document upload instructions
- **WHEN** a third-party developer reads the public API documentation
- **THEN** the documentation explains how to call `POST /documents/upload`, how to call the optional presigned upload flow, how to authenticate, what request and response fields mean, which status endpoint to poll, and how common errors should be handled

#### Scenario: Internal implementation details omitted from public docs
- **WHEN** the public API documentation describes S3-backed ingestion
- **THEN** it does not expose internal `object_source` metadata, `full_docs` or `doc_status` storage fields, upload session storage namespaces, object key generation rules, distributed pipeline scheduling, locks, fences, or recovery mechanics as third-party API obligations

#### Scenario: Internal docs may describe implementation
- **WHEN** maintainers need to reason about consistency, recovery, or deployment internals
- **THEN** those details are kept in OpenSpec, internal design documents, runbooks, or code-level contracts rather than the third-party integration guide
