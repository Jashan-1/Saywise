# SaySo — Technical Design

> A real-time decision and human-in-the-loop API for AI agents. Apply business rules, authorize exceptions, escalate to humans via SMS, and return safe decisions before the conversation ends.

Status: v0.2 design (after design review) · Owner: Jashan Singh · License: MIT

---

## 0. Decision log (v0.2)

| # | Decision | Choice |
|---|---|---|
| D1 | Tenant model | **Agency → business → location**; settings, rules and approvers inherit downward |
| D2 | Who edits rules | Agencies via API/JSON; **owners via SMS** within their own scope, after confirming a read-back |
| D3 | Timeout outcome | Configurable, but only **`defer` or `decline`**. A timeout never approves (fixed floor) |
| D4 | LLM role | Summarize for approvers, **recommend only** (a human or a rule always decides), draft rules from text |
| D5 | Approver scope | Assign at **any level**; inherited by every location below |
| D6 | SMS sender | **One shared SaySo number** for the pilot; per-agency numbers later |
| D7 | Decision types | **Starter templates + custom**: agencies clone and edit templates, or create their own |
| D8 | Escalation control | **A few client knobs**: deadline, max people asked, quiet hours, after-hours behavior. SaySo auto-tunes the rest |
| D9 | Customer context | **Caller sends it**, plus SaySo's own decision history. No CRM connectors in v1 |
| D10 | Pricing | Undecided, so **record usage now** (decisions, SMS asks, AI tokens) and decide after the pilot |
| D11 | LLM provider | One provider behind a thin adapter |
| D12 | After hours | **Per decision type**: defer immediately or ask the on-call person |
| D13 | Language | English now. `locale` setting exists, so per-location language comes later |
| D14 | Retention | Call context scrubbed after **90 days, configurable per scope** |
| D15 | API versioning | **Every endpoint is under `/v1`**. Only the infrastructure probes `/healthz` and `/readyz` stay unversioned, by convention |
| D16 | Late answers | Accepted for **24 h after expiry** (internal constant `LATE_ANSWER_WINDOW`). A reply to a decided decision gets "Handled by X"; a reply past the window or to a cancelled one gets "That request has closed." and never sets `late_outcome` |
| D17 | Callback signing | **One secret per agency**, derived as `HMAC(SAYSO_SIGNING_SECRET, agency_id + ":" + callback_secret_version)`, so one agency can rotate alone via `POST /v1/callback_secret/rotate` |
| D18 | Contact hours | `contact_hours` (scope setting and membership column) is when people **may** be texted. Default every day `07:00-21:00` in the decision's location timezone; a window may cross midnight and belongs to the day it starts; `null` = any time. Replaces `quiet_hours` / `on_hours` |
| D19 | API key management | `POST/GET /v1/api_keys` and `POST /v1/api_keys/{id}/revoke`, agency keys only. A key can't revoke itself |
| D20 | Decision `say` and `reason` | Stored on `decisions`. `reason` is one of `rule_matched`, `policy_default`, `no_policy`, `human`, `timeout`, `after_hours`, `no_eligible_approver`, `cancelled`. Retention scrubs `say` together with `context` |

## 1. Problem

AI voice and chat agents stall on judgment calls outside their script ("can you do $50 less?"). Today they either say "let me get back to you" and the lead goes cold, or they get too much freedom and over-commit. Existing human-in-the-loop tools are async approval inboxes: too slow for a live call, they need an app install, and they send every request to every reviewer.

SaySo gives an agent **one endpoint** that returns a decision within a deadline:

1. **Policy**: the client's rules decide instantly when they cover the case.
2. **Human**: otherwise, the right approver gets an SMS with an AI summary and recommendation, and answers with one tap or a digit.
3. **Safe fallback**: if nobody answers in time, the agent defers or declines. It never over-commits, and the late answer is delivered by callback.

## 2. Design principles

| Principle | Consequence |
|---|---|
| **Fixed safety floor, configurable everything above it** | Clients tune rules, approvers, timing, wording and AI features. They can't make a timeout approve, disable tenant isolation, or skip signature checks. |
| **Humans and rules decide; AI advises** | The LLM writes summaries, recommendations and draft rules. Its output never resolves a decision. Every outcome can be explained by a rule version or a named person. |
| **One stateful dependency** | PostgreSQL only. Waiting, queueing and scheduling use `LISTEN/NOTIFY` and `FOR UPDATE SKIP LOCKED`. No Redis or Celery until load proves it's needed. |
| **AI degrades gracefully** | If the LLM is slow or down, SMS goes out with a template summary and no recommendation. Decisions never wait on AI. |
| **Platform-agnostic** | REST is the contract. MCP and GoHighLevel are thin adapters over it. |
| **Never message end customers** | SaySo texts only the client's own staff. Customer follow-up goes through the caller's callback, so consent stays with the platform that owns it. |
| **Boring stack** | FastAPI, SQLAlchemy 2 (async), Alembic, Pydantic v2, httpx, pytest, uv. |

## 3. System overview

```
 AI agent (GHL Voice AI / Retell / Vapi / n8n / custom)            MCP clients
        │  REST /v1 (scoped API key)                                    │
        ▼                                                               ▼
 ┌────────────────────────────── backend (FastAPI) ─────────────┐   mcp/ (REST client)
 │ /v1/decisions ─► services/decisions ─► settings resolver      │
 │        ▲                │              ─► policy_engine       │
 │        │ long-poll      ▼                                     │
 │   waiter ◄── NOTIFY ── escalation ─► ai (summary+recommend) ──┼──► LLM provider
 │        ▲                 │                                    │
 │        │                 └──► channels/twilio_sms ────────────┼──► approvers (SMS)
 │ /v1/inbound/twilio/sms ◄── replies, owner rule commands ──────┼──◄
 │ /v1/approve/{token}   ◄── one-tap approval page ──────────────┼──◄
 └──────────────┬───────────────────────────────────────────────┘
                │ SQLAlchemy (asyncpg)
          ┌─────▼─────┐        ┌───────────────────────────────────────┐
          │ PostgreSQL│◄──────►│ worker: escalation steps, expiry,     │── callbacks ──► caller
          └───────────┘        │ callbacks, suggestions, retention     │
                               └───────────────────────────────────────┘
```

There are two processes from one codebase: `api` (uvicorn) and `worker` (`python -m app.worker`). Both scale horizontally.

## 4. Repository layout

```
sayso/
├── backend/
│   ├── app/
│   │   ├── main.py                 # app factory, lifespan (DB, waiter listener)
│   │   ├── worker.py               # escalation/expiry/callback/retention loop
│   │   ├── core/
│   │   │   ├── config.py           # pydantic-settings (process env)
│   │   │   ├── db.py               # async engine, session dependency
│   │   │   ├── security.py         # API-key hashing, HMAC tokens, signatures
│   │   │   └── logging.py          # JSON logs, request IDs, PII redaction
│   │   ├── api/
│   │   │   ├── deps.py             # auth → Principal(scope), session, idempotency
│   │   │   └── v1/
│   │   │       ├── router.py
│   │   │       ├── decisions.py
│   │   │       ├── orgs.py         # agencies, businesses, locations
│   │   │       ├── people.py       # people + memberships (roles, authority)
│   │   │       ├── settings.py     # per-scope overrides + resolved view
│   │   │       ├── policies.py     # decision types, templates, versions
│   │   │       ├── suggestions.py
│   │   │       ├── usage.py
│   │   │       └── inbound.py      # Twilio webhook + approval page
│   │   ├── models.py               # all ORM models (split past ~400 lines)
│   │   ├── schemas.py              # all Pydantic I/O models
│   │   ├── services/
│   │   │   ├── scopes.py           # scope chain + permission checks
│   │   │   ├── settings.py         # layered settings resolver
│   │   │   ├── decisions.py        # create / resolve orchestration
│   │   │   ├── policy_engine.py    # pure: evaluate rules → verdict
│   │   │   ├── escalation.py       # pure planner + advance()
│   │   │   ├── history.py          # customer facts + approver stats
│   │   │   ├── sms_commands.py     # reply parser + owner rule commands
│   │   │   ├── ai.py               # LLM adapter: summarize, recommend, draft_rule
│   │   │   ├── waiter.py           # one LISTEN conn → asyncio futures
│   │   │   ├── callbacks.py        # signed outbound webhooks w/ retry
│   │   │   ├── suggestions.py      # rule suggestions from history
│   │   │   └── retention.py        # context scrubbing
│   │   ├── channels/
│   │   │   ├── base.py             # Channel protocol
│   │   │   └── twilio_sms.py
│   │   └── templates/
│   │       └── decision_types/     # starter templates (JSON), seeded at system scope
│   ├── migrations/                 # Alembic
│   ├── scripts/create_agency.py    # bootstrap agency + API key
│   ├── tests/
│   ├── alembic.ini
│   ├── pyproject.toml
│   └── Dockerfile
├── mcp/                            # separate package, REST-only client
│   ├── sayso_mcp/server.py
│   ├── tests/
│   ├── pyproject.toml
│   └── README.md
├── docs/
│   ├── ARCHITECTURE.md
│   └── integrations/gohighlevel.md
├── examples/
├── docker-compose.yml
├── .env.example
├── .github/workflows/ci.yml
├── CLAUDE.md
├── LICENSE
└── README.md
```

## 5. Tenancy, people and permissions (D1, D2, D5)

### 5.1 Scopes

```
system (SaySo defaults + starter templates, read-only)
  └── agency
        └── business            (e.g. a dealer group)
              └── location      (e.g. one rooftop: where decisions happen)
```

- Every decision belongs to a **location**. `scopes.chain(location_id)` returns `[location, business, agency, system]`, and every inherited lookup walks it in that order.
- Every customer-data row carries `agency_id` (the isolation boundary) and `location_id`.

### 5.2 API keys

An API key is issued at agency, business or location scope. A key can act on its own scope and anything below it.

- A request that creates a decision with a business- or agency-scoped key must pass `location_id`.
- With a location key, `location_id` is implied.
- Any id outside the key's subtree returns `404`.

### 5.3 People and memberships

- `people` is global and keyed by phone number, because the shared SMS number means one person can work for two agencies.
- `memberships` attaches a person to a scope with a role and, for approvers, an authority limit.
- The display name lives on the membership, so agency A never sees what agency B called that person.

| Role | Approves (≤ authority) | Edits rules by SMS | Manages people | Notes |
|---|---|---|---|---|
| `agency_admin` | ✓ | ✓ (agency subtree) | ✓ | Usually the agency owner |
| `owner` | ✓ | ✓ (own scope) | ✓ (own scope) | Dealer principal / GM |
| `manager` | ✓ | — | — | Desk / service manager |
| `approver` | ✓ | — | — | Anyone allowed to say yes or no |

- Membership fields: `role`, `authority_limit`, `can_approve`, `on_call` (bool), `contact_hours` (weekly schedule of when they may be texted; null inherits the scope's `contact_hours`), `max_asks_per_hour`, `active`.
- **Inheritance:** a business-level manager is eligible for every location under that business.
- Permissions are checked in one place, `scopes.can(person|key, action, scope)`, with a table-driven test per role.

## 6. Layered settings (D3, D8, D12, D13, D14)

### 6.1 Resolution order (nearest wins)

```
code defaults → agency.settings → business.settings → location.settings
             → policy.overrides (per decision type) → request (tighten-only)
```

- Each scope row has a `settings jsonb` column holding **partial** overrides.
- `PATCH` sets keys, and `null` is a real value (`contact_hours: null` means any time, `two_person_over: null` means off). `PUT` replaces a scope's overrides, which is how an override is removed so the value inherits again.
- `settings.resolve(location_id, decision_type, request)` deep-merges the layers, then validates the result against one strict Pydantic model.
- A bad override is rejected at write time, never discovered at runtime.

### 6.2 Client knobs

| Key | Default | Floor / bounds |
|---|---|---|
| `deadlines.live_ms` | 30000 | 5 000 – 120 000 |
| `deadlines.async_ms` | 3600000 | 60 000 – 7 days |
| `escalation.max_fanout` | 3 | 1 – 10 |
| `escalation.two_person_over` | null (off) | ≥ 0 |
| `contact_hours` | every day `07:00-21:00` | When people may be texted, for members without their own. Location timezone; a window may cross midnight and belongs to the day it starts; `null` = any time (D18) |
| `after_hours` | `"defer"` | `"defer"` \| `"on_call"` |
| `on_timeout.outcome` | `"defer"` | **`"defer"` \| `"decline"` only** (fixed floor) |
| `on_timeout.say` / `hold_say` | English templates | ≤ 300 chars |
| `ai.summaries` / `ai.recommendations` / `ai.rule_drafting` | true / true / true | Any scope may disable |
| `retention.context_days` | 90 | 1 – 3650 |
| `locale` | `"en"` | `"en"` only in v1 (D13) |
| `callback_url` | null | HTTPS, SSRF-checked |

Internal values stay out of client settings: answer-probability targets, cold-start timing, and the AI latency budget. They are code constants or process env, auto-tuned by SaySo (§9).

**Tighten-only request overrides:** a request may lower `deadline_ms` or `max_fanout`. It can never raise them or change `on_timeout`.

`GET /v1/settings/resolved?location_id=&decision_type=` returns the merged result **with the origin of each value**, so "why did it wait 30 seconds?" has an answer.

## 7. Decision lifecycle

### 7.1 Live call (sync with long-poll)

```
agent ── POST /v1/decisions {mode:"live", wait_ms: 8000} ──► api
api: auth → resolve location → idempotency check → resolve settings + policy
api: facts = request + history.customer_facts() → policy_engine.evaluate()
  ├─ approve/decline/counter ─► persist (decided_by=policy) ─► 200 decided
  └─ escalate ─► persist pending (deadline_at, next_action_at=now)
                 escalation.advance()   # AI summary (≤ 1.5 s budget) + first SMS, inline
                 waiter.wait(id, wait_ms)
                   ├─ answered ─► 200 decided (decided_by=human)
                   └─ elapsed  ─► 200 pending {say: hold_say, poll_after_ms}
agent ── GET /v1/decisions/{id}?wait_ms=8000 ──► repeat until decided/expired
worker: deadline passed ─► expired, outcome = on_timeout ─► NOTIFY + callback
late human answer ─► late_outcome ─► callback "decision.late_answer"
```

### 7.2 Async

`mode:"async"` returns `202 pending` immediately. The result goes to `callback_url` and can also be polled.

### 7.3 States

```
pending ──► decided | expired | cancelled
expired ──► expired + late_outcome     (only within LATE_ANSWER_WINDOW = 24 h of expiry, D16)
```

- A reply to a **decided** decision gets "Handled by X ✓".
- A reply to an **expired** decision past the window, or to a **cancelled** one, gets "That request has closed." and never sets `late_outcome`.

Every transition is one `UPDATE ... WHERE status='pending' RETURNING`, so the first valid answer wins and duplicates are harmless.

## 8. Decision types and policies (D7)

### 8.1 Templates

Starter templates ship as JSON under `app/templates/decision_types/` and are seeded at **system scope**, for example `price_adjustment`, `booking_exception`, `trade_in_offer`, `refund`.

- `POST /v1/policies/{type}/clone?to_scope=` copies a template or a parent's policy down to a scope for editing.
- Agencies can also create brand-new types.
- **Policy lookup walks the scope chain:** the nearest active policy for `(type)` wins, falling back to the system template. A type with no policy anywhere escalates with `reason: "no_policy"`.

### 8.2 Policy body

```json
{
  "decision_type": "price_adjustment",
  "label": "Price adjustment",
  "amount_label": "discount ($)",
  "rules": [
    { "id": "r1", "when": { "all": [ { "var": "amount", "op": "<=", "value": 50 } ] },
      "then": "approve", "say": "Good news, we can do {context.requested}." },
    { "id": "r2", "when": { "all": [ { "var": "history.approved_count", "op": ">=", "value": 3 },
                                    { "var": "amount", "op": "<=", "value": 75 } ] },
      "then": "approve", "say": "Since you're a repeat customer, we can do that." },
    { "id": "r3", "when": { "all": [ { "var": "amount", "op": ">", "value": 150 } ] },
      "then": "decline", "say": "The best I can do is {context.quoted}." }
  ],
  "default": "escalate",
  "overrides": { "deadlines": { "live_ms": 25000 }, "after_hours": "on_call" }
}
```

**Engine rules**

- First matching rule wins; otherwise `default` applies. Outcomes are `approve`, `decline`, `counter` (with `value`) and `escalate`.
- Conditions support `all`, `any`, `not` and the operators `== != < <= > >= in not_in exists`. `var` paths are `amount`, `type`, `context.*` and `history.*`. A missing path is false and never raises.
- `evaluate(policy, facts) -> Verdict` is a pure function of about 60 lines, with no `eval` and full table-driven tests.
- `say` templates are `{path}` substitution only.
- Each PUT creates a new immutable version. Decisions record `policy_id@version` and the matched `rule_id`.

## 9. Escalation (D5, D8, D12)

### 9.1 Eligibility

Approvers are drawn from memberships on the location's scope chain: active, `can_approve`, `authority_limit >= amount`, under their hourly load cap, not opted out of SMS, and inside their `contact_hours`, falling back to the scope's `contact_hours` (D18).

**If nobody is inside their contact hours,** `after_hours` decides:

- `defer`: expire immediately with the `on_timeout` outcome.
- `on_call`: ask members with `on_call=true`, ignoring their hours.

### 9.2 Auto-tuned stats (not client knobs)

These come from `decision_asks` over the last 90 days, by hour bucket:

- `p_answer(window)`: the fraction of asks answered within the window.
- `t80`: the 80th-percentile response time.

Cold start uses constants (`p=0.5`, `t80=20 s`). Stats come from one SQL query and are cached in-process for 5 minutes.

```python
# shortcut: in-process TTL cache; move to a materialized view when asks exceed ~1M rows.
```

### 9.3 Planner (pure)

```
remaining = deadline_at - now ;  window = remaining / max(1, tiers_left)
k = smallest number of eligible approvers (ranked by p_answer desc, capped at max_fanout)
    such that  1 - Π(1 - p_i) ≥ TARGET_P (0.9)
wait = min(max(t80 of chosen), window) ; next_action_at = now + wait
```

`plan_step()` has no I/O. `advance()` sends the asks, writes `decision_asks` and sets `next_action_at`.

### 9.4 Rules that always hold

- First valid answer wins. Everyone else asked gets "Handled by Raj ✓".
- Two-person rule above `two_person_over`: two distinct approvals are needed, and one decline ends it.
- Authority is re-checked at answer time. A counter-offer above the replier's limit escalates again.
- No one is asked twice for the same decision.

## 10. SMS channel (D6)

### 10.1 Approver ask

```
SaySo #K7 · Fremont Ford · Price adj −$50 (850→800)
Repeat customer: 3 jobs, 3 approved. AI suggests: APPROVE (within usual range)
1=Yes 2=No or reply a number to counter
https://api.sayso.example/v1/approve/Xy8...
```

The AI line is omitted if AI is disabled, slow or failing (§11).

### 10.2 Reply grammar (`sms_commands.parse`, pure)

| Reply | Meaning |
|---|---|
| `1`, `y`, `yes`, `ok` | Approve |
| `2`, `n`, `no` | Decline |
| A number | Counter with that value |
| `#K7 1` | Answer a specific decision |
| `RULES` | Owners/admins: list active rules for their scope |
| Any other text from an owner/admin | Treated as a rule-change request (§11.3) |
| `STOP` / `START` | Opt out / in (carrier-mandated; respected before anything else) |

- **Shared number, many tenants:** a reply is matched against **all** open asks for that phone number across agencies.
- If more than one ask is open, a bare `1` gets "Which one? Reply #K7 1 or #M2 1".
- Short codes are **3 characters** from an alphabet without `0 O 1 I`. They are unique among **all** open decisions while the number is shared: a partial unique index covers pending decisions, and generation also checks expired decisions still inside the late-answer window and retries on a clash (a partial index can't use `now()`).
- Replies to decided, cancelled or closed decisions follow §7.3.

### 10.3 Approval link and inbound security

- `GET/POST /v1/approve/{token}`: the token is HMAC-SHA256 over `ask_id`, `person_id` and `exp`, single-use per ask. The page is server-rendered with Approve, Decline and Counter buttons. No login or app.
- `POST /v1/inbound/twilio/sms` is validated with `twilio.request_validator.RequestValidator`. A bad signature returns `403`.

The `Channel` protocol (`send(person, text) -> provider_id`) is the seam for Slack and WhatsApp later.

## 11. AI features (D4, D11)

### 11.1 Adapter

`services/ai.py` is the only module that knows about the LLM provider:

```python
async def summarize_and_recommend(decision, facts) -> AiAdvice | None   # one call, JSON out
async def draft_rule(text, policy, scope) -> DraftRule | None          # structured rule or None
```

- One provider, selected by env (`SAYSO_AI_PROVIDER`, `SAYSO_AI_MODEL`, `SAYSO_AI_API_KEY`).
- Output is parsed into Pydantic models. Invalid output becomes `None`, never an exception that reaches callers.
- Every call is recorded in `ai_calls` (tokens, latency, feature, scope) for usage and cost (D10).

### 11.2 Summary and recommendation (approver SMS)

- Live mode allows **1.5 s**. On timeout or error, the template summary goes out alone.
- The recommendation is shown to the human and stored on the ask. **It never resolves a decision.** `decided_by` is only ever `policy`, `human` or `timeout`.
- Context sent to the LLM is the decision summary, amount, `context` and `history.*`. Scopes with `ai.*=false` send nothing.

### 11.3 Rule drafting by SMS (owners/admins, D2)

```
Owner:  "repeat customers can get up to $75 off"
SaySo:  "New rule for Fremont Ford · Price adjustment:
         approve when customer has ≥3 approved jobs AND discount ≤ $75.
         Reply YES to save, NO to cancel. (expires in 15 min)"
Owner:  "YES" ─► new policy version at the owner's scope; audit records who/when/original text
```

- Drafts must validate against the policy schema. If not, the owner gets "I couldn't turn that into a rule. Try: 'approve discounts up to $40'."
- The read-back is generated deterministically **from the structured rule**, not by the LLM, so the owner confirms what will actually run.
- The scope is the owner's own membership scope. If they own several locations, SaySo asks which one.
- A drafted rule can only produce `approve`, `decline`, `counter` or `escalate`. It can't touch settings floors.

## 12. Memory, suggestions, usage and retention (D9, D10, D14)

- **Customer facts:** `history.decision_count`, `approved_count`, `declined_count`, `last_outcome` and `total_approved_amount` for the same `customer_ref`, **within the same business**. History is never shared across businesses. Caller-supplied facts go in `context` and are trusted as sent.
- **Rule suggestions:** a nightly job groups human decisions by `(type, amount bucket)` over 60 days. When `n ≥ 10` and the approve rate is `≥ 0.9`, it creates a suggestion. Adopting it via API, or by an owner replying YES to a suggestion SMS, writes a new policy version. **Nothing is applied automatically.**
- **Usage:** decisions, asks and AI calls are already rows. `GET /v1/usage?scope=&from=&to=` aggregates counts per scope and month. No separate metering table until pricing is decided.
- **Retention:** a daily job nulls `context`, `summary`, `say` and AI text on decisions older than the resolved `retention.context_days`. Outcomes, amounts, timings and `customer_ref` are kept so history and suggestions keep working.

## 13. API (all under `/v1`, D15)

Auth: `Authorization: Bearer sk_...` (scoped key). Writes accept an `Idempotency-Key` header. Errors use RFC 9457 problem+json.

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/decisions` | Request a decision |
| GET | `/v1/decisions/{id}?wait_ms=` | Read / long-poll (≤ 25 s) |
| POST | `/v1/decisions/{id}/cancel` | Cancel |
| GET | `/v1/decisions?location_id=&status=&from=` | List for audit |
| POST/GET/PATCH | `/v1/businesses`, `/v1/locations` | Org structure (agency keys) |
| POST/GET/PATCH | `/v1/people`, `/v1/memberships` | People, roles, authority |
| GET/PATCH/PUT | `/v1/settings/{scope_type}/{scope_id}` | Partial overrides at a scope (PUT replaces them) |
| GET | `/v1/settings/resolved` | Merged settings with origin of each value |
| GET | `/v1/policies/templates` | System starter templates |
| GET/PUT | `/v1/policies/{type}?scope=` | Read/replace (new version) at a scope |
| POST | `/v1/policies/{type}/clone?to_scope=` | Copy template/parent policy down |
| GET | `/v1/policies/{type}/versions` | Version history |
| GET | `/v1/suggestions`, POST `/v1/suggestions/{id}/adopt` | Rule suggestions |
| GET | `/v1/usage` | Usage per scope/month |
| POST/GET | `/v1/api_keys` | Issue a scoped key (shown once) / list keys. Agency keys only (D19) |
| POST | `/v1/api_keys/{id}/revoke` | Revoke a key. A key can't revoke itself |
| POST | `/v1/callback_secret/rotate` | New callback signing secret, shown once. Agency keys only (D17) |
| POST | `/v1/inbound/twilio/sms` | Twilio webhook (signature-verified) |
| GET/POST | `/v1/approve/{token}` | One-tap approval page |
| GET | `/healthz`, `/readyz` | Infra probes (unversioned by convention) |

### Request / response

```json
POST /v1/decisions
Idempotency-Key: call_8f2e:discount:1
{
  "location_id": "loc_01J...",
  "type": "price_adjustment",
  "mode": "live",
  "summary": "Customer asks $800 instead of $850 for San Jose → Fresno pickup",
  "subject": { "customer_ref": "ghl:contact_123", "amount": 50 },
  "context": { "quoted": 850, "requested": 800, "route": "San Jose → Fresno" },
  "wait_ms": 8000,
  "callback_url": "https://services.leadconnectorhq.com/hooks/abc"
}
```

```json
{
  "id": "dec_01JB...",
  "status": "decided",
  "outcome": "approve",
  "value": 800,
  "decided_by": "human",
  "approver": { "id": "per_...", "name": "Raj" },
  "ai_recommendation": "approve",
  "say": "Good news, we can do 800.",
  "reason": "human",
  "policy": { "id": "pol_...", "version": 7, "rule_id": null },
  "deadline_at": "2026-10-23T10:15:30Z",
  "poll_after_ms": null
}
```

Codes: `401` bad key; `404` unknown id or outside the key's scope; `409` idempotency conflict; `422` validation; `429` rate limit. "No policy" is a domain outcome, not an error.

## 14. Data model

Ids are prefixed ULIDs (`agy_ bus_ loc_ per_ mem_ key_ pol_ dec_ ask_ drf_ cbd_ sug_ aic_`). All rows have `created_at` and `updated_at`.

| Table | Key columns |
|---|---|
| `agencies` | `name`, `settings` jsonb, `callback_secret_version` (int, default 1) |
| `businesses` | `agency_id`, `name`, `settings` |
| `locations` | `agency_id`, `business_id`, `name`, `timezone`, `settings` |
| `api_keys` | `agency_id`, `scope_type`, `scope_id`, `prefix`, `key_hash`, `last_used_at`, `revoked_at` |
| `people` | `phone` (E.164, unique), `sms_opted_out` |
| `memberships` | `person_id`, `agency_id`, `scope_type`, `scope_id`, `display_name`, `role`, `authority_limit`, `can_approve`, `on_call`, `contact_hours` jsonb, `max_asks_per_hour`, `active` |
| `policies` | `agency_id` (null for system), `scope_type`, `scope_id`, `decision_type`, `version`, `body` jsonb, `active`, `created_by` (key or person), `source` (`api/sms/suggestion/clone`), `source_text` |
| `decisions` | `agency_id`, `location_id`, `type`, `mode`, `status`, `summary`, `subject`, `context`, `customer_ref`, `amount`, `policy_id`, `policy_version`, `rule_id`, `outcome`, `value`, `say`, `reason`, `decided_by`, `decided_by_person_id`, `late_outcome`, `short_code`, `deadline_at`, `next_action_at`, `callback_url`, `idempotency_key`, `request_hash`, `context_scrubbed_at`, `decided_at` |
| `decision_asks` | `decision_id`, `person_id`, `membership_id`, `channel`, `provider_message_id`, `ai_recommendation`, `sent_at`, `responded_at`, `response`, `response_value` |
| `rule_drafts` | `person_id`, `scope_type`, `scope_id`, `decision_type`, `text`, `draft` jsonb, `expires_at`, `status` |
| `suggestions` | `agency_id`, `scope_type`, `scope_id`, `decision_type`, `rule`, `evidence`, `status` |
| `callback_deliveries` | `decision_id`, `event`, `attempt`, `status_code`, `next_attempt_at`, `delivered_at` |
| `ai_calls` | `agency_id`, `location_id`, `feature`, `model`, `tokens_in`, `tokens_out`, `latency_ms`, `ok` |
| `worker_jobs` | `name`, `last_run_at` (nightly/daily job bookkeeping) |

**Indexes:** `decisions (status, next_action_at) WHERE status='pending'`; `decisions (agency_id, location_id, created_at)`; `decisions (agency_id, customer_ref)`; unique `decisions (agency_id, idempotency_key)`; unique `decisions (short_code) WHERE status='pending'`; `decision_asks (person_id, responded_at)`; `memberships (scope_type, scope_id)`; `policies (scope_type, scope_id, decision_type) WHERE active`.

## 15. Waiting and concurrency

- **Waiter:** one asyncpg connection per API process `LISTEN`s on `sayso_decisions`. The payload is just the decision id. Requests register futures keyed by id and re-check the row after subscribing, which closes the race with an answer arriving first.
- **Resolve:** a conditional `UPDATE ... RETURNING`, then `NOTIFY` in the same transaction.
- **Worker:** every 1 s, run `SELECT ... WHERE status='pending' AND next_action_at <= now() ... FOR UPDATE SKIP LOCKED LIMIT 50`, then call `advance()` or `expire()`. Callbacks use the same pattern with exponential backoff (8 attempts, up to 1 h). Daily jobs (suggestions, retention) are claimed through `worker_jobs` with `SKIP LOCKED`.
  - `shortcut: 1 s polling granularity; move to NOTIFY-driven wakeups if escalation needs sub-second timing.`

## 16. Security

- **API keys:** SHA-256 hashed, prefix-identified, scoped, revocable. Scope checks sit in one dependency, and tests prove `404` across agencies and outside a key's subtree.
- **People:** shared across agencies, but only the phone number is shared. Names, roles and history stay scoped to the membership's agency.
- **Inbound:** Twilio signature required. HMAC approval tokens are single-use and expire. SMS commands are authorized by membership role and scope.
- **Outbound callbacks:** HTTPS only in production, HMAC-signed (`SaySo-Signature: t=,v1=`) with the agency's own derived secret (D17), SSRF denylist. Rotating bumps `callback_secret_version` for that agency only.
- **LLM:** only the fields listed in §11.2 are sent, it can be disabled per scope, and the provider is named in the privacy notes. AI output is treated as untrusted text: escaped in SMS and never executed.
- **PII:** `context`, `summary`, phone numbers and AI text never appear in logs. Retention scrubbing is per scope.
- **Rate limits** per API key.
  - `shortcut: in-process bucket; move to Postgres/edge when running >1 API replica.`

## 17. Observability

- JSON logs with `request_id`, `agency_id`, `location_id`, `decision_id` and `latency_ms`.
- `/metrics` (Prometheus) with:
  - decisions by `decided_by`
  - time-to-decision histogram
  - asks, answer rate and expiries
  - AI latency and failure rate
  - callback failures

## 18. Testing

| Layer | How |
|---|---|
| Policy engine, planner, settings merge, SMS parser, permission matrix, token signing | Pure, table-driven unit tests |
| API and services | `httpx.AsyncClient` against the app, real Postgres (compose), rollback per test |
| Concurrency | Racing replies give one outcome; waiter wakes on NOTIFY; two workers never claim the same row |
| AI | A `FakeAi` adapter. Tests prove decisions complete when AI times out or returns garbage |
| Twilio | A `FakeChannel` in tests; signature validation against a known vector |
| MCP | Tools against a stubbed REST API |

CI runs `ruff check`, `ruff format --check`, `mypy app` and `pytest` with a Postgres service.

## 19. Configuration and deployment

Process env (`.env.example`):

```
SAYSO_DATABASE_URL=postgresql+asyncpg://sayso:sayso@localhost:5432/sayso
SAYSO_PUBLIC_BASE_URL=http://localhost:8000
SAYSO_SIGNING_SECRET=change-me-32-bytes-min
SAYSO_TWILIO_ACCOUNT_SID=
SAYSO_TWILIO_AUTH_TOKEN=
SAYSO_TWILIO_FROM_NUMBER=
SAYSO_AI_PROVIDER=anthropic
SAYSO_AI_MODEL=
SAYSO_AI_API_KEY=
SAYSO_LOG_LEVEL=INFO
```

- Client behavior lives in `settings` jsonb per scope (§6), **not** in env. Env is only for how the process runs.
- `docker compose up` starts `postgres:16`, `api` and `worker`.
- The image is multi-stage, built with uv, runs as non-root, and has a healthcheck.
- Migrations run with `alembic upgrade head` as a release step, never at app startup.

## 20. MCP server (`mcp/`)

A separate package (`sayso-mcp`) built on the official MCP Python SDK. It talks to the backend **only over REST** with a scoped API key.

| Tool | Maps to |
|---|---|
| `request_decision(type, summary, amount, location_id?, customer_ref?, context?, mode="live")` | `POST /v1/decisions` |
| `get_decision(id, wait_ms?)` | `GET /v1/decisions/{id}` |
| `cancel_decision(id)` | `POST /v1/decisions/{id}/cancel` |

Transports are stdio and streamable HTTP. Config is `SAYSO_API_URL` and `SAYSO_API_KEY`.

## 21. Integrations

**GoHighLevel Voice AI:** Custom Action `request_decision` calls `POST /v1/decisions` with `wait_ms` below GHL's webhook timeout. While the decision is pending, the agent says `say`, and `check_decision` calls `GET /v1/decisions/{id}?wait_ms=`. The `callback_url` points at a GHL inbound-webhook workflow for customer follow-up.

**Vapi / Retell / Bland / Synthflow:** the same two-call pattern through each platform's mid-call webhook feature (Vapi tools, Retell custom functions, Bland custom tools). Per platform, document the measured webhook timeout, the recommended `wait_ms` below it, and how the hold line (`say`) is spoken while pending. One guide per platform lives in `docs/integrations/`.

## 22. Out of scope for v1

- Dashboard UI: the API and SMS cover the needs.
- Slack and WhatsApp channels: the `Channel` seam is ready.
- Per-location languages: the `locale` setting exists.
- CRM connectors.
- Billing, until pricing is decided.
- Per-agency SMS numbers.
- Bring-your-own LLM key.
- SSO.
- Multi-region.

## 23. Open risks

1. **GHL webhook timeout:** measure it before tuning `wait_ms`. The live-call experience depends on it.
2. **US SMS registration:** the shared number needs A2P 10DLC or toll-free verification, and the sample messages must show both approvals and rule commands. Start on day one.
3. **Shared number at scale:** one sender for every agency means one carrier-filtering reputation for everyone. Plan the move to per-agency numbers once more than a handful of agencies are live.
4. **Approver answer rates:** if replies don't arrive inside 30 s, live mode degrades to "defer". Measure this in the pilot before promising live decisions.
5. **SMS rule edits:** the read-back plus YES confirmation is the only guard against a mistyped rule. Track how often owners cancel drafts, and tighten the drafting prompt if it's high.
