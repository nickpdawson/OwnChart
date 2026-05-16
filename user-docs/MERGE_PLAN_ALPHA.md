# Alpha 0.1.0 — `dev` → `main` merge plan

**Status:** Drafted by backend. Held until PM approval.

`dev` is feature-complete for alpha. 67 commits ahead of `main`. This
doc spells out the exact sequence so the merge + GitHub push + demo
redeploy + smoke can be executed in one block without surprises.

## Pre-merge gates (must all be green before PM go)

| Gate | Status | Source of truth |
|---|---|---|
| Backend pytest suite (80 tests) | ✅ green | `docker exec -u root -w /app ownchart-api-1 /opt/venv/bin/python -m pytest ownchart/tests/` |
| Web typecheck (`tsc --noEmit`) | ✅ green | `npm --prefix web run typecheck` |
| Web lint (`next lint`) | ✅ green | `npm --prefix web run lint` |
| Alembic migrations contiguous (0001 → 0026) | ✅ green | `ls api/alembic/versions/` |
| PHI / secrets sweep (no live keys, no Nick records in tracked files) | ✅ green | `git grep` audit completed |
| Demo bundle PHI-free + supports four canonical questions | ✅ green | `infra/demo_data/sample_patient.json` (Avery Walker) |
| Release notes + Docs handoff drafted | ✅ green | `RELEASE_NOTES_ALPHA.md`, `DOCS_HANDOFF_ALPHA.md` |
| Standing holds respected (NPM / Cloudflare / DNS not touched) | ✅ green | No infra changes in `dev` diff |

## Sequence (after PM approval, in order)

```bash
# 0. Final sync check — confirm dev is exactly what's about to merge.
git -C /Users/ndawson/Development/OwnChart fetch origin
git -C /Users/ndawson/Development/OwnChart log --oneline origin/main..origin/dev | wc -l   # expect 67+

# 1. Merge dev → main locally. Fast-forward — no merge commit, clean history.
git -C /Users/ndawson/Development/OwnChart checkout main
git -C /Users/ndawson/Development/OwnChart pull --ff-only origin main
git -C /Users/ndawson/Development/OwnChart merge --ff-only dev

# 2. Tag the release.
git -C /Users/ndawson/Development/OwnChart tag -a v0.1.0-alpha \
  -m "Alpha 0.1.0 — see user-docs/RELEASE_NOTES_ALPHA.md"

# 3. Push main + tag to public GitHub.
git -C /Users/ndawson/Development/OwnChart push origin main
git -C /Users/ndawson/Development/OwnChart push origin v0.1.0-alpha

# 4. Switch back to dev to keep the working branch current.
git -C /Users/ndawson/Development/OwnChart checkout dev
```

If the `merge --ff-only dev` fails (i.e. someone else pushed to main
during release prep), STOP. Don't force-merge. Investigate the
diverging commit before continuing.

## Demo redeploy

```bash
# 5. Demo lives on Maverick at /home/administrator/ownchart-demo (port 9988).
#    Rebuild from main (not dev).
ssh administrator@maverick.dzsec.net 'cd /home/administrator/ownchart-demo && git fetch origin && git checkout main && git pull --ff-only origin main && bash infra/deploy-demo.sh'

# 6. Smoke check — open demo.ownchart.me in a browser and run the four canonical questions:
#    a. "When did I get my COVID vaccine?"       — expect 3 mRNA doses, 2021
#    b. "What changed around starting lisinopril?" — expect HTN dx → Rx → BP trend
#    c. "How has my sleep looked recently?"      — expect 14 nights, May 2026
#    d. "What does my record say about blood pressure?" — expect 12 readings, pre/post Rx
```

The demo DB gets wiped on each deploy (idempotent seed re-runs against
empty source list per `demo_data_seed.py:68`). If the seed bundle
changed (it did — Avery Walker now), the demo will re-ingest on
restart.

If any of the four questions fails (LLM error / no results / wrong
answer), STOP. Don't roll forward to "ship it"; investigate.

## Rollback

If demo smoke fails or main has a critical regression discovered
post-push:

```bash
# Local: reset main to the commit before merge.
git -C /Users/ndawson/Development/OwnChart checkout main
git -C /Users/ndawson/Development/OwnChart reset --hard <pre-merge-sha>
git -C /Users/ndawson/Development/OwnChart push --force-with-lease origin main

# Drop the tag.
git -C /Users/ndawson/Development/OwnChart tag -d v0.1.0-alpha
git -C /Users/ndawson/Development/OwnChart push origin :refs/tags/v0.1.0-alpha

# Redeploy demo from the previous good main.
ssh administrator@maverick.dzsec.net 'cd /home/administrator/ownchart-demo && git fetch origin && git checkout main && bash infra/deploy-demo.sh'
```

Force-push to main is destructive and PM must specifically authorize
it. Default response to a regression: surface the issue, fix on `dev`,
re-cut.

## Outstanding holds (still in effect)

- ❌ Do NOT touch NPM / Cloudflare / DNS / cert configuration.
- ❌ Do NOT add new product surfaces (calendar, multi-tenant, provider
  OAuth) during the merge window.
- ❌ Do NOT redeploy demo before the merge is on main.
- ⏸ dev → main itself is held until PM go.

## What's NOT in this release (Docs needs to know)

See `RELEASE_NOTES_ALPHA.md` § "Known issues / deferred to beta" — the
two P1s (PDF date_start, clinical_note historical_undated) and the
P2 (FHIR Immunization → procedure mapping) are known and tracked.
Public demo conversations are written to the shared demo account
(caveat in `DOCS_HANDOFF_ALPHA.md`).
