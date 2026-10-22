# SaySo

**A real-time decision and human-in-the-loop API for AI agents.**
Apply business rules, authorize exceptions, escalate to humans via SMS, and return safe decisions before the conversation ends. Platform-agnostic: works with voice, chat, and custom agents via REST API or MCP.

> 🚧 Early development, built in public. Expect breaking changes before `v1.0`.

---

## Why

AI agents are great until a customer goes off-script:

> **Customer:** "Can you do it for $50 less?"
> **Agent:** "Let me check and get back to you…" *(the lead goes cold)*

The agent can't decide, and nobody it can reach in time is allowed to. SaySo is the missing **approval matrix for AI agents**: one API call that comes back with an answer while the customer is still on the line.

## How it works

```
Agent ──► POST /v1/decisions
            │
            ├─ 1. Rules        "discounts ≤ $50 are fine"        → answer in milliseconds
            ├─ 2. Human        texts the right approver(s)        → 1 tap or reply "1"
            └─ 3. Safe default nobody answered in time            → "I'll confirm and text you"
                                                                     (late answer → your webhook)
```

- **Decides instantly** when your rules or a customer's history cover the case
- **Escalates smartly** only to people with enough authority, timed and fanned out based on who actually answers
- **Never over-commits**: a timeout can only defer or decline, never approve
- **Built for agencies**: agency → business → location, with settings, rules and approvers inherited downward and overridable at any level
- **AI that advises, never decides**: approvers get a 2-line summary and a recommendation; owners can text "repeat customers can get $75 off" and confirm the rule SaySo reads back
- **Learns**: suggests new rules from past decisions. You adopt them with one tap; nothing changes automatically
- **No app for approvers**: plain SMS replies or a one-tap link
- **Explainable**: every outcome traces to a rule version or a named person

## Quickstart

Requirements: Docker, and [uv](https://docs.astral.sh/uv/) for local development.

```bash
git clone https://github.com/<you>/sayso.git && cd sayso
cp .env.example .env                       # add Twilio creds to send real SMS
docker compose up -d                       # postgres + api + worker
docker compose exec api alembic upgrade head
docker compose exec api python -m scripts.create_agency "Demo Agency"
# → prints an agency API key: sk_live_...
```

Create a business and location, add an approver, and clone a starter policy:

```bash
H=(-H "Authorization: Bearer $SAYSO_KEY" -H "Content-Type: application/json")

curl -X POST localhost:8000/v1/businesses "${H[@]}" -d '{"name":"Demo Motors"}'
# → {"id":"bus_..."}
curl -X POST localhost:8000/v1/locations "${H[@]}" \
  -d '{"business_id":"bus_...","name":"Fremont","timezone":"America/Los_Angeles"}'
# → {"id":"loc_..."}

curl -X POST localhost:8000/v1/people "${H[@]}" -d '{"phone":"+14085550100"}'
# → {"id":"per_..."}
curl -X POST localhost:8000/v1/memberships "${H[@]}" \
  -d '{"person_id":"per_...","scope_type":"business","scope_id":"bus_...",
       "display_name":"Raj","role":"owner","authority_limit":200}'

curl -X POST "localhost:8000/v1/policies/price_adjustment/clone?to_scope=business:bus_..." "${H[@]}"
```

Ask for a decision:

```bash
curl -X POST localhost:8000/v1/decisions \
  -H "Authorization: Bearer $SAYSO_KEY" -H "Content-Type: application/json" \
  -d '{
    "location_id": "loc_...",
    "type": "price_adjustment",
    "mode": "live",
    "summary": "Customer asks $800 instead of $850",
    "subject": {"customer_ref": "ghl:contact_123", "amount": 50},
    "context": {"quoted": 850, "requested": 800},
    "wait_ms": 8000
  }'
```

```json
{
  "id": "dec_01JB...",
  "status": "decided",
  "outcome": "approve",
  "decided_by": "policy",
  "say": "Good news, we can do 800."
}
```

If the rules send it to a human and nobody has answered within `wait_ms`, you get `"status": "pending"` with a `say` hold line. Poll `GET /v1/decisions/{id}?wait_ms=8000` until it's decided.

## Use with your agent

| Platform                              | How                                                                                                            |
| ------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| **GoHighLevel Voice AI**              | Two Custom Actions (request + check). See [docs/integrations/gohighlevel.md](docs/integrations/gohighlevel.md) |
| **Vapi / Retell / Bland / Synthflow** | The same two-call pattern as mid-call tools or custom functions                                                |
| **n8n / Make / Zapier**               | HTTP request node plus a webhook trigger for callbacks                                                         |
| **Claude / Cursor / any MCP client**  | [`mcp/`](mcp/README.md) package: `request_decision`, `get_decision`                                            |
| **Your own code**                     | Plain REST. See the [API section of the architecture doc](docs/ARCHITECTURE.md#6-api-contract-v1)              |

## Project layout

```
backend/   FastAPI service + worker (Python 3.12, PostgreSQL)
mcp/       MCP server, a thin client over the REST API
docs/      Architecture and integration guides
```

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full design: policy engine, escalation math, data model and security.

## Development

```bash
cd backend
uv sync
docker compose up -d postgres
uv run alembic upgrade head
uv run uvicorn app.main:app --reload       # API on :8000, docs on /docs
uv run python -m app.worker                # in another terminal
uv run pytest                              # tests
uv run ruff check . && uv run mypy app     # lint + types
```

## Roadmap

- [x] Design ([docs/ARCHITECTURE.md](docs/ARCHITECTURE.md))
- [ ] Agencies, businesses, locations, roles, layered settings
- [ ] Decision-type templates, policy engine, decisions API
- [ ] SMS escalation (Twilio) with reply-by-digit and approval links
- [ ] Worker: escalation steps, expiry, after-hours, callbacks
- [ ] Adaptive escalation timing and fan-out
- [ ] Customer history, rule suggestions, usage, retention
- [ ] AI summaries, recommendations, rule drafting by SMS
- [ ] MCP server
- [ ] Integration guides: GoHighLevel, Vapi, Retell, Bland
- [ ] Slack / WhatsApp channels, per-location languages
- [ ] Hosted version

## Contributing

Issues and PRs welcome. Please open an issue before large changes. Run `ruff`, `mypy` and `pytest` before you push.

## License

[MIT](LICENSE) © 2026 Jashan Singh
