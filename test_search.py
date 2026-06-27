import unittest

from search import (AgentSearch, ContentExtractor, GitHubEngine, SearchCache, SearchResult,
                    clean_url, normalize_repo_slug, result_rank_score, text_quality_ok, url_is_safe)


class CacheKeyTest(unittest.TestCase):
    def test_search_cache_key_includes_options(self):
        cache = SearchCache(db_path=":memory:")
        searx_key = cache._make_key("hello", backend="searxng", top_k=5)
        flare_key = cache._make_key("hello", backend="flaresolverr", top_k=5)
        more_key = cache._make_key("hello", backend="searxng", top_k=10)

        self.assertNotEqual(searx_key, flare_key)
        self.assertNotEqual(searx_key, more_key)


class UrlCleanTest(unittest.TestCase):
    def test_clean_url_removes_trailing_backslash_noise(self):
        self.assertEqual(
            clean_url("https://platform.openai.com/docs/models/gpt-5%5C"),
            "https://platform.openai.com/docs/models/gpt-5",
        )
        self.assertEqual(clean_url("https://example.com/foo\\"), "https://example.com/foo")

    def test_clean_url_unwraps_bing_redirect(self):
        self.assertEqual(
            clean_url("https://www.bing.com/ck/a?u=a1aHR0cHM6Ly9vcGVuYWkuY29tLw"),
            "https://openai.com/",
        )


class ExtractionQualityTest(unittest.TestCase):
    def test_css_heavy_text_is_rejected(self):
        css = "@layer theme;" + ".x:where(.astro-a){color:red;}" * 200
        self.assertFalse(text_quality_ok(css))

    def test_clean_text_removes_css_lines(self):
        extractor = ContentExtractor.__new__(ContentExtractor)
        cleaned = extractor._clean_text(
            "@layer theme, base;\n"
            "._Card_abc:where(.astro-x){color:red;background:#fff;}\n"
            "OpenAI API Pricing\n"
            "GPT-5 input $1.25 per 1M tokens"
        )
        self.assertIn("OpenAI API Pricing", cleaned)
        self.assertIn("GPT-5 input", cleaned)
        self.assertNotIn("@layer", cleaned)

    def test_clean_text_keeps_long_prose_collapsed_to_one_line(self):
        # 整页正文被压成一行(含若干分号), 不应被噪声过滤误删
        extractor = ContentExtractor.__new__(ContentExtractor)
        long_line = ("Python is a programming language created by Guido van Rossum; "
                     "it supports multiple paradigms; see the docs for details. ") * 40
        cleaned = extractor._clean_text(long_line)
        self.assertIn("Guido van Rossum", cleaned)
        self.assertGreater(len(cleaned), 500)


class RankingTest(unittest.TestCase):
    def test_official_domain_beats_keyword_stuffed_seo_page(self):
        query = "OpenAI API GPT-5 pricing"
        official = SearchResult(
            title="API Pricing - OpenAI",
            url="https://openai.com/api/pricing/",
            snippet="GPT-5 price per 1M tokens",
            score=1.0,
        )
        seo = SearchResult(
            title="OpenAI API Pricing Guide 2026: What GPT-5 Models Actually Cost",
            url="https://example-seo.test/openai-api-pricing-guide",
            snippet="OpenAI GPT-5 pricing guide OpenAI GPT-5 pricing guide OpenAI GPT-5 pricing guide",
            score=0.1,
        )

        self.assertGreater(result_rank_score(query, official, 1), result_rank_score(query, seo, 0))

    def test_content_farm_is_downweighted_below_official_docs(self):
        query = "Python asyncio coroutines tutorial"
        docs = SearchResult(
            title="Coroutines and Tasks — Python docs",
            url="https://docs.python.org/3/library/asyncio-task.html",
            snippet="asyncio coroutine tutorial", score=0.5,
        )
        farm = SearchResult(
            title="Python asyncio 协程教程",
            url="https://blog.csdn.net/xxx/article/details/123",
            snippet="asyncio coroutine 教程 asyncio coroutine 教程", score=0.9,
        )
        self.assertGreater(result_rank_score(query, docs, 1), result_rank_score(query, farm, 0))

    def test_authoritative_path_boosts_pricing_page(self):
        query = "OpenAI API pricing"
        pricing = SearchResult(
            title="Pricing", url="https://openai.com/api/pricing/",
            snippet="tokens", score=0.3,
        )
        marketing = SearchResult(
            title="OpenAI", url="https://openai.com/index/about/",
            snippet="company", score=0.3,
        )
        self.assertGreater(result_rank_score(query, pricing, 0), result_rank_score(query, marketing, 1))


class CachedExtractTest(unittest.TestCase):
    def test_cached_search_still_runs_extract(self):
        class FakeCache:
            def get_search(self, query, engines=None, **options):
                return {
                    "query": query,
                    "results": [{"title": "T", "url": "https://example.com/a%5C"}],
                    "total": 1,
                    "source": "searxng",
                    "error": None,
                    "engines_used": ["test"],
                    "elapsed_ms": 0,
                    "from_cache": False,
                }

        class FakeExtractor:
            def __init__(self):
                self.calls = []

            def extract(self, url, method="auto"):
                self.calls.append((url, method))
                return {"url": url, "markdown": "正文内容", "method_used": method, "error": None}

        search = AgentSearch.__new__(AgentSearch)
        search.cache = FakeCache()
        search.extractor = FakeExtractor()
        search._prefer_searxng = True

        result = search.search("q", top_k=1, extract=True)

        self.assertTrue(result["from_cache"])
        self.assertEqual(search.extractor.calls, [("https://example.com/a", "auto")])
        self.assertIn("extracted", result)


class GitHubCompareTest(unittest.TestCase):
    def test_normalize_repo_slug(self):
        self.assertEqual(normalize_repo_slug("https://github.com/tiangolo/fastapi"), "tiangolo/fastapi")
        self.assertEqual(normalize_repo_slug("tiangolo/fastapi"), "tiangolo/fastapi")
        self.assertEqual(normalize_repo_slug("https://github.com/django/django.git"), "django/django")
        self.assertEqual(normalize_repo_slug("owner/repo/tree/main"), "owner/repo")

    def test_compare_requires_input(self):
        gh = GitHubEngine()
        res = gh.compare(repos=None, query=None)
        self.assertEqual(res["compared"], 0)
        self.assertTrue(res["error"])

    def test_repo_facts_flags_archived_and_copyleft(self):
        # 不联网: 注入假的 gh/deps.dev, 只验证"成熟度信号"派生逻辑
        gh = GitHubEngine()
        gh._gh_json = lambda path: ({
            "full_name": "old/proj", "stargazers_count": 5, "forks_count": 1,
            "license": {"spdx_id": "AGPL-3.0"}, "language": "Python",
            "open_issues_count": 9, "archived": True,
            "created_at": "2015-01-01T00:00:00Z", "pushed_at": "2016-01-01T00:00:00Z",
        } if path == "repos/old/proj" else None)
        gh._deps_dev = lambda repo: {"scorecard_overall": 2.1, "scorecard_checks": {"Maintained": 0}}
        f = gh.repo_facts("old/proj")
        self.assertIn("已归档", f["flags"])
        self.assertTrue(any("copyleft" in x for x in f["flags"]))
        self.assertTrue(any("无提交" in x for x in f["flags"]))
        self.assertEqual(f["scorecard_overall"], 2.1)


class SSRFGuardTest(unittest.TestCase):
    def test_blocks_internal_targets(self):
        for u in ("http://localhost/x", "http://127.0.0.1/x", "http://169.254.169.254/",
                  "http://192.168.1.1/", "http://10.0.0.5/", "http://foo.internal/x",
                  "ftp://example.com/x"):
            self.assertFalse(url_is_safe(u), u)

    def test_allows_public(self):
        for u in ("https://docs.python.org/3/", "https://fastapi.tiangolo.com/",
                  "http://example.com/page"):
            self.assertTrue(url_is_safe(u), u)

    def test_safe_get_rejects_redirect_to_internal(self):
        # 公网起点过校验, 但服务端 302 跳内网 → 必须被拦
        from search import safe_get

        class _Resp:
            def __init__(self, status, location=None):
                self.status_code = status
                self.headers = {"Location": location} if location else {}
                self.is_redirect = location is not None and status in (301, 302, 303, 307, 308)

        class _Sess:
            def get(self, url, timeout=None, allow_redirects=False, **kw):
                return _Resp(302, "http://127.0.0.1/secret")  # 永远跳内网

        with self.assertRaises(ValueError):
            safe_get(_Sess(), "http://93.184.216.34/start")  # 公网 IP 字面量起点

    def test_dns_resolved_private_ip_blocked(self):
        # 公网域名解析到内网(DNS rebinding) → 开启 DNS 校验时必须被拦
        import os
        import socket
        import search
        orig_gai, orig_flag = socket.getaddrinfo, os.environ.get("AGENT_SEARCH_RESOLVE_DNS")
        socket.getaddrinfo = lambda host, *a, **k: [(2, 1, 6, "", ("10.0.0.7", 0))]
        os.environ["AGENT_SEARCH_RESOLVE_DNS"] = "1"
        try:
            self.assertFalse(search.url_is_safe("http://looks-public.example/x"))
        finally:
            socket.getaddrinfo = orig_gai
            if orig_flag is None:
                os.environ.pop("AGENT_SEARCH_RESOLVE_DNS", None)
            else:
                os.environ["AGENT_SEARCH_RESOLVE_DNS"] = orig_flag


class MultiSearchTest(unittest.TestCase):
    def test_merges_dedups_and_counts_failures(self):
        from search import SearchResponse

        s = AgentSearch.__new__(AgentSearch)

        class C:  # 假缓存: 永远 miss / no-op
            def get_search(self, *a, **k):
                return None

            def set_search(self, *a, **k):
                pass

        s.cache = C()

        def fake(q):
            if q == "qerr":
                return SearchResponse(query=q, error="boom")
            if q == "q1":
                return SearchResponse(query=q, results=[
                    SearchResult(title="A", url="https://a.com/x"),
                    SearchResult(title="B", url="https://b.com/y"),
                ])
            return SearchResponse(query=q, results=[
                SearchResult(title="B2", url="https://b.com/y"),   # 与 q1 重复
                SearchResult(title="C", url="https://c.com/z"),
            ])

        class SX:
            def search(self, q, engines=None, **k):
                return fake(q)

        s.searxng = SX()
        merged, perr, nfail = s._multi_search(["q1", "q2", "qerr"])
        self.assertEqual([r.url for r in merged],
                         ["https://a.com/x", "https://b.com/y", "https://c.com/z"])
        self.assertIsNone(perr)       # 主查询 q1 成功
        self.assertEqual(nfail, 1)    # qerr 失败被计数, 不静默吞


if __name__ == "__main__":
    unittest.main()
