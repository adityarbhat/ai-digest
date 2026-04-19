# IMAI AI Digest

Checks Claude + frontier AI lab blogs daily and arXiv weekly, enriches top candidates with article text,
deduplicates overlapping coverage, remembers what it already sent, and emails a consultant-focused digest every morning at 6 AM MDT.

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
5. Done — runs every morning at 6 AM MDT

## What changed

- Directly monitors [claude.com/blog](https://claude.com/blog) so Claude Code / Cowork posts don't get missed
- Deduplicates near-identical stories before and after scoring
- Biases ranking toward short, applied, high-signal reads instead of generic AI news
- Caps repeated sources so the digest feels curated instead of repetitive
- Fetches article text for the strongest candidates before scoring so titles alone don't dominate
- Tracks sent items in `data/sent_items.json` so the digest avoids repeating stories across days
- Scores items for consultant value, not just reading quality
- Splits the digest into `Client-Relevant Now`, `Build Patterns`, `Experiments To Run`, and `Strategic Signals`

## Schedule

- **Daily (Mon–Sun):** Blog posts from the last 28 hours, with Claude blog coverage expanded to catch recent posts reliably
- **Fridays only:** arXiv papers from the last 7 days (max 4, scored 8+/10 only)

## Sources

Blogs: Claude Blog, Anthropic, OpenAI, Google DeepMind, Meta AI, Mistral,
       Simon Willison, The Batch (deeplearning.ai), MIT Tech Review,
       Brookings AI, a16z, Martin Fowler, InfoQ Architecture, Render Blog,
       Supabase Engineering, Supabase Developers, pganalyze,
       Hacker News (AI, 100+ pts), Ars Technica, The Verge AI

arXiv: cs.AI / cs.CL / cs.LG filtered for agents, RAG, MCP, LLM engineering,
       enterprise AI, tool use, context management

## OpenAI Quick Links

- OpenAI Blog: [https://openai.com/blog](https://openai.com/blog)
- ChatGPT Release Notes: [https://help.openai.com/en/articles/6825453-chatgpt-release-notes](https://help.openai.com/en/articles/6825453-chatgpt-release-notes)
- Codex Overview: [https://openai.com/codex](https://openai.com/codex)
- Introducing Codex: [https://openai.com/index/introducing-codex/](https://openai.com/index/introducing-codex/)
- Using Codex with your ChatGPT plan: [https://help.openai.com/en/articles/11369540-codex-in-chatgpt](https://help.openai.com/en/articles/11369540-codex-in-chatgpt)
- Codex Academy: [https://openai.com/academy/codex/](https://openai.com/academy/codex/)
- How OpenAI uses Codex: [https://openai.com/business/guides-and-resources/how-openai-uses-codex/](https://openai.com/business/guides-and-resources/how-openai-uses-codex/)
- Prompt engineering best practices for ChatGPT: [https://help.openai.com/en/articles/10032626-prompt-engineering-best-practices-for-chatgpt](https://help.openai.com/en/articles/10032626-prompt-engineering-best-practices-for-chatgpt)
