# AI Digest — CLAUDE.md

## What this project does
Daily email digest: fetches RSS feeds → scores with Claude Haiku → emails ranked results. Runs on Render cron.

## Last commit: 46a986c (2026-03-30)
Fixed broken RSS feeds (6 sources were 404), added system design sources, loosened scoring.

## Active RSS sources
- Anthropic → Google News RSS (anthropic.com has no RSS feed)
- OpenAI, Google DeepMind
- Simon Willison, TechCrunch AI, MIT Tech Review, Ars Technica, The Verge AI
- Cloudflare Blog, GitHub Engineering, Netflix Tech Blog (system design)
- Hacker News /best

## Key env vars
`ANTHROPIC_API_KEY`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `DIGEST_TO`

## Run manually
```
python aggregator.py          # blogs only (default)
python aggregator.py --all    # blogs + arXiv
python aggregator.py --arxiv  # arXiv only (normally Fridays-only)
```

## Scoring calibration (ongoing)
Target: 5-10 articles/day at score 6+. Fallback: show top-5 if nothing scores >=6.
User is still calibrating — scoring prompt is in `BLOG_SCORE_PROMPT`.
