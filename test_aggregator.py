import sys
import types
import unittest

sys.modules.setdefault("feedparser", types.SimpleNamespace())
sys.modules.setdefault("requests", types.SimpleNamespace())
sys.modules.setdefault("anthropic", types.SimpleNamespace(Anthropic=object))
sys.modules.setdefault("bs4", types.SimpleNamespace(BeautifulSoup=object))
sys.modules.setdefault("dotenv", types.SimpleNamespace(load_dotenv=lambda: None))

import aggregator


class AggregatorRelevanceTests(unittest.TestCase):
    def test_rejects_claude_category_pages(self):
        article = {
            "source": "Claude Blog",
            "type": "Blog",
            "title": "Product announcements",
            "url": "https://claude.com/blog/category/announcements",
            "summary": "Browse Claude product announcements.",
        }
        self.assertFalse(aggregator.looks_relevant(article))

    def test_rejects_openai_customer_story_titles(self):
        article = {
            "source": "OpenAI",
            "type": "Blog",
            "title": "Uber uses OpenAI to help people earn smarter and book faster",
            "url": "https://openai.com/index/uber",
            "summary": "A customer story about adopting OpenAI products.",
        }
        self.assertFalse(aggregator.looks_relevant(article))

    def test_rejects_openai_education_program_posts(self):
        article = {
            "source": "OpenAI",
            "type": "Blog",
            "title": "Introducing ChatGPT Futures: Class of 2026",
            "url": "https://openai.com/index/introducing-chatgpt-futures-class-of-2026",
            "summary": "A new student and education initiative.",
        }
        self.assertFalse(aggregator.looks_relevant(article))

    def test_keeps_openai_product_posts(self):
        article = {
            "source": "OpenAI",
            "type": "Blog",
            "title": "Introducing the Responses API",
            "url": "https://openai.com/index/introducing-the-responses-api",
            "summary": "New API primitives for developers building agents and tool use workflows.",
        }
        self.assertTrue(aggregator.looks_relevant(article))

    def test_article_path_helper_accepts_real_posts(self):
        self.assertTrue(aggregator.looks_like_article_path("/blog/introducing-claude-code"))

    def test_article_path_helper_rejects_indexes(self):
        self.assertFalse(aggregator.looks_like_article_path("/blog/category/announcements"))


if __name__ == "__main__":
    unittest.main()
