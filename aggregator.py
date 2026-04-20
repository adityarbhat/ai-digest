"""
IMAI AI Digest — aggregator.py
Runs daily via Render cron. Fetches new blog posts + arXiv papers (both daily),
scores them with Claude, emails a ranked digest.

Env vars required:
    ANTHROPIC_API_KEY
    SMTP_HOST       e.g. smtp.zoho.com
    SMTP_PORT       e.g. 587
    SMTP_USER       your Gmail address
    SMTP_PASS       Gmail app password (not your login password)
    DIGEST_TO       email to send digest to (can be same as SMTP_USER)
"""

import datetime
import hashlib
import html
import json
import os
import re
import smtplib
import sys
import time
from difflib import SequenceMatcher
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urljoin, urlparse

import feedparser
import requests
from anthropic import Anthropic
from bs4 import BeautifulSoup
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
    ("Brookings AI",    "https://www.brookings.edu/topic/artificial-intelligence/feed/"),
    ("a]6z",            "https://a16z.com/feed/"),
    # Architecture / distributed systems / software delivery
    ("Martin Fowler",   "https://martinfowler.com/feed.atom"),
    ("InfoQ Architecture Articles", "https://feed.infoq.com/architecture/articles/"),
    ("InfoQ Architecture News", "https://feed.infoq.com/architecture/news/"),
    # Community signal (high-quality only)
    ("Hacker News",     "https://hnrss.org/best"),
]

HTML_SOURCES = [
    {
        "source": "Render Blog",
        "url": "https://render.com/blog",
        "path_prefixes": ("/blog/",),
    },
    {
        "source": "Supabase Engineering",
        "url": "https://supabase.com/blog/categories/engineering",
        "path_prefixes": ("/blog/",),
    },
    {
        "source": "Supabase Developers",
        "url": "https://supabase.com/blog/categories/developers",
        "path_prefixes": ("/blog/",),
    },
    {
        "source": "pganalyze",
        "url": "https://pganalyze.com/blog",
        "path_prefixes": ("/blog/",),
    },
]

OFFICIAL_UPDATE_PAGES = [
    {
        "source": "Anthropic Docs",
        "title": "Anthropic Release Notes",
        "url": "https://docs.anthropic.com/en/release-notes/overview",
        "lane": "Client-Relevant Now",
    },
    {
        "source": "Anthropic Docs",
        "title": "Claude Code Overview",
        "url": "https://docs.anthropic.com/en/docs/claude-code/overview",
        "lane": "Build Patterns",
    },
    {
        "source": "Anthropic Docs",
        "title": "Claude Code Tutorials",
        "url": "https://docs.anthropic.com/en/docs/claude-code/tutorials",
        "lane": "Experiments To Run",
    },
    {
        "source": "OpenAI Help",
        "title": "ChatGPT Release Notes",
        "url": "https://help.openai.com/en/articles/6825453-chatgpt-release-notes",
        "lane": "Client-Relevant Now",
    },
    {
        "source": "OpenAI Help",
        "title": "Using Codex with your ChatGPT plan",
        "url": "https://help.openai.com/en/articles/11369540-codex-in-chatgpt",
        "lane": "Client-Relevant Now",
    },
    {
        "source": "OpenAI Help",
        "title": "Prompt engineering best practices for ChatGPT",
        "url": "https://help.openai.com/en/articles/10032626-prompt-engineering-best-practices-for-chatgpt",
        "lane": "Build Patterns",
    },
    {
        "source": "OpenAI",
        "title": "Codex Overview",
        "url": "https://openai.com/codex",
        "lane": "Build Patterns",
    },
    {
        "source": "OpenAI Academy",
        "title": "Codex Academy",
        "url": "https://openai.com/academy/codex/",
        "lane": "Experiments To Run",
    },
]

CLAUDE_BLOG_URL = "https://claude.com/blog"
CLAUDE_BLOG_KEYWORDS = (
    "claude code",
    "claude cowork",
    "cowork",
    "managed agents",
    "skills",
    "mcp",
    "subagents",
    "tool use",
    "context",
    "hooks",
)
HIGH_SIGNAL_KEYWORDS = (
    "claude",
    "anthropic",
    "agent",
    "agents",
    "tool use",
    "mcp",
    "skill",
    "skills",
    "workflow",
    "prompt",
    "eval",
    "context",
    "enterprise",
    "architecture",
    "reasoning",
    "system design",
    "distributed system",
    "distributed systems",
    "microservices",
    "event-driven",
    "idempotency",
    "queue",
    "workflow",
    "billing",
    "invoice",
    "inventory",
    "scheduling",
    "warehouse",
    "audit",
    "state machine",
    "python",
    "fastapi",
    "flask",
    "streamlit",
    "supabase",
    "postgres",
    "render",
    "row level security",
    "rls",
    "background job",
    "cron",
    "api design",
)
LOW_SIGNAL_KEYWORDS = (
    "hiring",
    "partnership",
    "funding",
    "award",
    "webinar",
    "event",
    "conference",
    "livestream",
    "recap",
    "newsletter",
    "week in review",
    "kubernetes tutorial",
    "eks",
    "gke",
    "aks",
    "next.js tutorial",
    "react tutorial",
    "typescript tutorial",
)
STACK_KEYWORDS = (
    "python",
    "fastapi",
    "flask",
    "streamlit",
    "supabase",
    "postgres",
    "render",
    "row level security",
    "rls",
    "background job",
    "cron",
    "api",
    "dashboard",
)
SOURCE_PRIORITY = {
    "Claude Blog": 120,
    "Anthropic": 115,
    "Anthropic Docs": 114,
    "OpenAI": 100,
    "OpenAI Help": 99,
    "OpenAI Academy": 98,
    "Google DeepMind": 95,
    "Meta AI": 90,
    "Mistral AI": 88,
    "Render Blog": 87,
    "Supabase Engineering": 87,
    "Supabase Developers": 81,
    "pganalyze": 85,
    "Martin Fowler": 86,
    "InfoQ Architecture Articles": 84,
    "InfoQ Architecture News": 80,
    "Simon Willison": 82,
    "The Batch (deeplearning.ai)": 78,
    "MIT Tech Review": 72,
    "Brookings AI": 70,
    "a]6z": 68,
    "Ars Technica": 60,
    "The Verge AI": 58,
    "Hacker News AI": 45,
}
SOURCE_CAPS = {
    "Claude Blog": 3,
    "Anthropic": 2,
    "Anthropic Docs": 2,
    "OpenAI": 1,
    "OpenAI Help": 2,
    "OpenAI Academy": 1,
    "Google DeepMind": 1,
    "Meta AI": 1,
    "Mistral AI": 1,
    "Render Blog": 1,
    "Supabase Engineering": 2,
    "Supabase Developers": 1,
    "pganalyze": 1,
    "Martin Fowler": 1,
    "InfoQ Architecture Articles": 2,
    "InfoQ Architecture News": 1,
    "Simon Willison": 1,
    "The Batch (deeplearning.ai)": 1,
    "MIT Tech Review": 1,
    "Brookings AI": 1,
    "a]6z": 1,
    "Ars Technica": 1,
    "The Verge AI": 1,
    "Hacker News AI": 1,
}
STOPWORDS = {
    "a", "an", "and", "are", "be", "for", "from", "how", "in", "into", "is",
    "of", "on", "or", "the", "this", "to", "using", "with", "your",
}
DATE_PATTERNS = (
    (
        re.compile(
            r"\b("
            r"January|February|March|April|May|June|July|August|September|October|November|December"
            r")\s+\d{1,2},\s+\d{4}\b"
        ),
        "%B %d, %Y",
    ),
    (
        re.compile(
            r"\b\d{1,2}\s+("
            r"Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
            r")\s+\d{4}\b"
        ),
        "%d %b %Y",
    ),
    (
        re.compile(
            r"\b\d{1,2}\s+("
            r"January|February|March|April|May|June|July|August|September|October|November|December"
            r")\s+\d{4}\b"
        ),
        "%d %B %Y",
    ),
)

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

He is also building larger business systems, including warehouse-management-style software
with inventory, billing, invoicing, scheduling, and operational workflows. He values
system design and distributed systems when they help him design or advise on real software.
His primary implementation stack is Python, Streamlit dashboards, Flask/FastAPI web apps,
Supabase for database/backend/auth/storage, and Render for hosting/deployment.

He cares about:
1. What Anthropic ships (new Claude features, API changes, MCP updates) — top priority
2. Frontier AI capabilities: new agent techniques, computer use, tool use, multimodal
3. Applied AI techniques he can use with Claude (prompting, evals, RAG patterns, MCP)
4. Research distillations: a blog post explaining a new paper in plain terms
5. System design: how large-scale systems are built (relevant to building AI pipelines)
6. Big-picture: economic impact of AI, industry surveys, future-of-work analysis
7. Interesting, high-signal articles he will actually finish in one sitting

PENALIZE HEAVILY: journalism and news reporting.
- "Company X announces Y" style articles from WIRED, NBC, TechCrunch → 3-4 max
- "According to sources..." reporting → 2-3
- Product launch coverage with no technical depth → 3-4
Adi gets these from newsletters already. He wants INSIGHT and TECHNIQUE.

REWARD: technical practitioners writing about what they built or discovered.
- "I tested X and here's what I found" → 7-8 if relevant
- Deep analysis of how a new technique works → 7-9
- Research paper explained in plain terms → 6-8

You are scoring for consultant value, not literary quality. Ask:
"Will this make Adi better at advising, designing, selling, or implementing AI systems?"
"Does this fit his working stack and the kinds of business systems he ships?"

9-10  MUST READ — New Claude/Anthropic feature, API change, or MCP advance.
      Major model launch from any frontier lab with technical details.
      Practitioner deep-dive on agent/computer-use/tool-use technique.
7-8   WORTH READING — Applied AI technique Adi can use. Research distillation.
      System design pattern applicable to AI pipelines or business software.
      Practitioner analysis, workflow, reliability, or distributed-systems lessons.
6     INTERESTING — Technical blog post, deep product analysis, engineering write-up.
4-5   BORDERLINE — News coverage with some depth. Opinion pieces.
1-3   SKIP — Pure journalism/news reporting with no technical content.
      Cloud provider tutorials. Life sciences, robotics, pure ML theory.
      Hiring/partnership announcements.

Scoring traps to avoid:
- WIRED/NBC/TechCrunch article about Claude = score the depth, NOT the subject. Usually 3-4.
- A post from Anthropic/OpenAI is NOT automatically a 9. Score the CONTENT, not the brand.
- If two posts cover the same launch, prefer the original source and score commentary lower.
- "How to do X on AWS/Azure/GCP" is a 1-3 tutorial, not a 7-8 engineering pattern.
- Music/video/image generation is a 4 unless it has a clear business-tool angle.
- LangChain/LlamaIndex/CrewAI content is a 3-4 max — Adi doesn't use these.
- Reward concrete workflow, architecture, tradeoff, or product capability insight.
- Reward lessons that can improve client advising, solution design, or delivery quality.
- Prefer Python-first, Postgres-friendly, managed-stack-friendly patterns.
- Prefer content directly relevant to Streamlit, Flask, FastAPI, Supabase, or Render.
- Down-rank infra-heavy advice that assumes Kubernetes/platform teams unless clearly transferable.

Choose exactly one lane:
- "Client-Relevant Now" = launches, capability shifts, adoption/business implications
- "Build Patterns" = agent workflows, architecture, system design, distributed systems
- "Experiments To Run" = strong candidate for a near-term test or prototype
- "Strategic Signals" = market, policy, adoption, or consulting positioning signal

Return ONLY a JSON array. Each item:
{"index": N, "score": 1-10, "reason": "max 12 words", "lane": "...", "client_value": "max 16 words", "action": "max 16 words"}
"""

PAPER_SCORE_PROMPT = """\
You score arXiv papers for Adi. He builds Claude-based agents, RAG pipelines, MCP servers,
and data dashboards for SMB clients. He is NOT a researcher and does NOT build models.
He will only skim these, so they must be immediately useful to a practitioner.
He also wants actionable engineering research for coding agents, system design, and
distributed systems relevant to real business software.
His stack is Python, Streamlit, Flask/FastAPI, Supabase/Postgres, and Render.

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
- Papers about training/pretraining methods → score 1-3 (Adi doesn't train models).
- Inference optimization (quantization, speculative decoding, KV cache) → score 2-3.
- Reward papers with reusable eval setups, implementation patterns, failure modes, or experiments.
- Prefer research that maps cleanly to Python apps, APIs, Postgres-backed systems, or managed deployments.
- Down-rank work that assumes infra complexity far beyond his current stack unless highly transferable.

Choose exactly one lane:
- "Build Patterns"
- "Experiments To Run"
- "Strategic Signals"

Return ONLY a JSON array. Each item:
{"index": N, "score": 1-10, "reason": "max 12 words", "lane": "...", "client_value": "max 16 words", "action": "max 16 words"}
"""

MAX_PAPERS = 3  # daily cap — quality over quantity
MAX_BLOG_ENRICH = 12
STATE_PATH = Path("data/sent_items.json")
STATE_RETENTION_DAYS = 45
HTTP_HEADERS = {
    "User-Agent": "IMAI-AI-Digest/1.0 (+https://claude.com/blog)",
}

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


def clean_text(value):
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def parse_date(text):
    if not text:
        return None
    for regex, fmt in DATE_PATTERNS:
        match = regex.search(text)
        if not match:
            continue
        try:
            return datetime.datetime.strptime(match.group(0), fmt)
        except ValueError:
            continue
    return None


def now_utc():
    return datetime.datetime.utcnow()


def normalize_title(title):
    title = clean_text(title).lower()
    title = re.sub(r"[^a-z0-9\s]", " ", title)
    return re.sub(r"\s+", " ", title).strip()


def title_tokens(title):
    return {
        token.rstrip("s")
        for token in normalize_title(title).split()
        if len(token) > 2 and token not in STOPWORDS
    }


def source_priority(source):
    return SOURCE_PRIORITY.get(source, 50)


def topical_bonus(article):
    text = f"{article['title']} {article.get('summary', '')}".lower()
    bonus = 0
    bonus += sum(2 for keyword in CLAUDE_BLOG_KEYWORDS if keyword in text)
    bonus += sum(1 for keyword in HIGH_SIGNAL_KEYWORDS if keyword in text)
    bonus += sum(1 for keyword in STACK_KEYWORDS if keyword in text)
    bonus -= sum(2 for keyword in LOW_SIGNAL_KEYWORDS if keyword in text)
    if article["source"] == "Claude Blog":
        bonus += 4
    if article["source"] == "Anthropic":
        bonus += 2
    return bonus


def novelty_penalty(article, state):
    penalty = 0
    title = article["title"]
    key = article_key(article)
    seen = state.get("sent", {})
    if key in seen:
        penalty += 10
    recent_titles = [
        item.get("title", "")
        for item in seen.values()
        if item.get("kind") == article["type"]
    ]
    for old_title in recent_titles[-60:]:
        if not old_title:
            continue
        sim = similarity(title, old_title)
        if sim >= 0.92:
            penalty += 5
        elif sim >= 0.82:
            penalty += 2
    return penalty


def looks_relevant(article):
    text = f"{article['title']} {article.get('summary', '')}".lower()
    if any(keyword in text for keyword in LOW_SIGNAL_KEYWORDS):
        return False
    if article["source"] in {"Claude Blog", "Anthropic", "OpenAI", "Google DeepMind", "Meta AI", "Mistral AI"}:
        return True
    if any(keyword in text for keyword in CLAUDE_BLOG_KEYWORDS):
        return True
    if any(keyword in text for keyword in STACK_KEYWORDS):
        return True
    return any(keyword in text for keyword in HIGH_SIGNAL_KEYWORDS)


def infer_lane(article):
    text = f"{article['title']} {article.get('summary', '')}".lower()
    if article["source"] in {"Claude Blog", "Anthropic", "OpenAI", "Google DeepMind", "Meta AI", "Mistral AI"}:
        return "Client-Relevant Now"
    if any(
        keyword in text
        for keyword in (
            "experiment",
            "benchmark",
            "eval",
            "ablation",
            "test",
            "prototype",
            "verification",
        )
    ):
        return "Experiments To Run"
    if any(
        keyword in text
        for keyword in (
            "adoption",
            "survey",
            "economic",
            "policy",
            "market",
            "future of work",
            "productivity",
        )
    ):
        return "Strategic Signals"
    return "Build Patterns"


def similarity(a, b):
    return SequenceMatcher(None, normalize_title(a), normalize_title(b)).ratio()


def likely_same_story(a, b):
    if normalize_title(a["title"]) == normalize_title(b["title"]):
        return True
    if similarity(a["title"], b["title"]) >= 0.87:
        return True
    tokens_a = title_tokens(a["title"])
    tokens_b = title_tokens(b["title"])
    if len(tokens_a) < 3 or len(tokens_b) < 3:
        return False
    overlap = len(tokens_a & tokens_b) / max(1, min(len(tokens_a), len(tokens_b)))
    key_overlap = (
        any(keyword in normalize_title(a["title"]) for keyword in CLAUDE_BLOG_KEYWORDS) and
        any(keyword in normalize_title(b["title"]) for keyword in CLAUDE_BLOG_KEYWORDS)
    )
    return overlap >= 0.75 and key_overlap


def choose_better_article(current, candidate):
    current_rank = current.get("score", 0) * 10 + topical_bonus(current) + source_priority(current["source"])
    candidate_rank = candidate.get("score", 0) * 10 + topical_bonus(candidate) + source_priority(candidate["source"])
    if candidate_rank > current_rank:
        return candidate
    return current


def dedupe_articles(articles):
    unique = []
    seen_urls = set()
    for article in articles:
        url = article["url"]
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        replaced = False
        for idx, existing in enumerate(unique):
            if likely_same_story(existing, article):
                unique[idx] = choose_better_article(existing, article)
                replaced = True
                break
        if not replaced:
            unique.append(article)
    return unique


def cap_sources(articles, limit):
    kept = []
    per_source = {}
    for article in articles:
        source = article["source"]
        cap = SOURCE_CAPS.get(source, 1)
        if per_source.get(source, 0) >= cap:
            continue
        kept.append(article)
        per_source[source] = per_source.get(source, 0) + 1
        if len(kept) >= limit:
            break
    return kept


def article_key(article):
    parsed = urlparse(article["url"])
    canonical = article.get("version_key") or f"{parsed.netloc}{parsed.path}".rstrip("/")
    if not canonical:
        canonical = normalize_title(article["title"])
    digest = hashlib.sha1(canonical.encode("utf-8")).hexdigest()
    return digest


def load_state():
    if not STATE_PATH.exists():
        return {"sent": {}, "official_pages": {}}
    try:
        with STATE_PATH.open("r", encoding="utf-8") as fh:
            state = json.load(fh)
    except Exception:
        return {"sent": {}, "official_pages": {}}
    if not isinstance(state, dict):
        return {"sent": {}, "official_pages": {}}
    state.setdefault("sent", {})
    state.setdefault("official_pages", {})
    return state


def prune_state(state):
    cutoff = now_utc() - datetime.timedelta(days=STATE_RETENTION_DAYS)
    pruned = {}
    for key, item in state.get("sent", {}).items():
        sent_at = item.get("sent_at")
        if not sent_at:
            pruned[key] = item
            continue
        try:
            sent_dt = datetime.datetime.fromisoformat(sent_at)
        except ValueError:
            pruned[key] = item
            continue
        if sent_dt >= cutoff:
            pruned[key] = item
    state["sent"] = pruned
    official_pages = {}
    for url, item in state.get("official_pages", {}).items():
        updated_at = item.get("updated_at")
        if not updated_at:
            official_pages[url] = item
            continue
        try:
            updated_dt = datetime.datetime.fromisoformat(updated_at)
        except ValueError:
            official_pages[url] = item
            continue
        if updated_dt >= cutoff:
            official_pages[url] = item
    state["official_pages"] = official_pages
    return state


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with STATE_PATH.open("w", encoding="utf-8") as fh:
        json.dump(prune_state(state), fh, indent=2, sort_keys=True)


def mark_sent(state, items):
    sent = state.setdefault("sent", {})
    official_pages = state.setdefault("official_pages", {})
    timestamp = now_utc().isoformat()
    for article in items:
        sent[article_key(article)] = {
            "title": article["title"],
            "url": article["url"],
            "source": article["source"],
            "kind": article["type"],
            "sent_at": timestamp,
        }
        if article.get("is_official_update"):
            official_pages[article["url"]] = {
                "title": article["title"],
                "content_hash": article.get("content_hash"),
                "blocks": article.get("content_blocks", []),
                "updated_at": timestamp,
            }
    return state


def filter_unsent(articles, state):
    seen = state.get("sent", {})
    return [article for article in articles if article_key(article) not in seen]


def extract_article_text(html_text):
    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    container = soup.find("main") or soup.find("article") or soup.body or soup
    text = clean_text(container.get_text(" ", strip=True))
    return text[:1200]


def extract_structured_page_blocks(html_text, limit=160):
    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    container = soup.find("main") or soup.find("article") or soup.body or soup
    selectors = ["h1", "h2", "h3", "h4", "p", "li"]
    blocks = []
    seen = set()
    for node in container.find_all(selectors):
        text = clean_text(node.get_text(" ", strip=True))
        if len(text) < 8:
            continue
        lowered = text.lower()
        if lowered in {"openai", "anthropic", "search", "table of contents"}:
            continue
        if lowered in seen:
            continue
        seen.add(lowered)
        blocks.append(text)
        if len(blocks) >= limit:
            break
    if not blocks:
        fallback = clean_text(container.get_text("\n", strip=True))
        blocks = [line.strip() for line in fallback.splitlines() if len(line.strip()) >= 8][:limit]
    return blocks


def classify_update_change(previous_blocks, current_blocks):
    if not current_blocks:
        return {}

    if not previous_blocks:
        return {
            "change_kind": "Initial snapshot",
            "start_here": current_blocks[0],
            "start_position": "top of page",
            "scan_guidance": "Skim the top sections to get the baseline structure for future diffs.",
            "change_excerpt": current_blocks[0],
        }

    previous_norm = [normalize_title(block) for block in previous_blocks]
    current_norm = [normalize_title(block) for block in current_blocks]
    matcher = SequenceMatcher(None, previous_norm, current_norm)
    changes = [opcode for opcode in matcher.get_opcodes() if opcode[0] != "equal"]
    if not changes:
        return {
            "change_kind": "Minor wording change",
            "start_here": current_blocks[0],
            "start_position": "top of page",
            "scan_guidance": "No clear section shift found; scan the top few paragraphs for copy edits.",
            "change_excerpt": current_blocks[0],
        }

    first_tag, _, _, j1, j2 = changes[0]
    start_idx = min(j1, max(0, len(current_blocks) - 1))
    if first_tag == "delete" and start_idx >= len(current_blocks):
        start_idx = max(0, len(current_blocks) - 1)
    start_here = current_blocks[start_idx]

    if all(tag == "insert" and i1 >= len(previous_blocks) - 1 for tag, i1, _, _, _ in changes):
        change_kind = "Appended new material"
        scan_guidance = "Start at this point and read downward; older sections above are probably unchanged."
    elif all(tag == "insert" for tag, *_ in changes):
        change_kind = "Inserted new section"
        scan_guidance = "Start here, then skim the next few blocks because the new material was inserted mid-page."
    elif any(tag == "replace" for tag, *_ in changes):
        change_kind = "Edited existing section"
        scan_guidance = "Reread this section plus the next 2-3 blocks; the update modified existing guidance."
    else:
        change_kind = "Restructured content"
        scan_guidance = "Use this as the restart point and skim nearby headings because sections moved around."

    percent = int(((start_idx + 1) / max(1, len(current_blocks))) * 100)
    start_position = "top of page" if percent <= 20 else f"around {percent}% into the page"

    changed_blocks = []
    for tag, _, _, block_start, block_end in changes:
        if tag in {"insert", "replace"}:
            changed_blocks.extend(current_blocks[block_start:block_end])
    changed_blocks = [block for block in changed_blocks if len(block) >= 20]
    change_excerpt = changed_blocks[0] if changed_blocks else start_here

    return {
        "change_kind": change_kind,
        "start_here": start_here,
        "start_position": start_position,
        "scan_guidance": scan_guidance,
        "change_excerpt": change_excerpt,
    }


def fetch_html_source(config, hours_back=72):
    cutoff = now_utc() - datetime.timedelta(hours=hours_back)
    try:
        response = requests.get(config["url"], headers=HTTP_HEADERS, timeout=20)
        response.raise_for_status()
    except Exception as ex:
        print(f"  ⚠ {config['source']}: {ex}")
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    articles = []
    seen = set()

    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "")
        url = urljoin(config["url"], href)
        parsed = urlparse(url)
        if parsed.netloc != urlparse(config["url"]).netloc:
            continue
        if not any(parsed.path.startswith(prefix) for prefix in config["path_prefixes"]):
            continue
        title = clean_text(anchor.get_text(" ", strip=True))
        if not title or len(title) < 12:
            continue
        if url == config["url"] or url in seen:
            continue
        seen.add(url)

        summary = ""
        published = None
        container = anchor
        for _ in range(5):
            container = container.parent
            if container is None:
                break
            block_text = clean_text(container.get_text(" ", strip=True))
            if not summary and block_text:
                summary = block_text[:500]
            if published is None:
                published = parse_date(block_text)
            if published:
                break

        if published and published < cutoff:
            continue

        articles.append({
            "source": config["source"],
            "type": "Blog",
            "title": title,
            "url": url,
            "summary": summary,
            "published_dt": published,
            "published": published.strftime("%b %d") if published else "?",
        })

    return dedupe_articles(articles)


def extract_update_summary(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    trimmed = []
    for line in lines:
        lowered = line.lower()
        if lowered in {"openai", "anthropic", "search", "table of contents"}:
            continue
        trimmed.append(line)
        if len(" ".join(trimmed)) >= 500:
            break
    return clean_text(" ".join(trimmed))[:600]


def fetch_official_update_pages(state):
    articles = []
    previous_pages = state.get("official_pages", {})
    for config in OFFICIAL_UPDATE_PAGES:
        try:
            response = requests.get(config["url"], headers=HTTP_HEADERS, timeout=20)
            response.raise_for_status()
        except Exception as ex:
            print(f"  ⚠ {config['title']}: {ex}")
            continue

        content_blocks = extract_structured_page_blocks(response.text)
        page_text = " ".join(content_blocks)
        if not page_text:
            continue
        summary = extract_update_summary(page_text)
        content_hash = hashlib.sha1(page_text.encode("utf-8")).hexdigest()[:12]
        previous_blocks = previous_pages.get(config["url"], {}).get("blocks", [])
        change_info = classify_update_change(previous_blocks, content_blocks)
        articles.append({
            "source": config["source"],
            "type": "Blog",
            "title": config["title"],
            "url": config["url"],
            "summary": summary,
            "published_dt": None,
            "published": "Live",
            "lane": config["lane"],
            "version_key": f"{config['url']}#{content_hash}",
            "is_official_update": True,
            "content_hash": content_hash,
            "content_blocks": content_blocks,
            **change_info,
        })
    return articles


def enrich_articles(articles, limit=MAX_BLOG_ENRICH):
    ranked = sorted(
        articles,
        key=lambda article: (
            topical_bonus(article),
            source_priority(article["source"]),
            article.get("published_dt") or datetime.datetime.min,
        ),
        reverse=True,
    )
    for article in ranked[:limit]:
        try:
            response = requests.get(article["url"], headers=HTTP_HEADERS, timeout=20)
            response.raise_for_status()
            article_text = extract_article_text(response.text)
            if article_text:
                article["summary"] = article_text
                article["enriched"] = True
        except Exception as ex:
            print(f"  ⚠ enrich {article['source']}: {ex}")
    return articles


# ── Fetch ──────────────────────────────────────────────────────────────────────

def fetch_claude_blog(hours_back=72):
    cutoff = now_utc() - datetime.timedelta(hours=hours_back)
    try:
        response = requests.get(CLAUDE_BLOG_URL, headers=HTTP_HEADERS, timeout=20)
        response.raise_for_status()
    except Exception as ex:
        print(f"  ⚠ Claude Blog: {ex}")
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    articles = []
    seen = set()

    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "")
        if "/blog/" not in href:
            continue
        url = urljoin(CLAUDE_BLOG_URL, href)
        parsed = urlparse(url)
        if parsed.path.rstrip("/") == "/blog":
            continue
        title = clean_text(anchor.get_text(" ", strip=True))
        if (
            not title or
            title.lower() in {"read more", "blog", "try claude"} or
            len(title) < 12
        ):
            continue
        if url in seen:
            continue
        seen.add(url)

        summary = ""
        published = None
        container = anchor
        for _ in range(4):
            container = container.parent
            if container is None:
                break
            block_text = clean_text(container.get_text(" ", strip=True))
            if not summary and block_text:
                summary = block_text[:500]
            if published is None:
                published = parse_date(block_text)
            if published:
                break

        if published and published < cutoff:
            continue

        articles.append({
            "source": "Claude Blog",
            "type": "Blog",
            "title": title,
            "url": url,
            "summary": summary,
            "published_dt": published,
            "published": published.strftime("%b %d") if published else "?",
        })

    return dedupe_articles(articles)


def fetch_blogs(hours_back=28):
    cutoff = now_utc() - datetime.timedelta(hours=hours_back)
    articles = fetch_claude_blog(hours_back=max(hours_back, 72))
    for config in HTML_SOURCES:
        articles.extend(fetch_html_source(config, hours_back=max(hours_back, 72)))
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
                    "published_dt": pub,
                    "published": pub.strftime("%b %d") if pub else "?",
                })
        except Exception as ex:
            print(f"  ⚠ {name}: {ex}")
    filtered = [article for article in articles if looks_relevant(article)]
    return dedupe_articles(filtered)


def fetch_arxiv(days_back=2):
    try:
        r = requests.get(
            "http://export.arxiv.org/api/query",
            params={"search_query": ARXIV_QUERY, "sortBy": "submittedDate",
                    "sortOrder": "descending", "max_results": 30},
            headers=HTTP_HEADERS,
            timeout=20,
        )
        r.raise_for_status()
    except Exception as ex:
        print(f"  ⚠ arXiv: {ex}")
        return []

    cutoff = now_utc() - datetime.timedelta(days=days_back)
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
            "published_dt": pub,
            "published": pub.strftime("%b %d") if pub else "?",
        })
    return papers


# ── Score ──────────────────────────────────────────────────────────────────────

def score(articles, prompt, state=None):
    if not articles:
        return []
    state = state or {"sent": {}}
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
            a["lane"] = scores.get(j, {}).get("lane", infer_lane(a))
            a["client_value"] = scores.get(j, {}).get("client_value", "")
            a["action"] = scores.get(j, {}).get("action", "")
            a["priority_bonus"] = topical_bonus(a)
            a["novelty_penalty"] = novelty_penalty(a, state)
            a["digest_rank"] = (a["score"] * 10) + a["priority_bonus"] - a["novelty_penalty"]
            results.append(a)
    results = dedupe_articles(results)
    return sorted(
        results,
        key=lambda x: (
            x.get("digest_rank", x["score"] * 10),
            x["score"],
            x.get("priority_bonus", 0),
            -x.get("novelty_penalty", 0),
            source_priority(x["source"]),
            x.get("published_dt") or datetime.datetime.min,
        ),
        reverse=True,
    )


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


def select_consultant_sections(items):
    section_order = [
        ("Client-Relevant Now", 4),
        ("Build Patterns", 5),
        ("Experiments To Run", 4),
        ("Strategic Signals", 3),
    ]
    sections = {}
    for label, limit in section_order:
        chosen = [item for item in items if item.get("lane") == label and item["score"] >= 6]
        if label == "Strategic Signals":
            chosen = [item for item in items if item.get("lane") == label and item["score"] >= 5]
        sections[label] = cap_sources(chosen, limit=limit)
    return sections


def build_body(blogs, papers, official_updates):
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
            if a.get("client_value"):
                lines.append(f"  Client value: {a['client_value']}")
            if a.get("action"):
                lines.append(f"  Try next: {a['action']}")
            lines.append("")

    def format_updates(items):
        for a in items:
            lines.append(f"  📌 {a['title']}")
            lines.append(f"  {a['source']} · {a['published']}")
            lines.append(f"  {a['url']}")
            if a.get("start_here"):
                lines.append(f"  Start rereading: {a.get('start_position', 'near the top')} — {a['start_here'][:140]}")
            if a.get("change_kind"):
                lines.append(f"  Change type: {a['change_kind']}")
            if a.get("change_excerpt"):
                lines.append(f"  New text cue: {a['change_excerpt'][:220]}")
            if a.get("scan_guidance"):
                lines.append(f"  How to scan it: {a['scan_guidance']}")
            if a.get("summary"):
                lines.append(f"  → {a['summary'][:220]}")
            lines.append("")

    featured_items = []

    if official_updates:
        lines.append(f"Official Product Updates ({len(official_updates)}):\n")
        format_updates(official_updates)
        featured_items.extend(official_updates)

    combined = sorted(
        blogs + papers,
        key=lambda item: (
            item.get("digest_rank", item["score"] * 10),
            item["score"],
            item.get("published_dt") or datetime.datetime.min,
        ),
        reverse=True,
    )
    sections = select_consultant_sections(combined)

    if any(sections.values()):
        total_items = sum(len(items) for items in sections.values())
        lines.append(f"Consultant Intelligence ({total_items}):\n")
        for label, section_items in sections.items():
            if not section_items:
                continue
            lines.append(f"{label}:\n")
            format_items(section_items)
            featured_items.extend(section_items)
    else:
        lines.append("No notable consultant-relevant items today.\n")

    return "\n".join(lines), featured_items


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    today = datetime.date.today()
    is_friday = today.weekday() == 4
    force_all = "--all" in sys.argv
    force_blogs = "--blogs" in sys.argv
    force_arxiv = "--arxiv" in sys.argv
    allow_no_email = "--allow-no-email" in sys.argv

    run_blogs = force_all or force_blogs or (not force_arxiv)
    run_arxiv = force_all or force_arxiv or True  # run daily, not just Fridays

    can_email = check_env()
    if not can_email and not allow_no_email:
        sys.exit("❌ SMTP env vars missing. Refusing to continue without email delivery. Use --allow-no-email to override.")
    state = prune_state(load_state())
    print(f"Running digest — blogs: {run_blogs}, arXiv: {run_arxiv}")

    blogs, papers, official_updates = [], [], []

    print("Fetching official product update pages...")
    raw_updates = fetch_official_update_pages(state)
    official_updates = filter_unsent(raw_updates, state)
    print(f"  {len(official_updates)} changed official pages found")

    if run_blogs:
        print("Fetching blogs...")
        raw = fetch_blogs()
        raw = filter_unsent(raw, state)
        print(f"  {len(raw)} unsent articles found, enriching/scoring...")
        raw = enrich_articles(raw, limit=MAX_BLOG_ENRICH)
        blogs = score(raw, BLOG_SCORE_PROMPT, state=state)

    if run_arxiv:
        print("Fetching arXiv...")
        raw = fetch_arxiv()
        raw = filter_unsent(raw, state)
        print(f"  {len(raw)} unsent papers found, scoring...")
        papers = score(raw, PAPER_SCORE_PROMPT, state=state)

    body, featured_items = build_body(blogs, papers, official_updates)
    total = len(featured_items)
    update_count = len([item for item in featured_items if item.get("is_official_update")])
    blog_count = len([item for item in featured_items if item["type"] == "Blog" and not item.get("is_official_update")])
    paper_count = len([item for item in featured_items if item["type"] == "Paper"])
    print(f"Digest ready: {update_count} official updates, {blog_count} featured blog posts, {paper_count} featured papers")
    subject = f"AI Digest {today} — {total} items"

    # Print to stdout (visible in Render logs) and email
    print("\n" + body)

    if can_email:
        try:
            send_email(subject, body)
            mark_sent(state, featured_items)
            save_state(state)
            print("✅ Email sent.")
        except Exception as ex:
            print(f"⚠ Email failed: {ex}")
            raise
    else:
        print("⚠ Skipping email (SMTP env vars not set).")


if __name__ == "__main__":
    main()
