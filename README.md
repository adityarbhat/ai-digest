# IMAI AI Digest

Checks frontier AI lab blogs daily + arXiv weekly, scores articles with Claude,
and emails you a ranked digest every morning at 7 AM MT.

## What you get in your inbox

```
[9/10] Anthropic Releases Claude Tool Use API Update
       Anthropic · Jun 10
       https://anthropic.com/...
       → New tool-use feature directly applicable to agentic client work.

[7/10] How Meta Uses LLMs for Internal Knowledge Management
       Meta AI · Jun 10
       https://ai.meta.com/...
       → Real enterprise deployment case study, useful consulting context.
```

## Local test

```bash
pip install -r requirements.txt

export ANTHROPIC_API_KEY=sk-...
export SMTP_HOST=smtp.gmail.com
export SMTP_PORT=587
export SMTP_USER=you@gmail.com
export SMTP_PASS=your-gmail-app-password   # NOT your login password
export DIGEST_TO=you@gmail.com

python aggregator.py          # normal run (blogs daily, arXiv on Mondays)
python aggregator.py --all    # force both right now
python aggregator.py --blogs  # blogs only
python aggregator.py --arxiv  # arXiv only
```

## Gmail app password setup

1. Enable 2FA on your Google account
2. Go to myaccount.google.com → Security → App Passwords
3. Create one for "Mail" → copy the 16-char password
4. Use that as SMTP_PASS (not your normal Gmail password)

## Deploy to Render ($7/month)

1. Push this folder to a GitHub repo
2. Go to render.com → New → Blueprint → connect your repo
3. Render reads render.yaml automatically
4. In the Render dashboard, set these environment variables:
   - ANTHROPIC_API_KEY
   - SMTP_USER
   - SMTP_PASS
   - DIGEST_TO
5. Done — runs every morning at 7 AM MT

## Schedule

- **Daily (Mon–Sun):** Blog posts from the last 28 hours (short reads preferred)
- **Fridays only:** arXiv papers from the last 7 days (max 4, scored 8+/10 only)

## Sources

Blogs: Anthropic, OpenAI, Google DeepMind, Meta AI, Mistral,
       Simon Willison, The Batch (deeplearning.ai), MIT Tech Review,
       Brookings AI, a16z, Hacker News (AI, 100+ pts), Ars Technica, The Verge AI

arXiv: cs.AI / cs.CL / cs.LG filtered for agents, RAG, MCP, LLM engineering,
       enterprise AI, tool use, context management
