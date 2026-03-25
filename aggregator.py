"""
IMAI AI Digest — aggregator.py
Runs daily via Render cron. Fetches new blog posts (daily) + arXiv papers (Mondays),
scores them with Claude, emails a ranked digest.

Env vars required:
    ANTHROPIC_API_KEY
    SMTP_HOST       e.g. smtp.gmail.com
    SMTP_PORT       e.g. 587
    SMTP_USER       your Gmail address
    SMTP_PASS       Gmail app password (not your login password)
    DIGEST_TO       email to send digest to (can be same as SMTP_USER)
"""

import datetime
import json
import os
import smtplib
import sys
from email.mime.text import MIMEText

import feedparser
import requests
from anthropic import Anthropic

# ── Sources ────────────────────────────────────────────────────────────────────

BLOGS = [
    ("Anthropic",       "https://www.anthropic.com/rss.xml"),
    ("OpenAI",          "https://openai.com/blog/rss.xml"),
    ("Google DeepMind", "https://deepmind.google/blog/rss.xml"),
    ("Meta AI",         "https://ai.meta.com/blog/rss/"),
    ("Mistral AI",      "https://mistral.ai/news/rss"),
    ("Hugging Face",    "https://huggingface.co/blog/feed.xml"),
    ("LangChain",       "https://blog.langchain.dev/rss/"),
    ("AWS ML Blog",     "https://aws.amazon.com/blogs/machine-learning/feed/"),
    ("Microsoft AI",    "https://blogs.microsoft.com/ai/feed/"),
]

# arXiv: applied LLM / agent / RAG / MCP topics only
ARXIV_QUERY = (
    "(cat:cs.AI OR cat:cs.CL OR cat:cs.LG) AND ("
    "ti:agent OR ti:RAG OR ti:\"retrieval augmented\" OR ti:\"large language model\" OR "
    "ti:\"tool use\" OR ti:\"model context protocol\" OR ti:\"prompt\" OR "
    "ti:\"context window\" OR ti:\"AI application\" OR ti:\"enterprise\" OR "
    "ti:\"fine-tuning\" OR ti:\"autonomous\" OR ti:\"agentic\")"
)

SCORE_PROMPT = """You score articles for Adi, an AI consultant helping businesses adopt AI (not a researcher).

HIGH (8-10): New LLM features/products, applied agent/RAG/MCP techniques, AI for business workflows,
             economic impact studies, AI engineering best practices, new model capabilities.
MED  (5-7):  General AI news, model benchmarks, open-source releases, AI policy with biz implications.
LOW  (1-4):  Pure ML theory, life sciences/biotech AI, robotics, computer vision, social science.

Return ONLY a JSON array. Each item: {"index": N, "score": 1-10, "reason": "max 12 words"}
"""

client = Anthropic()


# ── Fetch ──────────────────────────────────────────────────────────────────────

def fetch_blogs(hours_back=28):
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=hours_back)
    articles = []
    for name, rss in BLOGS:
        try:
            feed = feedparser.parse(rss)
            for e in feed.entries[:20]:
                url = e.get("link", "")
                if not url:
                    continue
                pub = None
                for attr in ("published_parsed", "updated_parsed"):
                    val = getattr(e, attr, None)
                    if val:
                        pub = datetime.datetime(*val[:6])
                        break
                if pub and pub < cutoff:
                    continue
                articles.append({
                    "source": name, "type": "Blog",
                    "title": e.get("title", "").strip(), "url": url,
                    "summary": e.get("summary", "")[:400],
                    "published": pub.strftime("%b %d") if pub else "?",
                })
        except Exception as ex:
            print(f"  ⚠ {name}: {ex}")
    return articles


def fetch_arxiv(days_back=7):
    try:
        r = requests.get(
            "http://export.arxiv.org/api/query",
            params={"search_query": ARXIV_QUERY, "sortBy": "submittedDate",
                    "sortOrder": "descending", "max_results": 40},
            timeout=20,
        )
        r.raise_for_status()
    except Exception as ex:
        print(f"  ⚠ arXiv: {ex}")
        return []

    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=days_back)
    papers = []
    for e in feedparser.parse(r.text).entries:
        url = e.get("id", e.get("link", ""))
        if not url:
            continue
        pub = None
        if getattr(e, "published_parsed", None):
            pub = datetime.datetime(*e.published_parsed[:6])
        if pub and pub < cutoff:
            continue
        papers.append({
            "source": "arXiv", "type": "Paper",
            "title": e.get("title", "").replace("\n", " ").strip(), "url": url,
            "summary": e.get("summary", "")[:400],
            "published": pub.strftime("%b %d") if pub else "?",
        })
    return papers


# ── Score ──────────────────────────────────────────────────────────────────────

def score(articles):
    if not articles:
        return []
    results = []
    for i in range(0, len(articles), 10):
        batch = articles[i:i+10]
        payload = [{"index": j, "title": a["title"], "source": a["source"], "summary": a["summary"]}
                   for j, a in enumerate(batch)]
        try:
            resp = client.messages.create(
                model="claude-opus-4-5",
                max_tokens=800,
                system=SCORE_PROMPT,
                messages=[{"role": "user", "content": json.dumps(payload)}],
            )
            scores = {r["index"]: r for r in json.loads(resp.content[0].text)}
        except Exception as ex:
            print(f"  ⚠ scoring: {ex}")
            scores = {}
        for j, a in enumerate(batch):
            a["score"] = scores.get(j, {}).get("score", 5)
            a["reason"] = scores.get(j, {}).get("reason", "")
            results.append(a)
    return sorted(results, key=lambda x: x["score"], reverse=True)


# ── Email ──────────────────────────────────────────────────────────────────────

def send_email(subject, body):
    msg = MIMEText(body, "plain")
    msg["Subject"] = subject
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = os.environ["DIGEST_TO"]
    with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ["SMTP_PORT"])) as s:
        s.starttls()
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.send_message(msg)


def build_body(blogs, papers):
    today = datetime.date.today().strftime("%A, %b %d %Y")
    lines = [f"IMAI AI Digest — {today}\n{'='*50}\n"]

    if blogs:
        lines.append(f"📰 BLOG ARTICLES ({len(blogs)} new)\n")
        for a in blogs:
            lines.append(f"  [{a['score']}/10] {a['title']}")
            lines.append(f"  {a['source']} · {a['published']}")
            lines.append(f"  {a['url']}")
            if a.get("reason"):
                lines.append(f"  → {a['reason']}")
            lines.append("")
    else:
        lines.append("📰 No new blog articles in the last 24 hours.\n")

    if papers:
        lines.append(f"\n📄 ARXIV PAPERS ({len(papers)} new)\n")
        for a in papers:
            lines.append(f"  [{a['score']}/10] {a['title']}")
            lines.append(f"  {a['published']}")
            lines.append(f"  {a['url']}")
            if a.get("reason"):
                lines.append(f"  → {a['reason']}")
            lines.append("")

    return "\n".join(lines)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    today = datetime.date.today()
    is_monday = today.weekday() == 0
    force_all = "--all" in sys.argv
    force_blogs = "--blogs" in sys.argv
    force_arxiv = "--arxiv" in sys.argv

    run_blogs = force_all or force_blogs or (not force_arxiv)
    run_arxiv = force_all or force_arxiv or is_monday

    print(f"Running digest — blogs: {run_blogs}, arXiv: {run_arxiv}")

    blogs, papers = [], []

    if run_blogs:
        print("Fetching blogs...")
        raw = fetch_blogs()
        print(f"  {len(raw)} new articles found, scoring...")
        blogs = score(raw)

    if run_arxiv:
        print("Fetching arXiv...")
        raw = fetch_arxiv()
        print(f"  {len(raw)} new papers found, scoring...")
        papers = score(raw)
        # Only surface papers scoring 6+ to keep it manageable
        papers = [p for p in papers if p["score"] >= 6]

    total = len(blogs) + len(papers)
    print(f"Digest ready: {len(blogs)} blog posts, {len(papers)} papers")

    body = build_body(blogs, papers)
    subject = f"AI Digest {today} — {total} items"

    # Print to stdout (visible in Render logs) and email
    print("\n" + body)

    try:
        send_email(subject, body)
        print("✅ Email sent.")
    except Exception as ex:
        print(f"⚠ Email failed (check SMTP env vars): {ex}")


if __name__ == "__main__":
    main()
