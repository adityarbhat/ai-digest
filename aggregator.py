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

import base64
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
from email.utils import parsedate_to_datetime
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urljoin, urlparse

import feedparser
import requests
from anthropic import Anthropic
from anthropic.types import TextBlock
from bs4 import BeautifulSoup, Tag
from dotenv import load_dotenv

load_dotenv()

# ── Sources ────────────────────────────────────────────────────────────────────

BLOGS = [
    # Frontier labs — know what competitors ship
    ("OpenAI",          "https://openai.com/blog/rss.xml"),
    ("Google DeepMind", "https://deepmind.google/blog/rss.xml"),
    ("Meta AI",         "https://news.google.com/rss/search?q=site:ai.meta.com"),  # direct RSS 404s; Google News mirror
    ("Microsoft Research", "https://www.microsoft.com/en-us/research/feed/"),
    # Deep technical / practitioner blogs
    ("Simon Willison",  "https://simonwillison.net/atom/everything/"),
    ("Addy Osmani",     "https://addyosmani.com/rss.xml"),               # AI-assisted engineering / agentic coding essays
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
    ("Supabase Blog",   "https://supabase.com/blog/rss.xml"),            # Supabase product + engineering updates
    ("Neon Blog",       "https://neon.tech/blog/rss.xml"),               # Postgres/serverless architecture
    ("Streamlit Blog",  "https://news.google.com/rss/search?q=site:blog.streamlit.io"),  # direct RSS 403s; Google News mirror
    ("FastAPI Releases", "https://github.com/fastapi/fastapi/releases.atom"),  # changelog feed — FastAPI has no blog
    # Tech & economic analysis
    ("MIT Tech Review", "https://www.technologyreview.com/feed/"),
    # System design & engineering depth
    ("Cloudflare Blog",      "https://blog.cloudflare.com/rss/"),
    ("GitHub Engineering",   "https://github.blog/engineering.atom"),
    ("AWS Architecture Blog", "https://aws.amazon.com/blogs/architecture/feed/"),
    ("AWS Engineering",      "https://aws.amazon.com/blogs/developer/feed/"),
    ("Meta Engineering",     "https://engineering.fb.com/feed/"),
    ("Airbnb Engineering",   "https://medium.com/feed/airbnb-engineering/tagged/ai"),  # AI-tagged posts only
    ("Uber Engineering",     "https://news.google.com/rss/search?q=%22Uber+Engineering%22+blog"),  # direct RSS now returns 0 entries; Google News mirror
    ("Netflix TechBlog",     "https://netflixtechblog.com/feed"),
    ("DoorDash Engineering", "https://news.google.com/rss/search?q=site:careersatdoordash.com"),  # direct RSS 403s; Google News mirror
    ("Spotify Engineering",  "https://engineering.atspotify.com/feed"),
    ("Slack Engineering",    "https://slack.engineering/feed/"),
    ("Pinterest Engineering", "https://medium.com/feed/pinterest-engineering"),
    ("Dropbox Tech",         "https://dropbox.tech/feed"),
    ("Stripe Blog",          "https://stripe.com/blog/feed.rss"),
    ("Instacart Engineering", "https://tech.instacart.com/feed"),               # Medium-backed; ML/marketplace eng
    ("Lyft Engineering",     "https://eng.lyft.com/feed"),                      # Medium-backed; data/ML platform
    ("Etsy Code as Craft",   "https://www.etsy.com/codeascraft/rss"),           # long-running eng blog; AI/ML + system design
    ("Booking.com Engineering", "https://medium.com/feed/booking-com-development"),  # Medium-backed
    ("Grab Engineering",     "https://news.google.com/rss/search?q=site:engineering.grab.com"),  # direct RSS dead; Google News mirror
    ("Shopify Engineering",  "https://news.google.com/rss/search?q=site:shopify.engineering"),   # .atom redirects to HTML; Google News mirror
    ("LinkedIn Engineering", "https://news.google.com/rss/search?q=site:linkedin.com/blog/engineering"),  # no direct RSS; Google News mirror
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
    "claude code",
    "codex",
    "deepseek",
    "coding agent",
    "agentic",
    "harness",
    "software factory",
    "ai-assisted",
    "cloud certification",
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
    "Cloudflare Blog": 88,
    "Render Blog": 87,
    "Supabase Engineering": 87,
    "Supabase Blog": 87,
    "Supabase Developers": 81,
    "Neon Blog": 84,
    "pganalyze": 85,
    "AWS Architecture Blog": 86,
    "AWS Engineering": 86,
    "Meta Engineering": 87,
    "Airbnb Engineering": 85,
    "Uber Engineering": 86,
    "Netflix TechBlog": 88,
    "DoorDash Engineering": 84,
    "Spotify Engineering": 84,
    "Slack Engineering": 84,
    "Pinterest Engineering": 82,
    "Dropbox Tech": 82,
    "Stripe Blog": 84,
    "Instacart Engineering": 85,
    "Lyft Engineering": 85,
    "Etsy Code as Craft": 84,
    "Booking.com Engineering": 83,
    "Grab Engineering": 84,
    "Shopify Engineering": 85,
    "LinkedIn Engineering": 84,
    "Streamlit Blog": 90,
    "FastAPI Releases": 90,
    "Addy Osmani": 88,
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
    "Claude Blog": None,
    "Anthropic": None,
    "Anthropic Docs": None,
    "OpenAI": None,
    "OpenAI Help": None,
    "OpenAI Academy": None,
    "Google DeepMind": 1,
    "Meta AI": 1,
    "Mistral AI": 1,
    "Cloudflare Blog": 2,
    "Render Blog": 1,
    "Supabase Engineering": 2,
    "Supabase Blog": 2,
    "Supabase Developers": 1,
    "Neon Blog": 1,
    "pganalyze": 1,
    "AWS Architecture Blog": 1,
    "AWS Engineering": 1,
    "Meta Engineering": 1,
    "Airbnb Engineering": 1,
    "Uber Engineering": 1,
    "Netflix TechBlog": 1,
    "DoorDash Engineering": 1,
    "Spotify Engineering": 1,
    "Slack Engineering": 1,
    "Pinterest Engineering": 1,
    "Dropbox Tech": 1,
    "Stripe Blog": 1,
    "Instacart Engineering": 1,
    "Lyft Engineering": 1,
    "Etsy Code as Craft": 1,
    "Booking.com Engineering": 1,
    "Grab Engineering": 1,
    "Shopify Engineering": 1,
    "LinkedIn Engineering": 1,
    "Streamlit Blog": 1,
    "FastAPI Releases": 1,
    "Addy Osmani": 1,
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
FRONTIER_SOURCES = {
    "Claude Blog",
    "Anthropic",
    "Anthropic Docs",
    "OpenAI",
    "OpenAI Help",
    "OpenAI Academy",
    "Google DeepMind",
    "Meta AI",
    "Mistral AI",
}
# Big-company engineering blogs: only AI/agentic/stack-relevant posts get through.
# Adi reads these for AI engineering signal, not generic infra content.
ENGINEERING_AI_GATED_SOURCES = {
    "Cloudflare Blog",
    "Meta Engineering",
    "Airbnb Engineering",
    "Uber Engineering",
    "Netflix TechBlog",
    "AWS Architecture Blog",
    "AWS Engineering",
    "DoorDash Engineering",
    "Spotify Engineering",
    "Slack Engineering",
    "Pinterest Engineering",
    "Dropbox Tech",
    "Stripe Blog",
    "GitHub Engineering",
    "Instacart Engineering",
    "Lyft Engineering",
    "Etsy Code as Craft",
    "Booking.com Engineering",
    "Grab Engineering",
    "Shopify Engineering",
    "LinkedIn Engineering",
}
# Product-update feeds for Adi's own stack: always relevant, even when the title
# is just a version number (e.g. FastAPI release tags).
STACK_UPDATE_SOURCES = {
    "Supabase Blog",
    "Supabase Engineering",
    "Supabase Developers",
    "Neon Blog",
    "pganalyze",
    "Render Blog",
    "Streamlit Blog",
    "FastAPI Releases",
}
# Word-boundary AI topic matcher for gated engineering sources. Substring checks
# would false-positive ("maintain" contains "ai"), so this must stay a regex.
AI_TOPIC_PATTERN = re.compile(
    r"\b(ai|llm|llms|genai|generative|machine learning|deep learning|agent|agents|agentic|"
    r"copilot|gpt|claude|gemini|llama|deepseek|codex|rag|retrieval|embedding|embeddings|"
    r"vector|prompt|prompting|inference|fine-?tuning|transformer|recommendation|mlops)\b"
)
NON_ARTICLE_SEGMENTS = {
    "author",
    "authors",
    "blog",
    "categories",
    "category",
    "page",
    "search",
    "tag",
    "tags",
    "topic",
    "topics",
}
OFFICIAL_PRODUCT_KEYWORDS = (
    "api",
    "apis",
    "agent",
    "agents",
    "chatgpt",
    "claude",
    "codex",
    "computer use",
    "developer",
    "developers",
    "eval",
    "function calling",
    "gpt",
    "mcp",
    "model",
    "models",
    "prompt",
    "reasoning",
    "release",
    "responses",
    "sdk",
    "security",
    "tool use",
    "tools",
    "update",
)
OFFICIAL_MARKETING_KEYWORDS = (
    "case study",
    "class of",
    "customer",
    "customers",
    "education",
    "futures",
    "nonprofit",
    "student",
    "students",
)
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
# 36h (not 24h) so a post published right after yesterday's run still lands in
# today's digest. Persistent sent-state dedupe makes the overlap safe.
BLOG_RECENCY_HOURS = 36
OFFICIAL_UPDATE_RECENCY_HOURS = 36
MAX_UNDATED_PRIORITY_SOURCE_ITEMS = 4
FRONTIER_HEADLINE_LIMIT = 3
MAX_RESEARCH_ITEMS = 2
MIN_RESEARCH_SCORE = 7
REQUIRE_PUBLISH_DATE_FOR_DAILY_LIST = True

LOW_SIGNAL_PUBLISHERS = (
    "business insider",
    "the information",
    "fortune",
    "cnbc",
    "yahoo",
    "the verge",
    "ars technica",
    "techcrunch",
    "venturebeat",
    "wired",
    "reuters",
    "bloomberg",
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
You score blog articles for Adi. He is an AI consultant who works primarily in the
Claude/Anthropic ecosystem — building agents, RAG pipelines, MCP servers, and data
dashboards for SMB clients. His daily coding tools are Claude (Claude Code), OpenAI
Codex, and DeepSeek. He does NOT use LangChain, LlamaIndex, or other orchestration
frameworks. He is NOT a researcher and does NOT build or train models — he is an
engineer and builder at heart.

He builds web applications and dashboards for the moving/logistics industry —
quoting, dispatch, inventory, billing, invoicing, scheduling, and operational
workflows. He values system design and distributed systems when they help him
design or advise on real software. His implementation stack is Python, FastAPI/Flask
web apps, Streamlit dashboards, Supabase (Postgres) for database/backend/auth/storage,
and Render for hosting/deployment.

He cares about:
1. What Anthropic ships (new Claude features, API changes, MCP updates) — top priority
2. Agentic AI coding: coding agents, harness engineering, software factories,
   AI-assisted development workflows, Claude Code / Codex / DeepSeek techniques
3. Frontier AI capabilities: new agent techniques, computer use, tool use, multimodal
4. Applied AI techniques he can use with Claude (prompting, evals, RAG patterns, MCP)
5. Stack updates that change his daily work: FastAPI, Streamlit, Supabase, Postgres, Render
6. Research distillations: a blog post explaining a new paper in plain terms
7. System design: how large-scale systems are built (relevant to building AI pipelines)
8. Cloud platform capability news relevant to cloud certifications (moderate priority)
9. Big-picture: economic impact of AI, industry surveys, future-of-work analysis
10. Interesting, high-signal articles he will actually finish in one sitting

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
      Deep dive on agentic coding workflows, harness engineering, or software factories.
7-8   WORTH READING — Applied AI technique Adi can use. Research distillation.
      System design pattern applicable to AI pipelines or business software.
      Practitioner analysis, workflow, reliability, or distributed-systems lessons.
      Meaningful release/update for his stack (FastAPI, Streamlit, Supabase, Postgres, Render).
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
- Category pages, landing pages, or documentation indexes are 1-2, not digest items.
- Vendor customer stories, education/community programs, and brand campaigns are 2-4 unless
  they include reusable technical detail, architecture, or implementation lessons.

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
and data dashboards for SMB clients. He is NOT a researcher and does NOT build models,
but he is a former PhD candidate who genuinely enjoys understanding new research —
he wants distillable, builder-relevant findings, never math-heavy theory.
He will only skim these, so they must be immediately useful to a practitioner.
He especially wants research on coding agents, agentic workflows, agent harnesses
and evaluation, plus system design relevant to real business software.
His stack is Python, Streamlit, Flask/FastAPI, Supabase/Postgres, and Render.

Target: 1-2 papers per day at 7+. Adi wants to stay current with frontier research
on agents, coding agents, and computer use — even if he won't implement every detail.

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

# GitHub-backed state: Render cron filesystems are ephemeral (and crons can't mount
# disks), so sent-state is committed back to this repo via the Contents API after
# each run. "[skip render]" in the commit message prevents a redeploy loop — which
# also means the deployed build never contains the latest state file, so load_state
# MUST read from the API, never trust the local clone, when a token is configured.
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "adityarbhat/ai-digest")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
STATE_REPO_PATH = "data/sent_items.json"
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


def split_trailing_publisher(title):
    if " - " not in title:
        return title, ""
    head, tail = title.rsplit(" - ", 1)
    head = head.strip()
    tail = tail.strip()
    if len(head) >= 8 and 2 <= len(tail) <= 60:
        return head, tail
    return title, ""


def is_low_signal_publisher(publisher):
    lowered = (publisher or "").lower()
    if not lowered:
        return False
    return any(token in lowered for token in LOW_SIGNAL_PUBLISHERS)


def parse_date(text):
    if not text:
        return None
    # Handle RFC 2822 style strings when present.
    try:
        rfc_dt = parsedate_to_datetime(text)
        if rfc_dt:
            return rfc_dt.replace(tzinfo=None) if rfc_dt.tzinfo is None else rfc_dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    except Exception:
        pass
    # Handle ISO timestamps embedded in HTML snippets/attributes.
    iso_match = re.search(
        r"\b\d{4}-\d{2}-\d{2}(?:[T\s]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)?\b",
        text,
    )
    if iso_match:
        candidate = iso_match.group(0).replace("Z", "+00:00")
        try:
            iso_dt = datetime.datetime.fromisoformat(candidate)
            return iso_dt.astimezone(datetime.timezone.utc).replace(tzinfo=None) if iso_dt.tzinfo else iso_dt
        except ValueError:
            pass
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
    # Naive UTC: all stored/parsed datetimes in this pipeline are naive UTC.
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def is_recent(dt, hours_back):
    if dt is None:
        return False
    cutoff = now_utc() - datetime.timedelta(hours=hours_back)
    return dt >= cutoff


def parse_iso_datetime(value):
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value)
    except ValueError:
        return None


def is_valid_article_url(source, url):
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if not path:
        return False
    lowered = path.lower()

    # Claude: exclude category/listing pages and keep concrete article slugs.
    if source == "Claude Blog":
        if lowered in {"/blog", "/blog/category", "/blog/categories", "/blog/tag", "/blog/tags"}:
            return False
        blocked_segments = ("/blog/category/", "/blog/categories/", "/blog/tag/", "/blog/tags/")
        if any(segment in lowered for segment in blocked_segments):
            return False
        return lowered.startswith("/blog/") and lowered.count("/") >= 2

    # OpenAI: keep article pages, exclude root indexes.
    # /news/ was added because OpenAI migrated some blog posts to /news/ after 2024.
    if source == "OpenAI":
        if lowered in {"/blog", "/index", "/news"}:
            return False
        return lowered.startswith("/index/") or lowered.startswith("/blog/") or lowered.startswith("/news/")

    # Cloudflare: blog posts are typically under /<slug>/ on blog.cloudflare.com.
    if source == "Cloudflare Blog":
        return lowered not in {"", "/"}

    return True


def parse_date_from_url(url):
    match = re.search(r"/(20\d{2})/(0?[1-9]|1[0-2])/(0?[1-9]|[12]\d|3[01])(?:/|$)", url)
    if not match:
        return None
    year, month, day = map(int, match.groups())
    try:
        return datetime.datetime(year, month, day)
    except ValueError:
        return None


def extract_published_datetime(html_text):
    soup = BeautifulSoup(html_text, "html.parser")
    # Common metadata keys used by CMS/blog engines.
    meta_selectors = [
        ("property", "article:published_time"),
        ("property", "og:published_time"),
        ("property", "article:modified_time"),
        ("name", "parsely-pub-date"),
        ("name", "publish_date"),
        ("name", "pubdate"),
        ("name", "date"),
        ("name", "dc.date"),
        ("name", "dc.date.issued"),
        ("name", "lastmod"),
        ("itemprop", "datePublished"),
        ("itemprop", "dateModified"),
    ]
    for attr, key in meta_selectors:
        tag = soup.find("meta", attrs={attr: key})
        if isinstance(tag, Tag) and tag.get("content"):
            parsed = parse_date(tag.get("content"))
            if parsed:
                return parsed

    for time_tag in soup.find_all("time"):
        if not isinstance(time_tag, Tag):
            continue
        if time_tag.get("datetime"):
            parsed = parse_date(time_tag.get("datetime"))
            if parsed:
                return parsed
        text_value = clean_text(time_tag.get_text(" ", strip=True))
        parsed = parse_date(text_value)
        if parsed:
            return parsed
    return None


# Anchor texts that are card buttons, not article titles. When a scraped link
# carries one of these, the real title must come from the article page itself.
JUNK_LINK_TITLES = {
    "read more",
    "read post",
    "read article",
    "read the post",
    "learn more",
    "continue reading",
    "see more",
    "blog",
    "try claude",
}


def is_junk_link_title(title):
    return clean_text(title).lower().rstrip(".…→ ") in JUNK_LINK_TITLES


def _strip_site_suffix(title):
    # Drop trailing site-name suffixes like "Post Title | Anthropic".
    return clean_text(re.split(r"\s+[|\\—·]\s+", title)[0])


def extract_page_title(html_text):
    if not html_text:
        return ""
    soup = BeautifulSoup(html_text, "html.parser")
    for attr, key in (("property", "og:title"), ("name", "twitter:title")):
        tag = soup.find("meta", attrs={attr: key})
        if isinstance(tag, Tag) and tag.get("content"):
            return _strip_site_suffix(str(tag.get("content")))
    if soup.title and soup.title.string:
        return _strip_site_suffix(soup.title.string)
    h1 = soup.find("h1")
    if isinstance(h1, Tag):
        return clean_text(h1.get_text(" ", strip=True))
    return ""


def title_from_slug(path):
    segments = [segment for segment in (path or "").split("/") if segment]
    if not segments:
        return ""
    words = re.sub(r"[-_]+", " ", segments[-1]).strip()
    return words[:1].upper() + words[1:] if len(words) >= 8 else ""


def fetch_frontier_watchlist(state=None, limit=FRONTIER_HEADLINE_LIMIT, hours_back=BLOG_RECENCY_HOURS):
    watch = {"Claude Blog": [], "OpenAI Blog": [], "Cloudflare Blog": []}
    diagnostics = []
    # Never reshow a headline: skip anything already shown in a past watchlist OR
    # already emailed as a digest item. This is what stops the same Claude/Anthropic
    # link from reappearing every morning.
    state = state or {}
    already_shown = set(state.get("watchlist_seen", {}))
    already_shown.update(
        item.get("url", "") for item in state.get("sent", {}).values()
    )

    # Claude Blog watch: parse direct /blog/ links from the page and keep top headings.
    try:
        response = requests.get(CLAUDE_BLOG_URL, headers=HTTP_HEADERS, timeout=20)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        seen = set()
        for anchor in soup.find_all("a", href=True):
            if not isinstance(anchor, Tag):
                continue
            href = anchor.get("href", "")
            if not isinstance(href, str) or "/blog/" not in href:
                continue
            url = urljoin(CLAUDE_BLOG_URL, href)
            parsed = urlparse(url)
            if parsed.path.rstrip("/") == "/blog":
                continue
            if not looks_like_article_path(parsed.path):
                continue
            if url in seen or url in already_shown:
                continue
            seen.add(url)
            title = clean_text(anchor.get_text(" ", strip=True))
            published = None
            article_html = ""
            try:
                article_resp = requests.get(url, headers=HTTP_HEADERS, timeout=20)
                if article_resp.status_code in (404, 410):
                    continue  # dead link — never show it
                article_resp.raise_for_status()
                article_html = article_resp.text
                published = extract_published_datetime(article_html)
            except Exception:
                published = None
            # Card buttons ("Read more") are not titles — pull the real title
            # from the article page, falling back to a slug-derived title.
            if not title or is_junk_link_title(title):
                title = extract_page_title(article_html) or title_from_slug(parsed.path)
            if not title or len(title) < 8 or is_junk_link_title(title):
                continue
            # Fail-open for Claude Blog: if date parsing fails, include up to `limit`
            # undated headlines rather than showing an empty watchlist block.
            if published is not None and not is_recent(published, hours_back):
                continue
            # Undated entries are allowed through; they'll still be capped by `limit`.
            watch["Claude Blog"].append({
                "title": title,
                "url": url,
                "published": published.strftime("%b %d") if published else "?",
            })
            if len(watch["Claude Blog"]) >= limit:
                break
        if not watch["Claude Blog"]:
            diagnostics.append("Claude Blog watch returned 0 entries (JS-rendered page or scraping blocked) — trying Anthropic news fallback.")
            # Fallback: anthropic.com/news renders server-side and is scrapeable even when
            # claude.com/blog is JS-only. Articles appear under /news/<slug>.
            try:
                anth_resp = requests.get("https://www.anthropic.com/news", headers=HTTP_HEADERS, timeout=20)
                anth_resp.raise_for_status()
                anth_soup = BeautifulSoup(anth_resp.text, "html.parser")
                anth_seen = set()
                for anchor in anth_soup.find_all("a", href=True):
                    if not isinstance(anchor, Tag):
                        continue
                    href = anchor.get("href", "")
                    if not isinstance(href, str) or "/news/" not in href:
                        continue
                    anth_url = urljoin("https://www.anthropic.com", href)
                    parsed_anth = urlparse(anth_url)
                    if parsed_anth.path.rstrip("/") in {"/news", "/news/"}:
                        continue
                    # Require a real /news/<slug> article path so nav/category
                    # anchors ("links that go nowhere") can't slip in.
                    anth_segments = [seg for seg in parsed_anth.path.split("/") if seg]
                    if len(anth_segments) < 2 or len(anth_segments[-1]) < 4:
                        continue
                    if anth_url in anth_seen or anth_url in already_shown:
                        continue
                    anth_seen.add(anth_url)
                    anth_title = clean_text(anchor.get_text(" ", strip=True))
                    anth_pub = None
                    anth_html = ""
                    try:
                        anth_article_resp = requests.get(anth_url, headers=HTTP_HEADERS, timeout=20)
                        if anth_article_resp.status_code in (404, 410):
                            continue  # dead link — never show it
                        anth_article_resp.raise_for_status()
                        anth_html = anth_article_resp.text
                        anth_pub = extract_published_datetime(anth_html)
                    except Exception:
                        anth_pub = None
                    if not anth_title or is_junk_link_title(anth_title):
                        anth_title = extract_page_title(anth_html) or title_from_slug(parsed_anth.path)
                    if not anth_title or len(anth_title) < 8 or is_junk_link_title(anth_title):
                        continue
                    if anth_pub is not None and not is_recent(anth_pub, hours_back):
                        continue
                    watch["Claude Blog"].append({
                        "title": anth_title,
                        "url": anth_url,
                        "published": anth_pub.strftime("%b %d") if anth_pub else "?",
                    })
                    if len(watch["Claude Blog"]) >= limit:
                        break
                if watch["Claude Blog"]:
                    diagnostics.append(f"Claude Blog: Anthropic news fallback found {len(watch['Claude Blog'])} item(s).")
                else:
                    diagnostics.append("Anthropic news fallback also returned 0 entries.")
            except Exception as anth_ex:
                diagnostics.append(f"Anthropic news fallback failed: {anth_ex}")
    except Exception as ex:
        diagnostics.append(f"Claude Blog watch failed: {ex}")

    # OpenAI blog watch: try primary RSS, then fall back to /news/ RSS if primary returns 0.
    # OpenAI has moved posts between /blog/ and /news/ paths since 2024.
    OPENAI_RSS_URLS = [
        "https://openai.com/blog/rss.xml",
        "https://openai.com/news/rss.xml",
    ]
    try:
        openai_entries = []
        for rss_url in OPENAI_RSS_URLS:
            feed = feedparser.parse(rss_url)
            if feed.entries:
                openai_entries = feed.entries[:30]
                break
            else:
                diagnostics.append(f"OpenAI RSS {rss_url} returned 0 entries, trying next.")
        for e in openai_entries:
            url = e.get("link", "")
            if not url or url in already_shown:
                continue
            if not is_valid_article_url("OpenAI", url):
                continue
            title = clean_text(e.get("title", ""))
            if not title:
                continue
            watch_item = {
                "source": "OpenAI",
                "type": "Blog",
                "title": title,
                "url": url,
                "summary": clean_text(str(e.get("summary", ""))[:400]),
            }
            if is_marketing_heavy_frontier_item(
                watch_item,
                f"{watch_item['title']} {watch_item['summary']}".lower(),
            ):
                continue
            pub = None
            for attr in ("published_parsed", "updated_parsed"):
                val = getattr(e, attr, None)
                if val:
                    pub = datetime.datetime(*val[:6])
                    break
            if pub is None:
                for attr in ("published", "updated", "created"):
                    raw_value = e.get(attr, "")
                    if raw_value:
                        pub = parse_date(str(raw_value))
                        if pub:
                            break
            # Fail-open: include undated OpenAI entries up to `limit` rather than silently dropping.
            if pub is not None and not is_recent(pub, hours_back):
                continue
            # Undated entries are allowed through; capped by `limit`.
            watch["OpenAI Blog"].append({
                "title": title,
                "url": url,
                "published": pub.strftime("%b %d") if pub else "?",
            })
            if len(watch["OpenAI Blog"]) >= limit:
                break
        if not watch["OpenAI Blog"]:
            diagnostics.append("OpenAI Blog watch returned 0 entries (RSS may be blocked or changed).")
    except Exception as ex:
        diagnostics.append(f"OpenAI Blog watch failed: {ex}")

    # Cloudflare watch for architecture/system design visibility.
    # Falls back to HTML scraping of blog.cloudflare.com if RSS returns 0 entries.
    try:
        cloudflare_entries_raw = []
        feed = feedparser.parse("https://blog.cloudflare.com/rss/")
        if feed.entries:
            for e in feed.entries[:30]:
                url = e.get("link", "")
                if not url or not is_valid_article_url("Cloudflare Blog", url):
                    continue
                title = clean_text(e.get("title", ""))
                if not title:
                    continue
                pub = None
                parsed_tuple = getattr(e, "published_parsed", None)
                if parsed_tuple:
                    pub = datetime.datetime(*parsed_tuple[:6])
                cloudflare_entries_raw.append({"title": title, "url": url, "pub": pub})
        else:
            diagnostics.append("Cloudflare RSS returned 0 entries — attempting HTML fallback.")
            try:
                cf_resp = requests.get("https://blog.cloudflare.com", headers=HTTP_HEADERS, timeout=20)
                cf_resp.raise_for_status()
                cf_soup = BeautifulSoup(cf_resp.text, "html.parser")
                cf_seen = set()
                for anchor in cf_soup.find_all("a", href=True):
                    if not isinstance(anchor, Tag):
                        continue
                    href = anchor.get("href", "")
                    if not isinstance(href, str):
                        continue
                    cf_url = urljoin("https://blog.cloudflare.com", href)
                    if not is_valid_article_url("Cloudflare Blog", cf_url):
                        continue
                    if cf_url in cf_seen:
                        continue
                    cf_seen.add(cf_url)
                    cf_title = clean_text(anchor.get_text(" ", strip=True))
                    if not cf_title or len(cf_title) < 12:
                        continue
                    cloudflare_entries_raw.append({"title": cf_title, "url": cf_url, "pub": None})
                    if len(cloudflare_entries_raw) >= 30:
                        break
            except Exception as cf_ex:
                diagnostics.append(f"Cloudflare HTML fallback failed: {cf_ex}")

        for entry in cloudflare_entries_raw:
            if entry["url"] in already_shown:
                continue
            pub = entry["pub"]
            # Fail-open: include undated entries up to `limit` rather than silently dropping.
            if pub is not None and not is_recent(pub, hours_back):
                continue
            # Undated entries allowed through; capped by `limit`.
            watch["Cloudflare Blog"].append({
                "title": entry["title"],
                "url": entry["url"],
                "published": pub.strftime("%b %d") if pub else "?",
            })
            if len(watch["Cloudflare Blog"]) >= limit:
                break
        if not watch["Cloudflare Blog"]:
            diagnostics.append("Cloudflare Blog watch returned 0 entries (RSS and HTML fallback both failed).")
    except Exception as ex:
        diagnostics.append(f"Cloudflare Blog watch failed: {ex}")

    return watch, diagnostics


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


def looks_like_article_path(path):
    segments = [segment for segment in path.split("/") if segment]
    if "blog" not in segments:
        return True
    blog_idx = segments.index("blog")
    tail = segments[blog_idx + 1:]
    if not tail:
        return False
    if tail[0].lower() in NON_ARTICLE_SEGMENTS:
        return False
    slug = tail[-1].strip().lower()
    return len(slug) >= 4 and any(ch.isalpha() for ch in slug)


def is_case_study_title(title):
    cleaned = clean_text(title)
    return bool(
        re.match(
            r"^[A-Z0-9][\w&+.'-]*(?:\s+[A-Z0-9][\w&+.'-]*){0,3}\s+(uses|builds|helps|powers|transforms)\b",
            cleaned,
        )
    )


def is_marketing_heavy_frontier_item(article, text):
    if article["source"] != "OpenAI":
        return False
    if is_case_study_title(article["title"]):
        return True
    if any(keyword in text for keyword in ("class of", "futures", "education", "student", "students")):
        return True
    has_marketing_signal = any(keyword in text for keyword in OFFICIAL_MARKETING_KEYWORDS)
    has_product_signal = any(keyword in text for keyword in OFFICIAL_PRODUCT_KEYWORDS)
    return has_marketing_signal and not has_product_signal


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
    parsed = urlparse(article["url"])
    if any(keyword in text for keyword in LOW_SIGNAL_KEYWORDS):
        return False
    if is_low_signal_publisher(article.get("publisher", "")):
        return False
    if parsed.path and not looks_like_article_path(parsed.path):
        return False
    if is_marketing_heavy_frontier_item(article, text):
        return False
    if article["source"] in FRONTIER_SOURCES:
        if any(keyword in text for keyword in CLAUDE_BLOG_KEYWORDS):
            return True
        if any(keyword in text for keyword in HIGH_SIGNAL_KEYWORDS):
            return True
        return any(keyword in text for keyword in OFFICIAL_PRODUCT_KEYWORDS)
    if article["source"] in STACK_UPDATE_SOURCES:
        return True
    if article["source"] in ENGINEERING_AI_GATED_SOURCES:
        if AI_TOPIC_PATTERN.search(text):
            return True
        return any(keyword in text for keyword in STACK_KEYWORDS)
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


def infer_tags(article):
    text = f"{article.get('title', '')} {article.get('summary', '')}".lower()
    tags = []
    if any(keyword in text for keyword in ("mcp", "tool use", "tool calling", "claude code", "codex")):
        tags.append("tooling-update")
    if any(keyword in text for keyword in ("architecture", "distributed", "scaling", "reliability", "migration", "incident")):
        tags.append("system-design")
    if any(keyword in text for keyword in ("benchmark", "eval", "ablation", "experiment")):
        tags.append("hands-on-method")
    if any(keyword in text for keyword in ("security", "auth", "rls", "compliance")):
        tags.append("security-reliability")
    if article.get("source") in {"Claude Blog", "OpenAI", "Anthropic Docs", "OpenAI Help"}:
        tags.append("frontier-update")
    if article.get("type") == "Paper":
        tags.append("cutting-edge-research")
    if not tags:
        tags.append("practical-build-pattern")
    return tags[:2]


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
        if cap is not None and per_source.get(source, 0) >= cap:
            continue
        kept.append(article)
        per_source[source] = per_source.get(source, 0) + 1
        if len(kept) >= limit:
            break
    return kept


def article_topic_signature(article):
    tokens = [
        token for token in title_tokens(article.get("title", ""))
        if token not in STOPWORDS
    ]
    return tuple(sorted(tokens[:4]))


def select_diverse_items(candidates, limit):
    if limit <= 0 or not candidates:
        return []

    # Start from highest-ranked items, then greedily add diversity-aware picks.
    pool = sorted(
        candidates,
        key=lambda item: (
            item.get("digest_rank", item.get("score", 0) * 10),
            item.get("score", 0),
            source_priority(item.get("source", "")),
            item.get("published_dt") or datetime.datetime.min,
        ),
        reverse=True,
    )

    chosen = []
    per_source = {}
    used_topics = set()

    while pool and len(chosen) < limit:
        best_idx = None
        best_value = None

        for idx, item in enumerate(pool):
            source = item["source"]
            cap = SOURCE_CAPS.get(source, 1)
            if cap is not None and per_source.get(source, 0) >= cap:
                continue

            topic_key = article_topic_signature(item)
            diversity_bonus = 0
            if per_source.get(source, 0) == 0:
                diversity_bonus += 4
            if topic_key and topic_key not in used_topics:
                diversity_bonus += 3

            score_value = item.get("digest_rank", item.get("score", 0) * 10) + diversity_bonus
            if best_value is None or score_value > best_value:
                best_value = score_value
                best_idx = idx

        if best_idx is None:
            break

        selected = pool.pop(best_idx)
        chosen.append(selected)
        per_source[selected["source"]] = per_source.get(selected["source"], 0) + 1
        topic_key = article_topic_signature(selected)
        if topic_key:
            used_topics.add(topic_key)

    return chosen


def article_key(article):
    parsed = urlparse(article["url"])
    canonical = article.get("version_key") or f"{parsed.netloc}{parsed.path}".rstrip("/")
    if not canonical:
        canonical = normalize_title(article["title"])
    digest = hashlib.sha1(canonical.encode("utf-8")).hexdigest()
    return digest


_github_state_sha = None


def github_state_enabled():
    return bool(GITHUB_TOKEN and GITHUB_REPO)


def _github_state_url():
    return f"https://api.github.com/repos/{GITHUB_REPO}/contents/{STATE_REPO_PATH}"


def _github_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _empty_state():
    return {"sent": {}, "official_pages": {}, "watchlist_seen": {}}


def _normalize_state(state):
    if not isinstance(state, dict):
        return _empty_state()
    state.setdefault("sent", {})
    state.setdefault("official_pages", {})
    state.setdefault("watchlist_seen", {})
    return state


def load_state():
    global _github_state_sha
    if github_state_enabled():
        try:
            resp = requests.get(
                _github_state_url(),
                params={"ref": GITHUB_BRANCH},
                headers=_github_headers(),
                timeout=20,
            )
            if resp.status_code == 404:
                print("  [state] no state file in GitHub yet — starting fresh")
                return _empty_state()
            resp.raise_for_status()
            payload = resp.json()
            _github_state_sha = payload.get("sha")
            content = base64.b64decode(payload.get("content", "") or "").decode("utf-8")
            state = _normalize_state(json.loads(content))
            print(f"  [state] loaded {len(state['sent'])} sent items from GitHub")
            return state
        except Exception as ex:
            print(f"  ⚠ GitHub state load failed ({ex}) — falling back to local file")
    if not STATE_PATH.exists():
        return _empty_state()
    try:
        with STATE_PATH.open("r", encoding="utf-8") as fh:
            return _normalize_state(json.load(fh))
    except Exception:
        return _empty_state()


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
        stamp = parse_iso_datetime(item.get("last_seen_at")) or parse_iso_datetime(item.get("updated_at"))
        if not stamp:
            official_pages[url] = item
            continue
        if stamp >= cutoff:
            official_pages[url] = item
    state["official_pages"] = official_pages
    watchlist_seen = {}
    for url, stamp in state.get("watchlist_seen", {}).items():
        seen_dt = parse_iso_datetime(stamp)
        if seen_dt is None or seen_dt >= cutoff:
            watchlist_seen[url] = stamp
    state["watchlist_seen"] = watchlist_seen
    return state


def save_state(state, push_remote=True):
    global _github_state_sha
    state = prune_state(state)
    serialized = json.dumps(state, indent=2, sort_keys=True)
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with STATE_PATH.open("w", encoding="utf-8") as fh:
        fh.write(serialized)
    if not (push_remote and github_state_enabled()):
        return
    body = {
        "message": f"chore: record digest state {now_utc().date().isoformat()} [skip render]",
        "content": base64.b64encode(serialized.encode("utf-8")).decode("ascii"),
        "branch": GITHUB_BRANCH,
    }
    for attempt in range(2):
        if _github_state_sha:
            body["sha"] = _github_state_sha
        try:
            resp = requests.put(_github_state_url(), headers=_github_headers(), json=body, timeout=20)
            if resp.status_code in (409, 422) and attempt == 0:
                # sha went stale (file changed since load) — refetch and retry once
                head = requests.get(
                    _github_state_url(),
                    params={"ref": GITHUB_BRANCH},
                    headers=_github_headers(),
                    timeout=20,
                )
                if head.status_code == 200:
                    _github_state_sha = head.json().get("sha")
                continue
            resp.raise_for_status()
            _github_state_sha = resp.json().get("content", {}).get("sha")
            print("  [state] committed sent-items state to GitHub")
            return
        except Exception as ex:
            print(f"  ⚠ GitHub state save failed ({ex}) — state kept locally only")
            return


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
                "last_seen_at": timestamp,
                "updated_at": timestamp,
            }
    return state


def filter_unsent(articles, state):
    seen = state.get("sent", {})
    return [article for article in articles if article_key(article) not in seen]


def mark_watchlist_shown(state, frontier_watch):
    seen = state.setdefault("watchlist_seen", {})
    stamp = now_utc().isoformat()
    for items in frontier_watch.values():
        for item in items:
            if item.get("url"):
                seen[item["url"]] = stamp
    return state


def extract_article_text(html_text):
    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    container_node = soup.find("main") or soup.find("article") or soup.body or soup
    container: Tag = container_node if isinstance(container_node, Tag) else soup
    text = clean_text(container.get_text(" ", strip=True))
    return text[:1200]


def extract_structured_page_blocks(html_text, limit=160):
    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    container_node = soup.find("main") or soup.find("article") or soup.body or soup
    container: Tag = container_node if isinstance(container_node, Tag) else soup
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

    first_tag, _, _, j1, _ = changes[0]
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


def fetch_html_source(config, hours_back=BLOG_RECENCY_HOURS):
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
        if not isinstance(anchor, Tag):
            continue
        href = anchor.get("href", "")
        if not isinstance(href, str):
            continue
        url = urljoin(config["url"], href)
        parsed = urlparse(url)
        if parsed.netloc != urlparse(config["url"]).netloc:
            continue
        if not any(parsed.path.startswith(prefix) for prefix in config["path_prefixes"]):
            continue
        if not looks_like_article_path(parsed.path):
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

        article_html = ""
        try:
            article_resp = requests.get(url, headers=HTTP_HEADERS, timeout=20)
            article_resp.raise_for_status()
            article_html = article_resp.text
        except Exception:
            article_html = ""

        if article_html:
            meta_published = extract_published_datetime(article_html)
            if meta_published:
                published = meta_published
            article_text = extract_article_text(article_html)
            if article_text:
                summary = article_text[:500]

        if published is None:
            published = parse_date_from_url(url)

        # Strict freshness rule for scraped HTML sources:
        # only include if we can parse a date and it's within the window.
        if not is_recent(published, hours_back):
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


def fetch_official_update_pages(state, hours_back=OFFICIAL_UPDATE_RECENCY_HOURS):
    articles = []
    snapshots = {}
    cutoff = now_utc() - datetime.timedelta(hours=hours_back)
    observed_at = now_utc().isoformat()
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
        previous_page = previous_pages.get(config["url"], {})
        previous_blocks = previous_page.get("blocks", [])
        previous_hash = previous_page.get("content_hash")
        previous_seen = parse_iso_datetime(previous_page.get("last_seen_at")) or parse_iso_datetime(previous_page.get("updated_at"))
        change_info = classify_update_change(previous_blocks, content_blocks)
        snapshots[config["url"]] = {
            "title": config["title"],
            "content_hash": content_hash,
            "blocks": content_blocks,
            "last_seen_at": observed_at,
            "updated_at": previous_page.get("updated_at"),
        }

        # Strict freshness rule for official docs pages:
        # include only newly-detected changes when our previous snapshot is within 24h.
        if not previous_hash or previous_hash == content_hash:
            continue
        if previous_seen is None or previous_seen < cutoff:
            continue

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
    return articles, snapshots


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

def fetch_claude_blog(hours_back=BLOG_RECENCY_HOURS):
    try:
        response = requests.get(CLAUDE_BLOG_URL, headers=HTTP_HEADERS, timeout=20)
        response.raise_for_status()
    except Exception as ex:
        print(f"  ⚠ Claude Blog: {ex}")
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    articles = []
    seen = set()
    undated_kept = 0

    for anchor in soup.find_all("a", href=True):
        if not isinstance(anchor, Tag):
            continue
        href = anchor.get("href", "")
        if not isinstance(href, str) or "/blog/" not in href:
            continue
        url = urljoin(CLAUDE_BLOG_URL, href)
        parsed = urlparse(url)
        if parsed.path.rstrip("/") == "/blog":
            continue
        if not looks_like_article_path(parsed.path):
            continue
        title = clean_text(anchor.get_text(" ", strip=True))
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

        article_html = ""
        try:
            article_resp = requests.get(url, headers=HTTP_HEADERS, timeout=20)
            if article_resp.status_code in (404, 410):
                continue  # dead link — never include it
            article_resp.raise_for_status()
            article_html = article_resp.text
        except Exception:
            article_html = ""

        # "Read more" card buttons are not titles — rescue the real title from
        # the article page (og:title/h1) or the URL slug before giving up.
        if not title or is_junk_link_title(title) or len(title) < 12:
            title = extract_page_title(article_html) or title_from_slug(parsed.path)
        if not title or len(title) < 12 or is_junk_link_title(title):
            continue

        if article_html:
            meta_published = extract_published_datetime(article_html)
            if meta_published:
                published = meta_published
            article_text = extract_article_text(article_html)
            if article_text:
                summary = article_text[:500]

        if published is None:
            published = parse_date_from_url(url)

        if published is None:
            # Fail-open for Claude Blog: include a few newest links even if date parsing fails.
            if undated_kept >= MAX_UNDATED_PRIORITY_SOURCE_ITEMS:
                continue
            undated_kept += 1
        elif not is_recent(published, hours_back):
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


def fetch_blogs(hours_back=BLOG_RECENCY_HOURS):
    articles = fetch_claude_blog(hours_back=hours_back)
    print(f"  Claude Blog: {len(articles)} articles found")
    for config in HTML_SOURCES:
        before = len(articles)
        articles.extend(fetch_html_source(config, hours_back=hours_back))
        added = len(articles) - before
        if added:
            print(f"  {config['source']}: {added} articles found")
    for name, rss in BLOGS:
        try:
            before_rss = len(articles)
            feed = feedparser.parse(rss)
            if not feed.entries:
                print(f"  [fetch] {name}: 0 entries from RSS ({rss})")
            for e in feed.entries[:20]:
                url = str(e.get("link", ""))
                if not url:
                    continue
                if not is_valid_article_url(name, url):
                    continue
                title = clean_text(str(e.get("title", "")))
                publisher = ""
                if "news.google.com" in urlparse(url).netloc:
                    title, publisher = split_trailing_publisher(title)
                pub = None
                for attr in ("published_parsed", "updated_parsed"):
                    val = getattr(e, attr, None)
                    if val:
                        pub = datetime.datetime(*val[:6])
                        break
                if pub is None:
                    for attr in ("published", "updated", "created"):
                        raw_value = e.get(attr, "")
                        if not raw_value:
                            continue
                        pub = parse_date(str(raw_value))
                        if pub:
                            break
                if pub is None:
                    try:
                        article_resp = requests.get(url, headers=HTTP_HEADERS, timeout=20)
                        article_resp.raise_for_status()
                        pub = extract_published_datetime(article_resp.text) or pub
                    except Exception:
                        pass
                # Strict freshness rule for RSS feeds.
                # Priority sources (OpenAI) get a fail-open cap so the watchlist stays
                # populated even when the RSS omits timestamps.
                if pub is None:
                    if name == "OpenAI":
                        openai_undated_count = len([
                            a for a in articles
                            if a.get("source") == "OpenAI" and a.get("published_dt") is None
                        ])
                        if openai_undated_count >= MAX_UNDATED_PRIORITY_SOURCE_ITEMS:
                            continue
                        # fall through — allow this undated OpenAI entry
                    elif REQUIRE_PUBLISH_DATE_FOR_DAILY_LIST:
                        print(f"  [date-filter] {name}: dropping entry '{str(e.get('title',''))[:60]}' — no publish date")
                        continue
                elif not is_recent(pub, hours_back):
                    continue
                articles.append({
                    "source": name, "type": "Blog",
                    "title": title, "url": url,
                    "summary": str(e.get("summary", ""))[:400],
                    "publisher": publisher,
                    "published_dt": pub,
                    "published": pub.strftime("%b %d") if pub else "?",
                })
            added_rss = len(articles) - before_rss
            if added_rss:
                print(f"  [fetch] {name}: {added_rss} recent article(s) collected")
        except Exception as ex:
            print(f"  ⚠ {name}: {ex}")
    print(f"  [fetch] Total raw articles before relevance/date filter: {len(articles)}")
    filtered = [article for article in articles if looks_relevant(article)]
    print(f"  [fetch] After relevance filter: {len(filtered)}")
    # Priority sources (Claude Blog, OpenAI) already enforce their own undated-article cap
    # inside fetch_claude_blog / fetch_blogs. The secondary filter must not silently drop
    # those entries again — only enforce recency when a date is actually available.
    PRIORITY_UNDATED_SOURCES = {"Claude Blog", "OpenAI"}
    if REQUIRE_PUBLISH_DATE_FOR_DAILY_LIST:
        before_date_filter = len(filtered)
        filtered = [
            article for article in filtered
            if (
                (article.get("published_dt") is not None and is_recent(article.get("published_dt"), hours_back))
                or (article.get("published_dt") is None and article.get("source") in PRIORITY_UNDATED_SOURCES)
            )
        ]
        dropped = before_date_filter - len(filtered)
        if dropped:
            print(f"  [date-filter] dropped {dropped} articles lacking a recent publish date (kept {len(filtered)})")
    else:
        filtered = [
            article for article in filtered
            if (
                is_recent(article.get("published_dt"), hours_back) or
                (
                    article.get("published_dt") is None and
                    article.get("source") in PRIORITY_UNDATED_SOURCES
                )
            )
        ]
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
        url = str(e.get("id", e.get("link", "")))
        if not url:
            continue
        pub = None
        parsed_tuple = getattr(e, "published_parsed", None)
        if parsed_tuple:
            pub = datetime.datetime(*parsed_tuple[:6])
        if pub and pub < cutoff:
            continue
        papers.append({
            "source": "arXiv", "type": "Paper",
            "title": str(e.get("title", "")).replace("\n", " ").strip(), "url": url,
            "summary": str(e.get("summary", ""))[:400],
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
    # Batch of 8 items × ~120 tokens of JSON each needs ~1000 output tokens;
    # max_tokens=4000 leaves ample headroom. The old 800 cap truncated the JSON
    # mid-string on EVERY batch — all articles silently defaulted to score 5.
    for i in range(0, len(articles), 8):
        batch = articles[i:i+8]
        payload = [{"index": j, "title": a["title"], "source": a["source"], "summary": a["summary"]}
                   for j, a in enumerate(batch)]
        scores = {}
        for attempt in range(3):
            try:
                resp = get_client().messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=4000,
                    system=prompt,
                    messages=[{"role": "user", "content": json.dumps(payload)}],
                )
                block = resp.content[0]
                if not isinstance(block, TextBlock):
                    raise ValueError(f"Unexpected content block type: {type(block)}")
                text = block.text.strip()
                # The model may wrap the array in code fences or append prose
                # before/after it — parse the first complete JSON array and
                # ignore everything around it.
                start = text.find("[")
                if start == -1:
                    raise ValueError("no JSON array in scoring response")
                parsed, _ = json.JSONDecoder().raw_decode(text[start:])
                scores = {r["index"]: r for r in parsed}
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
    # Per-lane minimum score thresholds. Raised Build Patterns + Experiments to 7 and
    # Strategic Signals to 6 to reduce low-value items from sources like Brookings/a16z/InfoQ.
    LANE_MIN_SCORE = {
        "Client-Relevant Now": 6,
        "Build Patterns": 7,
        "Experiments To Run": 7,
        "Strategic Signals": 6,
    }
    section_order = [
        ("Client-Relevant Now", 4),
        ("Build Patterns", 5),
        ("Experiments To Run", 4),
        ("Strategic Signals", 3),
    ]
    sections = {}
    for label, limit in section_order:
        min_score = LANE_MIN_SCORE.get(label, 6)
        chosen = [item for item in items if item.get("lane") == label and item.get("score", 0) >= min_score]
        sections[label] = select_diverse_items(chosen, limit=limit)

    # No volume padding: Adi prefers 1-3 genuinely high-value items over a digest
    # filled out with below-threshold picks. A small or even empty day is fine.
    return sections


def build_body(blogs, papers, official_updates, frontier_watch=None, diagnostics=None):
    today = datetime.date.today().strftime("%A, %b %d %Y")
    lines = [f"IMAI AI Digest — {today}\n{'='*50}\n"]
    frontier_watch = frontier_watch or {}
    diagnostics = diagnostics or []

    def format_items(items):
        for a in items:
            emoji = "📰" if a["type"] == "Blog" else "📄"
            lines.append(f"  {emoji} [{a['score']}/10] {a['title']}")
            lines.append(f"  {a['source']} · {a['published']}")
            lines.append(f"  {a['url']}")
            tags = a.get("tags") or infer_tags(a)
            if tags:
                lines.append(f"  Tags: {', '.join(tags)}")
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

    # Cross-dedupe within a single email: an article featured in Consultant
    # Intelligence must not also appear as a watchlist headline. Mutates
    # frontier_watch in place so main()'s subject-line count stays accurate.
    section_urls = {
        item.get("url", "")
        for section_items in sections.values()
        for item in section_items
    }
    for source in list(frontier_watch.keys()):
        frontier_watch[source] = [
            item for item in frontier_watch[source] if item.get("url") not in section_urls
        ]

    if frontier_watch:
        total_frontier = sum(len(items) for items in frontier_watch.values())
        # Only render the section when at least one source returned headlines.
        if total_frontier > 0:
            lines.append(f"Frontier Blog Watch ({total_frontier}):\n")
            for source in ("Claude Blog", "OpenAI Blog", "Cloudflare Blog"):
                items = frontier_watch.get(source, [])
                if not items:
                    continue
                lines.append(f"{source}:\n")
                for item in items:
                    lines.append(f"  {item['title']}")
                    lines.append(f"  {source} · {item.get('published', '?')}")
                    lines.append(f"  {item['url']}")
                    lines.append("")

    if official_updates:
        lines.append(f"Official Product Updates ({len(official_updates)}):\n")
        format_updates(official_updates)
        featured_items.extend(official_updates)

    if any(sections.values()):
        total_items = sum(len(v) for v in sections.values())
        lines.append(f"Consultant Intelligence ({total_items}):\n")
        for label, section_items in sections.items():
            if not section_items:
                continue
            lines.append(f"{label}:\n")
            format_items(section_items)
            featured_items.extend(section_items)
    else:
        lines.append("No notable consultant-relevant items today.\n")

    if diagnostics:
        lines.append("Pipeline Diagnostics:\n")
        for message in diagnostics:
            lines.append(f"  - {message}")
        lines.append("")

    return "\n".join(lines), featured_items


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    today = datetime.date.today()
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
    frontier_watch, watch_diagnostics = fetch_frontier_watchlist(state=state)
    if watch_diagnostics:
        for message in watch_diagnostics:
            print(f"  ⚠ {message}")

    print("Fetching official product update pages...")
    raw_updates, page_snapshots = fetch_official_update_pages(state)
    state.setdefault("official_pages", {}).update(page_snapshots)
    # Local-only save here; the single GitHub commit happens after the email goes out.
    save_state(state, push_remote=False)
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
        papers = [paper for paper in papers if paper.get("score", 0) >= MIN_RESEARCH_SCORE]
        papers = papers[:MAX_RESEARCH_ITEMS]

    body, featured_items = build_body(blogs, papers, official_updates, frontier_watch=frontier_watch, diagnostics=watch_diagnostics)
    update_count = len([item for item in featured_items if item.get("is_official_update")])
    blog_count = len([item for item in featured_items if item["type"] == "Blog" and not item.get("is_official_update")])
    paper_count = len([item for item in featured_items if item["type"] == "Paper"])
    # Watchlist items are displayed in the email but not tracked in featured_items (they are
    # not deduplicated against sent_items.json). Count them separately for the subject line
    # so a digest with only watchlist headlines does not read "0 items".
    watch_count = sum(len(v) for v in frontier_watch.values())
    total = update_count + blog_count + paper_count + watch_count
    print(f"Digest ready: {update_count} official updates, {blog_count} featured blog posts, {paper_count} featured papers, {watch_count} watchlist headlines")
    subject = f"AI Digest {today} — {total} items"

    # Print to stdout (visible in Render logs) and email
    print("\n" + body)

    if can_email:
        try:
            send_email(subject, body)
            mark_sent(state, featured_items)
            mark_watchlist_shown(state, frontier_watch)
            save_state(state)
            print("✅ Email sent.")
        except Exception as ex:
            print(f"⚠ Email failed: {ex}")
            raise
    else:
        print("⚠ Skipping email (SMTP env vars not set).")


if __name__ == "__main__":
    main()
