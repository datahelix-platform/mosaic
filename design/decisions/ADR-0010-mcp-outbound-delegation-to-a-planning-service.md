# ADR-0010: The MCP boundary may delegate outbound to a planning service, as an untrusted planner behind a validating relay

- **Status:** Proposed
- **Date:** 2026-09-08
- **Deciders:** labadorf (review of [#199](https://github.com/datahelix-platform/mosaic/pull/199)); clandaverde (implementation)
- **Related:** ADR-0009 (MCP boundary, capability manifest, QuerySpec — this ADR extends its
  surface with the one tool that is not a `MosaicClient` wrapper); **Aperture ADR-0032**
  (rejects a dedicated control-plane service — the constraint that forces the relay here) and
  **ADR-0035** (`QuerySpec`); [sec8](../sec8_auth_integration.md) (auth integration — inbound
  only, see Consequences); issue [#54](https://github.com/datahelix-platform/mosaic/issues/54)
  Part A (authn/authz, referenced not resolved); `mosaic-demo-small`'s
  `openspec/changes/add-exon-conversational-contract/design.md` Decision 8 (the wire
  contract, owned by that repo).
- **Tracking issue:** [#186](https://github.com/datahelix-platform/mosaic/issues/186)
  (`converse_query_spec`), implemented in [#199](https://github.com/datahelix-platform/mosaic/pull/199).

## Context

Aperture's chat interface needs to build a `QuerySpec` collaboratively across conversational
turns rather than in one shot. That requires an LLM call, and three prior decisions close off
every place it could happen except one:

- **Aperture has no backend of its own.** Its `ScopedDataClient` is a thin GraphQL
  pass-through, so a browser-side call would mean shipping model credentials to the browser.
- **Aperture ADR-0032 explicitly rejected a dedicated control-plane service**, so there is no
  third component to put it in.
- **ADR-0009 put an MCP boundary in Mosaic**, but scoped it entirely to local capability:
  every tool it defines is a thin wrapper over a `MosaicClient` method, and its item 5 fixes
  the surface as read-only with no write or mutation path.

The turn-taking planner itself (`mosaic-demo-small`'s Exon) holds the model credentials and
must keep holding them — importing it into Mosaic would drag `litellm` and a provider-credential
surface into `datahelix-mosaic`, which is meant to stay a generic LinkML runtime.

That leaves exactly one shape: **Mosaic relays**. Aperture → Mosaic (MCP) → Exon (HTTP).

This is a change in Mosaic's deployment shape, not just a new tool. Every transport Mosaic has
today is inbound-only: it answers requests and touches nothing but its own storage. A tool that
makes an outbound HTTP request to a third-party service when configured to is the first of its
kind here, and the question this ADR settles is **on what terms that is allowed** — what Mosaic
must guarantee about a planner's output, what it must not disclose, and what it does not yet
account for.

Getting this wrong has two distinct failure modes worth naming, because they are not the same
risk:

1. **A planner's output reaching a caller unvalidated.** Exon generates a `QuerySpec` with an
   LLM. If Mosaic passes that through on Exon's word alone, ADR-0009's central guarantee —
   "validation is specific, actionable, and happens before anything executes" — becomes only as
   strong as a service in another repository, on another release cycle.
2. **A relay being cheaper to abuse than the thing it relays.** A Mosaic query costs CPU. A
   relayed conversational turn costs a hosted model invocation, i.e. money, at a third party.

## Decision

**Mosaic may host an MCP tool that delegates outbound to a configured planning service, on
these terms.** The terms are the decision; `converse_query_spec` (#186/#199) is its first and
currently only instance.

1. **Configured-or-absent.** The tool is registered only when its endpoint env var
   (`MOSAIC_EXON_URL`) is set and non-blank, and the server `instructions` text describes it
   only under the same condition. An unconfigured deployment does not advertise a delegate it
   cannot reach — mirroring how `--mcp`/`MOSAIC_SERVE_MCP` already gates this whole module. An
   optional integration is *absent*, never present-and-broken.

2. **The planner is untrusted; Mosaic re-validates in-process.** **Every** `QuerySpec` in the
   response — not only the turn just taken — is re-parsed and re-validated against this
   deployment's own capability manifest, via a direct in-process `validate_query_spec` call,
   never a self-call back through MCP, before any of it is handed to a caller. A spec the planner
   labelled a `proposal` that this deployment's validator rejects is returned as an `error`, not
   a proposal. This is defence in depth, not redundancy: it holds even if the planner's own check
   is stale, buggy, or bypassed, and it keeps ADR-0009's guarantee a property of Mosaic rather
   than of another repo's release cycle.

   "Every" is load-bearing, and was widened by [#200](https://github.com/datahelix-platform/mosaic/pull/200)
   rather than assumed here: editing an earlier turn makes the planner *recompute* the turns that
   follow it, so a response can carry freshly generated specs this deployment has never seen.
   Validating only the turn just taken would let those reach the caller unchecked — precisely the
   hole this term exists to close. Any future field that can carry a `QuerySpec` inherits the
   same rule.

3. **The response shape is validated strictly, not duck-typed.** An unrecognized turn status, a
   missing `message`, a `proposal` with no spec, a non-`proposal` turn carrying one, a turn
   missing the `id`/`utterance` a caller needs to rewind or edit — each fails at this boundary
   with the reason named, and applies per-turn to every turn in the response, not just the one
   just taken. Mosaic is the only thing between an out-of-contract planning service and a UI; a
   silent contract break is far harder to diagnose downstream than here.

   A corollary, from #200: where a response field carries authoritative state a caller is told to
   adopt, **absent and empty must stay distinguishable end to end**. An omitted conversation means
   "nothing was applied, keep your own"; an empty one means "the conversation is now empty". A
   relay that coerces the former into the latter tells a compliant caller to discard state the
   planner never touched — so absent stays absent, on success and failure paths alike.

4. **Every failure is a structured `error` turn, never a bare tool exception.** Unreachable,
   timed out, non-JSON body, the planner's own 4xx, or still-invalid-after-revalidation all come
   back inside the same discriminated envelope the caller already branches on, with
   `query_spec: null` and no `id` (an error is not a conversational step that can be rewound —
   there is nothing to recompute). A chat UI has no structured way to render an exception beside
   the turns it has already drawn.

5. **Client-visible failures name the *role*, not the *address*.** An `error` turn says "the
   configured conversational planning service" and may carry the HTTP status class, but never
   the endpoint URL, host, port, or the planner's response body verbatim. The full URL and the
   upstream detail go to a server-side log line for operators instead. Rationale: per ADR-0009
   this surface carries no authn/authz, so anything on an `error` turn is effectively public,
   and the endpoint address is deployment topology. Operators keep their diagnostics; callers
   get a stable, non-leaking classification.

6. **It never executes.** A validated `proposal` is handed back for a separate, explicit
   `execute_query_spec` call once a human confirms it. ADR-0009's "the LLM never decides to
   execute" holds for this tool too, and the read-only constraint of item 5 there is unchanged:
   no write or mutation path is added here.

7. **The wire contract is not Mosaic's to change.** The request/response shape is Decision 8 of
   `mosaic-demo-small`'s `add-exon-conversational-contract/design.md`. Mosaic implements it
   faithfully and does not add, rename, or repurpose fields unilaterally — including fields
   Mosaic would find useful (see Consequences on actor propagation). A change to the shape is an
   amendment there first, then here.

## Consequences

- **Mosaic makes an egress HTTP request when configured to.** This is a deployment-shape change:
  operators must now reason about outbound reachability, an outbound timeout
  (`MOSAIC_EXON_TIMEOUT`, default 60s — sized for hosted-model latency so a hung turn fails
  rather than hangs), and the availability of a service Mosaic does not own. With the env var
  unset, Mosaic behaves exactly as before.

- **No new dependency.** `httpx2` arrives with `mcp>=2.1.1` (the MCP SDK's own HTTP client), so
  the `mcp` extra is unchanged. Verified: `mcp` 2.1.1 and 2.2.0 both declare `httpx2>=2.5.0`.
  It is imported inside the tool, so the rest of the server keeps no import-time coupling to an
  HTTP client it never uses.

- **The egress call carries no credential and no actor identity.** Authn/authz remains issue #54
  Part A. The deployment assumption that makes this acceptable — the planning service reachable
  only from a trusted network segment, and Mosaic's MCP surface not publicly exposed while the
  delegate is configured — is **inherited from ADR-0009's existing no-authn posture, not
  resolved here**.

- **Unauthenticated cost amplification is a NEW exposure, not one inherited from ADR-0009.**
  ADR-0009 accepted that the MCP surface has "the same trust boundary (or lack thereof) as
  REST/GraphQL today". That equivalence does not extend to this tool: a REST query costs
  Mosaic's own CPU, whereas a relayed turn spends a hosted model invocation at a third party.
  Anyone who can reach a configured Mosaic can therefore cause spend, with no attribution and no
  quota anywhere in the path. This is stated explicitly so it is not filed under "same as REST".

- **Per-actor accounting across an egress hop is unspecified.** [sec8](../sec8_auth_integration.md)
  settles the *inbound* story — Bridge validates credentials and injects `X-DataHelix-Actor`/
  `-Roles`/`-Projects`; Mosaic trusts them and never holds signing keys (§8.1, §8.3); the audit
  trail is deliberately split, with Bridge logging auth lifecycle and denials and Mosaic's
  provenance logging data mutations, correlated by `request_id` (§8.6). sec8 contains no
  counterpart for Mosaic acting as a *client* to a downstream paid service: it mentions neither
  egress, quota, nor rate limiting, and predates the MCP transport entirely. Consequently Bridge,
  once implemented, will know *who called Mosaic* but nothing will know *what Mosaic spent on
  whose behalf*. **The intended shape is §8.6's two-log correlation extended one hop** — Bridge's
  audit log for who called, the planning service's own log for what it cost, joined on a
  correlation id — and the missing piece is that Mosaic currently drops the actor at the tool
  boundary.

- **Actor propagation is mechanically available but deliberately not implemented here.** The
  actor does reach this boundary (the auth middleware is in the request path for the `/mcp/`
  mount, and contextvars propagate into the worker thread where sync MCP tools run, so
  `get_current_actor()` would work inside the tool), and the surface being read-only means
  there are no provenance writes to carry it. Adding it to the outbound payload would require a
  new field in a wire contract this repo does not own (Decision 7 above), so it is an amendment
  in `mosaic-demo-small` first. Until Bridge verifies identity, any actor Mosaic forwarded would
  be unverified, caller-supplied provenance — and per ADR-0009 item 5 it must never be presented
  as authentication.

- **A validating relay is now a pattern with terms.** Any future outbound delegate on this
  surface (a Reel engine, another planner) inherits items 1–7 rather than renegotiating them.

- **CI now exercises this module.** `tests/mcp/` was skipped on every CI run — the package
  skips itself via `pytest.importorskip("mcp")` and no job installed that extra — so the MCP
  transport had no CI coverage at all. The SQLite job now installs `[dev,graphql,mcp]`. This
  mattered more for this tool than its predecessors: it is the first to depend on a third-party
  HTTP client's exception hierarchy.

## Alternatives considered

- **Import Exon into Mosaic (in-process planner).** Rejected: drags `litellm` and a
  provider-credential surface into `datahelix-mosaic`, which is meant to run *any* LinkML schema
  and hold no model credentials. It would also couple Mosaic's release cycle to a demo repo's.

- **Let Aperture call Exon directly.** Rejected: Aperture is a browser app with no backend, so
  this means model credentials in the browser. This is the constraint that produced the issue.

- **A dedicated control-plane service between them.** Rejected upstream by Aperture ADR-0032;
  re-proposing it here would relitigate a settled decision in another component.

- **Trust the planner's own validation and pass proposals straight through.** Rejected: makes
  ADR-0009's validation guarantee a property of another repository's release cycle. The
  in-process re-check costs one function call against an already-loaded manifest.

- **Raise MCP tool exceptions on failure instead of `error` turns.** Rejected: a chat UI must
  render a failure beside the turns it has already drawn, which needs the failure inside the
  same discriminated envelope. Verified in #199: with the planner killed, the call returns
  `is_error: False` at the protocol level plus a structured `error` turn.

- **Register the tool unconditionally and fail at call time when unconfigured.** Rejected:
  advertising a tool in `list_tools` that cannot work sends every client — human or LLM — down a
  path that always fails. Absent is a better contract than broken.

- **Keep the endpoint URL and upstream body in client-visible error messages** (as first
  implemented in #199). Rejected per item 5: on a surface with no authn, that publishes
  deployment topology and proxies an external service's raw text to any caller. The cost is
  real — the planner's own 4xx names what disagreed, which is actionable — so the status class
  is kept in the client message and the detail goes to the operator log rather than being
  discarded.

- **Add an outbound bearer token (`MOSAIC_EXON_TOKEN`) in this change.** Rejected *for now*, on
  ordering grounds rather than merit: a credential is only meaningful once the planning service
  validates it, so the requirement belongs at that service's ingress first (symmetric with sec8's
  own philosophy, where the component owning the resource validates the caller). A header nobody
  checks closes nothing while adding a credential to handle, redact, and test. See the Exon-side
  follow-ups below.

## Exon-side follow-ups (not Mosaic's to implement)

Recorded here so they can be picked up in `mosaic-demo-small` later. Nothing in this ADR depends
on them; each makes a Consequence above less sharp.

| # | Change | Why it is Exon's |
|---|---|---|
| E1 | **Authenticate the conversational turn endpoint** (require a shared secret or a Bridge-issued token, reject unauthenticated callers). Then, and only then, Mosaic sends it — a small additive change here (`MOSAIC_EXON_TOKEN`). | The service owning the paid resource validates its callers. A token Mosaic sends that Exon does not check closes nothing. **Assumption not verified:** whether that endpoint has any auth today was not checked — `mosaic-demo-small` was out of scope for the session that produced this ADR. Confirm before treating E1 as outstanding. |
| E2 | **Amend Decision 8 to carry a caller identity and/or correlation id** on the request (e.g. `actor`, `request_id`), so a turn can be attributed. | The wire contract is Decision 8's, and Mosaic must not add fields to it unilaterally (Decision 7 above). Mosaic can populate it the day the contract has the field. |
| E3 | **Log per-turn cost/usage against that identity**, so the two-log correlation in sec8 §8.6 extends one hop. | Only Exon sees the model invocation and its cost. |
| E4 | **Rate limiting / quota per caller** at the endpoint. | Addresses the cost-amplification Consequence at the point where spend actually happens; Mosaic cannot bound another service's spend. |

## Notes / open sub-questions

- **Status is `Proposed`.** The implementation (#199) is complete and verified end to end, but
  the terms above — particularly items 5 and 7, and the cost-amplification Consequence — were
  settled during review of that PR and have not been ratified in a design session.
- Whether a future outbound delegate should share one env-var/timeout/logging convention (a
  small `mosaic.mcp.delegate` helper) or keep per-tool config, once there is a second one. One
  instance is not yet a pattern worth abstracting.
- Whether Mosaic should bound relay concurrency. Sync MCP tools run on the SDK's worker-thread
  pool (`anyio.to_thread.run_sync`, default 40 slots), so a burst of slow turns is bounded but
  shares that pool with every other tool on the surface. Not a problem at MVP scale; revisit
  before a deployed multi-user chat.
- Actor propagation (E2 above) needs the sec8 vocabulary settled first: if it is carried as a
  header rather than a payload field, `X-DataHelix-Actor` is the name sec8 §8.3 already uses,
  and reusing it would make the relay slot into Bridge's model without a second convention.
