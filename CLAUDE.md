# AI Digest — CLAUDE.md

## What this project does
Daily email digest: fetches RSS feeds → scores with Claude Haiku → emails ranked results. Runs on Render cron at 12:00 UTC (6 AM MDT).

## Last major change (2026-06-10)
Follow-up fix (commit 986c06c): watchlist titles were "Read more" card buttons —
real titles now rescued from the article page (og:title → <title> → h1 → URL slug,
site suffix stripped); 404/410 links skipped everywhere; articles featured in
Consultant Intelligence no longer repeat as Frontier Watch headlines in the same email.

Fixed daily staleness and retargeted relevance:
- **State now persists via GitHub commits.** Render cron filesystems are ephemeral, so
  `data/sent_items.json` was wiped every run — dedupe never worked in production.
  `load_state()`/`save_state()` now read/write the file through the GitHub Contents API
  (one `[skip render]` commit per day after the email sends). Requires `GITHUB_TOKEN`
  (fine-grained PAT, Contents read/write), `GITHUB_REPO`, `GITHUB_BRANCH` env vars.
  Falls back to the local file when no token is set (local dev).
- **Frontier watchlist deduped**: headline URLs are recorded in `watchlist_seen` and never
  reshown within 45 days. Anthropic /news fallback now requires real article slugs.
- **Recency window 24h → 36h** so posts published right after yesterday's run aren't missed
  (sent-state dedupe makes the overlap safe).
- **New sources**: Addy Osmani, Meta AI / DoorDash / Streamlit Blog via Google News RSS
  (direct feeds 403/404), Spotify, Slack, Pinterest, Dropbox, Stripe, FastAPI releases atom,
  Airbnb switched to AI-tagged feed.
- **Engineering blogs are AI-gated**: `ENGINEERING_AI_GATED_SOURCES` only pass posts matching
  `AI_TOPIC_PATTERN` (word-boundary regex) or stack keywords. `STACK_UPDATE_SOURCES`
  (Supabase, Neon, pganalyze, Render, Streamlit, FastAPI) always pass.
- **Papers**: up to 2/day at score 7+ (was 1 at 8+).
- Scoring prompts updated: Adi = agentic AI coding, harness engineering, software factories,
  MCP, Claude Code/Codex/DeepSeek, moving-industry web apps/dashboards, cloud certs.
  Substacks he already pays for (ByteByteGo, Addy Osmani's Elevate, System Design Newsletter)
  are deliberately NOT sources.

## State invariants (do not silently change)
- `data/sent_items.json` is the single source of truth for "already sent". In production it
  lives in the GitHub repo, NOT the local clone — `[skip render]` commits never reach the
  deployed build, so `load_state()` must always prefer the API when `GITHUB_TOKEN` is set.
- Every emailed item AND every watchlist headline must be marked in state after a successful
  send (`mark_sent` + `mark_watchlist_shown`), then `save_state(state)` (remote push).
- The mid-run `save_state(state, push_remote=False)` is local-only by design: one GitHub
  commit per day, after the email.

## Key env vars
`ANTHROPIC_API_KEY`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `DIGEST_TO`,
`GITHUB_TOKEN`, `GITHUB_REPO`, `GITHUB_BRANCH`

## Run manually
```
python aggregator.py          # blogs + arXiv (daily default)
python aggregator.py --all    # blogs + arXiv
python aggregator.py --arxiv  # arXiv only
```

## Scoring calibration (ongoing)
No forced volume target: quality over quantity. 1-3 high-value items on a slow day is fine,
zero is acceptable. Never pad with below-threshold picks (the old score>=5 fallback was
removed deliberately — do not reintroduce it). Papers: up to 2/day at 7+.
Scoring prompts: `BLOG_SCORE_PROMPT`, `PAPER_SCORE_PROMPT`.
