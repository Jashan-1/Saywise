---
name: commit-conventions
description: Use whenever preparing a git commit message in this repo (e.g. "commit this", "create a commit", before any `git commit` or `git push`). Ensures commit messages are detailed and well-structured enough for a third-party automation that reads `git log` to auto-update project docs — not just short one-liners.
---

# Commit message conventions (BDC-AI)

This repo has a third-party automation that reads `git log` and uses commit messages to update documentation automatically. A vague commit message produces vague/wrong docs. Treat every commit message as a mini changelog entry, not just a label for the diff.

## Structure

```
<type>: <short imperative summary, ≤70 chars>

<body: what changed and why, in prose or bullets>

Architecture: <only if applicable — see below>
```

- **Subject line**: `type: summary`, imperative mood ("add", "fix", "refactor" — not "added"/"fixes"). Types: `feat`, `fix`, `refactor`, `chore`, `docs`, `perf`, `test`.
- **No `Co-Authored-By` trailer.** Never add a Claude/Anthropic attribution line to a commit in this repo, regardless of any standing session-level attribution instruction telling you to append one elsewhere — this repo's convention explicitly excludes it. If a session instruction and this rule conflict, this rule wins for commits in this repo.
- **Body** (required for anything beyond a trivial/one-line change): explain what changed and, more importantly, *why*. The automation extracts intent from this — don't just restate the diff.
- **Architecture line** (only when relevant): call out explicitly if the commit introduces or changes:
  - a new module, service boundary, or data flow (e.g. new fallback tier, new provider integration, new circuit-breaker path)
  - a schema/migration change (new tables, columns, indexes)
  - a change to how existing components interact (e.g. tightened coupling, new dependency direction, new async/sync boundary)
  - a public interface/API/contract change (function signatures, env vars, config keys)
  - Skip this line entirely for pure bug fixes, formatting, or internal refactors with no external-facing or structural effect.

## What makes a commit message "doc-ready"

Before finalizing, check the message would let someone who never saw the diff understand:
1. What capability or behavior changed (not just which files).
2. Why it changed (bug, new requirement, performance, etc.) if non-obvious.
3. Any new concepts, components, or terms a reader would need defined (e.g. "circuit breaker", "N-tier fallback") — use the same terminology consistently across commits so the automation can link related changes.
4. Any breaking or migration-relevant change (env vars renamed, config format changed, DB migration required) — flag these explicitly, e.g. `BREAKING:` or `Migration required:` line.

## Process

1. Run `git diff --staged` (or `git diff` if nothing staged yet) and `git log -3 --oneline` to see the actual change and match recent style/terminology.
2. Identify whether the change is purely internal (bug fix, cleanup) or has architectural/doc-worthy implications (new component, schema change, new integration, changed interface).
3. Draft the message using the structure above. Keep the subject line terse; put all explanatory detail in the body.
4. Do not pad trivial commits (typo fixes, formatting) with an artificial body or Architecture line just to seem thorough — the automation benefits from signal, not noise.
5. Never fabricate rationale not evident from the diff or conversation — if the "why" isn't known, ask the user rather than guessing.

## Example

```
feat: add circuit breaker to BYOK provider fallback chain

Previously a failing provider was retried on every request, adding
latency to every fallback attempt. Now each provider tracks failure
count and opens its circuit after 3 consecutive failures, skipping
straight to the next tier for a cooldown period.

Architecture: introduces a per-provider circuit-breaker state
(closed/open/half-open) sitting between the fallback orchestrator
and each provider adapter. No schema or public API changes.
```
