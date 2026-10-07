# Orchestrating the link-aggregation plan with subagents

For the session that runs the plan. The orchestrator holds the plan and dispatches fresh
subagents; it never edits code itself.

- Plan: `docs/superpowers/plans/2026-10-07-link-aggregation.md`
- Spec (binding authority): `docs/superpowers/specs/2026-10-07-link-aggregation.md`
- Standards every implementer and reviewer must read: `docs/superpowers/standards.md`
- Owner's rules: `.claude/CLAUDE.md` (no Docker; commit each task; be civil)

## Kick-off prompt (paste into the new session)

```
Execute docs/superpowers/plans/2026-10-07-link-aggregation.md with the
superpowers:subagent-driven-development skill. Read docs/superpowers/orchestration.md first
and follow its lanes, model map and pre-made rulings. Every implementer and reviewer dispatch
must include "read docs/superpowers/standards.md before you start". Work on a branch, not on
main; do not merge or push; do not run Docker. Report at the end with the rulings you made and
whether the container needs a rebuild.
```

## Branch

The skill refuses to implement on `main`. Create the worktree/branch `link-aggregation` from
`main` (`superpowers:using-git-worktrees`). Merging back to `main` and rebuilding the
container are the owner's decisions, taken after the final review.

## Dependency lanes

Tasks share files (`app/retailers/generic.py` in 4–6; `app/templates/product.html` in 3, 8, 9,
10; `tests/test_web.py` in almost all), so the default is **strictly sequential, 1 → 11**.
The skill forbids parallel implementers in one working tree. If parallelism is wanted anyway,
only these pairs are disjoint, and each needs its own worktree plus a merge afterwards:

| Can run in parallel | Why it is safe | Must finish before |
|---|---|---|
| Task 4 ∥ (Task 2 → Task 3) | 4 creates `generic.py`/`test_generic.py` and only adds `PriceCandidate` to `schemas/domain.py`; 2–3 touch `services/listings.py`, `monitoring.py`, `web/products.py`, `product.html`, `test_monitoring.py`, `test_web.py` | Task 5 |
| Task 6 ∥ Task 8 | 6: `rules.py`, `generic.py`, `runtime.py`, `web/settings.py`, `settings.html`; 8: `alerts.py`, `queries.py`, `dashboard.html`, `product.html` | Task 7 (needs 6) and Task 9 (needs 6, 7, 8) |

Expected saving: roughly two task-durations out of eleven. Recommendation: run sequentially
unless wall-clock matters more than merge risk.

## Model map

Implementers work from a plan whose tasks carry the tests, signatures and exact values, so
most tasks are transcription plus integration. Suggested tiers (the owner may override with
Opus 5.5 everywhere; the cost is higher, the result is not expected to differ on the
mechanical tasks):

| Task | Nature | Implementer | Task reviewer |
|---|---|---|---|
| 1 Schema + migration | mechanical, one migration to read carefully | Sonnet 5.5 | Sonnet 5.5 |
| 2 History every check | mechanical | Sonnet 5.5 | Sonnet 5.5 |
| 3 Per-listing interval | small integration (service + route + template) | Sonnet 5.5 | Sonnet 5.5 |
| 4 Generic reading functions | algorithms are specified; needs care with BeautifulSoup | Opus 5.5 | Sonnet 5.5 |
| 5 GenericAdapter + Registry + wiring | multi-file integration, test transport hook | Opus 5.5 | Opus 5.5 |
| 6 Store rules + teach/learn | algorithms specified; integration with 5 | Opus 5.5 | Sonnet 5.5 |
| 7 Confirm price in preview | service + route + template + JS | Opus 5.5 | Sonnet 5.5 |
| 8 Heuristic gating | mechanical, but touches alert semantics | Sonnet 5.5 | Opus 5.5 |
| 9 Confirm price from product page | reuse of 6/7 | Sonnet 5.5 | Sonnet 5.5 |
| 10 Links-first add flow | templates + validation + one existing test to re-check | Sonnet 5.5 | Sonnet 5.5 |
| 11 Docs + verification | mechanical | Sonnet 5.5 | Sonnet 5.5 |
| Final whole-branch review | judgment | — | Opus 5.5 (or Fable 5.1) |

Fix rounds 4–5 escalate one tier above the stuck implementer, per the skill.

## Gates (every task)

1. Implementer report at `<workspace>/task-N-report.md` with: tests written first and seen
   failing, `.venv/bin/pytest -q` last line, ruff clean, commit hash(es).
2. Task 1 and Task 11 additionally: the scratch `alembic check` output
   (`No new upgrade operations detected.`).
3. Review package built from the recorded BASE (never `HEAD~1`); task reviewer gets the brief,
   the report, the package, the Global Constraints block copied verbatim from the plan, and
   the standards file path.
4. Ledger line `Task N: complete (...)` before the next dispatch.

## Pre-made rulings (so the orchestrator does not have to guess)

- **Test transport order (Task 5):** in `tests/conftest.py`, an `int` status under the host
  key is checked **before** a page under the full URL, so a host-level 403 wins. The plan says
  so; a reviewer flagging the reverse order is right.
- **`summary()["errors"] → ["review"]` (Task 8):** the rename is intended; both templates
  change with it. No compatibility alias.
- **Existing tests whose counts encode the old "history only on change" rule (Task 2):**
  `test_scheduled_due_check_and_blocked_retailer_isolation` goes from 6 to 9 history rows;
  `tests/test_scheduler.py` stays at 2. Any other count change must be justified in the report.
- **`test_uncertain_and_conflicting_candidate_review` (Task 10):** it covers *discovered*
  candidates and should keep passing; only an assertion that a *manual* CONFLICT is blocked
  may change (to expect 303 and the `saved as pasted` warning).
- **Generic pages default to condition `new`** (spec decision 5). A reviewer may question it;
  the ruling stands unless the owner changes the spec.
- **Availability `unknown` never alerts** (spec decision 6). Same.
- **No Playwright fallback on 429/403** (spec decision 11). Out of scope; do not add it.
- **`Listing.retailer` stays `String(50)`.** SQLite does not enforce the length; a hostname
  longer than 50 characters is not expected. No migration to widen it.
- **Plan vs. spec conflict:** the spec wins; ledger the ruling; carry it into the next
  dispatch.

## What the orchestrator tells the owner at the end

- The branch name and the commit range.
- Every ruling made, with what it costs if wrong.
- "The container needs `docker compose up -d --build` to run the migration and the new code."
  Do not run it.
- Any parked findings from the final review.
