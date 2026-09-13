## Purpose

为 LightRAG 提供可配置的 HugeGraph 图存储，使实体、关系、图浏览和维护功能能够使用外部 HugeGraph 服务，并保持现有公开接口、数据隔离和失败恢复契约。

## ADDED Requirements

### Requirement: Selectable backend and safe lifecycle
The system SHALL support selecting HugeGraphStorage through existing storage configuration, connecting asynchronously to HugeGraph 1.7, validating version and schema, optionally provisioning only its own schema, and releasing connections.

#### Scenario: Repeated startup
- **WHEN** two clients initialize an existing compatible schema
- **THEN** both succeed without destructive schema changes; incompatible definitions or unavailable services raise actionable errors

### Requirement: Isolated undirected property graph
The backend SHALL isolate workspace and namespace, preserve scalar attributes and partial updates, use original entity IDs externally, and represent each unordered endpoint pair as one relationship.

#### Scenario: Reciprocal and repeated writes
- **WHEN** a batch writes A-B and B-A with overlapping and distinct properties
- **THEN** both directions read the same merged properties, conflicting properties use the last input, and no duplicate relationship exists

#### Scenario: Workspace deletion
- **WHEN** a workspace is dropped
- **THEN** only its scoped data is deleted; other namespaces, workspaces and business schemas remain unchanged

### Requirement: Complete graph operations
The backend SHALL support single and bounded-batch existence, read, upsert, degree and deletion operations, original-ID graph export, and bounded label and edge iteration without first collecting the graph.

#### Scenario: Maintenance iteration
- **WHEN** maintenance iterates a graph larger than a page
- **THEN** all scoped labels and edges are returned in bounded batches without omission or duplication on a quiescent graph

### Requirement: Graph browsing
The backend SHALL provide label search, popular labels including isolated entities, capped breadth-first subgraphs and full-graph views. Full-graph and popular cutoffs SHALL rank degree descending then entity name by Unicode code point ascending.

#### Scenario: Truncated graph
- **WHEN** more nodes are reachable than max_nodes allows
- **THEN** the response stays within the cap, contains no dangling edge endpoints, and indicates truncation

### Requirement: Failure and recovery semantics
The backend SHALL distinguish confirmed absence, empty relations and backend failure; preserve core attribution and purge ordering; report partial or uncertain writes rather than silently succeeding. It SHALL never interpolate user data into executable Gremlin. Before sending each graph-data mutation, it SHALL record a pending fence in the shared coordination domain, keyed by the normalized destination URI, graph path, and workspace/namespace scope, and SHALL clear that fence only after a successful response and validated acknowledgement. URI normalization SHALL use yarl.URL consistently with aiohttp for scheme/host case, default ports, equivalent URL encodings, and IPv6 representations. An outstanding fence SHALL reject subsequent mutations for the same key without sending them, while permitting diagnostic reads and operations for other destination scopes. All writers to the same destination scope SHALL use one service URI and one shared coordination domain; DNS aliases, localhost versus IP addresses, and different proxy endpoints are not automatically recognized as the same backend and SHALL NOT be used to bypass a pending fence.

#### Scenario: Failed read
- **WHEN** the service times out, refuses authentication, returns malformed data or reports a Gremlin error
- **THEN** the caller receives an exception rather than an absent node or empty graph

#### Scenario: Retried mutation
- **WHEN** all writers have been stopped, an operator has confirmed that the server has no outstanding old mutation and audited committed state, the entire shared coordination domain has been restarted, and a caller manually repeats the deterministic write
- **THEN** it converges without duplicate nodes, edges or doubled relationship weights

#### Scenario: Uncertain write cannot race a successor
- **WHEN** a sent mutation times out, is cancelled, fails, or returns an invalid acknowledgement while an old server transaction might still commit
- **THEN** its pending fence remains and every queued or new mutation for that destination scope, including deletion and drop, fails closed rather than allowing an old property snapshot to overwrite a later acknowledged update

#### Scenario: Equivalent URI spellings share coordination identity
- **WHEN** clients in one shared coordination domain use service URIs that normalize to the same yarl.URL identity and target the same graph and workspace/namespace scope
- **THEN** they share the mutation lock and pending fence despite differences in scheme/host case, default-port spelling, equivalent URL encoding, or IPv6 representation

#### Scenario: Service aliases are an operator configuration boundary
- **WHEN** several DNS names, localhost and an IP address, or different proxy URLs route to the same HugeGraph target scope
- **THEN** operators must configure every writer with one consistent service URI and shared coordination domain rather than assuming endpoint discovery will merge the identities or using another address to escape a pending fence

#### Scenario: Worker loss retains the mutation fence
- **WHEN** a worker exits after recording a pending fence and before validating its mutation acknowledgement while the shared coordination domain remains alive
- **THEN** another worker cannot resume mutations for that destination scope merely by acquiring the released or recovered keyed lock; client finalization, initialization, instance replacement, and individual worker restart do not clear the fence

#### Scenario: Recovery requires an audited coordination-domain restart
- **WHEN** a pending fence blocks mutations
- **THEN** reads remain available for diagnosis and ordinary retries remain blocked until all writers are stopped, an operator confirms that no old server request remains in flight and audits committed state, and the entire shared coordination domain is restarted; a healthy connection or client restart alone does not authorize replay

### Requirement: Product integration and deployment
The backend SHALL work through normal LightRAG ingestion, retrieval, graph editing and document purge paths and SHALL be selectable in the external-service setup wizard.

#### Scenario: Document lifecycle
- **WHEN** documents sharing entities are ingested, queried, edited and one is deleted
- **THEN** retrieval uses HugeGraph and deletion preserves surviving contributions and their recovery anchors
