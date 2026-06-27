#!/usr/bin/env python3
"""
Agent Search — 本地搜索增强中间件 / local search-augmentation middleware
======================================================================

架构:
  SearXNG (本地 Docker, JSON API)
       ↓
  Python Wrapper (缓存 + 聚合 + 排序)
       ↓
  Jina Reader (r.jina.ai → URL→Markdown, 免费)
       ↓
  Crawl4AI (深度 JS 渲染抓取, 兜底)
       ↓
  Hermes 工具调用

用法:
  python search.py "你的搜索词"
  python search.py "你的搜索词" --engines google,bing,github
  python search.py "你的搜索词" --extract  # 同时提取首条结果全文
  python search.py "你的搜索词" --deep     # 用 Crawl4AI 深度抓取

依赖:
  pip install requests beautifulsoup4 lxml
  # 可选: pip install crawl4ai
"""

import json
import base64
import ipaddress
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.parse
from copy import deepcopy
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urljoin, urlparse, urlunparse

try:
    import requests
except ImportError:
    print("[!] 需要 requests 库: pip install requests")
    sys.exit(1)


# ================================================================
# 配置
# ================================================================

SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://localhost:8888")
CACHE_DIR = os.environ.get("CACHE_DIR", os.path.expanduser("~/.cache/agent-search"))
CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL", "3600"))  # 1h default
CACHE_SCHEMA_VERSION = 7  # v7: cache_options 纳入 expand_mode(覆盖 auto_rewrite 维度, 修 BUG-3)
JINA_READER = "https://r.jina.ai"     # URL→Markdown (免费)
REQUEST_TIMEOUT = 15

# DeepSeek API (生成/汇总层) — OpenAI 兼容接口
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

# FlareSolverr — CAPTCHA 绕过
FLARESOLVERR_URL = os.environ.get("FLARESOLVERR_URL", "http://localhost:8191")


def _load_dotenv():
    """轻量加载同目录 .env (无需 python-dotenv 依赖)。已存在的环境变量不覆盖。"""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        return
    try:
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except OSError:
        pass


_load_dotenv()
# .env 加载后刷新配置
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

# 批量抽取预算(可配): 宽召回(answer / 方案对比)场景的并发抽取上限。
# 旧实现把 8 条 / 6 worker 写死, 宽召回时优质候选抽不到、answer 凑不够来源。
EXTRACT_MAX_URLS = int(os.environ.get("EXTRACT_MAX_URLS", "12"))
EXTRACT_CONCURRENCY = int(os.environ.get("EXTRACT_CONCURRENCY", "8"))
EXTRACT_TIMEOUT_S = int(os.environ.get("EXTRACT_TIMEOUT_S", "20"))

# SearXNG 默认启用的引擎 (settings.yml 中配置的)
DEFAULT_ENGINES = [
    "google", "bing", "duckduckgo", "brave", "wikipedia",
    "github", "stackoverflow", "reddit", "news"
]

# 通用的低质量 SEO / 内容农场域名(用于排序降权)。
# 这是"内容农场"这一类的通用表达, 不是为单个 case 写死的规则。
CONTENT_FARM_HOSTS = (
    "csdn.net", "51cto.com", "juejin.cn", "cnblogs.com", "jianshu.com",
    "segmentfault.com", "oschina.net", "php.cn", "yisu.com", "toutiao.com",
    "sohu.com", "baijiahao.baidu.com", "zhihu.com", "w3schools.com",
    "geeksforgeeks.org", "runoob.com", "tutorialspoint.com", "simplilearn.com",
    "guru99.com", "javatpoint.com", "programiz.com", "donews.com",
)

# 文档/参考/版本/价格类 URL 路径片段(命中则加权, 通用表达)。
AUTHORITATIVE_PATH_RE = re.compile(
    r"/(docs?|documentation|reference|api|guide|manual|changelog|releases?|"
    r"pricing|whatsnew|release-notes)(/|$|\.)"
)

# 技术选型对比: deps.dev 免费一手 API(无需 key), 返回 OpenSSF Scorecard 健康分等
DEPS_DEV_PROJECT_API = "https://api.deps.dev/v3alpha/projects/github.com%2F"


# SSRF 防护: 默认拒绝抓取指向私网/环回/保留地址的 URL。
# 需要抓本地/内网(如本地文档站)时, 设环境变量 AGENT_SEARCH_ALLOW_PRIVATE=1 放开。
ALLOW_PRIVATE_URLS = os.environ.get("AGENT_SEARCH_ALLOW_PRIVATE", "").lower() in ("1", "true", "yes")


_INTERNAL_HOST_RE = re.compile(r"(^|\.)(localhost|local|internal|intranet|lan|home|corp)$", re.I)


def _ip_blocked(addr) -> bool:
    return (addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_reserved or addr.is_multicast or addr.is_unspecified)


def _dns_check_enabled() -> bool:
    """DNS 解析校验默认关。常见 fake-ip / 分裂 DNS / 透明代理环境会把公网域名解析到
    198.18.0.0/15 等保留段, 开了会全量误杀。硬化的直连部署可设 AGENT_SEARCH_RESOLVE_DNS=1。"""
    return os.environ.get("AGENT_SEARCH_RESOLVE_DNS", "0").lower() in ("1", "true", "yes")


def _resolve_ips_blocked(host: str) -> bool:
    """解析主机名的全部 A/AAAA, 任一落在私网/环回/保留即拦截(防 DNS rebinding /
    公网域名解析到内网)。解析失败返回 False(交给后续请求自行失败, 不因解析不了误判)。"""
    import socket
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, OSError, ValueError):
        return False
    for info in infos:
        ip = (info[4][0] or "").split("%")[0]  # 去 IPv6 scope id
        try:
            if _ip_blocked(ipaddress.ip_address(ip)):
                return True
        except ValueError:
            continue
    return False


def url_is_safe(url: str) -> bool:
    """抓取前的 SSRF 校验(拦截标准 SSRF 载荷)。

    - IP 字面量按私网/环回/链路本地(含云元数据 169.254.169.254)/保留判定并拒绝。
    - 明显内部主机名(localhost / *.local / *.internal 等)拒绝。
    - 公网主机名: 仅当 AGENT_SEARCH_RESOLVE_DNS=1 时解析 A/AAAA, 任一指向内网即拒绝
      (防 DNS rebinding; 默认关, 因 fake-ip/透明代理环境会误杀)。
    可设 AGENT_SEARCH_ALLOW_PRIVATE=1 整体放开(用于抓本地/内网文档)。
    """
    if ALLOW_PRIVATE_URLS:
        return True
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    host = parsed.hostname
    if parsed.scheme not in ("http", "https") or not host:
        return False
    try:
        return not _ip_blocked(ipaddress.ip_address(host))  # IP 字面量
    except ValueError:
        pass
    if host.lower() == "localhost" or _INTERNAL_HOST_RE.search(host):
        return False
    if _dns_check_enabled() and _resolve_ips_blocked(host):
        return False
    return True


# 抓取统一走 safe_get: 禁用自动重定向, 对每一跳 Location 重新做 url_is_safe 校验后才跟随,
# 堵住"公网 URL 过校验 → 服务端 302 跳内网/元数据"的 SSRF 绕过。
MAX_REDIRECTS = 4


def safe_get(session, url, *, timeout=REQUEST_TIMEOUT, max_redirects=MAX_REDIRECTS, **kwargs):
    """SSRF 安全的 GET。逐跳校验重定向目标; 目标不安全抛 ValueError, 跳数超限亦然。"""
    kwargs.pop("allow_redirects", None)
    current = url
    for _ in range(max_redirects + 1):
        if not url_is_safe(current):
            raise ValueError(f"SSRF 拒绝(指向私网/环回/保留地址): {current}")
        resp = session.get(current, timeout=timeout, allow_redirects=False, **kwargs)
        if resp.is_redirect:  # 状态码属重定向且带 Location
            current = urljoin(current, resp.headers.get("Location", ""))
            continue
        return resp
    raise ValueError(f"重定向过多(>{max_redirects}): {url}")


def normalize_repo_slug(repo: str) -> str:
    """把各种写法的 GitHub 仓库归一成 'owner/name'。"""
    repo = (repo or "").strip().strip("'\"")
    repo = re.sub(r"^https?://(www\.)?github\.com/", "", repo, flags=re.I)
    repo = repo.rstrip("/")
    if repo.endswith(".git"):
        repo = repo[:-4]
    parts = [p for p in repo.split("/") if p]
    return f"{parts[0]}/{parts[1]}" if len(parts) >= 2 else repo


def clean_url(url: str) -> str:
    """清理搜索引擎返回的常见 URL 污染。"""
    url = (url or "").strip().strip("'\"")
    while url.lower().endswith("%5c"):
        url = url[:-3].rstrip()
    url = url.rstrip("\\").strip()

    try:
        parsed = urllib.parse.urlparse(url)
        if "bing.com" in parsed.netloc and parsed.path.startswith("/ck/"):
            u = urllib.parse.parse_qs(parsed.query).get("u", [""])[0]
            if u.startswith("a1"):
                encoded = u[2:]
                padding = "=" * (-len(encoded) % 4)
                decoded = base64.urlsafe_b64decode(encoded + padding).decode("utf-8", errors="ignore")
                if decoded.startswith("http"):
                    return decoded
    except Exception:
        pass

    return url


def canonical_url(url: str) -> str:
    """用于去重和排序的规范化 URL。"""
    url = clean_url(url)
    try:
        parsed = urlparse(url)
        scheme = parsed.scheme.lower() or "https"
        netloc = parsed.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        path = parsed.path.rstrip("/") or "/"
        query_items = []
        for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
            lk = key.lower()
            if lk.startswith("utm_") or lk in {"fbclid", "gclid", "yclid", "mc_cid", "mc_eid"}:
                continue
            query_items.append((key, value))
        query = urllib.parse.urlencode(query_items, doseq=True)
        return urlunparse((scheme, netloc, path, "", query, ""))
    except Exception:
        return url


def text_quality_ok(text: str, min_chars: int = 120) -> bool:
    """过滤明显不是正文的内容，比如整页 CSS/JS。"""
    if not text:
        return False
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) < min_chars:
        return False

    head = compact[:6000]
    css_noise = head.count("{") + head.count("}") + head.count(";")
    if head.startswith("@layer ") or css_noise > 900:
        return False
    if len(re.findall(r"\._?[A-Za-z0-9_-]+[:.\[]", head)) > 120:
        return False
    if "astro-island" in head and css_noise > 300:
        return False
    return True


# 自然语言样板行(cookie 横幅/登录注册/分享/版权/订阅/导航)。只对"短行"生效, 长正文永不删。
_BOILERPLATE_RE = re.compile(
    r"(we use cookies|accept (all )?cookies|cookie (settings|policy|preferences|consent)|"
    r"manage (your )?(cookies|preferences)|this (site|website) uses cookies|"
    r"sign\s?in|sign\s?up|log\s?in|create (an )?account|"
    r"subscribe to (our )?newsletter|sign up for|"
    r"skip to (main )?content|back to top|"
    r"all rights reserved|©\s?\d{4}|\(c\)\s?\d{4}|"
    r"share (on|this)|follow us on|"
    r"本(网站|站)使用|使用\s?cookie|我们使用\s?cookie|cookie\s?(设置|政策|偏好|声明)|"
    r"立即(登录|注册)|登录\s?[/·|]\s?注册|"
    r"版权所有|保留所有权利|订阅(我们的)?(电子报|新闻|资讯|邮件)|"
    r"分享到|关注我们|返回顶部|跳(到|转)(主要)?内容)",
    re.I)


def _is_boilerplate_line(line: str) -> bool:
    """判定一行是否为样板(只对短行生效, 保护长正文/段落)。"""
    if len(line) > 80:                       # 长行视为正文, 永不删
        return False
    if _BOILERPLATE_RE.search(line):
        return True
    # 纯链接/导航短行: 去掉 markdown 链接/裸 URL/分隔符后近乎为空
    stripped = re.sub(r"\[[^\]]*\]\([^)]*\)", "", line)
    stripped = re.sub(r"https?://\S+", "", stripped)
    stripped = re.sub(r"[\s|·•‹›<>/\\\-—_]+", "", stripped)
    if (line.count("](") >= 3 or line.lower().count("http") >= 3) and len(stripped) <= 3:
        return True
    return False


def query_terms(query: str) -> list[str]:
    """提取用于片段相关性排序的轻量关键词。"""
    terms = []
    for part in re.findall(r"[A-Za-z0-9_.-]+|[\u4e00-\u9fff]{2,}", query.lower()):
        if len(part) >= 2 and part not in {"the", "and", "for", "with", "是多少"}:
            terms.append(part)
    return list(dict.fromkeys(terms))


DOC_INTENT_RE = re.compile(
    r"\b(tutorial|guide|how\s+to|what\s+is|documentation|docs|api|reference|sdk|"
    r"example|usage|best\s+practice|getting\s+started)\b"
    r"|教程|文档|指南|手册|用法|示例|最佳实践|怎么|如何|是什么|入门", re.I)
PRICE_INTENT_RE = re.compile(
    r"\b(pricing|price|cost|how\s+much)\b|价格|定价|收费|多少钱", re.I)


def query_expansions(query: str) -> list[str]:
    """对"找官方文档/价格"意图的查询给出 1 个增强查询(追加通用 hint)。

    只追加 "official documentation" / "official pricing" 这类**通用**修饰词,
    不绑定任何具体站点; 由调用方把增强查询的结果与原结果合并后统一重排。
    已含 official/官方 的查询不再增强(避免重复)。
    """
    q = (query or "").lower()
    if "official" in q or "官方" in q:
        return []
    if PRICE_INTENT_RE.search(query or ""):
        return [f"{query} official pricing"]
    if DOC_INTENT_RE.search(query or ""):
        return [f"{query} official documentation"]
    return []


# "对比/选型"意图: 命中则把查询扇出成多角度子查询, 一次并发把候选方案找全找快。
COMPARE_INTENT_RE = re.compile(
    r"\b(vs\.?|versus|alternatives?|compare|comparison|which\s+is\s+better)\b"
    r"|对比|相比|选型|哪个好|哪个更好|哪个比较好|区别|替代|代替|孰优|谁更", re.I)

PLAN_MAX_SUBQ = int(os.environ.get("PLAN_MAX_SUBQ", "5"))

# 容错搜索: 结果数低于此阈值才触发"纠错重搜"(短路, 正常查询零额外开销)
FUZZY_MIN_RESULTS = int(os.environ.get("FUZZY_MIN_RESULTS", "3"))

# 容错纠错(L1) 内置高频技术词表(硬兜底, 可继续补)。rapidfuzz 为可选依赖, 缺失则整层静默降级。
COMMON_TECH_TERMS = {
    "skill", "skills", "python", "javascript", "typescript", "kubernetes", "docker",
    "anthropic", "claude", "openai", "deepseek", "pydantic", "fastapi", "flask", "django",
    "asyncio", "async", "await", "coroutine", "numpy", "pandas", "pytorch", "tensorflow",
    "postgres", "postgresql", "mysql", "sqlite", "redis", "memcached", "mongodb", "kafka",
    "nginx", "react", "vue", "svelte", "nextjs", "node", "express", "rust", "golang",
    "rapidfuzz", "searxng", "trafilatura", "crawl4ai", "selenium", "playwright", "tutorial",
    "documentation", "pricing", "comparison", "benchmark", "alternatives", "kubernetes",
}


def _ascii_tokens(query: str) -> list[str]:
    """仅取 ASCII token 用于编辑距离纠错; CJK 不参与(靠字形而非编辑距离, 交给 L0/L3)。"""
    return [t for t in query_terms(query) if re.fullmatch(r"[a-z0-9_.-]+", t)]


def build_correction_vocab(cache, results) -> set:
    """纠错词典 = 内置词表 ∪ 本轮结果 title token ∪ 历史查询 token(尽力而为, 失败忽略)。"""
    vocab = set(COMMON_TECH_TERMS)
    for r in results or []:
        for t in re.findall(r"[a-z0-9_.-]{3,}", (getattr(r, "title", "") or "").lower()):
            vocab.add(t)
    try:
        with sqlite3.connect(cache.db_path) as conn:
            for (key,) in conn.execute("SELECT cache_key FROM search_cache LIMIT 500"):
                q = json.loads(key).get("query", "")
                for t in re.findall(r"[a-z0-9_.-]{3,}", q.lower()):
                    vocab.add(t)
    except Exception:
        pass
    return vocab


def fuzzy_correct_query(query: str, vocab: set, max_fix: int = 2) -> list[str]:
    """对 query 的 ASCII token 做编辑距离纠错, 生成至多 1 个纠正变体(保留 CJK/语序)。

    误纠防护: token 已在词典→跳过(正确词不纠); 相似度 >=88 且编辑距离 <= 长度自适应阈值
    (len<=4→1, <=8→2, 否则 3) 才替换; 一次最多纠 max_fix 个。rapidfuzz 未装→[] 静默降级。
    """
    try:
        from rapidfuzz import process, fuzz, distance
    except ImportError:
        return []
    if not vocab:
        return []
    vocab_lower = {v.lower() for v in vocab}
    fixes, fixed = {}, 0
    for tok in dict.fromkeys(_ascii_tokens(query)):
        if fixed >= max_fix or tok in vocab_lower:
            continue
        max_dist = 1 if len(tok) <= 4 else (2 if len(tok) <= 8 else 3)
        cand = process.extractOne(tok, vocab_lower, scorer=fuzz.ratio, score_cutoff=88)
        if not cand:
            continue
        best = cand[0]
        if best != tok and distance.Levenshtein.distance(tok, best) <= max_dist:
            fixes[tok] = best
            fixed += 1
    if not fixes:
        return []
    variant = query
    for tok, best in fixes.items():           # 就地替换 typo token, 不破坏 CJK/语序/其余词
        variant = re.sub(rf"\b{re.escape(tok)}\b", best, variant, flags=re.I)
    return [variant] if variant.lower() != query.strip().lower() else []


def plan_queries(query: str, mode: str = "auto") -> list[tuple]:
    """把查询规划成 (子查询, 权重, 标签) 列表(不含原查询)。

    mode:
      off     → 不扩展, 返回 []
      compare → 强制对比扇出(alternatives/comparison/benchmark/best + 官方增强)
      auto    → 命中对比意图才扇出, 否则退化为 query_expansions(文档/价格官方增强)
    权重<1, 供调用方轻度下压"角度子查询"的结果, 避免 best/benchmark 类软文盖过官方源。
    子查询数封顶 PLAN_MAX_SUBQ。
    """
    if mode == "off":
        return []
    q = (query or "").strip()
    if not q:
        return []
    is_compare = mode == "compare" or bool(COMPARE_INTENT_RE.search(q))
    plan: list[tuple] = []
    if is_compare:
        plan += [
            (f"{q} alternatives", 0.85, "alt"),
            (f"{q} comparison", 0.85, "vs"),
            (f"{q} benchmark", 0.7, "benchmark"),
            (f"best {q}", 0.7, "best"),
        ]
    for e in query_expansions(q):           # 官方文档/价格增强(对比与非对比都加)
        plan.append((e, 0.9, "official"))
    # 去重 + 去掉与原查询相同 + 封顶
    seen, out = set(), []
    for subq, w, tag in plan:
        k = subq.lower()
        if subq and k != q.lower() and k not in seen:
            seen.add(k)
            out.append((subq, w, tag))
        if len(out) >= PLAN_MAX_SUBQ:
            break
    return out


def result_rank_score(query: str, result: "SearchResult", index: int) -> float:
    """轻量重排：综合引擎分数、关键词命中、官方/文档域名和原始位置。"""
    terms = query_terms(query)
    text = f"{result.title} {result.snippet}".lower()
    term_hits = min(sum(text.count(t) for t in terms), 5)
    title_hits = min(sum((result.title or "").lower().count(t) for t in terms), 3)

    score = float(result.score or 0.0)
    score += term_hits * 1.2 + title_hits * 1.8
    score += max(0, 10 - index) * 0.05

    try:
        host = urlparse(result.url).netloc.lower()
    except Exception:
        host = ""
    official_hints = ("docs.", "developer.", "developers.", "api.", "github.com", "wikipedia.org")
    if any(h in host for h in official_hints):
        score += 2.0
    host_tokens = set(re.findall(r"[a-z0-9]+", host))
    query_host_hits = sum(1 for t in terms if t in host_tokens or any(t in h for h in host_tokens))
    score += min(query_host_hits * 3.0, 6.0)
    if host.endswith(("openai.com", "anthropic.com", "microsoft.com", "google.com", "github.com")):
        score += 1.5

    # 文档/API/参考/价格/版本 路径加权(通用, 不绑定具体站点)
    try:
        path = urlparse(result.url).path.lower()
    except Exception:
        path = ""
    if AUTHORITATIVE_PATH_RE.search(path):
        score += 1.5

    # 低质量 SEO / 内容农场降权(通用域名类, 无条件)
    if any(host == cf or host.endswith("." + cf) for cf in CONTENT_FARM_HOSTS):
        score -= 2.5
    # 老式 medium 软文在技术/文档/价格意图下额外降权
    if "medium.com" in host and any(
        t in query.lower() for t in ("api", "docs", "pricing", "版本", "价格", "documentation")
    ):
        score -= 1.0

    if re.search(r"20\d{2}[-年/]\d{1,2}|latest|release|changelog|更新|发布", text):
        score += 0.6

    # 容错 L2: query 中"未精确出现在标题"的 ASCII 词, 用模糊匹配补分(近形命中也能浮上来)。
    # rapidfuzz 可选, 缺失则跳过(排序退化为现有行为)。
    try:
        from rapidfuzz import fuzz
        title_l = (result.title or "").lower()
        fuzzy_bonus = 0.0
        for t in terms:
            if len(t) < 4 or not re.fullmatch(r"[a-z0-9_.-]+", t) or t in title_l:
                continue
            if fuzz.partial_ratio(t, title_l) >= 88:
                fuzzy_bonus += 1.0
        score += min(fuzzy_bonus, 3.0)
    except ImportError:
        pass
    return score


# ================================================================
# 数据模型
# ================================================================

@dataclass
class SearchResult:
    title: str
    url: str
    content: str = ""
    snippet: str = ""
    engine: str = ""
    score: float = 0.0
    source: str = "searxng"  # searxng, jina, cache
    fetched_at: str = ""

    def to_dict(self):
        return asdict(self)


@dataclass
class SearchResponse:
    query: str
    results: list = field(default_factory=list)
    total: int = 0
    source: str = ""
    elapsed_ms: int = 0
    engines_used: list = field(default_factory=list)
    error: Optional[str] = None
    corrections: list = field(default_factory=list)   # SearXNG 拼写纠正词(brave spellcheck 等引擎贡献)
    suggestions: list = field(default_factory=list)   # SearXNG 相关查询建议


# ================================================================
# SQLite 缓存
# ================================================================

class SearchCache:
    """本地 SQLite 缓存，避免重复搜索。默认 TTL 1h。"""

    def __init__(self, db_path=None):
        self.db_path = db_path or os.path.join(CACHE_DIR, "search_cache.db")
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS search_cache (
                    cache_key TEXT PRIMARY KEY,
                    response_json TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS content_cache (
                    url TEXT PRIMARY KEY,
                    markdown TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("PRAGMA wal_mode=1")
            conn.commit()

    def _make_key(self, query, engines=None, **options):
        payload = {
            "v": CACHE_SCHEMA_VERSION,
            "query": query.strip().lower(),
            "engines": sorted(engines or []),
            "options": {k: options[k] for k in sorted(options) if options[k] is not None},
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def _content_key(self, url, method="auto"):
        return self._make_key(clean_url(url), engines=None, method=method or "auto")

    def get_search(self, query, engines=None, **options):
        key = self._make_key(query, engines, **options)
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT response_json, created_at FROM search_cache WHERE cache_key = ?",
                (key,)
            ).fetchone()
        if row:
            created = datetime.strptime(row[1], "%Y-%m-%d %H:%M:%S")
            if (datetime.now() - created).total_seconds() < CACHE_TTL_SECONDS:
                return json.loads(row[0])
        return None

    def set_search(self, query, response_dict, engines=None, **options):
        key = self._make_key(query, engines, **options)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO search_cache (cache_key, response_json) VALUES (?, ?)",
                (key, json.dumps(response_dict, ensure_ascii=False))
            )
            conn.commit()

    def get_content(self, url, method="auto"):
        key = self._content_key(url, method)
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT markdown, created_at FROM content_cache WHERE url = ?",
                (key,)
            ).fetchone()
        if row:
            created = datetime.strptime(row[1], "%Y-%m-%d %H:%M:%S")
            if (datetime.now() - created).total_seconds() < CACHE_TTL_SECONDS:
                return row[0]
        return None

    def set_content(self, url, markdown, method="auto"):
        key = self._content_key(url, method)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO content_cache (url, markdown) VALUES (?, ?)",
                (key, markdown)
            )
            conn.commit()

    def stats(self):
        with sqlite3.connect(self.db_path) as conn:
            search_count = conn.execute("SELECT COUNT(*) FROM search_cache").fetchone()[0]
            content_count = conn.execute("SELECT COUNT(*) FROM content_cache").fetchone()[0]
            return {"search_cached": search_count, "content_cached": content_count}

    def clear_expired(self):
        cutoff = (datetime.now() - timedelta(seconds=CACHE_TTL_SECONDS)).strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM search_cache WHERE created_at < ?", (cutoff,))
            conn.execute("DELETE FROM content_cache WHERE created_at < ?", (cutoff,))
            conn.commit()


# ================================================================
# 搜索引擎后端
# ================================================================

class SearXNGEngine:
    """通过本地 SearXNG JSON API 搜索"""

    def __init__(self, base_url=SEARXNG_URL):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        })

    def is_available(self):
        """检查 SearXNG 是否在运行。

        用轻量的 /healthz 端点，避免触发真实搜索（真实搜索会等被 CAPTCHA
        拦截的引擎超时，可能 >5s，导致误判离线）。
        """
        try:
            r = self.session.get(f"{self.base_url}/healthz", timeout=5)
            return r.status_code == 200
        except (requests.ConnectionError, requests.Timeout):
            return False

    def search(self, query, engines=None, page=1, time_range=None, categories=None, language=None, safe_search=None):
        """调用 SearXNG JSON API

        Args:
            query: 搜索词
            engines: 引擎列表, 如 ["google","github"]
            page: 页码, 默认 1
            time_range: None / "day" / "month" / "year"
            categories: None 或 ["general", "news", "images"]
            language: 语言代码，如 "zh-CN" / "en-US"
            safe_search: None / 0 / 1 / 2
        """
        params = {
            "q": query,
            "format": "json",
            "pageno": page,
        }
        if engines:
            params["engines"] = ",".join(engines)
        if time_range:
            params["time_range"] = time_range
        if categories:
            params["categories"] = ",".join(categories)
        if language:
            params["language"] = language
        if safe_search is not None:
            params["safesearch"] = safe_search

        try:
            r = self.session.get(f"{self.base_url}/search", params=params, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            data = r.json()

            results = []
            for item in data.get("results", []):
                results.append(SearchResult(
                    title=item.get("title", ""),
                    url=clean_url(item.get("url", "")),
                    snippet=item.get("content", ""),
                    engine=item.get("engine", ""),
                    score=float(item.get("score", 0)),
                    source="searxng",
                    fetched_at=datetime.now().isoformat(),
                ))

            # 去重 (URL 去重)
            seen = set()
            deduped = []
            for r in results:
                key = canonical_url(r.url)
                if key not in seen and r.url:
                    seen.add(key)
                    deduped.append(r)

            # 拼写纠正/相关建议(每项形如 {"title": 词, "url": 表单值}, 取 title 才是干净 query)
            corrections = [c.get("title", "").strip() for c in data.get("corrections", [])
                           if isinstance(c, dict) and c.get("title", "").strip()]
            suggestions = [s.get("title", "").strip() for s in data.get("suggestions", [])
                           if isinstance(s, dict) and s.get("title", "").strip()]

            return SearchResponse(
                query=query,
                results=deduped,
                total=len(deduped),
                source="searxng",
                engines_used=list(set(r.engine for r in deduped if r.engine)),
                corrections=corrections,
                suggestions=suggestions,
            )

        except requests.exceptions.ConnectionError:
            return SearchResponse(query=query, error=f"SearXNG 未运行 ({self.base_url})")
        except Exception as e:
            return SearchResponse(query=query, error=str(e))


class WebFallbackEngine:
    """兜底搜索引擎 — 通过 requests 直接爬 Google/Bing

    当 SearXNG 离线时使用。不依赖第三方 API，直接爬取。
    使用真实浏览器 User-Agent + 随机延迟避免被封。
    """

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })

    def is_available(self):
        return True

    def search(self, query, top_k=10):
        """通过 HTML 爬取搜索引擎结果"""
        engines = [
            ("google", f"https://www.google.com/search?q={urllib.parse.quote(query)}&hl=en"),
            ("bing", f"https://www.bing.com/search?q={urllib.parse.quote(query)}&hl=en"),
        ]

        results = []
        for engine_name, url in engines:
            try:
                r = self.session.get(url, timeout=REQUEST_TIMEOUT)
                r.raise_for_status()
                html = r.text

                # Google 结果提取
                if engine_name == "google":
                    # 匹配 <a href="url" ...><h3>title</h3></a> 或类似结构
                    # 以及 <div data-snc="..."><div>snippet</div>
                    for match in re.finditer(r'<a\s+href=["\'](https?://[^"\']+)["\'][^>]*>(.*?)</a>', html, re.I):
                        url = match.group(1)
                        title = re.sub(r'<[^>]+>', '', match.group(2)).strip()
                        if title and url and url.startswith('http') and 'google' not in url.lower():
                            results.append(SearchResult(
                                title=title,
                                url=clean_url(url),
                                snippet="",
                                engine=engine_name,
                                source="web",
                                fetched_at=datetime.now().isoformat(),
                            ))

                # Bing 结果提取
                elif engine_name == "bing":
                    for match in re.finditer(r'<a\s+href=["\'](https?://[^"\']+)["\'][^>]*>(.*?)</a>', html, re.I):
                        url = match.group(1)
                        title = re.sub(r'<[^>]+>', '', match.group(2)).strip()
                        if title and url and url.startswith('http') and 'bing' not in url.lower():
                            results.append(SearchResult(
                                title=title,
                                url=clean_url(url),
                                snippet="",
                                engine=engine_name,
                                source="web",
                                fetched_at=datetime.now().isoformat(),
                            ))

            except Exception:
                continue

        # 去重
        seen = set()
        deduped = []
        for r in results:
            key = canonical_url(r.url)
            if key not in seen:
                seen.add(key)
                deduped.append(r)

        return SearchResponse(
            query=query,
            results=deduped[:top_k],
            total=min(len(deduped), top_k),
            source="web",
            engines_used=list(set(r.engine for r in deduped[:top_k] if r.engine)),
        )


class FlareSolverrEngine:
    """通过 FlareSolverr (undetected-chromedriver) 绕过 CAPTCHA 搜索

    FlareSolverr 启动一个无头 Chrome + undetected-chromedriver，
    自动解 Cloudflare IUAM/Turnstile/reCAPTCHA 后返回渲染好的 HTML。

    依赖:
      Docker 容器: ghcr.io/flaresolverr/flaresolverr (port 8191)

    架构:
      search.py → POST /v1 → FlareSolverr → Chrome → 目标网站
    """

    def __init__(self, base_url=None):
        self.base_url = (base_url or FLARESOLVERR_URL).rstrip("/")
        self.session = requests.Session()

    def is_available(self):
        try:
            r = self.session.get(f"{self.base_url}/v1", timeout=5)
            return r.status_code == 405  # 405 Method Not Allowed = 服务活着，但需 POST
        except (requests.ConnectionError, requests.Timeout):
            return False

    def search_google(self, query, top_k=10):
        """通过 FlareSolverr 搜 Google（自动过 CAPTCHA）"""
        url = f"https://www.google.com/search?q={urllib.parse.quote(query)}&hl=en&num={top_k}"
        return self._fetch_via_flaresolverr(url, "google")

    def search_bing(self, query, top_k=10):
        """通过 FlareSolverr 搜 Bing"""
        url = f"https://www.bing.com/search?q={urllib.parse.quote(query)}&hl=en&count={top_k}"
        return self._fetch_via_flaresolverr(url, "bing")

    def search_duckduckgo(self, query, top_k=10):
        """通过 FlareSolverr 搜 DuckDuckGo"""
        url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
        return self._fetch_via_flaresolverr(url, "duckduckgo")

    def search(self, query, top_k=10):
        """统一搜索入口 — 同时问 Google + Bing + DDG"""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        results = []
        with ThreadPoolExecutor(max_workers=3) as ex:
            futures = {
                ex.submit(self.search_google, query, top_k): "google",
                ex.submit(self.search_bing, query, top_k): "bing",
                ex.submit(self.search_duckduckgo, query, top_k): "duckduckgo",
            }
            for f in as_completed(futures):
                try:
                    resp = f.result()
                    results.extend(resp.results)
                except Exception:
                    continue

        # 去重
        seen = set()
        deduped = []
        for r in results:
            key = canonical_url(r.url)
            if key not in seen:
                seen.add(key)
                deduped.append(r)

        return SearchResponse(
            query=query,
            results=deduped[:top_k],
            total=min(len(deduped), top_k),
            source="flaresolverr",
            engines_used=["google", "bing", "duckduckgo"],
        )

    def _fetch_via_flaresolverr(self, url: str, engine_name: str):
        """调用 FlareSolverr API 获取页面 HTML 并解析搜索结果"""
        try:
            payload = {
                "cmd": "request.get",
                "url": url,
                "maxTimeout": 60000,
                "session_ttl_minutes": 10,
            }

            r = self.session.post(
                f"{self.base_url}/v1",
                json=payload,
                timeout=90,
            )
            r.raise_for_status()
            result = r.json()

            if result.get("solution", {}).get("status") != 200:
                return SearchResponse(query=url, error=result.get("solution", {}).get("error", "unknown"))

            html = result["solution"]["response"]
            raw_url = result["solution"].get("url", url)

            # 解析结果
            results = self._parse_html(html, engine_name, raw_url)

            return SearchResponse(
                query=url,
                results=results,
                total=len(results),
                source="flaresolverr",
                engines_used=[engine_name],
            )

        except Exception as e:
            return SearchResponse(query=url, error=str(e))

    def _parse_html(self, html: str, engine: str, source_url: str):
        """从搜索引擎的 HTML 中提取结果"""
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        results = []

        if engine == "google":
            # Google 结果 (当前 DOM 结构): <div class="yuRUbf"> → <a href=""><h3>title</h3></a>
            # 摘要: <div class="VwiC3b">
            for block in soup.select("div.yuRUbf"):
                a_tag = block.select_one("a[href]")
                h3 = block.select_one("h3")
                if a_tag and h3:
                    href = a_tag.get("href", "")
                    title = h3.get_text(strip=True)
                    # 找兄弟元素中的摘要
                    snippet = ""
                    parent = block.parent
                    if parent:
                        snip = parent.select_one("div.VwiC3b")
                        if snip:
                            snippet = snip.get_text(strip=True)
                    if title and href.startswith("http"):
                        results.append(SearchResult(
                            title=title, url=clean_url(href), snippet=snippet[:300],
                            engine=engine, source="flaresolverr",
                            fetched_at=datetime.now().isoformat(),
                        ))

        elif engine == "bing":
            # Bing 结果: <li class="b_algo"> → <a href="url">title</a>
            for li in soup.select("li.b_algo"):
                a_tag = li.select_one("h2 a[href]")
                snippet_p = li.select_one(".b_caption p")
                if a_tag:
                    href = a_tag.get("href", "")
                    title = a_tag.get_text(strip=True)
                    snippet = snippet_p.get_text(strip=True) if snippet_p else ""
                    if title and href.startswith("http"):
                        results.append(SearchResult(
                            title=title, url=clean_url(href), snippet=snippet[:300],
                            engine=engine, source="flaresolverr",
                            fetched_at=datetime.now().isoformat(),
                        ))

        elif engine == "duckduckgo":
            # DDG HTML 版: <div class="result"> → <a class="result__a">title</a>
            for result_div in soup.select("div.result"):
                a_tag = result_div.select_one("a.result__a[href]")
                snippet_a = result_div.select_one("a.result__snippet")
                if a_tag:
                    href = a_tag.get("href", "")
                    title = a_tag.get_text(strip=True)
                    snippet = snippet_a.get_text(strip=True) if snippet_a else ""
                    # DDG 用重定向链接，提取真实 URL
                    if "uddg=" in href:
                        from urllib.parse import parse_qs, urlparse
                        parsed = urlparse(href)
                        qs = parse_qs(parsed.query)
                        href = qs.get("uddg", [href])[0]
                    if title and href.startswith("http"):
                        results.append(SearchResult(
                            title=title, url=clean_url(href), snippet=snippet[:300],
                            engine=engine, source="flaresolverr",
                            fetched_at=datetime.now().isoformat(),
                        ))

        return results


class ContentExtractor:
    """网页全文提取层

    策略:
    1. Jina Reader (r.jina.ai) — 免费, 零部署, 效果最好
    2. Crawl4AI (可选) — 深度 JS 渲染提取, 兜底
    3. 直接 requests + BeautifulSoup — 轻量兜底
    """

    def __init__(self, cache: SearchCache):
        self.cache = cache
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        })

    def extract(self, url: str, method="auto") -> dict:
        """提取网页内容为 markdown

        Args:
            url: 目标 URL
            method: "auto" / "jina" / "crawl4ai" / "requests"

        Returns:
            {"url": ..., "markdown": ..., "title": ..., "method_used": ..., "error": ...}
        """
        url = clean_url(url)
        # 检查缓存
        cached = self.cache.get_content(url, method=method)
        if cached:
            return {"url": url, "markdown": cached, "method_used": "cache", "error": None}

        def _auto_chain():
            # 默认抽取链(不含 crawl4ai): trafilatura(若装) → jina → requests
            chain = ["jina", "requests"]
            try:
                import trafilatura  # noqa: F401
                chain.insert(0, "trafilatura")
            except ImportError:
                pass
            return chain

        if method == "auto":
            methods = _auto_chain()
        elif method == "crawl4ai":
            # deep 模式: 优先 crawl4ai(若已装), 未装/失败则回退普通链, 绝不因缺依赖而崩
            methods = ["crawl4ai"] + _auto_chain()
        else:
            methods = [method]

        for m in methods:
            result = self._try_extract(url, m)
            if result and not result.get("error"):
                result["markdown"] = self._clean_text(result.get("markdown", ""))
                if not text_quality_ok(result.get("markdown", "")):
                    continue
                # 缓存成功结果
                if result.get("markdown"):
                    self.cache.set_content(url, result["markdown"], method=method)
                return result

        return {"url": url, "markdown": "", "method_used": "failed", "error": "All methods failed"}

    def _clean_text(self, text: str) -> str:
        """清理抽取结果里的明显样式/脚本噪声。"""
        if not text:
            return ""

        lines = []
        for raw in text.splitlines():
            line = re.sub(r"\s+", " ", raw).strip()
            if not line:
                continue
            if line.startswith("@layer ") or line.startswith("(()=>") or line.startswith("(()=>{"):
                continue
            # 按"噪声字符占比"判定, 而非绝对计数: 长正文(即使整页压成一行)即便含较多
            # 标点也不会被误删, 而 CSS/JS 这类高密度噪声行仍会被剔除。
            braces = line.count("{") + line.count("}") + line.count(";")
            if braces > 80 and braces / max(len(line), 1) > 0.08:
                continue
            if re.search(r"\._?[A-Za-z0-9_-]+:where\(", line):
                continue
            if _is_boilerplate_line(line):   # cookie/登录/版权/纯链接等样板短行
                continue
            lines.append(line)
        cleaned = "\n".join(lines)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        return cleaned

    def _try_extract(self, url: str, method: str) -> dict:
        if not url_is_safe(url):
            return {"url": url, "markdown": "", "method_used": method,
                    "error": "URL 指向私网/环回地址, 已按 SSRF 防护拒绝(设 AGENT_SEARCH_ALLOW_PRIVATE=1 可放开)"}
        try:
            if method == "trafilatura":
                import trafilatura

                # 用 safe_get 自抓(逐跳校验重定向), 再喂给 trafilatura.extract;
                # 不用 trafilatura.fetch_url —— 它自管重定向会绕过 SSRF 校验。
                resp = safe_get(self.session, url)
                resp.raise_for_status()
                if not resp.encoding or resp.encoding.lower() in {"iso-8859-1", "latin-1"}:
                    resp.encoding = resp.apparent_encoding or "utf-8"
                downloaded = resp.text
                if not downloaded:
                    return {"url": url, "markdown": "", "method_used": "trafilatura", "error": "empty download"}

                def _extract(favor):
                    # 老版 trafilatura 无 output_format/include_links 时 TypeError, 退到精简签名
                    kw = {favor: True}
                    try:
                        return trafilatura.extract(
                            downloaded, output_format="markdown", include_comments=False,
                            include_tables=True, include_links=True, **kw)
                    except TypeError:
                        return trafilatura.extract(
                            downloaded, include_comments=False, include_tables=True, **kw)

                # 普通抽取召回优先(对比/选型场景少丢正文); 召回结果空/质量差时再降到精确优先
                md = _extract("favor_recall")
                if not md or not text_quality_ok(md):
                    md = _extract("favor_precision") or md
                if not md:
                    return {"url": url, "markdown": "", "method_used": "trafilatura", "error": "empty extraction"}
                return {"url": url, "markdown": md, "title": "", "method_used": "trafilatura", "error": None}

            if method == "jina":
                r = self.session.get(f"{JINA_READER}/{url}", timeout=REQUEST_TIMEOUT)
                r.raise_for_status()
                md = r.text
                if not text_quality_ok(md):
                    return {"url": url, "markdown": "", "method_used": "jina", "error": "low quality content"}
                title = ""
                for line in md.split("\n")[:10]:
                    if line.startswith("# "):
                        title = line.lstrip("# ").strip()
                        break
                return {"url": url, "markdown": md, "title": title, "method_used": "jina", "error": None}

            elif method == "crawl4ai":
                # 可选依赖, 处理 JS 渲染重的页面。未安装则抛 ImportError, 由外层捕获为该方法失败,
                # 上层方法链会自动回退到普通抽取(jina/trafilatura/requests), 不影响主流程。
                import asyncio
                from crawl4ai import AsyncWebCrawler  # 现行 API(旧版 WebCrawler 已移除)

                async def _run_crawl():
                    async with AsyncWebCrawler() as crawler:
                        res = await crawler.arun(url=url)
                        md_obj = getattr(res, "markdown", None)
                        # 新版 markdown 可能是带 fit_markdown/raw_markdown 的对象
                        md = (getattr(md_obj, "fit_markdown", None)
                              or getattr(md_obj, "raw_markdown", None)
                              or (md_obj if isinstance(md_obj, str) else "") or "")
                        meta = getattr(res, "metadata", None) or {}
                        title = meta.get("title", "") if isinstance(meta, dict) else ""
                        return md, title

                try:
                    md, title = asyncio.run(_run_crawl())
                except RuntimeError:
                    # 已在事件循环内(如某些异步服务器)时, 用独立 loop 兜底
                    loop = asyncio.new_event_loop()
                    try:
                        md, title = loop.run_until_complete(_run_crawl())
                    finally:
                        loop.close()
                return {"url": url, "markdown": md, "title": title, "method_used": "crawl4ai",
                        "error": None if md else "crawl4ai 返回空正文"}

            elif method == "requests":
                r = safe_get(self.session, url)  # 逐跳校验重定向, 防 SSRF
                r.raise_for_status()
                if not r.encoding or r.encoding.lower() in {"iso-8859-1", "latin-1"}:
                    r.encoding = r.apparent_encoding or "utf-8"
                html = r.text
                title, text = "", ""
                # 主体识别优先 readability-lxml(按文本密度选正文, 更准); 未装/失败/质量差回退 bs4 启发式
                try:
                    from readability import Document
                    from bs4 import BeautifulSoup
                    doc = Document(html)
                    title = (doc.short_title() or "").strip()
                    text = BeautifulSoup(doc.summary(html_partial=True), "lxml").get_text("\n", strip=True)
                except Exception:
                    text = ""
                if not text_quality_ok(text):
                    try:
                        from bs4 import BeautifulSoup
                        soup = BeautifulSoup(html, "lxml")
                        for tag in soup([
                            "script", "style", "noscript", "template", "svg", "canvas",
                            "header", "footer", "nav", "aside", "form",
                        ]):
                            tag.decompose()
                        title = title or (soup.title.get_text(" ", strip=True) if soup.title else "")
                        main = soup.find("main") or soup.find("article") or soup.body or soup
                        text = main.get_text("\n", strip=True)
                    except Exception:
                        title_match = re.search(r'<title[^>]*>(.*?)</title>', html, re.I | re.S)
                        title = title or (title_match.group(1).strip() if title_match else "")
                        text = re.sub(r'<(script|style|noscript|template)[^>]*>.*?</\1>', ' ', html, flags=re.I | re.S)
                        text = re.sub(r'<[^>]+>', '\n', text)
                # 仅压缩行内空白, 保留换行结构, 以便后续按行清洗 / 保留段落
                text = re.sub(r'[ \t]+', ' ', text)
                text = re.sub(r'\n[ \t]+', '\n', text)
                text = re.sub(r'\n{3,}', '\n\n', text).strip()
                text = text[:100000]  # 限制 100KB
                return {"url": url, "markdown": text, "title": title, "method_used": "requests", "error": None}

        except Exception as e:
            return {"url": url, "markdown": "", "method_used": method, "error": str(e)}

    def batch_extract(self, urls: list, method="auto",
                      max_urls=None, concurrency=None, timeout_s=None) -> list:
        """并行批量提取多个 URL 内容。

        max_urls / concurrency / timeout_s 留空时取环境预算
        (EXTRACT_MAX_URLS / EXTRACT_CONCURRENCY / EXTRACT_TIMEOUT_S)。
        单个 URL 抽取超时不阻塞整体: 降级为 timeout 占位(上层会回退到 snippet);
        返回顺序与输入对齐。总等待以 2×timeout_s 为软上限, 避免个别慢页拖垮全局。
        """
        import time as _time
        from concurrent.futures import ThreadPoolExecutor

        cap = max_urls or EXTRACT_MAX_URLS
        workers = concurrency or EXTRACT_CONCURRENCY
        per_timeout = timeout_s or EXTRACT_TIMEOUT_S
        urls = [clean_url(u) for u in urls[:cap] if clean_url(u)]
        if not urls:
            return []

        def _timeout_result(u):
            return {"url": u, "markdown": "", "title": "", "method_used": "timeout",
                    "error": f"抽取超时(>{per_timeout}s), 已降级"}

        ex = ThreadPoolExecutor(max_workers=min(workers, len(urls)))
        try:
            futures = [ex.submit(self.extract, u, method) for u in urls]
            deadline = _time.monotonic() + per_timeout * 2  # 全局软预算
            out = []
            for u, fut in zip(urls, futures):
                remaining = max(0.1, deadline - _time.monotonic())
                try:
                    out.append(fut.result(timeout=remaining))
                except Exception:
                    out.append(_timeout_result(u))
            return out
        finally:
            # 不在退出时 join 慢线程(它们各自带 REQUEST_TIMEOUT, 会自行收尾)
            ex.shutdown(wait=False, cancel_futures=True)


class SiteMapper:
    """轻量站点地图：从页面和 sitemap.xml 发现可读链接。"""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        })

    def map(self, url: str, max_links=50, same_domain=True) -> dict:
        url = clean_url(url)
        base = urlparse(url)
        if not base.scheme or not base.netloc:
            return {"url": url, "links": [], "total": 0, "error": "URL 无效"}
        if not url_is_safe(url):
            return {"url": url, "links": [], "total": 0,
                    "error": "URL 指向私网/环回地址, 已按 SSRF 防护拒绝(设 AGENT_SEARCH_ALLOW_PRIVATE=1 可放开)"}

        links = []
        error = None
        try:
            sitemap_links = self._sitemap_links(base, max_links=max_links)
            links.extend(sitemap_links)
        except Exception as e:
            error = str(e)

        if len(links) < max_links:
            try:
                page_links = self._page_links(url, base, max_links=max_links - len(links), same_domain=same_domain)
                links.extend(page_links)
            except Exception as e:
                error = error or str(e)

        seen = set()
        deduped = []
        for item in links:
            key = canonical_url(item["url"])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
            if len(deduped) >= max_links:
                break

        return {"url": url, "links": deduped, "total": len(deduped), "error": error}

    def _sitemap_links(self, base, max_links=50):
        sitemap_url = urlunparse((base.scheme, base.netloc, "/sitemap.xml", "", "", ""))
        r = safe_get(self.session, sitemap_url)  # 逐跳校验重定向, 防 SSRF
        if r.status_code >= 400:
            return []
        locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", r.text, flags=re.I)
        links = []
        for loc in locs[:max_links]:
            links.append({"url": clean_url(loc), "title": "", "source": "sitemap"})
        return links

    def _page_links(self, url, base, max_links=50, same_domain=True):
        from bs4 import BeautifulSoup

        r = safe_get(self.session, url)  # 逐跳校验重定向, 防 SSRF
        r.raise_for_status()
        if not r.encoding or r.encoding.lower() in {"iso-8859-1", "latin-1"}:
            r.encoding = r.apparent_encoding or "utf-8"
        soup = BeautifulSoup(r.text, "lxml")
        links = []
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue
            absolute = clean_url(urljoin(url, href))
            parsed = urlparse(absolute)
            if parsed.scheme not in {"http", "https"}:
                continue
            if same_domain and parsed.netloc.lower().removeprefix("www.") != base.netloc.lower().removeprefix("www."):
                continue
            title = re.sub(r"\s+", " ", a.get_text(" ", strip=True))[:160]
            links.append({"url": absolute, "title": title, "source": "page"})
            if len(links) >= max_links:
                break
        return links


# ================================================================
# GitHub 搜索 (复用本机 gh CLI)
# ================================================================

class GitHubEngine:
    """通过本机 gh CLI 搜索 GitHub。

    复用已登录的 gh，无需额外 API key，且速率限制比匿名高。
    适合查仓库 / 代码 / issue / PR —— 比网页搜索精准。
    """

    # 各类型对应的 --json 字段
    # repos 额外带 license / 最近提交 / 是否归档 / fork 数, 让调用方能做"选型评估"
    # (评估开源项目可用性应看维护活跃度+license, 不止 star)
    KIND_FIELDS = {
        "repos": "fullName,description,url,stargazersCount,license,pushedAt,isArchived,forksCount",
        "code": "path,repository,url",
        "issues": "title,url,repository,state",
        "prs": "title,url,repository,state",
    }

    def is_available(self):
        """gh 是否已安装且已登录"""
        try:
            r = subprocess.run(["gh", "auth", "status"],
                               capture_output=True, timeout=5)
            return r.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def search(self, query, kind="repos", limit=5):
        """搜索 GitHub

        Args:
            query: 搜索词
            kind: repos / code / issues / prs
            limit: 返回条数
        """
        kind = kind if kind in self.KIND_FIELDS else "repos"
        fields = self.KIND_FIELDS[kind]
        cmd = ["gh", "search", kind, query, "--limit", str(limit), "--json", fields]
        # repos 按 star 排序, 让选型时先看到该领域最主流的项目(而非 best-match 里的小众仓)
        if kind == "repos":
            cmd += ["--sort", "stars"]
        try:
            r = subprocess.run(
                cmd,
                capture_output=True, text=True, timeout=REQUEST_TIMEOUT,
            )
            if r.returncode != 0:
                return {"kind": kind, "query": query, "results": [], "total": 0,
                        "error": f"gh search 失败: {r.stderr.strip()[:200]}"}
            items = json.loads(r.stdout or "[]")
            return {"kind": kind, "query": query, "results": items,
                    "total": len(items), "error": None}
        except FileNotFoundError:
            return {"kind": kind, "query": query, "results": [], "total": 0,
                    "error": "未找到 gh CLI，请先安装 GitHub CLI 并 gh auth login"}
        except subprocess.TimeoutExpired:
            return {"kind": kind, "query": query, "results": [], "total": 0,
                    "error": "gh search 超时"}
        except Exception as e:
            return {"kind": kind, "query": query, "results": [], "total": 0,
                    "error": str(e)}

    # ---------- 技术选型对比(一手数据, 不下结论) ----------

    def _gh_json(self, path):
        """gh api <path> → dict/list, 失败返回 None。"""
        try:
            r = subprocess.run(["gh", "api", path],
                               capture_output=True, text=True, timeout=REQUEST_TIMEOUT)
            if r.returncode != 0:
                return None
            return json.loads(r.stdout or "null")
        except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
            return None

    def _deps_dev(self, repo):
        """deps.dev 免费 API(无需 key): OpenSSF Scorecard 健康分 + 关键检查项。"""
        try:
            url = DEPS_DEV_PROJECT_API + repo.replace("/", "%2F")
            r = requests.get(url, timeout=REQUEST_TIMEOUT)
            if r.status_code != 200:
                return {}
            sc = (r.json() or {}).get("scorecard") or {}
            checks = {c.get("name"): c.get("score") for c in sc.get("checks", [])}
            keep = ("Maintained", "Code-Review", "Vulnerabilities",
                    "CII-Best-Practices", "Security-Policy", "Dangerous-Workflow")
            return {
                "scorecard_overall": sc.get("overallScore"),
                "scorecard_checks": {k: checks[k] for k in keep if k in checks},
            }
        except Exception:
            return {}

    def repo_facts(self, repo):
        """单个仓库的一手事实 + 成熟度信号(GitHub API + deps.dev/OpenSSF Scorecard)。"""
        repo = normalize_repo_slug(repo)
        info = self._gh_json("repos/" + repo)
        if not info:
            return {"repo": repo, "error": "gh api 取仓库失败(不存在/未登录/限流)"}
        rel = self._gh_json("repos/" + repo + "/releases/latest") or {}
        now = datetime.now()

        def _days(iso):
            if not iso:
                return None
            try:
                return (now - datetime.strptime(iso[:10], "%Y-%m-%d")).days
            except ValueError:
                return None

        pushed, rel_date = info.get("pushed_at"), rel.get("published_at")
        facts = {
            "repo": info.get("full_name", repo),
            "stars": info.get("stargazers_count"),
            "forks": info.get("forks_count"),
            "license": (info.get("license") or {}).get("spdx_id"),
            "language": info.get("language"),
            "open_issues": info.get("open_issues_count"),
            "archived": bool(info.get("archived", False)),
            "created": (info.get("created_at") or "")[:10],
            "last_commit": (pushed or "")[:10] or None,
            "days_since_commit": _days(pushed),
            "latest_release": rel.get("tag_name"),
            "latest_release_date": (rel_date or "")[:10] or None,
            "days_since_release": _days(rel_date),
            "homepage": info.get("homepage") or None,
            "description": info.get("description") or "",
            "error": None,
        }
        facts.update(self._deps_dev(repo))

        # 事实性标记(只陈述观察, 不给"用/不用"的结论)
        flags = []
        if facts["archived"]:
            flags.append("已归档")
        if facts["days_since_commit"] is not None and facts["days_since_commit"] > 365:
            flags.append(f"近{facts['days_since_commit']}天无提交")
        if not facts.get("latest_release"):
            flags.append("无 release")
        lic = (facts["license"] or "").upper()
        if "AGPL" in lic or "GPL" in lic:
            flags.append(f"copyleft 许可证({facts['license']})")
        facts["flags"] = flags
        return facts

    def compare(self, repos=None, query=None, limit=5):
        """技术选型对比: 给一组 repo 或一个关键词, 拉齐一手事实+成熟度信号。

        只给证据, 不替你下"选哪个"的结论。
        """
        if repos:
            targets = [normalize_repo_slug(r) for r in repos if r][:10]
        elif query:
            res = self.search(query, kind="repos", limit=limit)
            targets = [it.get("fullName") for it in res.get("results", []) if it.get("fullName")]
        else:
            return {"candidates": [], "compared": 0, "error": "请提供 repos 或 query"}
        targets = [t for t in dict.fromkeys(targets) if t]
        if not targets:
            return {"candidates": [], "compared": 0, "error": "没有可对比的候选"}

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(6, len(targets))) as ex:
            candidates = list(ex.map(self.repo_facts, targets))
        return {
            "candidates": candidates,
            "compared": len(candidates),
            "error": None,
            "note": "一手事实 + 成熟度信号(GitHub API + OpenSSF Scorecard via deps.dev); 仅供选型参考, 不替你下结论",
        }


# ================================================================
# DeepSeek 生成/汇总层 (OpenAI 兼容)
# ================================================================

class DeepSeekClient:
    """调用 DeepSeek API 做"基于搜索结果的带引用回答"(RAG)。

    用 requests 直连 OpenAI 兼容的 /chat/completions，不引入额外依赖。
    换别的兼容服务(Ollama / OpenAI)只需改 base_url + model。
    """

    def __init__(self, api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL, model=DEEPSEEK_MODEL):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.session = requests.Session()

    def is_configured(self):
        return bool(self.api_key)

    def chat(self, messages, temperature=0.3, max_tokens=2000):
        """最小化的 chat 调用，返回助手文本。"""
        if not self.api_key:
            return {"error": "未配置 DEEPSEEK_API_KEY，请在 .env 中填入"}
        url = f"{self.base_url}/chat/completions"
        try:
            r = self.session.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "stream": False,
                },
                timeout=120,
            )
            r.raise_for_status()
            data = r.json()
            return {"content": data["choices"][0]["message"]["content"],
                    "usage": data.get("usage", {})}
        except requests.HTTPError as e:
            body = e.response.text[:300] if e.response is not None else ""
            return {"error": f"DeepSeek HTTP {e.response.status_code if e.response else '?'}: {body}"}
        except Exception as e:
            return {"error": f"DeepSeek 调用失败: {e}"}

    def answer_from_sources(self, query, sources, max_tokens=2000):
        """基于抓取到的网页正文生成带引用([1][2])的中文回答。

        Args:
            query: 用户问题
            sources: [{"title","url","markdown"}, ...]
        """
        context_blocks = []
        for i, s in enumerate(sources, 1):
            body = self._relevant_excerpt(query, s.get("markdown") or "", max_chars=6000)
            snippet = s.get("snippet") or ""
            context_blocks.append(
                f"[{i}] 标题: {s.get('title','')}\n"
                f"来源: {s.get('url','')}\n"
                f"摘要: {snippet}\n"
                f"内容:\n{body}"
            )
        context = "\n\n---\n\n".join(context_blocks)

        system = (
            "你是一个严谨的搜索助手。只依据提供的【资料】回答用户问题，绝不编造资料中没有的信息。要求：\n"
            "1. 用简体中文；每个关键论断后用 [编号] 标注来源(如 [1][2])，多个来源相互印证时一并标注。\n"
            "2. 涉及版本号、价格、日期、数值时，照抄资料中的具体值并注明其时间/来源；若资料未给时间或可能已过时，明确提示。\n"
            "3. 官方/权威来源与社区/二手来源冲突时，以官方为准并指出分歧。\n"
            "4. 资料不足或相互矛盾时直接说明，不要强行作答。\n"
            "5. 结尾列出'参考来源'清单。"
        )
        user = f"用户问题：{query}\n\n【资料】\n{context}"
        return self.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.3,
            max_tokens=max_tokens,
        )

    def _relevant_excerpt(self, query: str, text: str, max_chars=6000) -> str:
        """从长正文中挑选和问题更相关的片段，避免只截页面开头。"""
        text = re.sub(r"\n{3,}", "\n\n", text or "").strip()
        if len(text) <= max_chars:
            return text

        terms = query_terms(query)
        chunks = [c.strip() for c in re.split(r"\n{2,}|(?<=[。.!?])\s+", text) if len(c.strip()) > 40]
        if not chunks:
            return text[:max_chars]

        scored = []
        for idx, chunk in enumerate(chunks):
            lower = chunk.lower()
            score = sum(lower.count(t) for t in terms)
            if re.search(r"\$\s?\d|price|pricing|cost|tokens?|价格|定价|费用|收费", lower):
                score += 2
            if re.search(r"\b20\d{2}[-/年]|\b\d+(\.\d+)?\s?(m|million|百万|万)?\s?tokens?", lower):
                score += 1
            scored.append((score, idx, chunk))

        picked = []
        total = 0
        for score, idx, chunk in sorted(scored, key=lambda x: (-x[0], x[1])):
            if score <= 0 and picked:
                continue
            if total + len(chunk) > max_chars:
                continue
            picked.append((idx, chunk))
            total += len(chunk) + 2
            if total >= max_chars * 0.9:
                break

        if not picked:
            return text[:max_chars]
        return "\n\n".join(chunk for _, chunk in sorted(picked))


# ================================================================
# 主搜索入口
# ================================================================

class AgentSearch:
    """统一的搜索入口, 自动选择后端并进行缓存"""

    def __init__(self):
        self.cache = SearchCache()
        self.searxng = SearXNGEngine()
        self.web_fallback = WebFallbackEngine()
        self.flaresolverr = FlareSolverrEngine()
        self.extractor = ContentExtractor(self.cache)
        self.mapper = SiteMapper()
        self.llm = DeepSeekClient()
        self.github = GitHubEngine()
        self._prefer_searxng = False

        # 检测后端可用性
        self._check_backends()

    def _check_backends(self):
        if self.searxng.is_available():
            self._prefer_searxng = True
            sys.stderr.write(f"[*] SearXNG: 在线 ({SEARXNG_URL})\n")
        else:
            sys.stderr.write(f"[*] SearXNG: 离线\n")
        if self.flaresolverr.is_available():
            sys.stderr.write(f"[*] FlareSolverr: 在线 (可绕过 CAPTCHA)\n")
        else:
            sys.stderr.write(f"[*] FlareSolverr: 离线 (CAPTCHA 引擎不可用)\n")
        sys.stderr.write(f"[*] Jina Reader: 可用 (r.jina.ai 内容提取)\n\n")

    def _result_dicts(self, query: str, results: list[SearchResult], top_k: int, rerank=True,
                      weight_map=None) -> list[dict]:
        ranked = []
        for i, r in enumerate(results):
            rank_score = result_rank_score(query, r, i) if rerank else float(r.score or 0.0)
            # plan fan-out: 角度子查询的结果按权重轻度下压(只在重排时生效)
            if rerank and weight_map:
                rank_score *= weight_map.get(canonical_url(r.url), 1.0)
            ranked.append((rank_score, i, r))
        if rerank:
            ranked.sort(key=lambda x: (-x[0], x[1]))
        out = []
        for rank_score, _, r in ranked[:top_k]:
            item = r.to_dict()
            item["rank_score"] = round(rank_score, 4)
            out.append(item)
        return out

    def _multi_search(self, queries, engines=None, *, time_range=None, categories=None,
                      language=None, safe_search=None, max_workers=None, group=False):
        """并发搜多个(子)查询, 每个子查询独立缓存, 合并去重(canonical_url)。

        用于"找方案/对比"场景: 把多角度子查询一次并发打出去, 替代串行扩展。
        返回 (results, primary_error, n_failed):
          results — group=False(默认): 去重后的 SearchResult 列表(按 queries 顺序);
                    group=True: dict{query: [SearchResult,...]}(保留来源, 供按子查询加权);
          primary_error — 第一个查询的 error;
          n_failed — 失败子查询数(供诊断, 不再静默吞掉, 修 BUG-6)。
        子查询缓存走 cache.get_search/set_search(带 subq 标记, 与顶层 query 缓存隔离);
        评测桩会把 engine.cache 置空, 故 eval 下自动失效、不影响"每轮新鲜重跑"。
        """
        from concurrent.futures import ThreadPoolExecutor

        queries = list(dict.fromkeys(q for q in queries if q))
        if not queries:
            return ({} if group else []), None, 0
        sub_opts = {"subq": True, "time_range": time_range,
                    "categories": ",".join(categories) if categories else None,
                    "language": language, "safe_search": safe_search}

        def _one(q):
            cached = self.cache.get_search(q, engines, **sub_opts)
            if cached is not None:
                return q, [SearchResult(**d) for d in cached.get("results", [])], None
            resp = self.searxng.search(q, engines, time_range=time_range, categories=categories,
                                       language=language, safe_search=safe_search)
            if not resp.error:
                self.cache.set_search(q, {"results": [r.to_dict() for r in resp.results]},
                                      engines, **sub_opts)
            return q, resp.results, resp.error

        workers = max_workers or int(os.environ.get("MULTI_SEARCH_WORKERS", "4"))
        by_query = {}
        with ThreadPoolExecutor(max_workers=min(workers, len(queries))) as ex:
            for q, results, err in ex.map(_one, queries):
                by_query[q] = (results, err)

        primary_error = by_query[queries[0]][1]
        n_failed = sum(1 for _, e in by_query.values() if e)
        if group:
            return {q: by_query.get(q, ([], None))[0] for q in queries}, primary_error, n_failed

        merged, seen = [], set()
        for q in queries:
            for r in by_query.get(q, ([], None))[0]:
                key = canonical_url(r.url)
                if r.url and key not in seen:
                    seen.add(key)
                    merged.append(r)
        return merged, primary_error, n_failed

    def search(self, query: str, engines=None, top_k=10, extract=False, deep=False,
               use_flaresolverr=False, time_range=None, categories=None,
               language=None, safe_search=None, rerank=True, auto_rewrite=True,
               expand_mode="auto") -> dict:
        """搜索入口

        Args:
            query: 搜索词
            engines: 指定引擎列表
            top_k: 返回结果数
            extract: 是否提取首条结果全文
            deep: 是否用 Crawl4AI 深度抓取
            time_range: None / day / month / year
            categories: None 或 ["general", "news", "images"]
            language: 语言代码
            safe_search: None / 0 / 1 / 2
            expand_mode: off / auto / compare —— 多查询扩展策略。auto(默认)命中对比意图
                         才多角度扇出; compare 强制扇出; off 不扩展。

        Returns:
            dict: {query, results, source, elapsed_ms, engines_used, ...}
        """
        t0 = time.time()
        backend = "flaresolverr" if use_flaresolverr else ("searxng" if self._prefer_searxng else "web")
        # auto_rewrite=False 时实际不扩展, 缓存键按 off 记(顺带覆盖 BUG-3 的 auto_rewrite 维度)
        eff_expand = expand_mode if auto_rewrite else "off"
        cache_options = {
            "top_k": top_k,
            "backend": backend,
            "time_range": time_range,
            "categories": ",".join(categories or []) if categories else None,
            "language": language,
            "safe_search": safe_search,
            "rerank": bool(rerank),
            "expand_mode": eff_expand,
        }
        plan_weight_map = None

        # 1. 检查缓存
        cached = self.cache.get_search(query, engines, **cache_options)
        if cached:
            result_dict = deepcopy(cached)
            result_dict["from_cache"] = True
            result_dict["elapsed_ms"] = int((time.time() - t0) * 1000)
            if extract and result_dict["results"]:
                first_url = clean_url(result_dict["results"][0]["url"])
                method = "crawl4ai" if deep else "auto"
                extracted = self.extractor.extract(first_url, method=method)
                result_dict["extracted"] = extracted
                result_dict["elapsed_ms"] = int((time.time() - t0) * 1000)
            return result_dict

        # 2. 搜索
        if use_flaresolverr and self.flaresolverr.is_available():
            resp = self.flaresolverr.search(query, top_k)
        elif self._prefer_searxng:
            resp = self.searxng.search(
                query,
                engines,
                time_range=time_range,
                categories=categories,
                language=language,
                safe_search=safe_search,
            )
            # 多查询扩展: plan_queries 把"对比/选型"意图扇出成多角度子查询(并发, 替代串行,
            # 修 BUG-6 不静默吞错), 合并进主结果后按子查询权重轻度下压角度结果再统一重排。
            if auto_rewrite and not resp.error and eff_expand != "off":
                plan = plan_queries(query, eff_expand)
                if plan:
                    by_q, _perr, n_failed = self._multi_search(
                        [p[0] for p in plan], engines, time_range=time_range,
                        categories=categories, language=language, safe_search=safe_search,
                        group=True)
                    weight_of = {p[0]: p[1] for p in plan}
                    merged, seen, plan_weight_map = [], set(), {}
                    for r in resp.results:           # 原查询结果优先, 权重 1.0
                        key = canonical_url(r.url)
                        if r.url and key not in seen:
                            seen.add(key)
                            merged.append(r)
                            plan_weight_map[key] = 1.0
                    for subq, results in by_q.items():   # 角度子查询结果, 同 url 取最大权重
                        w = weight_of.get(subq, 0.7)
                        for r in results:
                            if not r.url:
                                continue
                            key = canonical_url(r.url)
                            if key in seen:
                                plan_weight_map[key] = max(plan_weight_map.get(key, 0.0), w)
                                continue
                            seen.add(key)
                            merged.append(r)
                            plan_weight_map[key] = w
                    resp.results = merged
                    resp.total = len(merged)
                    if n_failed:
                        sys.stderr.write(f"[*] 扩展查询 {n_failed} 条失败(已忽略)\n")

            # 容错 L0: 结果仍稀少且引擎给了拼写纠正 → 用纠正词重搜并入(合并进原 query, 缓存键不变)
            if (auto_rewrite and not resp.error and resp.corrections
                    and len(resp.results) < FUZZY_MIN_RESULTS):
                corr = resp.corrections[0]
                if corr and corr.lower() != query.strip().lower():
                    extra, _e, _n = self._multi_search(
                        [corr], engines, time_range=time_range, categories=categories,
                        language=language, safe_search=safe_search)
                    merged, seen = [], set()
                    for r in list(resp.results) + extra:
                        key = canonical_url(r.url)
                        if r.url and key not in seen:
                            seen.add(key)
                            merged.append(r)
                    resp.results = merged
                    resp.total = len(merged)
                    sys.stderr.write(f"[*] 容错: 结果稀少, 用纠正词重搜 '{corr}'\n")

            # 容错 L1: L0 后仍稀少 → 本地编辑距离纠错(rapidfuzz, 缺失则空), 纠正变体重搜并入
            if (auto_rewrite and not resp.error and len(resp.results) < FUZZY_MIN_RESULTS):
                variants = fuzzy_correct_query(query, build_correction_vocab(self.cache, resp.results))
                if variants:
                    extra, _e, _n = self._multi_search(
                        variants, engines, time_range=time_range, categories=categories,
                        language=language, safe_search=safe_search)
                    merged, seen = [], set()
                    for r in list(resp.results) + extra:
                        key = canonical_url(r.url)
                        if r.url and key not in seen:
                            seen.add(key)
                            merged.append(r)
                    resp.results = merged
                    resp.total = len(merged)
                    sys.stderr.write(f"[*] 容错: 本地纠错重搜 {variants}\n")

            # 容错 L3: L1 后仍 0 结果 且配了 LLM → 让 LLM 只纠拼写(不改语义), 重搜并入
            if (auto_rewrite and not resp.error and not resp.results
                    and self.llm.is_configured()):
                variants = self._llm_spelling_rewrite(query)
                if variants:
                    extra, _e, _n = self._multi_search(
                        variants, engines, time_range=time_range, categories=categories,
                        language=language, safe_search=safe_search)
                    seen, merged = set(), []
                    for r in extra:
                        key = canonical_url(r.url)
                        if r.url and key not in seen:
                            seen.add(key)
                            merged.append(r)
                    resp.results = merged
                    resp.total = len(merged)
                    sys.stderr.write(f"[*] 容错: LLM 纠拼写重搜 {variants}\n")
        else:
            resp = self.web_fallback.search(query, top_k)

        # 3. SearXNG 失败了? 回退 Web 直爬
        if resp.error and not self._prefer_searxng is False:
            sys.stderr.write(f"[!] SearXNG error: {resp.error}\n[*] 回退 Web 直爬\n")
            resp = self.web_fallback.search(query, top_k)

        # 4. 构建返回
        result_dict = {
            "query": query,
            "results": self._result_dicts(query, resp.results, top_k, rerank=rerank,
                                          weight_map=plan_weight_map),
            "total": min(len(resp.results), top_k),
            "source": resp.source,
            "error": resp.error,
            "engines_used": resp.engines_used,
            "elapsed_ms": int((time.time() - t0) * 1000),
            "from_cache": False,
        }

        # 5. 缓存
        if not resp.error:
            self.cache.set_search(query, result_dict, engines, **cache_options)

        # 6. 内容提取 (可选)
        if extract and result_dict["results"]:
            first_url = clean_url(result_dict["results"][0]["url"])
            method = "crawl4ai" if deep else "auto"
            extracted = self.extractor.extract(first_url, method=method)
            result_dict["extracted"] = extracted
            result_dict["elapsed_ms"] = int((time.time() - t0) * 1000)

        return result_dict

    def _llm_spelling_rewrite(self, query: str) -> list[str]:
        """容错 L3: 用 LLM 只纠明显拼写错误(不改语义), 返回至多 2 个候选。无 key/失败→[]。"""
        if not self.llm.is_configured():
            return []
        msgs = [
            {"role": "system", "content":
             "你是搜索查询纠错器。只纠正明显的拼写错误, 不改变语义、不增删词、不翻译。"
             "每行输出一个候选, 最多 2 个, 不要任何解释。若原查询没有拼写错误, 原样输出一行。"},
            {"role": "user", "content": query},
        ]
        out = self.llm.chat(msgs, temperature=0.0, max_tokens=60)
        if not isinstance(out, dict) or out.get("error"):
            return []
        cands = [ln.strip() for ln in (out.get("content") or "").splitlines() if ln.strip()]
        return [c for c in cands[:2] if c.lower() != query.strip().lower()]

    def stats(self) -> dict:
        """缓存统计"""
        return self.cache.stats()

    def clear_cache(self):
        """清除过期缓存"""
        self.cache.clear_expired()

    def extract(self, url: str, deep=False) -> dict:
        """单 URL 内容提取"""
        method = "crawl4ai" if deep else "auto"
        return self.extractor.extract(url, method=method)

    def map_site(self, url: str, max_links=50, same_domain=True) -> dict:
        """发现站点链接，用于先 map 再 extract/crawl。"""
        return self.mapper.map(url, max_links=max_links, same_domain=same_domain)

    def github_search(self, query: str, kind="repos", limit=5) -> dict:
        """搜索 GitHub（仓库/代码/issue/PR），走本机 gh CLI"""
        return self.github.search(query, kind=kind, limit=limit)

    def github_compare(self, repos=None, query=None, limit=5) -> dict:
        """技术选型对比：拉齐多个 GitHub 项目的一手事实 + 成熟度信号，不下结论。"""
        return self.github.compare(repos=repos, query=query, limit=limit)

    def answer(self, query: str, engines=None, num_sources=4, deep=False,
               use_flaresolverr=False, time_range=None, categories=None,
               language=None, safe_search=None) -> dict:
        """RAG 问答: 搜索 → 抓前 N 条正文 → DeepSeek 生成带引用的答案。

        Args:
            query: 用户问题
            engines: 指定搜索引擎
            num_sources: 抓取并喂给 LLM 的来源数量
            deep: 是否用 Crawl4AI 深度抓取
        Returns:
            dict: {query, answer, sources, usage, error}
        """
        t0 = time.time()
        if not self.llm.is_configured():
            return {"query": query, "error": "未配置 DEEPSEEK_API_KEY，请在 .env 中填入后重试"}

        # 1. 搜索
        search_res = self.search(
            query,
            engines=engines,
            top_k=max(num_sources * 3, 10),
            use_flaresolverr=use_flaresolverr,
            time_range=time_range,
            categories=categories,
            language=language,
            safe_search=safe_search,
            rerank=True,
        )
        if search_res.get("error") and not search_res.get("results"):
            return {"query": query, "error": f"搜索失败: {search_res['error']}"}
        candidates = search_res.get("results", [])
        if not candidates:
            return {"query": query, "error": "没有搜索到任何结果"}

        # 2. 抓正文
        # candidates 已由 search() 按 result_rank_score 重排, 截断即保留高分候选;
        # 抽取预算与 num_sources 挂钩(留 2x 缓冲), 避免旧实现写死 8 条凑不够来源。
        method = "crawl4ai" if deep else "auto"
        sources = []
        urls = [clean_url(r["url"]) for r in candidates]
        extracted = self.extractor.batch_extract(
            urls, method=method, max_urls=max(num_sources * 2, 8))
        for r, ext in zip(candidates, extracted):
            if len(sources) >= num_sources:
                break
            url = clean_url(r["url"])
            md = ext.get("markdown", "")
            snippet = r.get("snippet", "")
            if md and text_quality_ok(md):
                sources.append({
                    "title": r.get("title", "") or ext.get("title", ""),
                    "url": url,
                    "snippet": snippet,
                    "markdown": md,
                    "method": ext.get("method_used", ""),
                })
            elif text_quality_ok(snippet, min_chars=40):
                sources.append({
                    "title": r.get("title", ""),
                    "url": url,
                    "snippet": snippet,
                    "markdown": snippet,
                    "method": "snippet",
                })

        if not sources:
            return {"query": query, "error": "搜索有结果但正文抓取全部失败"}

        # 3. DeepSeek 生成答案
        llm_res = self.llm.answer_from_sources(query, sources)
        if llm_res.get("error"):
            return {"query": query, "error": llm_res["error"],
                    "sources": [{"title": s["title"], "url": s["url"]} for s in sources]}

        # 每个来源附 chunk 级 excerpt(可追溯证据), 优先取与问题相关的片段
        def _excerpt(s):
            body = s.get("markdown") or s.get("snippet") or ""
            ex = self.llm._relevant_excerpt(query, body, max_chars=280)
            return re.sub(r"\s+", " ", ex).strip()[:280]

        return {
            "query": query,
            "answer": llm_res["content"],
            "sources": [{"title": s["title"], "url": s["url"], "method": s.get("method", ""),
                         "excerpt": _excerpt(s)} for s in sources],
            "usage": llm_res.get("usage", {}),
            "model": self.llm.model,
            "elapsed_ms": int((time.time() - t0) * 1000),
        }


# ================================================================
# CLI 入口
# ================================================================

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Agent Search — v2.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("query", nargs="?", help="搜索词")
    parser.add_argument("--engines", "-e", default=None, help="引擎, 逗号分隔: google,bing,github")
    parser.add_argument("--top-k", "-k", type=int, default=10, help="返回结果数")
    parser.add_argument("--extract", "-x", action="store_true", help="提取首条结果全文")
    parser.add_argument("--answer", "-a", action="store_true", help="用 DeepSeek 生成带引用的答案 (RAG)")
    parser.add_argument("--sources", "-n", type=int, default=4, help="--answer 模式抓取的来源数 (默认 4)")
    parser.add_argument("--deep", "-d", action="store_true", help="Crawl4AI 深度抓取")
    parser.add_argument("--flaresolverr", "-f", action="store_true", help="用 FlareSolverr 绕过 CAPTCHA 搜 Google/Bing/DDG")
    parser.add_argument("--url", "-u", help="直接提取指定 URL 内容")
    parser.add_argument("--map", dest="map_url", help="发现指定站点的内部链接")
    parser.add_argument("--time-range", choices=["day", "month", "year"], help="SearXNG 时间范围过滤")
    parser.add_argument("--categories", help="SearXNG 分类，逗号分隔: general,news,images")
    parser.add_argument("--language", help="SearXNG 语言代码，如 zh-CN / en-US")
    parser.add_argument("--safe-search", type=int, choices=[0, 1, 2], help="SearXNG 安全搜索等级 0/1/2")
    parser.add_argument("--no-rerank", action="store_true", help="关闭本地结果重排")
    parser.add_argument("--stats", action="store_true", help="显示缓存统计")
    parser.add_argument("--clear-cache", action="store_true", help="清除过期缓存")
    parser.add_argument("--json", action="store_true", help="JSON 输出")

    args = parser.parse_args()

    search = AgentSearch()

    if args.stats:
        stats = search.stats()
        if args.json:
            print(json.dumps(stats, ensure_ascii=False, indent=2))
        else:
            print(f"缓存统计:")
            print(f"  搜索缓存: {stats['search_cached']} 条")
            print(f"  内容缓存: {stats['content_cached']} 条")
        return

    if args.clear_cache:
        search.clear_cache()
        print("过期缓存已清除。")
        return

    if args.map_url:
        result = search.map_site(args.map_url, max_links=args.top_k)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"\n站点: {result['url']}")
            print(f"链接: {result['total']} 条")
            if result.get("error"):
                print(f"提示: {result['error']}")
            for i, link in enumerate(result.get("links", []), 1):
                print(f"  [{i:2d}] {link.get('title') or '(无标题)'}")
                print(f"       {link['url']}")
        return

    if args.url:
        result = search.extract(args.url, deep=args.deep)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"URL: {result['url']}")
            print(f"方法: {result['method_used']}")
            if result.get("title"):
                print(f"标题: {result['title']}")
            if result.get("error"):
                print(f"错误: {result['error']}")
            else:
                print(f"\n{result.get('markdown', '')[:2000]}...")
        return

    if not args.query:
        parser.print_help()
        return

    engines = args.engines.split(",") if args.engines else None
    categories = args.categories.split(",") if args.categories else None

    # RAG 问答模式
    if args.answer:
        result = search.answer(args.query, engines=engines, num_sources=args.sources, deep=args.deep,
                               use_flaresolverr=args.flaresolverr,
                               time_range=args.time_range,
                               categories=categories,
                               language=args.language,
                               safe_search=args.safe_search)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        print(f"\n{'='*60}")
        print(f"  问题: {result['query']}")
        if result.get("error"):
            print(f"{'='*60}")
            print(f"[!] {result['error']}")
            return
        print(f"  模型: {result.get('model','?')} | 耗时: {result.get('elapsed_ms',0)}ms", end="")
        u = result.get("usage", {})
        if u:
            print(f" | token: {u.get('total_tokens','?')}", end="")
        print(f"\n{'='*60}\n")
        print(result["answer"])
        print(f"\n{'─'*60}\n  参考来源:")
        for i, s in enumerate(result.get("sources", []), 1):
            print(f"  [{i}] {s['title'][:60]}\n      {s['url']}")
        return

    result = search.search(
        query=args.query,
        engines=engines,
        top_k=args.top_k,
        extract=args.extract,
        deep=args.deep,
        use_flaresolverr=args.flaresolverr,
        time_range=args.time_range,
        categories=categories,
        language=args.language,
        safe_search=args.safe_search,
        rerank=not args.no_rerank,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    # 标准输出
    source = result.get("source", "?")
    elapsed = result.get("elapsed_ms", 0)
    engines_used = result.get("engines_used", [])
    cached = result.get("from_cache", False)

    print(f"\n{'='*60}")
    print(f"  搜索: \"{result['query']}\"")
    print(f"  来源: {source} | 引擎: {', '.join(engines_used) or '默认'}")
    print(f"  耗时: {elapsed}ms{' (缓存)' if cached else ''}")
    print(f"  结果: {result['total']} 条")
    print(f"{'='*60}\n")

    for i, r in enumerate(result.get("results", []), 1):
        print(f"  [{i:2d}] {r['title']}")
        print(f"       {r['url']}")
        if r.get("snippet"):
            print(f"       {r['snippet'][:200]}")
        if r.get("engine") and source == "searxng":
            print(f"       [引擎: {r['engine']}]")
        print()

    if result.get("extracted"):
        ext = result["extracted"]
        print(f"{'─'*60}")
        print(f"  全文提取: {ext.get('url', '')}")
        print(f"  方法: {ext.get('method_used', '?')}")
        print(f"{'─'*60}")
        print(ext.get("markdown", "")[:3000])
        if len(ext.get("markdown", "")) > 3000:
            print(f"\n  ... (共 {len(ext['markdown'])} 字符, 已截断)")
        print()

    if result.get("error"):
        print(f"[!] 错误: {result['error']}")


if __name__ == "__main__":
    main()
