# Trace Phase 1 Audit and Minimal Contract

Status: Phase 1 complete after observability supplement; implementation has not started

Date: 2026-08-25

Scope: new Agent/Web on 8790, isolated 8896 acceptance, and 8795 feedback/cost reporting;
8788 Feishu remains outside the change boundary

## Objective and boundary

The goal is to make one user-visible operation reconstructable from ingress through routing,
model/tool work, cost, public response, and later feedback. This phase audits the current
identifiers and defines the minimum contract for that work.

This phase does not change runtime behavior, databases, public output, retrieval/reranking,
A2/A3 state transitions, 8788 Feishu, or model prompts. It also does not implement task
snapshots, checkpoints, idempotency, HTTP decoupling, or pause/resume.

Authoritative sources reviewed:

- `tiku_agent/fastapi_demo.py` and `tiku_agent/demo_web/demo.js` for ingress, public protocol,
  response history and feedback submission;
- `tiku_agent/session_runtime.py`, `tiku_agent/task_log.py` and `tiku_agent/state.py` for A2
  request/search lifecycles and task persistence;
- `tiku_agent/a3_runtime.py` for A3 workflow/unit/child-search state and page error records;
- `tiku_shared/request_protocol.py` and `tiku_shared/model_costs.py` for protocol, run and call
  schemas;
- `tiku_agent/feedback_store.py` and `tiku_admin/reporting.py` for feedback/cost joins;
- focused request-protocol, runtime, A3, cost, FastAPI and administrator tests.

The detailed log/error/feedback generation and storage matrix is maintained in
[`trace_phase1_observability_inventory.md`](trace_phase1_observability_inventory.md). The
minimal contract in this file must be read together with that verified current-state inventory.

## Decision

Add a server-authoritative `trace_id`; do not relabel the current `request_id` as the trace.

The current `request_id` is useful public correlation metadata, but it is supplied by the
browser when valid and the same field name is also used for a model provider's request ID.
It therefore cannot be the authoritative internal join key. Existing `request_id` and
`search_id` behavior remains compatible while trace support is introduced additively.

## Current identifier inventory

| Identifier | Current owner and lifetime | Current uses | Finding |
| --- | --- | --- | --- |
| HTTP `request_id` | Browser normally creates `req_<32 hex>` for one HTTP attempt; the server validates it or generates a replacement | Response protocol, `X-Request-ID`, A2 task log, streamed errors | Caller-controlled correlation ID, not an authoritative trace |
| Protocol `request_id` | A2 and most HTTP boundary paths copy the HTTP ID; pure A3 may generate another value or omit it | Public result/error and feedback payload | Not reliably equal to `X-Request-ID`; same spelling as the provider ID below |
| Model-call `request_id` | Provider response `request_id` or `id` | `model_cost_calls.request_id` | Semantically a provider request ID, not the HTTP request ID |
| `search_id` | A2 creates one for a new image search; text/candidate/answer turns retain it | Agent state, protocol, task log, cost `search_key`, feedback | Correct question-search identity, but not a request/turn identity |
| `workflow_search_id` | A3 creates one for the uploaded page and retains it across child questions | A3 state, feedback and page-cost reporting | Correct A3 parent identity; direct A2 compatibility needs an explicit rule |
| A3 `current_search_id` | Starts as the page workflow ID; an A2-routed page can replace it with a child search ID | A3 state and some error/cost paths | The name can mean parent or child depending on route and time |
| `unit_id` | Qwen/GLM page contract; unique only inside one A3 page understanding | Selection, crop, media guard and completion state | Must be joined as `(workflow_search_id, unit_id)`, never globally |
| Cost `run_id` | One `ModelCostCollector` flush | `model_cost_runs` and `model_cost_calls` | A2 often reuses HTTP `request_id`; A3 creates a fresh value with the `req_` generator |
| Cost `call_id` | One locally recorded provider call | `model_cost_calls` primary key | Stable local call identity; currently has no trace field |
| `task_id` | One completed A2/API-boundary log entry | JSONL task log primary correlation field | Usually equals HTTP `request_id`; A3 parent model stages have no equivalent task event |
| `task_revision` | Monotonic session workflow revision | Stale-action checks, media and feedback scoping | A version/concurrency field, not a trace ID |
| `candidate_generation` | One candidate-list generation | Stale candidate and media-delivery guards | A version/concurrency field, not a trace ID |
| `message_id` | Browser creates one for a rendered chat item | Feedback target and browser history | The server has no authoritative response row behind it |
| `feedback_id` | Feedback store creates one per unique identity/session/message | Feedback administration and case media | Stable feedback identity; rated response linkage is currently client-mediated |
| `session_key` | Server hashes the session ID | Logs, costs and feedback | Private join dimension; feedback hashing alone does not prove current session validity or response ownership |
| `identity_key` | Stable invitation identity | Budget, costs, feedback and administration | Private identity dimension; never log invite plaintext |

`request_id`, `search_id`, `workflow_search_id`, `run_id`, and `provider_request_id` are
different concepts even when historical rows happen to contain the same string.

## Current flow and verified gaps

### HTTP and public protocol

The browser sends `X-Request-ID`; middleware validates the exact `req_<32 hex>` format and
stores it on the request. HTTP and A2 handlers normally propagate that value into the public
protocol. Pure A3 responses are inconsistent: `_response()` creates a separate request ID and
some direct `AgentResponse.from_code` paths omit it, while the FastAPI payload adapter does not
replace it with the HTTP header value. Thus an A3 payload request ID may differ from
`X-Request-ID` or be empty. Even where propagation works, a client can deliberately reuse a
valid value, so it is not an authoritative database key.

### A2 task, tool and cost flow

A2 creates a new `search_id` for a new uploaded image and retains it for later text turns.
The runtime normally writes one structured task-log entry per turn and one cost run; an internal
exception may also produce a second API-boundary entry for the same request. The task log has
the public request/search IDs and safe protocol outcome. The cost run uses the HTTP request
ID as `run_id`, while provider calls store the provider response ID in a field also named
`request_id`.

Gap: task logs, cost runs and calls can be joined only by conventions that are not represented
in one schema. Provider failures often have no provider request ID. There is no explicit
`trace_id` on any of these records.

### A3 parent and child flow

A3 creates a stable `workflow_search_id` for the uploaded page. Page-understanding, crop and
validation calls write separate cost runs using that workflow ID as `search_key`. Each chosen
unit enters A2, which creates a child `search_id`; the public snapshot exposes the child search
ID and separately retains the workflow ID.

Gaps:

- A3 model runs create independent `run_id` values and do not retain the HTTP request ID.
- Pure A3 public responses do not reliably retain the ingress request ID; payload and header
  correlation can diverge before any trace implementation exists.
- A3 has bounded page-error records but no unified task/stage event stream.
- `current_search_id` is route-dependent and must not be the canonical parent-child field.
- `unit_id` is not persisted on model cost runs, so per-unit validation costs cannot always be
  joined without timing and task-kind inference.

### Public response and feedback

The server returns safe protocol metadata. The browser creates `message_id`, copies the
response request/search IDs into history, and later submits those values with feedback. The
feedback store validates their format. Only when `conversation` is submitted does it verify the
target is present and crop evidence to that message; omitting conversation currently bypasses
target binding.

Gaps:

- There is no server-authored `response_id` for the exact public response revision.
- Feedback correlation fields come back from the client rather than from an authoritative
  server response record.
- Administrative cost lookup joins feedback to runs using identity, search keys and a time
  cutoff. This is a reasonable compatibility fallback, not an exact trace join.

### Priority of gaps

| Priority | Gap | Why it comes first |
| --- | --- | --- |
| P0 | No server-authoritative trace root | Other joins cannot have one stable owner |
| P0 | App and provider `request_id` meanings collide | A schema migration could silently join unrelated IDs |
| P0 | A3 parent stages do not carry the ingress request | The most expensive path cannot be reconstructed exactly |
| P1 | No server response identity | Feedback cannot bind authoritatively to the rated output |
| P1 | No explicit A3 workflow/unit fields on every relevant event/run | Multi-question attribution still needs inference |
| P1 | Task log, model cost, page errors and feedback are separate contracts | Operators must manually correlate stores |
| P1 | Observability writers can fail or drop records silently | Missing evidence is currently indistinguishable from an event that never occurred |
| P1 | Feedback retention provider is not wired and cleanup runs only when 8795 starts | New cases use fixed 30-day expiry and that per-row deadline is not continuously enforced |
| P2 | Admin reporting uses search/time fallback joins | Correct enough for current summaries, insufficient for incident reconstruction |

## Minimal Trace Contract V1

### Identifiers

| Field | Format and owner | Lifetime and rule |
| --- | --- | --- |
| `trace_id` | `trace_<32 lowercase hex>`, generated only by the server | Exactly one inbound API operation, including failures before Agent admission; immutable through all synchronous child work |
| `event_id` | `evt_<32 lowercase hex>`, generated by the event writer | Exactly one structured event; unique within a runtime database |
| `request_id` | Existing `req_<32 lowercase hex>` public field | One client-visible network attempt; retained for compatibility and support, never used as the sole internal join key |
| `response_id` | `resp_<32 lowercase hex>`, generated by the server | One finalized public response payload or terminal streamed result |
| `workflow_search_id` | Existing `search_...` value owned by the parent workflow | One uploaded page; 8790 retains its A3-wrapper parent even when routed to A2, while a standalone A2 flow may set it equal to its initial `search_id` in new trace records |
| `search_id` | Existing `search_...` value owned by A2 | One question-search lifecycle; retrying the same question retains it, uploading/reselecting a new question creates a new one |
| `unit_id` | Existing page-local value | Valid only with `workflow_search_id`; the logical unit key is the pair, not a concatenated public ID |
| `run_id` | New records use `run_<32 lowercase hex>` | One model cost collector/run; historical values remain readable |
| `call_id` | Existing locally generated value | One provider call record |
| `provider_request_id` | Opaque provider value | Provider-side correlation only; replaces the semantic use of `model_cost_calls.request_id` in the next additive schema |
| `feedback_id` | Existing server-generated value | One saved feedback record |
| `rated_response_id` | New batch-2.4 `response_id`, returned to the client and supplied back with feedback | Verified by the server as the exact owned response; never inferred from latest session state |

`trace_id` is internal operational metadata in V1. Public APIs continue to expose `request_id`
and `search_id`; exposing trace IDs to users is not required to obtain reliable internal joins.

### Event envelope

Every trace event uses the following envelope. Empty optional fields are omitted or stored as
empty strings consistently; they must never be overloaded with another identifier type.

```json
{
  "schema_version": 1,
  "event_id": "evt_...",
  "trace_id": "trace_...",
  "event_type": "model_call_finished",
  "occurred_at": "2026-08-25T08:00:00+00:00",
  "stage": "a3_page_understanding",
  "outcome": "success",
  "request_id": "req_...",
  "response_id": "",
  "session_key": "...",
  "identity_key": "...",
  "workflow_search_id": "search_...",
  "search_id": "",
  "unit_id": "",
  "run_id": "run_...",
  "call_id": "...",
  "provider_request_id": "...",
  "feedback_id": "",
  "protocol": {
    "status": "SUCCESS",
    "layer": "tool",
    "code": "REQUEST_SUCCEEDED"
  },
  "duration_ms": 123,
  "safe_attributes": {}
}
```

Required fields are `schema_version`, `event_id`, `trace_id`, `event_type`, `occurred_at`,
`stage`, and `outcome`. Identifier fields are included only when the event is inside that
lifecycle. `protocol` is included only for public-boundary outcomes.

Initial event types are deliberately small:

- `request_received`
- `route_decided`
- `stage_started`
- `stage_finished`
- `model_call_started`
- `model_call_finished`
- `tool_finished`
- `cost_run_written`
- `public_response_finalized`
- `feedback_recorded`
- `request_failed`

Do not create one event type per protocol code or per model. Those belong in reviewed fields.

### Lifecycle rules

1. Middleware creates `trace_id` before authentication/admission; every terminal path records
   a final event, including login, quota, queue, upload and unhandled failures.
2. A synchronous A3-to-A2 handoff retains the same `trace_id`. A later user message gets a new
   trace but retains the applicable `workflow_search_id` and `search_id`.
3. A network retry always gets a new server `trace_id`. It may reuse the client `request_id`;
   trace uniqueness must not depend on the caller.
4. Retrying the same question retains `search_id`; a new upload or newly selected A3 unit gets
   a new child `search_id`. The A3 page retains `workflow_search_id` throughout.
5. Every model run gets its own `run_id`; every provider attempt gets a `call_id`. Provider IDs
   are stored only as `provider_request_id`.
6. The server assigns `response_id` when the final safe public payload is assembled. Feedback
   has its own request trace and binds to `rated_response_id` after ownership/session checks.
7. Trace writing is fail-open for the user request but records an observable local warning;
   trace failure must not mutate the public response or repeat model/tool work.

### Privacy and retention

The event envelope may store stable codes, timestamps, durations, counts, model/provider names,
safe stage names and hashed/private join keys. It must not store:

- API keys, Tunnel tokens, invite plaintext, administrator credentials or cookies;
- raw prompts, raw model output, full user text, full conversation history or model reasoning;
- local absolute media/config paths, stack traces or arbitrary exception messages;
- unrestricted `safe_attributes` copied from tool/model payloads.

`safe_attributes` requires an event-type-specific whitelist. A case's already stored per-record
expiry must not be extended by trace metadata. Wiring the configured retention provider and
adding periodic cleanup remain known gaps rather than properties of the current system.

## Compatibility and implementation constraints

- Introduce fields/tables additively and keep historical cost, feedback and task-log records
  readable. Do not reinterpret historical `model_cost_calls.request_id` as an app request ID.
- Keep the five-state public protocol unchanged in the trace propagation stage; separately add
  contract tests for registered reasons, production emitters and browser-local reasons/actions.
- Do not change A2/A3 routing, chapter boundaries, retrieval order, candidate ranking, answer
  delivery, budget checks, or feedback evidence content. Target ownership hardening is isolated
  to roadmap batch 2.4 and must retain old records read-only.
- 8790 and 8896 use independent trace stores under their existing runtime roots. Do not share
  runtime state with 8788, 8794 or 8795; 8795 may later receive read-only access to the new trace
  stores in addition to its current feedback/cost access.
- Trace writes are local and fail-open. No external observability service is introduced in V1.
- Do not weaken existing feedback validation. In batch 2.4, exact `rated_response_id` ownership
  verification must close the optional-conversation bypass while old records remain readable.

## Acceptance scenarios for the remaining Trace stage

These are required stage-wide tests, not optional illustrations. They are implemented in the
five ordered batches in `.agents/roadmap.md`; they are not all part of the first code batch.

1. **Direct A2 success:** one HTTP operation has one trace; task log, cost run, provider calls
   and the server-side finalized-response record carry that trace and one question `search_id`.
2. **A3 multi-question page:** the upload trace records the parent workflow and page model
   stages; selecting a unit records `(workflow_search_id, unit_id)` and a child `search_id`;
   another unit gets another child search without changing the workflow ID.
3. **Failure before Agent admission:** login/quota/queue/upload failure still has one trace,
   request ID, public protocol outcome and terminal event, with zero model/tool calls.
4. **Same-question retry:** the retry gets a new trace and response ID, retains the question
   search ID, and does not get joined to the earlier turn only because a client request ID was
   reused.
5. **Provider failure:** the failed call has a local call/run/trace even when no provider request
   ID or token usage is returned.
6. **Feedback on an older reply:** feedback has its own trace and exact rated response ID; it
   remains linked to the older response rather than the latest session snapshot.
7. **Trace writer failure:** the normal user result and model/tool call count are unchanged;
   the failure does not trigger a retry.
8. **Privacy regression:** serialized events reject secrets, arbitrary paths, raw exception
   text, raw prompts/output and unregistered safe attributes.
9. **JSON/stream parity:** an equivalent terminal failure produces the same public protocol and
   one authoritative terminal event in both response modes.
10. **Media post-processing:** the terminal event reflects the protocol actually delivered after
    media persistence, rather than the earlier A2 business result.
11. **Client-local failure:** network/timeout/invalid-response reports can be distinguished from
    a server terminal event and do not silently assert that the server did no work or incur no cost.
12. **Feedback target enforcement:** omitting conversation cannot create feedback for an
    unverified arbitrary message; the server verifies `rated_response_id` and session ownership.

## Next implementation step

Implement only roadmap batch 2.2 first: server trace creation and additive propagation across
HTTP, threads, A3, A2, tool and model-cost scopes. Reuse the V1 envelope contract but leave the
full terminal event stream, authoritative response/feedback binding and 8795 timeline to batches
2.3–2.5. Preserve the active/legacy path distinctions, writer-failure requirements and feedback
trust boundaries in the observability inventory. Do not combine this with task snapshots,
checkpoints, idempotency or pause/resume.
