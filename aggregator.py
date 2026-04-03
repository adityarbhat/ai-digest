"""
IMAI AI Digest — aggregator.py
Runs daily via Render cron. Fetches new blog posts + arXiv papers (both daily),
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
import time
from email.mime.text import MIMEText

import feedparser
import requests
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

# ── Sources ────────────────────────────────────────────────────────────────────

BLOGS = [
    # Anthropic news (anthropic.com has no RSS — using Google News)
    ("Anthropic",       "https://news.google.com/rss/search?q=Anthropic+Claude+AI&hl=en-US&gl=US&ceid=US:en"),
    # Frontier labs — know what competitors ship
    ("OpenAI",          "https://openai.com/blog/rss.xml"),
    ("Google DeepMind", "https://deepmind.google/blog/rss.xml"),
    ("Meta AI",         "https://news.google.com/rss/search?q=Meta+AI+Llama+agents&hl=en-US&gl=US&ceid=US:en"),
    ("Microsoft Research", "https://www.microsoft.com/en-us/research/feed/"),
    # Deep technical / practitioner blogs
    ("Simon Willison",  "https://simonwillison.net/atom/everything/"),
    ("Lilian Weng",     "https://lilianweng.github.io/index.xml"),       # OpenAI research lead, deep technical
    ("Chip Huyen",      "https://huyenchip.com/feed.xml"),               # ML systems & production AI
    ("HuggingFace",     "https://huggingface.co/blog/feed.xml"),         # model releases, techniques, papers
    # University & top research lab blogs (paper summaries in plain language)
    ("BAIR Blog",       "https://bair.berkeley.edu/blog/feed.xml"),      # Berkeley AI Research
    ("Google Research", "https://blog.research.google/feeds/posts/default?alt=rss"),
    ("Stanford HAI",    "https://hai.stanford.edu/news/feed"),           # Stanford Human-Centered AI
    # Application-company engineering blogs (agentic / product AI)
    ("Replit",          "https://blog.replit.com/rss.xml"),              # AI-native dev tools
    ("Weights & Biases", "https://wandb.ai/fully-connected/rss.xml"),    # ML tooling & evals
    # Tech & economic analysis
    ("MIT Tech Review", "https://www.technologyreview.com/feed/"),
    # System design & engineering depth
    ("Cloudflare Blog",      "https://blog.cloudflare.com/rss/"),
    ("GitHub Engineering",   "https://github.blog/engineering.atom"),
    # Community signal (high-quality only)
    ("Hacker News",     "https://hnrss.org/best"),
]

# arXiv: applied LLM / agent / computer use topics — practitioner-relevant only
ARXIV_QUERY = (
    "(cat:cs.AI OR cat:cs.CL OR cat:cs.HC) AND ("
    "ti:agent OR ti:\"multi-agent\" OR ti:\"computer use\" OR ti:\"computer agent\" OR "
    "ti:\"web agent\" OR ti:\"GUI agent\" OR ti:\"tool use\" OR ti:\"tool calling\" OR "
    "ti:RAG OR ti:\"retrieval augmented\" OR ti:\"model context protocol\" OR "
    "ti:\"chain-of-thought\" OR ti:\"in-context learning\" OR ti:\"prompt\" OR "
    "ti:\"context window\" OR ti:\"agentic\" OR ti:\"autonomous agent\" OR "
    "ti:\"AI assistant\" OR ti:\"code generation\" OR ti:\"function calling\")"
)

BLOG_SCORE_PROMPT = """\
You score blog articles for Adi. He is an AI consultant who works exclusively in the
Claude/Anthropic ecosystem — building agents, RAG pipelines, MCP servers, and data
dashboards for SMB clients. He does NOT use LangChain, LlamaIndex, or other orchestration
frameworks. He is NOT a researcher and does NOT build or train models.

He cares about:
1. What Anthropic ships (new Claude features, API changes, MCP updates) — top priority
2. Frontier AI capabilities: new agent techniques, computer use, tool use, multimodal
3. Applied AI techniques he can use with Claude (prompting, evals, RAG patterns, MCP)
4. Research distillations: a blog post explaining a new paper in plain terms
5. System design: how large-scale systems are built (relevant to building AI pipelines)
6. Big-picture: economic impact of AI, industry surveys, future-of-work analysis

PENALIZE HEAVILY: journalism and news reporting.
- "Company X announces Y" style articles from WIRED, NBC, TechCrunch → 3-4 max
- "According to sources..." reporting → 2-3
- Product launch coverage with no technical depth → 3-4
Adi gets these from newsletters already. He wants INSIGHT and TECHNIQUE.

REWARD: technical practitioners writing about what they built or discovered.
- "I tested X and here's what I found" → 7-8 if relevant
- Deep analysis of how a new technique works → 7-9
- Research paper explained in plain terms → 6-8

9-10  MUST READ — New Claude/Anthropic feature, API change, or MCP advance.
      Major model launch from any frontier lab with technical details.
      Practitioner deep-dive on agent/computer-use/tool-use technique.
7-8   WORTH READING — Applied AI technique Adi can use. Research distillation.
      System design pattern applicable to AI pipelines. Practitioner analysis.
6     INTERESTING — Technical blog post, deep product analysis, engineering write-up.
4-5   BORDERLINE — News coverage with some depth. Opinion pieces.
1-3   SKIP — Pure journalism/news reporting with no technical content.
      Cloud provider tutorials. Life sciences, robotics, pure ML theory.
      Hiring/partnership announcements.

Scoring traps to avoid:
- WIRED/NBC/TechCrunch article about Claude = score the depth, NOT the subject. Usually 3-4.
- A post from Anthropic/OpenAI is NOT automatically a 9. Score the CONTENT.
- "How to do X on AWS/Azure/GCP" is a 1-3 tutorial.
- LangChain/LlamaIndex/CrewAI content is a 3-4 max — Adi doesn't use these.

Return ONLY a JSON array. Each item: {"index": N, "score": 1-10, "reason": "max 12 words"}
"""

PAPER_SCORE_PROMPT = """\
You score arXiv papers for Adi. He builds Claude-based agents, RAG pipelines, MCP servers,
and data dashboards for SMB clients. He is NOT a researcher and does NOT build models.
He will only skim these, so they must be immediately useful to a practitioner.

Target: 1-3 papers per day at 7+. Adi wants to stay current with frontier research
on agents and computer use — even if he won't implement every detail.

8-10  MUST SKIM — New technique for agents, computer use, GUI agents, or tool use.
      Better RAG or in-context learning approach. New finding on how LLMs reason/plan.
      Practical benchmark showing what actually works in agentic systems.
7     WORTH A LOOK — Interesting applied result, clear takeaway even if indirect.
      New approach to prompt engineering, context management, or agent coordination.
5-6   MAYBE — Applied AI result, relevant topic but unclear practical use.
1-4   SKIP — Math-heavy. Theoretical proofs. Pure training/pretraining methods.
      Model architecture research. Computer vision, robotics, life sciences.
      Benchmarks on standard NLP tasks with no agent/tool-use angle.

Scoring traps to avoid:
- If abstract has equations, Greek letters, or theorems → score 1-3.
- "We prove that..." or "We derive bounds..." → score 1-2.
- Papers about training/pretraining → score 1-3 (Adi doesn't train models).
- Inference optimization (quantization, speculative decoding, KV cache) → score 2-3.

Return ONLY a JSON array. Each item: {"index": N, "score": 1-10, "reason": "max 12 words"}
"""

MAX_PAPERS = 3  # daily cap — quality over quantity

REQUIRED_ENV = ["ANTHROPIC_API_KEY"]
SMTP_ENV = ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "DIGEST_TO"]


def check_env():
    missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
    if missing:
        sys.exit(f"❌ Missing required env vars: {', '.join(missing)}")
    smtp_missing = [v for v in SMTP_ENV if not os.environ.get(v)]
    if smtp_missing:
        print(f"⚠ Missing SMTP env vars (email will be skipped): {', '.join(smtp_missing)}")
    return len(smtp_missing) == 0


client = None


def get_client():
    global client
    if client is None:
        client = Anthropic()
    return client


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


def fetch_arxiv(days_back=2):
    try:
        r = requests.get(
            "http://export.arxiv.org/api/query",
            params={"search_query": ARXIV_QUERY, "sortBy": "submittedDate",
                    "sortOrder": "descending", "max_results": 30},
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

def score(articles, prompt):
    if not articles:
        return []
    results = []
    for i in range(0, len(articles), 10):
        batch = articles[i:i+10]
        payload = [{"index": j, "title": a["title"], "source": a["source"], "summary": a["summary"]}
                   for j, a in enumerate(batch)]
        scores = {}
        for attempt in range(3):
            try:
                resp = get_client().messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=800,
                    system=prompt,
                    messages=[{"role": "user", "content": json.dumps(payload)}],
                )
                text = resp.content[0].text.strip()
                # Strip markdown code fences if present
                if text.startswith("```"):
                    text = text.split("\n", 1)[1]  # drop first line (```json or ```)
                if text.endswith("```"):
                    text = text.rsplit("```", 1)[0]
                scores = {r["index"]: r for r in json.loads(text.strip())}
                break
            except Exception as ex:
                print(f"  ⚠ scoring (attempt {attempt+1}/3): {ex}")
                if attempt < 2:
                    time.sleep(2 ** attempt)
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

    def format_items(items):
        for a in items:
            emoji = "📰" if a["type"] == "Blog" else "📄"
            lines.append(f"  {emoji} [{a['score']}/10] {a['title']}")
            lines.append(f"  {a['source']} · {a['published']}")
            lines.append(f"  {a['url']}")
            if a.get("reason"):
                lines.append(f"  → {a['reason']}")
            lines.append("")

    # Blog posts (daily) — score >= 7, fallback to 6, then top-5 regardless
    top_blogs = [a for a in blogs if a["score"] >= 7]
    if not top_blogs:
        top_blogs = [a for a in blogs if a["score"] >= 6]
    if not top_blogs:
        top_blogs = blogs[:5]  # always show something
    top_blogs = top_blogs[:10]

    if top_blogs:
        lines.append(f"📰 Blog Posts ({len(top_blogs)}):\n")
        format_items(top_blogs)
    else:
        lines.append("No notable blog posts today.\n")

    # Papers (daily) — score >= 7, hard cap at MAX_PAPERS
    top_papers = [a for a in papers if a["score"] >= 7]
    top_papers = top_papers[:MAX_PAPERS]

    if top_papers:
        lines.append(f"\n📄 Research Papers ({len(top_papers)}):\n")
        format_items(top_papers)
    elif papers:
        lines.append("\nNo notable papers today.\n")

    return "\n".join(lines)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    today = datetime.date.today()
    is_friday = today.weekday() == 4
    force_all = "--all" in sys.argv
    force_blogs = "--blogs" in sys.argv
    force_arxiv = "--arxiv" in sys.argv

    run_blogs = force_all or force_blogs or (not force_arxiv)
    run_arxiv = force_all or force_arxiv or True  # run daily, not just Fridays

    can_email = check_env()
    print(f"Running digest — blogs: {run_blogs}, arXiv: {run_arxiv}")

    blogs, papers = [], []

    if run_blogs:
        print("Fetching blogs...")
        raw = fetch_blogs()
        print(f"  {len(raw)} new articles found, scoring...")
        blogs = score(raw, BLOG_SCORE_PROMPT)

    if run_arxiv:
        print("Fetching arXiv...")
        raw = fetch_arxiv()
        print(f"  {len(raw)} new papers found, scoring...")
        papers = score(raw, PAPER_SCORE_PROMPT)

    total = len(blogs) + len(papers)
    print(f"Digest ready: {len(blogs)} blog posts, {len(papers)} papers")

    body = build_body(blogs, papers)
    subject = f"AI Digest {today} — {total} items"

    # Print to stdout (visible in Render logs) and email
    print("\n" + body)

    if can_email:
        try:
            send_email(subject, body)
            print("✅ Email sent.")
        except Exception as ex:
            print(f"⚠ Email failed: {ex}")
    else:
        print("⚠ Skipping email (SMTP env vars not set).")


if __name__ == "__main__":
    main()
