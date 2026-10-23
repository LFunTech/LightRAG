# Opt-in query usage accounting

## Scope and activation

`create_app(args, query_usage_authority=authority)` enables a trusted, injected
`QueryUsageAuthority`. It is disabled by default; ordinary queries retain their
existing response shape and provider behavior. No credential, database, remote
callback URL, tariff, or allowance is discovered or created by this feature.

The bounded contract is `query-usage-v1`. An authenticated `/query` request may
include `accounting: {query_id, grant}` with `only_need_context: true`. It must use
a retrieval mode rather than `bypass`. The accounting envelope is not forwarded
to the retriever. `/query/stream` and `/query/data` reject that envelope instead
of silently performing an unaccounted query. Existing API authentication still
runs before retrieval or budget admission.

The supported accounting runtime uses the OpenAI binding for every LLM role and
embedding, with the LightRAG LLM cache disabled and no reranker. Runtime checks
reject unsupported combinations before retrieval. This deliberately narrows the
new opt-in contract, not the existing non-accounting APIs. Adding another paid
provider requires instrumentation at its actual wire boundary first.

## Durable authority contract

The authority is trusted application composition, not a user-selected callback.
It must implement:

1. `claim(query_id, grant, request_digest, workspace)`: atomically claim an already
   admitted request once. Bind the original owner, turn, fixed index manifest,
   workspace and normalized retrieval digest. Verify current authorization and
   fencing. Return an immutable quote, stable lease, deadline and call cap.
2. `reserve_call(lease_id, call_id, ...)`: durably register this actual provider
   attempt before it can be sent. Recheck current authorization, cancellation,
   deadline, model, origin and ceilings. Concurrent calls share the same escrow.
3. `settle_call(lease_id, call_id, usage)`: accept only the original attempt's
   provider usage. An absent or invalid proof retains unknown liability. This
   operation must still be possible after business access has been revoked.
4. `finish(lease_id, status)`: durably close the source's admission window and
   return its authoritative receipt. Late existing-call usage may close an
   unresolved liability; a client HTTP error is never evidence of zero usage.

The quote permits at most 16 actual calls and 16 model/origin entries. Each entry
has an input ceiling and an output ceiling (zero only for embeddings). The
authority owns monetary tariffs, the existing cumulative budget and rounding.
It must reserve the complete worst-case amount from that budget before issuing
the grant. The local module neither maintains a second allowance nor derives
prices from an end user's request. A trusted tariff must cover all applicable
provider billing dimensions; raw token counts alone are not a vendor invoice.

## Provider proof and cancellation

`account_openai_call` wraps the real Chat Completions and embeddings calls. It
requires the exact quoted model and origin, disables hidden SDK retries,
redirects and environment proxy selection, and rejects unquoted wire overrides.
`max_retries=0` alone does not exclude an HTTP pool reassigning a request
inside one SDK send. A request-local trace therefore latches the first
HTTP/1.1 or HTTP/2 request-header start and rejects a second start before
headers can be sent again. Existing trace callbacks still run and their
errors/cancellation propagate. This guard is independent of optional timing
telemetry; an uncertain first attempt retains its original call ID and
liability. It does not retry the call, infer zero usage, or authorize another
query. Without an active accounting scope, ordinary provider behavior is
unchanged.

Chat requests allow one choice only and enforce the quoted `max_tokens` limit;
request serialization is bounded before reservation. The source rechecks its
closed/failed/deadline state after reservation returns and before sending.

Strict clients share the process's verified public CA context, not credentials
or authorization. Trust-bundle rotation requires a process restart. An opted-in
host can call `prewarm_openai_accounting()` before becoming ready; it imports SDK
resources and loads trust without constructing a provider client or sending a
model request.

The trusted authority may own an `OpenAIAccountingTransport` with an explicit
connection bound and expose it as `openai_accounting_transport`. Each actual
attempt still gets a distinct HTTP/SDK client, header set and cookie jar; only
the stateless connection pool is borrowed. A pool slot is acquired before fresh
authority checks and durable reservation; scope/deadline checks run again after
the wait, and cancellation returns the slot without reserving a new call.
Environment proxies, redirects and
transport/SDK retries remain disabled. Arbitrary supplied clients/transports
remain rejected, and every actual model send still passes its own durable
reservation and usage settlement. No model response or authorization is cached.
Without this explicit host composition, clients retain isolated transports.

A trusted host may set `OpenAIAccountingTransport(keepalive_expiry=...)` within
`(0, 60]` seconds; the default remains five seconds. This only controls idle
connection retention, not connection/slot limits, query deadlines, SDK retries,
credentials, admission or accounting. It does not issue warmup or inference
requests automatically. Host readiness checks must not be mistaken for query
authorization or evidence resolving a previous unknown provider outcome.

The host must drain requests/workers before closing the owned pool, keeping its
accounting callback transport alive through that drain. Closing an individual
client does not close other queries' connections. Owner shutdown joins physical
pool closure despite cancellation; a retired pool cannot be borrowed again.

Usage comes from the actual SDK response: exact response model, nonnegative
integer prompt/completion/total counts and a consistent total. Embeddings have
no generated completion tokens. Missing usage, mismatched models, transport
errors and cancellation keep liability unknown rather than substituting token
estimates or a successful-looking zero. A known response is durably settled
before its content is returned to the retriever.

The existing priority queue propagates each work item's context into its worker
task. The request-local context therefore associates real provider attempts with
the correct query even when independent retrieval work runs concurrently. It
does not introduce another provider queue or serialize independent network
calls for the duration of their responses.

Cancellation joins owned settlement and finalization, including repeated
cancellation. Closing a query prevents fresh dispatch; already dispatched calls
retain their original stable IDs and liability. If an authority operation or
provider outcome is uncertain, the durable authority keeps the escrow until
original evidence resolves it. Restart/recovery must use that durable state;
the in-memory context is not a recovery ledger.

## Receipt and deployment boundary

An accounting `/query` returns content only after `finish` supplies a settled
receipt. Integrators must compare it with their original authority's durable
receipt, including fixed index and tariff binding; an arbitrary HTTP response
claiming `settled` is not sufficient proof. Grants are opaque, single-use and
must not appear in logs or public tool metadata. Query text and parameters are
not recorded as accounting metadata.

This source integration does not provision a budget authority, install a remote
transport, deploy the server, freeze an existing shared workspace, or establish
provider pricing. Those runtime actions require explicit trusted composition
and separate operational approval. A server without the enabled capability
must not be used by a caller requiring this contract.
