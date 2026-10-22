# SaySo — Build Plan for Claude Code

Paste this whole file into Claude Code at the root of a `sayso/` repo that already contains `README.md` and `docs/ARCHITECTURE.md` (v0.2).

---

## Instructions to Claude Code

You are building **SaySo**, a real-time decision and human-in-the-loop API for AI agents. The design is fixed in `docs/ARCHITECTURE.md`, and its decision log (§0) records choices the owner has already made. **Read the whole document before writing any code**, and treat it as the source of truth. If you find a real problem with the design, stop and tell me; don't silently diverge.

### Working rules

1. Work **one phase at a time**, in order. Don't start the next phase until the current one's checks pass.
2. At the end of each phase:
   - run `uv run ruff check . && uv run ruff format --check . && uv run mypy app && uv run pytest` from `backend/`
   - commit with a conventional message (`feat:`, `test:`, `chore:`)
   - give me a 3-line summary: what was built, what was tested, anything deferred
3. Write the **smallest code that fully meets the phase**. No speculative abstractions, base classes with one implementation, or flags nobody asked for.
4. **Client behavior lives in scope `settings` (ARCHITECTURE §6), never in env or constants.** Env is only for how the process runs. Internal tuning values (target probability, cold-start timing, AI latency budget) are named constants in one module.
5. **Allowed dependencies only:** `fastapi`, `uvicorn[standard]`, `sqlalchemy[asyncio]`, `asyncpg`, `alembic`, `pydantic`, `pydantic-settings`, `httpx`, `twilio` (request validator only), `python-ulid`, and the official SDK of the one chosen LLM provider. Dev: `pytest`, `pytest-asyncio`, `ruff`, `mypy`. MCP package: `mcp`, `httpx`. Ask before adding anything else. **No Redis or Celery.**
6. **The fixed safety floor is non-negotiable.** A timeout never approves. AI output never resolves a decision. Tenant isolation, Twilio signatures, HMAC tokens, signed callbacks and PII-free logs are required from the phase that introduces them.
7. When you take a deliberate shortcut, leave a code comment `# shortcut: <limit>, <when to upgrade>`.
8. Every pure function (policy engine, settings merge, permission matrix, escalation planner, SMS parser, token signing) gets table-driven unit tests.
9. **Every endpoint lives under `/v1`** except `/healthz` and `/readyz`.
10. Keep the folder layout from ARCHITECTURE §4. Create a file only when a phase needs it.

---

## Phase 0 — Scaffold

- Create the folder layout, `backend/pyproject.toml` (Python 3.12, uv, ruff line length 100 with `select = ["E","F","I","B","UP","S","ASYNC"]`, mypy strict on `app/`, pytest `asyncio_mode="auto"`).
- `core/config.py` with every env variable from ARCHITECTURE §19.
- `core/logging.py` with JSON logs and request-id middleware.
- `main.py` with `create_app()`, a lifespan, `/healthz`, and `/readyz` (which pings the DB).
- `docker-compose.yml` (postgres:16 with a healthcheck, `api`, `worker`), a multi-stage non-root `Dockerfile`, `.env.example`, `LICENSE` (MIT, 2026 Jashan Singh), `.gitignore`, and `.github/workflows/ci.yml`.
- `CLAUDE.md` at the repo root containing the working rules above, plus: "Design: docs/ARCHITECTURE.md (decision log §0)."

**Done when:** `docker compose up` gives a 200 from `/healthz` and CI is green.

## Phase 1 — Tenancy, people, permissions, settings

- All tables from ARCHITECTURE §14 via Alembic (async), with prefixed ULID ids and the listed indexes. Review the first migration by hand.
- `core/security.py`: API key generate/verify (SHA-256, constant-time compare), HMAC tokens with expiry, and Stripe-style payload signing.
- `services/scopes.py`:
  - `chain(location_id)` returns location → business → agency → system
  - `can(principal, action, scope)` is the permission matrix from §5.3
- `api/deps.py`: Bearer key → `Principal` (agency plus scope subtree). Anything outside the subtree returns `404`.
- `services/settings.py`: `resolve(location_id, decision_type=None, request=None)` deep-merges the layers per §6.1, validates against a strict model with the bounds and floors from §6.2, applies tighten-only request overrides, and returns the **origin of each value**.
- Endpoints: `/v1/businesses`, `/v1/locations`, `/v1/people`, `/v1/memberships`, `/v1/settings/{scope_type}/{scope_id}`, `/v1/settings/resolved`.
- `scripts/create_agency.py` (with `scripts/__init__.py`): creates an agency and prints an agency-scoped key once.

**Tests:**
- permission matrix per role
- cross-agency and outside-subtree `404`
- settings merge order and origin reporting
- `on_timeout.outcome: "approve"` rejected at every layer
- tighten-only overrides (a request can't raise `deadline_ms`)
- a person shared by phone across two agencies leaks no names between them

## Phase 2 — Decision types, policy engine, policy-only decisions

- `services/policy_engine.py`: the pure `evaluate()` from §8.2. No `eval`. Missing paths are false. `{path}` templates.
- Starter templates in `app/templates/decision_types/` (`price_adjustment`, `booking_exception`, `trade_in_offer`, `refund`), seeded at system scope by a migration.
- Policy endpoints from §13: templates, read/replace at a scope (each PUT is a new version), clone down, and versions. Lookup walks the scope chain.
- `POST /v1/decisions`: location resolution from the key or `location_id`, idempotency (same body returns the same decision, a different body returns `409`), facts (`history.*` is `{}` for now), and evaluation. In this phase `escalate` just persists `pending`.
- `GET /v1/decisions/{id}` and `GET /v1/decisions` (list for audit).
- `examples/policies/price_adjustment.json`.

**Tests:**
- about 25 engine cases
- the nearest-scope policy wins
- clone produces an independent copy
- versioning never mutates old versions
- idempotency replay and conflict
- RFC 9457 error shape
- "no policy" escalates with a reason

## Phase 3 — SMS escalation, inbound, waiting

- `channels/base.py` (protocol) and `channels/twilio_sms.py` (Twilio REST via httpx). `FakeChannel` for tests.
- `services/sms_commands.py`: the pure reply parser for the grammar in §10.2. Rule-change text is recognized but returns "not yet supported" until phase 7.
- `services/escalation.py`, part 1: eligible approvers from memberships on the scope chain (§9.1, without stats), one at a time by authority, fixed wait, and the message format from §10.1 without the AI line. Short codes are 3 characters, unique among all open decisions (§10.2, D16).
- `services/decisions.resolve()`: conditional `UPDATE ... RETURNING`, plus `NOTIFY` in the same transaction.
- `services/waiter.py`: one LISTEN connection per process, futures by id, a re-check after subscribing, and cleanup.
- `POST /v1/decisions` with `escalate` sends the first ask inline, then waits `wait_ms`. It returns the decision, or `pending` with `hold_say` and `poll_after_ms`. `GET` long-polls up to 25 s.
- `POST /v1/inbound/twilio/sms`:
  - validates the signature
  - honours STOP/START
  - matches replies across **all** open asks for that phone (cross-agency)
  - asks "which one?" when ambiguous
  - re-checks authority at answer time
  - sends "Handled by X" to the others
- `GET/POST /v1/approve/{token}`: a minimal server-rendered page.

**Tests:**
- parser table
- racing replies produce one outcome
- the waiter wakes on NOTIFY and times out to `pending`
- an invalid signature returns 403
- ambiguous replies across two agencies
- authority re-check
- an expired or tampered link fails
- an opted-out person is never texted

## Phase 4 — Worker: steps, expiry, after-hours, callbacks

- `app/worker.py`: an asyncio loop with SIGTERM handling. `SKIP LOCKED` claiming, `advance()` or `expire()`.
- `after_hours` behavior (§9.1): `defer` expires immediately; `on_call` asks members with `on_call=true`.
- Late answers within `LATE_ANSWER_WINDOW` are stored as `late_outcome`, with the `decision.late_answer` event (D16).
- `services/callbacks.py`: the outbox, HMAC signing, exponential backoff, and the SSRF guard.
- `POST /v1/decisions/{id}/cancel`. `mode:"async"` returns `202`.

**Tests:**
- moves to the next approver after the wait
- expiry yields the configured `defer` or `decline`, and **never** `approve`
- after-hours `defer` vs `on_call`
- a late answer fires a callback
- callback signature verification
- the SSRF guard
- two workers never process the same row

## Phase 5 — Adaptive escalation

- `services/history.py`: approver `p_answer(window)` and `t80` from `decision_asks` (one SQL query with `percentile_cont`), a 5-minute in-process cache, and cold-start constants.
- The pure `escalation.plan_step()` from §9.3, respecting `max_fanout`, `contact_hours`, load caps and the two-person rule. `advance()` becomes a thin wrapper.

**Tests:** about 15 planner cases (cold start, k=1 for a strong approver, fan-out up to max, off-hours skip, load cap, window shrinking near the deadline, two-person rule).

## Phase 6 — Customer history, suggestions, usage, retention

- `history.customer_facts()` per §12, scoped to the business and injected into the facts.
- `services/suggestions.py`: a nightly job claimed via `worker_jobs`. `GET /v1/suggestions` and `POST /v1/suggestions/{id}/adopt` (writes a new version; never automatic).
- `GET /v1/usage` aggregates decisions, asks and AI calls per scope and month.
- `services/retention.py`: a daily job that scrubs `context`, `summary`, `say` and AI text past the resolved `retention.context_days`.

**Tests:**
- a repeat customer is approved via a `history.*` rule
- history doesn't cross businesses
- suggestion thresholds
- adopt creates a new version
- retention scrubs only expired rows and keeps outcomes and amounts

## Phase 7 — AI: summaries, recommendations, SMS rule drafting

- `services/ai.py`: a single-provider adapter with `summarize_and_recommend()` and `draft_rule()`. Output is parsed into Pydantic models; anything invalid becomes `None`. Every call is recorded in `ai_calls`. Check the provider SDK's current docs before writing code.
- Escalation adds the AI line to SMS within a **1.5 s** budget in live mode. Otherwise the SMS goes out without it. The recommendation is stored on the ask and returned as `ai_recommendation`. It **never resolves** a decision.
- Owner/admin rule commands in `sms_commands` + `inbound` (§11.3):
  - `RULES` lists active rules
  - free text → `draft_rule()` → a **deterministic read-back** generated from the structured rule
  - `YES` saves a new policy version at the owner's scope (with `source="sms"` and `source_text`)
  - `NO` or expiry cancels the draft
  - owners of several locations are asked which one
- `ai.summaries`, `ai.recommendations` and `ai.rule_drafting` settings are honoured per scope.

**Tests (FakeAi):**
- a decision completes when AI times out, errors or returns garbage
- an AI recommendation never changes `decided_by`
- the read-back matches the stored rule exactly
- a non-owner's rule text is refused
- a draft expires
- AI disabled at a scope sends nothing to the provider

## Phase 8 — MCP server (`mcp/`)

- A separate `sayso-mcp` package with a console script, built on the official MCP Python SDK. Check the current SDK docs first.
- Tools `request_decision`, `get_decision` and `cancel_decision` (§20), calling REST via httpx with `SAYSO_API_URL` and `SAYSO_API_KEY`. API errors become readable tool errors.
- stdio and streamable HTTP transports. `mcp/README.md` with Claude Desktop / Claude Code config snippets.

**Tests:** each tool against `httpx.MockTransport`.

## Phase 9 — Voice platform integrations and live-call spike

- `scripts/measure_webhook_timeout.py`: an endpoint that sleeps N seconds, used to measure each platform's mid-call webhook timeout. Record the numbers.
- One guide per platform in `docs/integrations/`: `gohighlevel.md`, `vapi.md`, `retell.md`, `bland.md`. Each covers the request/check tool definitions, payload mapping, the measured timeout, a recommended `wait_ms` below it, how the hold line is spoken, and the `callback_url` → customer follow-up workflow. Check each platform's current docs; don't rely on memory.
- Keep the core untouched. If a platform needs a different response shape, add a thin response adapter (`?format=vapi`) only when its docs require it.

**Done when:** a real test call on GHL and on at least one of Vapi/Retell/Bland gets a policy decision, and an escalated one gets a human SMS answer before the call ends.

## Phase 10 — Hardening

- Per-key rate limiting (in-process, with a `shortcut:` note).
- `/metrics` with the counters from §17.
- A log-redaction test: logs never contain `context`, phone numbers or AI text.
- `examples/` curl scripts for each flow.
- Update the README roadmap checkboxes.

---

## Final checklist before going public

- [ ] Fresh clone → `docker compose up` → working API with only `.env` edits
- [ ] CI green; coverage on `policy_engine`, `settings`, `scopes`, `escalation`, `sms_commands`, `inbound` ≥ 90%
- [ ] No secrets in the git history
- [ ] README quickstart verified line by line
- [ ] A2P 10DLC / toll-free verification started for the shared number, with sample messages covering approvals and rule commands
