# Trace Phase 1 Audit and Minimal Contract

Status: audit plus roadmap batches 2.2, 2.3 and 2.4 complete; batch 2.5 is next

Date: 2026-08-27

Scope: new Agent/Web on 8790, isolated 8896 acceptance, and 8795 feedback/cost reporting;
8788 Feishu remains outside the change boundary

## Objective and boundary

The goal is to make one user-visible operation reconstructable from ingress through routing,
model/tool work, cost, public response, and later feedback. This phase audits the current
identifiers and defines the minimum contract for that work.

`Trace` names the correlation spine, not the whole observability scope. The persisted facts also
cover structured logs, safe error classes, estimated-cost writes, feedback occurrence and the
final public outcome.

The original audit batch did not change runtime behavior. The additive 2.2, 2.3 and 2.4
implementations described below do not change user-facing business wording, retrieval/reranking,
A2/A3 state transitions, 8788 Feishu, or model prompts. Batch 2.4 additively exposes public
`response_id` metadata and hardens the feedback request contract; it does not change the search
result itself. These batches also do not implement task snapshots, checkpoints, execution
idempotency, HTTP decoupling, or pause/resume.

Authoritative sources reviewed:

- `tiku_agent/fastapi_demo.py` and `tiku_agent/demo_web/demo.js` for ingress, public protocol,
  response history and feedback submission;
- `tiku_agent/session_runtime.py`, `tiku_agent/task_log.py` and `tiku_agent/state.py` for A2
  request/search lifecycles and task persistence;
- `tiku_agent/a3_runtime.py` for A3 workflow/unit/child-search state and page error records;
- `tiku_shared/request_protocol.py` and `tiku_shared/model_costs.py` for protocol, run and call
  schemas;
- `tiku_shared/response_store.py`, `tiku_agent/feedback_store.py` and
  `tiku_admin/reporting.py` for authoritative responses, feedback binding and cost joins;
- focused request-protocol, runtime, A3, cost, FastAPI and administrator tests.

The detailed log/error/feedback generation and storage matrix is maintained in
[`trace_phase1_observability_inventory.md`](trace_phase1_observability_inventory.md). The
minimal contract in this file must be read together with that verified current-state inventory.

## Decision

Use server-authoritative `trace_id` and `response_id`; do not relabel the current `request_id`
or browser `message_id` as either authority.

The current `request_id` is useful public correlation metadata, but it is supplied by the
browser when valid and the same field name is also used for a model provider's request ID.
It therefore cannot be the authoritative internal join key. Existing `request_id` and
`search_id` behavior remains compatible while trace support is introduced additively.

## Current identifier inventory

| Identifier | Current owner and lifetime | Current uses | Finding |
| --- | --- | --- | --- |
| HTTP `request_id` | Browser normally creates `req_<32 hex>` for one HTTP attempt; the server validates it or generates a replacement | Response protocol, `X-Request-ID`, A2 task log, streamed errors | Caller-controlled correlation ID, not an authoritative trace |
| Protocol `request_id` | A2 and most HTTP boundary paths copy the HTTP ID; pure A3 may generate another value or omit it | Public result/error and feedback payload | Not reliably equal to `X-Request-ID`; same spelling as the provider ID below |
| `response_id` | Server creates `resp_<32 hex>` after final media/protocol projection and before exposing a rateable reply | JSON/stream response, browser history, `responses.sqlite3`, trace terminal and feedback binding | Authoritative identity for one finalized safe public projection; one projection per trace |
| Model-call `request_id` | Provider response `request_id` or `id` | `model_cost_calls.request_id` | Semantically a provider request ID, not the HTTP request ID |
| `search_id` | A2 creates one for a new image search; text/candidate/answer turns retain it | Agent state, protocol, task log, cost `search_key`, feedback | Correct question-search identity, but not a request/turn identity |
| `workflow_search_id` | A3 creates one for the uploaded page and retains it across child questions | A3 state, feedback and page-cost reporting | Correct A3 parent identity; direct A2 compatibility needs an explicit rule |
| A3 `current_search_id` | Starts as the page workflow ID; an A2-routed page can replace it with a child search ID | A3 state and some error/cost paths | The name can mean parent or child depending on route and time |
| `unit_id` | Qwen/GLM page contract; unique only inside one A3 page understanding | Selection, crop, media guard and completion state | Must be joined as `(workflow_search_id, unit_id)`, never globally |
| Cost `run_id` | One `ModelCostCollector` flush | `model_cost_runs` and `model_cost_calls` | A2 often reuses HTTP `request_id`; A3 creates a fresh value with the `req_` generator |
| Cost `call_id` | One locally recorded provider call | `model_cost_calls` primary key and trace event | Stable local call identity; new rows carry explicit trace/run correlation |
| `task_id` | One completed A2/API-boundary log entry | JSONL task log primary correlation field | Usually equals HTTP `request_id`; A3 parent model stages have no equivalent task event |
| `task_revision` | Monotonic session workflow revision | Stale-action checks, media and feedback scoping | A version/concurrency field, not a trace ID |
| `candidate_generation` | One candidate-list generation | Stale candidate and media-delivery guards | A version/concurrency field, not a trace ID |
| `message_id` | Browser creates one for a rendered chat item | Feedback UI target and browser history | Secondary UI identity; feedback requires its target message to carry the same server `response_id` |
| `feedback_id` | Feedback store creates one per response-bound record | Feedback administration and case media | Stable feedback identity; schema v8 stores unique `rated_response_id`, while migrated v7 rows remain unbound |
| `session_key` | Server hashes the session ID | Logs, costs and feedback | Private join dimension; feedback hashing alone does not prove current session validity or response ownership |
| `identity_key` | Stable invitation identity | Budget, costs, feedback and administration | Private identity dimension; never log invite plaintext |

`request_id`, `search_id`, `workflow_search_id`, `run_id`, and `provider_request_id` are
different concepts even when historical rows happen to contain the same string.

## Current flow and verified gaps

### HTTP and public protocol

The browser sends `X-Request-ID`; middleware validates the exact `req_<32 hex>` format and also
creates an independent server-owned `trace_id`. Existing protocol request IDs remain public
compatibility metadata and pure A3 values may still differ from the ingress header, but routing,
cost, response and feedback joins no longer depend on that equality.

### A2 task, tool and cost flow

A2 creates a new `search_id` for a new uploaded image and retains it for later turns. New task
logs, cost runs and provider calls carry the same server trace; new runs use independent
`run_...` IDs and provider values use canonical `provider_request_id` (with the historical
column retained only as a compatibility mirror). Provider failures still have a local
trace/run/call even when no provider ID or usage is returned. Physical records remain separate,
so a bounded query layer is still needed.

### A3 parent and child flow

A3 retains a stable `workflow_search_id`; selected questions receive child `search_id` values
and units are identified by `(workflow_search_id, unit_id)`. The ingress trace now propagates
through parent stages, synchronous A2 handoff, model-cost runs/calls and the structured stage
event stream. `current_search_id` remains a compatibility field with route-dependent meaning and
must not replace explicit parent/child dimensions. Historical rows without the new dimensions
still require compatibility fallbacks.

### Public response and feedback

For every rateable Agent/Web reply, the server now persists a privacy-bounded projection in the
runtime-local `responses.sqlite3` after media post-processing and before public delivery. It then
returns the generated `response_id`; the browser stores it with its own `message_id`. The record
contains protocol and lifecycle identifiers, phase/revision, bounded counts, route, response
mode and duration, but no question/reply text, prompt, path or URL. Re-finalizing the same trace
with the same projection is idempotent; a different projection is rejected.

Feedback schema v8 requires both `rated_response_id` and `conversation`. The service verifies
identity, session, response expiry, target-message presence and exact message/response ID match,
then derives protocol and parent/child lifecycle fields from the stored response rather than
client history or the latest session snapshot. Cross-user, cross-session and message/response
rebinding fail closed. Existing v7 feedback remains readable with an empty response binding and
cannot be silently upgraded to a fabricated authority.

Remaining gap: administrative cost display still joins feedback to cost runs using identity,
search keys and a time cutoff. This compatibility fallback is not the bounded diagnostic query
path planned for batch 2.5.

### Priority and status of gaps

| Status | Gap | Why it comes first |
| --- | --- | --- |
| DONE 2.2 | No server-authoritative trace root | Other joins cannot have one stable owner |
| DONE 2.2 | App and provider `request_id` meanings collide | A schema migration could silently join unrelated IDs |
| DONE 2.2 | A3 parent stages do not carry the ingress request | The most expensive path cannot be reconstructed exactly |
| DONE 2.4 | Server response identity was absent | Feedback could not bind authoritatively until this closed |
| P1 | No explicit A3 workflow/unit fields on every relevant event/run | Multi-question attribution still needs inference |
| NEXT 2.5 | Task log, model cost, page errors, response and feedback remain separate physical stores | Operators still need a bounded query layer instead of manual correlation |
| PARTIAL 2.3 | Observability writers can fail or drop records | Trace writer failures are counted, but historical sinks still have silent-loss paths |
| NEXT 2.5 | Response/feedback retention and cleanup are not fully periodic | Expiry is enforced for new feedback ownership, but expired rows/evidence still need lifecycle maintenance |
| NEXT 2.5 | Admin reporting uses search/time fallback joins | Correct enough for current summaries, insufficient for incident reconstruction |

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
| `rated_response_id` | Implemented batch-2.4 `response_id`, returned to the client and supplied back with feedback | Verified by the server as the exact owned response; never inferred from latest session state |

`trace_id` is internal operational metadata in V1. Public APIs continue to expose `request_id`
and `search_id`, and rateable replies now expose `response_id`; exposing trace IDs to users is
not required to obtain reliable internal joins.

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
5. Every model run gets its own `run_id`; every provider attempt gets a `call_id`. New code reads
   provider IDs from `provider_request_id`; the deprecated `request_id` column may mirror the
   same provider value during the additive compatibility window and is never an app request ID.
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
  delivery, budget checks, or feedback evidence content. Batch 2.4 isolated target-ownership
  hardening from those behaviors and retains old records read-only.
- 8790 and 8896 use independent trace and response stores under their existing runtime roots.
  Do not share runtime state with 8788, 8794 or 8795. The primary consumer is a stable local
  diagnostic query layer for Codex/Agent; 8795 may optionally receive read-only access as a
  temporary UI adapter, but it is not the trace/response owner or a runtime dependency.
- Trace writes are local and fail-open. No external observability service is introduced in V1.
- Do not weaken feedback validation. Batch 2.4 closed the optional-conversation bypass with
  exact `rated_response_id`, identity/session ownership and target-message equality checks while
  keeping old unbound records readable.

## Acceptance scenarios for the Trace stage

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

## Implementation status

Roadmap batch 2.2 is complete: middleware creates one server-owned trace per inbound operation;
explicit stream and thread propagation retains it through A3, A2, tools, task logs and model-cost
runs/calls. New cost records use independent `run_...` IDs and canonical `provider_request_id`;
the historical provider-valued `request_id` remains a compatibility mirror and never carries
the app request ID.

Roadmap batch 2.3 is complete. `tiku_shared/trace_events.py` provides a strict privacy-bounded
event envelope, one-terminal-per-trace SQLite store, request-owned context session and a bounded
non-blocking writer with fail-open health counters. Middleware records ingress before
authentication; JSON, stream, rejection, exception, cancellation and media post-processing
paths write the public result actually delivered. A2/A3 routing and stages, model calls, tool
results, committed cost runs and successful
feedback persistence emit joined events without storing user/model text, paths or exception
messages. The 8790, 8896 and demo launchers each own a store under their existing runtime root;
none depends on 8795.

Roadmap batch 2.4 is complete. `tiku_shared/response_store.py` persists one server-authored,
privacy-bounded response projection per trace in runtime-local `responses.sqlite3`. JSON and
stream results, A3 parent replies, A2 child replies and targetable server errors receive
`resp_<32 hex>` only after their final safe protocol/media projection is known. The response row
is joined to the terminal trace event, and identical re-finalization is idempotent while a
conflicting projection fails closed. Stream persistence runs outside the event loop; disconnects
before result exposure roll back an in-flight write or remove the still-private committed row.

Feedback schema v8 requires `rated_response_id`; `/api/feedback` also requires conversation and
an exact target `message_id`/`response_id` match. The server validates identity, session and
expiry against the response row and copies protocol/lifecycle facts from that row. Feedback
updates cannot rebind either response or message, deletes use response ownership, and old v7
rows remain readable but intentionally unbound. The stored response projection excludes user
and assistant text, prompts, local paths and URLs.

Bounded Codex/Agent diagnostic queries, periodic retention maintenance and old/new cutover are
now batch 2.5 (`NEXT`). 8795 remains only an optional read-only adapter, never the trace/response
data owner or a runtime dependency. Task snapshots, checkpoints, execution idempotency and
pause/resume stay outside this stage.
