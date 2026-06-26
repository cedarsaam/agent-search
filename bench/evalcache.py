#!/usr/bin/env python3
"""评测专用的"网络边界缓存 + 预算"层。

设计目标(整个回测 loop 的地基):
  - 把真实联网调用缓存在磁盘最底层(原始 SearXNG 结果 / 各方法原始正文 /
    站点原始 HTML / gh 原始输出 / DeepSeek 答案)。
  - 排序、清洗、打分等"被优化的代码"每轮重新跑，从而无需重复联网即可
    衡量代码改动带来的提升。
  - 用 budget.json 累计统计真实联网次数，硬性约束全流程预算。

只供 bench/ 下的脚本使用，绝不改动主流程(search.py / server.py / mcp_server.py)。
"""

import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_ROOT = os.path.join(HERE, "cache")
BUDGET_PATH = os.path.join(HERE, "budget.json")

# 全流程硬上限(对应任务预算)
HARD_NETWORK_LIMIT = 120   # search / extract / map / github 真实联网总数
HARD_ASK_LIMIT = 24        # web_ask / DeepSeek RAG 真实调用总数

# 评测层效率上限: RAG 每次最多抓 N 个候选(不改 search.py, 只在评测里收敛抓取扇出, 省预算)
EVAL_MAX_EXTRACT = 6


def _key(*parts) -> str:
    blob = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def _cache_path(kind: str, key: str) -> str:
    return os.path.join(CACHE_ROOT, kind, f"{key}.json")


def _read_cache(kind: str, key: str):
    path = _cache_path(kind, key)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None
    return None


def _write_cache(kind: str, key: str, value) -> None:
    path = _cache_path(kind, key)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False)
    except OSError:
        pass


class Budget:
    """工具级(tool-level)真实联网调用计数器。

    口径(对齐任务预算): 一个 case = 一次工具调用。
      - search/extract/map/github case 各计 1 次 network(无论内部重试几次 HTTP)。
      - ask case 计 1 次 ask(其内部 search+extract+LLM 都算在这一次 ask 里)。
    用法: run_eval 在每个 case 前后调用 begin_case()/end_case()；缓存命中(无真实
    联网)的 case 不计费。
    """

    def __init__(self, max_network=None, max_ask=None, offline=False, refresh=False):
        self.offline = offline
        self.refresh = refresh
        self.run_network = 0
        self.run_ask = 0
        self.run_network_cap = max_network if max_network is not None else 10 ** 9
        self.run_ask_cap = max_ask if max_ask is not None else 10 ** 9
        self.cum = self._load()
        self.current_kind = "network"   # 当前 case 计费口径: network / ask
        self.fetched = False            # 本 case 是否发生过真实联网
        self.internal_fetches = 0       # 仅统计用: 累计真实 HTTP 子调用次数

    def _load(self):
        if os.path.exists(BUDGET_PATH):
            try:
                with open(BUDGET_PATH, encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, ValueError):
                pass
        return {"network_total": 0, "ask_total": 0}

    def save(self):
        try:
            with open(BUDGET_PATH, "w", encoding="utf-8") as f:
                json.dump(self.cum, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    # ---- 每个 case 的边界 ----
    def begin_case(self, kind="network"):
        self.current_kind = "ask" if kind == "ask" else "network"
        self.fetched = False

    def end_case(self):
        if self.fetched:
            if self.current_kind == "ask":
                self.run_ask += 1
                self.cum["ask_total"] += 1
            else:
                self.run_network += 1
                self.cum["network_total"] += 1
        self.fetched = False

    # ---- 缓存层在真实联网前调用 ----
    def can_fetch(self) -> bool:
        if self.offline:
            return False
        if self.current_kind == "ask":
            return self.run_ask < self.run_ask_cap and self.cum["ask_total"] < HARD_ASK_LIMIT
        return self.run_network < self.run_network_cap and self.cum["network_total"] < HARD_NETWORK_LIMIT

    def note_fetch(self):
        self.fetched = True
        self.internal_fetches += 1

    def summary(self):
        return {
            "run_network": self.run_network,
            "run_ask": self.run_ask,
            "cum_network": self.cum["network_total"],
            "cum_ask": self.cum["ask_total"],
            "internal_http_fetches": self.internal_fetches,
            "hard_network_limit": HARD_NETWORK_LIMIT,
            "hard_ask_limit": HARD_ASK_LIMIT,
        }


class CacheMiss(Exception):
    """offline / 预算耗尽时的缓存未命中信号。"""


def activate(engine, budget: Budget):
    """给 AgentSearch 实例打补丁，把所有网络边界换成"磁盘缓存优先"。

    关键点:
      - 绕过引擎自带的 SQLite 缓存(get_search/get_content)，确保排序/清洗
        逻辑每轮都重新跑在原始数据上。
      - SearXNG / 抽取 / map / github / DeepSeek 各自缓存"最原始"那一层。
    """
    sys.path.insert(0, os.path.dirname(HERE))
    from search import SearchResult, SearchResponse  # noqa: E402

    # ---- 1) 关闭引擎自带 SQLite 缓存，强制走我们的原始缓存 + 新鲜逻辑 ----
    engine.cache.get_search = lambda *a, **k: None
    engine.cache.set_search = lambda *a, **k: None
    engine.cache.get_content = lambda *a, **k: None
    engine.cache.set_content = lambda *a, **k: None

    # ---- 2) SearXNG: 缓存原始结果列表，重排在 AgentSearch.search 里新鲜跑 ----
    orig_searx = engine.searxng.search

    def cached_searx(query, engines=None, page=1, time_range=None,
                     categories=None, language=None, safe_search=None):
        key = _key("searx", query, engines, page, time_range, categories, language, safe_search)
        cached = None if budget.refresh else _read_cache("searxng", key)
        if cached is not None:
            results = [SearchResult(**r) for r in cached["results"]]
            return SearchResponse(query=query, results=results, total=len(results),
                                  source=cached.get("source", "searxng"),
                                  engines_used=cached.get("engines_used", []),
                                  error=cached.get("error"))
        if not budget.can_fetch():
            return SearchResponse(query=query, results=[], total=0,
                                  source="searxng", error="CACHE_MISS_NO_BUDGET")
        budget.note_fetch()
        resp = orig_searx(query, engines=engines, page=page, time_range=time_range,
                          categories=categories, language=language, safe_search=safe_search)
        if not resp.error:
            _write_cache("searxng", key, {
                "results": [r.to_dict() for r in resp.results],
                "source": resp.source,
                "engines_used": resp.engines_used,
                "error": resp.error,
            })
        return resp

    engine.searxng.search = cached_searx

    # ---- 3) 抽取: 缓存各方法"清洗前"原始正文，_clean_text/质量判定每轮新鲜 ----
    orig_try = engine.extractor._try_extract

    def cached_try(url, method):
        key = _key("extract", url, method)
        cached = None if budget.refresh else _read_cache("extract", key)
        if cached is not None:
            return cached
        if not budget.can_fetch():
            return {"url": url, "markdown": "", "method_used": method, "error": "CACHE_MISS_NO_BUDGET"}
        budget.note_fetch()
        res = orig_try(url, method)
        # 只缓存成功抓取(有正文或明确错误)，CACHE_MISS 不写
        if res is not None:
            _write_cache("extract", key, res)
        return res

    engine.extractor._try_extract = cached_try

    # 评测层: 收敛 RAG 抓取扇出, 避免一次 ask 抓 8 个 URL 把预算打满
    orig_batch = engine.extractor.batch_extract

    def capped_batch(urls, method="auto"):
        return orig_batch(urls[:EVAL_MAX_EXTRACT], method=method)

    engine.extractor.batch_extract = capped_batch

    # ---- 4) Map: 缓存原始 HTTP 响应，解析/去重每轮新鲜 ----
    orig_get = engine.mapper.session.get

    class _FakeResp:
        def __init__(self, d):
            self.status_code = d["status_code"]
            self.text = d["text"]
            self.encoding = d.get("encoding")
            self.apparent_encoding = d.get("apparent_encoding") or "utf-8"

        def raise_for_status(self):
            if self.status_code >= 400:
                import requests
                raise requests.HTTPError(f"{self.status_code}")

    def cached_map_get(url, **kwargs):
        key = _key("mapget", url)
        cached = None if budget.refresh else _read_cache("map", key)
        if cached is not None:
            return _FakeResp(cached)
        if not budget.can_fetch():
            return _FakeResp({"status_code": 599, "text": ""})
        budget.note_fetch()
        r = orig_get(url, **kwargs)
        _write_cache("map", key, {
            "status_code": r.status_code,
            "text": r.text[:400000],
            "encoding": r.encoding,
            "apparent_encoding": getattr(r, "apparent_encoding", None),
        })
        return r

    engine.mapper.session.get = cached_map_get

    # ---- 5) GitHub: 缓存最终输出(本地 gh CLI，按 github 类型计入预算) ----
    orig_gh = engine.github.search

    def cached_gh(query, kind="repos", limit=5):
        key = _key("github", query, kind, limit)
        cached = None if budget.refresh else _read_cache("github", key)
        if cached is not None:
            return cached
        if not budget.can_fetch():
            return {"kind": kind, "query": query, "results": [], "total": 0,
                    "error": "CACHE_MISS_NO_BUDGET"}
        budget.note_fetch()
        res = orig_gh(query, kind=kind, limit=limit)
        if not res.get("error"):
            _write_cache("github", key, res)
        return res

    engine.github.search = cached_gh

    # ---- 6) DeepSeek: 按 query 缓存答案，源装配代码改动不触发重算(省 ask 预算) ----
    orig_ans = engine.llm.answer_from_sources

    def cached_answer(query, sources, max_tokens=2000):
        key = _key("deepseek", query, len(sources))
        cached = None if budget.refresh else _read_cache("deepseek", key)
        if cached is not None:
            return cached
        if not budget.can_fetch():
            return {"error": "CACHE_MISS_NO_ASK_BUDGET"}
        budget.note_fetch()
        res = orig_ans(query, sources, max_tokens=max_tokens)
        if not res.get("error"):
            _write_cache("deepseek", key, res)
        return res

    engine.llm.answer_from_sources = cached_answer

    return engine
