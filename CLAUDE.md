# AI Digest — CLAUDE.md

## What this project does
Daily email digest: fetches RSS feeds → scores with Claude Haiku → emails ranked results. Runs on Render cron.

## Last commit: (2026-04-03)
Overhauled sources and scoring: removed generic journalism (TechCrunch, Verge, Ars Technica),
added practitioner/research blogs (Lilian Weng, Chip Huyen, HuggingFace, BAIR, Google Research,
Stanford HAI, Meta AI, Microsoft Research, Replit, W&B). arXiv now runs daily (not just Fridays).
Scoring now heavily penalizes journalism and rewards technical insight.

## Active RSS sources
- Anthropic, Meta AI → Google News RSS
- OpenAI, Google DeepMind, Microsoft Research
- Simon Willison, Lilian Weng, Chip Huyen, HuggingFace (practitioner blogs)
- BAIR Blog, Google Research, Stanford HAI (university research blogs)
- Replit, Weights & Biases (application-company engineering)
- MIT Tech Review, Cloudflare Blog, GitHub Engineering
- Hacker News /best

## arXiv
- Runs **daily** (was Fridays-only), 2-day lookback window, max 30 papers fetched
- Shows top 3 papers scored 7+ (was 4 papers at 8+)
- Query covers: agents, computer use, GUI agents, tool use, RAG, MCP, chain-of-thought, code gen

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
