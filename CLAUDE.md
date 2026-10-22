# SaySo

Design: docs/ARCHITECTURE.md (decision log §0). Build plan and phases: docs/PLAN.md.

## Working rules

1. Work **one phase at a time**, in order. Don't start the next phase until the current one's checks pass.
2. At the end of each phase:
   - run `uv run ruff check . && uv run ruff format --check . && uv run mypy app && uv run pytest` from `backend/`
   - commit with a conventional message (`feat:`, `test:`, `chore:`)
   - give a 3-line summary: what was built, what was tested, anything deferred
3. Write the **smallest code that fully meets the phase**. No speculative abstractions, base classes with one implementation, or flags nobody asked for.
4. **Client behavior lives in scope `settings` (ARCHITECTURE §6), never in env or constants.** Env is only for how the process runs. Internal tuning values (target probability, cold-start timing, AI latency budget) are named constants in one module.
5. **Allowed dependencies only:** `fastapi`, `uvicorn[standard]`, `sqlalchemy[asyncio]`, `asyncpg`, `alembic`, `pydantic`, `pydantic-settings`, `httpx`, `twilio` (request validator only), `python-ulid`, and the official SDK of the one chosen LLM provider. Dev: `pytest`, `pytest-asyncio`, `ruff`, `mypy`. MCP package: `mcp`, `httpx`. Ask before adding anything else. **No Redis or Celery.**
6. **The fixed safety floor is non-negotiable.** A timeout never approves. AI output never resolves a decision. Tenant isolation, Twilio signatures, HMAC tokens, signed callbacks and PII-free logs are required from the phase that introduces them.
7. When you take a deliberate shortcut, leave a code comment `# shortcut: <limit>, <when to upgrade>`.
8. Every pure function (policy engine, settings merge, permission matrix, escalation planner, SMS parser, token signing) gets table-driven unit tests.
9. **Every endpoint lives under `/v1`** except `/healthz` and `/readyz`.
10. Keep the folder layout from ARCHITECTURE §4. Create a file only when a phase needs it.

## Local database

Tests never use `SAYSO_DATABASE_URL`. They use `SAYSO_TEST_DATABASE_URL`
(default `postgresql+asyncpg://sayso:sayso@localhost:5433/sayso_test`), and the database name
must end in `_test` because the suite drops and recreates its schema.
