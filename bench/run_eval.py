#!/usr/bin/env python3
"""Agent Search 回测评测器。

用法:
  .venv/bin/python bench/run_eval.py                       # 默认走缓存
  .venv/bin/python bench/run_eval.py --json                # JSON 输出
  .venv/bin/python bench/run_eval.py --offline             # 只用缓存，绝不联网
  .venv/bin/python bench/run_eval.py --limit 10            # 只跑前 N 个
  .venv/bin/python bench/run_eval.py --case s_openai_price # 只跑某个 case
  .venv/bin/python bench/run_eval.py --refresh             # 忽略缓存重新联网
  .venv/bin/python bench/run_eval.py --max-network-calls 30 --max-ask-calls 6
  .venv/bin/python bench/run_eval.py --type search         # 只跑某类型
  .venv/bin/python bench/run_eval.py --out results/round_1.json

打分: 每个 case 用"组件 / 满分"归一到 0..1，pass 阈值 0.6。
原始网络结果缓存在 bench/cache/，排序/清洗/打分逻辑每轮新鲜重跑。
"""

import argparse
import json
import os
import sys
import time
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import evalcache  # noqa: E402  (bench/evalcache.py)
from search import AgentSearch, text_quality_ok  # noqa: E402

CASES_PATH = os.path.join(HERE, "eval_cases.json")
PASS_THRESHOLD = 0.6

COMMUNITY_HOSTS = (
    "stackoverflow.com", "reddit.com", "medium.com", "csdn.net", "juejin.cn",
    "zhihu.com", "quora.com", "dev.to", "cnblogs.com", "jianshu.com",
    "segmentfault.com", "51cto.com", "tutorialspoint.com", "geeksforgeeks.org",
    "w3schools.com",
)
NEWS_HOSTS = (
    "reuters.com", "bloomberg.com", "techcrunch.com", "theverge.com",
    "arstechnica.com", "bbc.com", "bbc.co.uk", "nytimes.com", "cnbc.com",
    "engadget.com", "wired.com", "venturebeat.com", "36kr.com", "ithome.com",
    "thepaper.cn", "infoq.cn", "xinhuanet.com", "scmp.com",
)


# ----------------------------- 通用工具 -----------------------------

def domain_of(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def host_matches(host: str, expected: str) -> bool:
    """expected 作为域名后缀匹配 host(host==expected 或 host 以 .expected 结尾)。"""
    host = host.lower().lstrip(".")
    expected = expected.lower().lstrip(".")
    return host == expected or host.endswith("." + expected)


def any_domain_match(hosts, expected_list) -> bool:
    return any(host_matches(h, e) for h in hosts for e in expected_list)


def classify_source(url: str) -> str:
    host = domain_of(url)
    path = ""
    try:
        path = urlparse(url).path.lower()
    except Exception:
        pass
    if host == "github.com":
        return "github"
    if any(host_matches(host, h) for h in NEWS_HOSTS):
        return "news"
    if any(host_matches(host, h) for h in COMMUNITY_HOSTS):
        return "community"
    if host.startswith(("docs.", "developer.", "developers.", "devdocs.", "api.")) or \
       any(seg in path for seg in ("/docs", "/reference", "/documentation", "/guide", "/api/", "/manual")):
        return "docs"
    if host.endswith(".org") or host.endswith(".io") or host.endswith(".dev") or "." in host:
        return "official"
    return "other"


def authoritative(stype: str) -> bool:
    return stype in ("official", "docs", "github")


def term_matches(term: str, text: str) -> bool:
    return term.lower() in (text or "").lower()


def fraction_terms(terms, text):
    if not terms:
        return 1.0, []
    matched = [t for t in terms if term_matches(t, text)]
    return len(matched) / len(terms), matched


def css_noise_ratio(text: str) -> float:
    if not text:
        return 1.0
    head = text[:6000]
    noise = head.count("{") + head.count("}") + head.count(";")
    return noise / max(len(head), 1)


# ----------------------------- 各类型评测 -----------------------------

def eval_search(engine, case, budget):
    q = case["query"]
    res = engine.search(q, top_k=10, time_range=case.get("time_range"),
                        language=case.get("language"))
    results = res.get("results", [])
    if res.get("error") in ("CACHE_MISS_NO_BUDGET",) and not results:
        return None  # 缓存未命中且无预算
    hosts = [domain_of(r["url"]) for r in results]
    top3, top5 = hosts[:3], hosts[:5]
    text_top5 = " ".join(
        f"{r.get('title','')} {r.get('snippet','')} {r['url']}" for r in results[:5]
    )

    pts = max_pts = 0.0
    detail = {}

    # 有结果
    max_pts += 1; pts += 1 if results else 0
    detail["has_results"] = bool(results)

    expected = case.get("expected_domains") or []
    hit3 = any_domain_match(top3, expected) if expected else None
    hit5 = any_domain_match(top5, expected) if expected else None
    if expected:
        max_pts += 3
        pts += 3 if hit3 else (2 if hit5 else 0)
        detail["expected_domain_top3"] = bool(hit3)
        detail["expected_domain_top5"] = bool(hit5)

    must = case.get("must_include_terms") or []
    frac, matched = fraction_terms(must, text_top5)
    if must:
        max_pts += 2; pts += 2 * frac
        detail["must_terms_frac"] = round(frac, 2)

    pref = case.get("preferred_source_type") or "any"
    if pref in ("official", "docs", "github", "news"):
        max_pts += 1.5
        types3 = [classify_source(r["url"]) for r in results[:3]]
        if pref in ("official", "docs"):
            ok = any(authoritative(t) for t in types3)
        elif pref == "github":
            ok = "github" in types3
        else:  # news
            ok = "news" in types3
        pts += 1.5 if ok else 0
        detail["preferred_source_ok"] = ok

    avoid = case.get("avoid_domains") or []
    violated = any_domain_match(top5, avoid) if avoid else False
    if avoid:
        max_pts += 1; pts += 0 if violated else 1
        detail["avoid_violated"] = violated

    score = pts / max_pts if max_pts else 0.0
    return {
        "score": round(score, 4),
        "observed_domains": hosts[:8],
        "matched_terms": matched,
        "source_urls": [r["url"] for r in results[:5]],
        "detail": detail,
        "extra": {"hit_expected": bool(hit5) if expected else None,
                  "avoid_violated": violated if avoid else None,
                  "must_frac": frac if must else None,
                  "preferred_official": detail.get("preferred_source_ok")
                  if pref in ("official", "docs") else None},
    }


def eval_ask(engine, case, budget):
    q = case["query"]
    res = engine.answer(q, num_sources=case.get("num_sources", 4),
                        time_range=case.get("time_range"), language=case.get("language"))
    err = res.get("error")
    if err in ("CACHE_MISS_NO_ASK_BUDGET", "CACHE_MISS_NO_BUDGET") or \
       (err and "CACHE_MISS" in str(err)):
        return None
    answer = res.get("answer", "") or ""
    sources = res.get("sources", []) or []
    src_hosts = [domain_of(s.get("url", "")) for s in sources]

    pts = max_pts = 0.0
    detail = {}

    max_pts += 1; pts += 1 if answer.strip() else 0
    detail["answer_nonempty"] = bool(answer.strip())
    max_pts += 1; pts += 1 if sources else 0
    detail["sources_nonempty"] = bool(sources)

    must = case.get("must_include_terms") or []
    frac, matched = fraction_terms(must, answer)
    if must:
        max_pts += 2; pts += 2 * frac
        detail["answer_terms_frac"] = round(frac, 2)

    expected = case.get("expected_domains") or []
    hit = any_domain_match(src_hosts, expected) if expected else None
    if expected:
        max_pts += 2; pts += 2 if hit else 0
        detail["source_domain_hit"] = bool(hit)

    # excerpt 加分(baseline 通常缺失 → 优化点)
    max_pts += 1
    has_excerpt = any((s.get("excerpt") or "").strip() for s in sources)
    pts += 1 if has_excerpt else 0
    detail["source_has_excerpt"] = has_excerpt

    score = pts / max_pts if max_pts else 0.0
    return {
        "score": round(score, 4),
        "observed_domains": src_hosts,
        "matched_terms": matched,
        "source_urls": [s.get("url", "") for s in sources],
        "detail": detail,
        "extra": {"hit_expected": bool(hit) if expected else None,
                  "sources_nonempty": bool(sources),
                  "must_frac": frac if must else None,
                  "has_excerpt": has_excerpt},
    }


def eval_extract(engine, case, budget):
    url = case["url"]
    deep = case.get("requires_deep", False)
    res = engine.extract(url, deep=deep)
    if res.get("error") in ("CACHE_MISS_NO_BUDGET",):
        return None
    md = res.get("markdown", "") or ""

    pts = max_pts = 0.0
    detail = {}

    max_pts += 1
    long_enough = len(md.strip()) >= 200
    pts += 1 if long_enough else (0.4 if md.strip() else 0)
    detail["md_len"] = len(md)

    max_pts += 2
    quality = text_quality_ok(md)
    noise = css_noise_ratio(md)
    qpts = 0.0
    if quality:
        qpts += 1.3
    if noise < 0.02:
        qpts += 0.7
    elif noise < 0.05:
        qpts += 0.35
    pts += min(qpts, 2)
    detail["quality_ok"] = quality
    detail["noise_ratio"] = round(noise, 4)

    must = case.get("must_include_terms") or []
    frac, matched = fraction_terms(must, md)
    if must:
        max_pts += 2; pts += 2 * frac
        detail["must_terms_frac"] = round(frac, 2)

    score = pts / max_pts if max_pts else 0.0
    return {
        "score": round(score, 4),
        "observed_domains": [domain_of(url)],
        "matched_terms": matched,
        "source_urls": [res.get("url", url)],
        "detail": {**detail, "method": res.get("method_used", "")},
        "extra": {"quality_ok": quality, "must_frac": frac if must else None},
    }


def eval_map(engine, case, budget):
    url = case["url"]
    res = engine.map_site(url, max_links=case.get("max_links", 50),
                          same_domain=case.get("same_domain", True))
    links = res.get("links", []) or []
    if not links and res.get("error") and "CACHE_MISS" in str(res.get("error", "")):
        return None
    base = domain_of(url)

    pts = max_pts = 0.0
    detail = {}

    max_pts += 2; pts += 2 if links else 0
    detail["link_count"] = len(links)

    if links:
        same = sum(1 for l in links if host_matches(domain_of(l["url"]), base))
        ratio = same / len(links)
        max_pts += 2; pts += 2 * ratio
        detail["same_domain_ratio"] = round(ratio, 2)

        valid_src = sum(1 for l in links if l.get("source") in ("sitemap", "page"))
        max_pts += 1; pts += 1 * (valid_src / len(links))
        detail["valid_source_ratio"] = round(valid_src / len(links), 2)
        detail["sources"] = sorted(set(l.get("source", "") for l in links))

    score = pts / max_pts if max_pts else 0.0
    return {
        "score": round(score, 4),
        "observed_domains": sorted(set(domain_of(l["url"]) for l in links))[:8],
        "matched_terms": [],
        "source_urls": [l["url"] for l in links[:5]],
        "detail": detail,
        "extra": {},
    }


GITHUB_REQUIRED_FIELDS = {
    "repos": ["fullName", "url"],
    "code": ["path", "url"],
    "issues": ["title", "url"],
    "prs": ["title", "url"],
}


def eval_github(engine, case, budget):
    q = case["query"]
    kind = case.get("kind", "repos")
    res = engine.github_search(q, kind=kind, limit=case.get("limit", 5))
    if res.get("error") in ("CACHE_MISS_NO_BUDGET",):
        return None
    items = res.get("results", []) or []

    pts = max_pts = 0.0
    detail = {}

    max_pts += 2; pts += 2 if items else 0
    detail["total"] = len(items)

    req = GITHUB_REQUIRED_FIELDS.get(kind, ["url"])
    if items:
        complete = sum(1 for it in items if all(it.get(f) for f in req)) / len(items)
        max_pts += 2; pts += 2 * complete
        detail["field_complete_ratio"] = round(complete, 2)

    max_pts += 1
    pts += 1 if res.get("kind") == kind else 0
    detail["kind_ok"] = res.get("kind") == kind

    # 可选: 期望命中某仓库/域
    expected = case.get("expected_domains") or []
    matched = []
    if expected:
        blob = json.dumps(items, ensure_ascii=False).lower()
        matched = [e for e in expected if e.lower() in blob]

    score = pts / max_pts if max_pts else 0.0
    return {
        "score": round(score, 4),
        "observed_domains": [],
        "matched_terms": matched,
        "source_urls": [it.get("url", "") for it in items[:5]],
        "detail": detail,
        "extra": {},
    }


def eval_crawl(engine, case, budget):
    """递归深抓: 验证抓到 >=2 页、有正文、同域、达到 depth>=1, 以及 must 词命中。"""
    if getattr(budget, "offline", False):
        return None  # 深抓无缓存层, 离线不跑
    url = case["url"]
    res = engine.crawl(url, max_depth=case.get("max_depth", 1),
                       max_pages=case.get("max_pages", 6),
                       scope=case.get("scope", "same-domain"))
    if res.get("error"):
        return {"score": 0.0, "observed_domains": [domain_of(url)], "matched_terms": [],
                "source_urls": [], "detail": {"error": res["error"]}, "extra": {}}
    pages = res.get("pages", []) or []
    base = domain_of(url)
    pts = max_pts = 0.0
    detail = {}

    max_pts += 2; pts += 2 if len(pages) >= 2 else (1 if pages else 0)
    detail["page_count"] = len(pages)

    max_pts += 2
    with_md = sum(1 for p in pages if (p.get("markdown") or "").strip())
    pts += 2 * (with_md / len(pages)) if pages else 0
    detail["pages_with_markdown"] = with_md

    if pages:
        same = sum(1 for p in pages if host_matches(domain_of(p["url"]), base))
        ratio = same / len(pages)
        max_pts += 1; pts += 1 * ratio
        detail["same_domain_ratio"] = round(ratio, 2)

    max_pts += 1
    reached = any(p.get("depth", 0) >= 1 for p in pages)
    pts += 1 if reached else 0
    detail["reached_depth1"] = reached

    must = case.get("must_include_terms") or []
    blob = " ".join((p.get("markdown") or "")[:5000] for p in pages)
    frac, matched = fraction_terms(must, blob)
    if must:
        max_pts += 2; pts += 2 * frac
        detail["must_terms_frac"] = round(frac, 2)

    score = pts / max_pts if max_pts else 0.0
    return {
        "score": round(score, 4),
        "observed_domains": sorted(set(domain_of(p["url"]) for p in pages))[:8],
        "matched_terms": matched,
        "source_urls": [p["url"] for p in pages[:5]],
        "detail": detail,
        "extra": {},
    }


EVALUATORS = {
    "search": eval_search,
    "ask": eval_ask,
    "extract": eval_extract,
    "map": eval_map,
    "github": eval_github,
    "crawl": eval_crawl,
}


# ----------------------------- 主流程 -----------------------------

def run(cases, budget, engine):
    rows = []
    for case in cases:
        ctype = case["type"]
        fn = EVALUATORS.get(ctype)
        t0 = time.time()
        if fn is None:
            rows.append({"id": case["id"], "type": ctype, "passed": False, "score": 0.0,
                         "latency_ms": 0, "reason": f"未知类型 {ctype}", "skipped": True})
            continue
        budget.begin_case("ask" if ctype == "ask" else "network")
        try:
            r = fn(engine, case, budget)
        except Exception as e:  # noqa: BLE001
            budget.end_case()
            rows.append({"id": case["id"], "type": ctype, "passed": False, "score": 0.0,
                         "latency_ms": int((time.time() - t0) * 1000),
                         "reason": f"异常: {e}", "skipped": False, "error": str(e)})
            continue
        budget.end_case()
        latency = int((time.time() - t0) * 1000)
        if r is None:
            rows.append({"id": case["id"], "type": ctype, "passed": False, "score": 0.0,
                         "latency_ms": latency, "reason": "缓存未命中且无联网预算(skipped)",
                         "skipped": True})
            continue
        passed = r["score"] >= PASS_THRESHOLD
        rows.append({
            "id": case["id"], "type": ctype, "passed": passed, "score": r["score"],
            "latency_ms": latency, "reason": _reason(case, r, passed),
            "observed_domains": r["observed_domains"], "matched_terms": r["matched_terms"],
            "source_urls": r["source_urls"], "detail": r["detail"],
            "extra": r.get("extra", {}), "skipped": False,
        })
    return rows


def _reason(case, r, passed):
    d = r.get("detail", {})
    bits = [f"score={r['score']}"]
    if "expected_domain_top5" in d:
        bits.append("域命中" if d.get("expected_domain_top5") else "域未命中")
    if "source_domain_hit" in d:
        bits.append("源域命中" if d["source_domain_hit"] else "源域未命中")
    if "must_terms_frac" in d or "answer_terms_frac" in d:
        bits.append(f"术语{d.get('must_terms_frac', d.get('answer_terms_frac'))}")
    if "avoid_violated" in d and d["avoid_violated"]:
        bits.append("命中避免域!")
    if "quality_ok" in d:
        bits.append("正文质量OK" if d["quality_ok"] else "正文质量差")
    if "preferred_source_ok" in d:
        bits.append("权威源✓" if d["preferred_source_ok"] else "权威源✗")
    if "source_has_excerpt" in d:
        bits.append("有excerpt" if d["source_has_excerpt"] else "无excerpt")
    return ("PASS " if passed else "FAIL ") + " | ".join(bits)


def aggregate(rows, cases_by_id):
    scored = [r for r in rows if not r.get("skipped")]
    n = len(rows)
    total_score = round(sum(r["score"] for r in scored) / n * 100, 2) if n else 0.0
    passed = sum(1 for r in rows if r["passed"])

    def rate(pred_pairs):
        vals = [v for v in pred_pairs if v is not None]
        return round(sum(1 for v in vals if v) / len(vals), 4) if vals else None

    def mean(vals):
        vals = [v for v in vals if v is not None]
        return round(sum(vals) / len(vals), 4) if vals else None

    ex = lambda r, k: r.get("extra", {}).get(k)  # noqa: E731

    expected_domain_hit_rate = rate([ex(r, "hit_expected") for r in scored])
    must_terms_hit_rate = mean([ex(r, "must_frac") for r in scored])
    avoid_violation_rate = rate([ex(r, "avoid_violated") for r in scored])
    official_source_rate = rate([ex(r, "preferred_official") for r in scored])
    extraction_quality_rate = rate(
        [ex(r, "quality_ok") for r in scored if r["type"] == "extract"])
    ask_source_rate = rate(
        [ex(r, "sources_nonempty") for r in scored if r["type"] == "ask"])
    avg_latency = round(sum(r["latency_ms"] for r in rows) / n, 1) if n else 0.0

    by_type = {}
    for r in scored:
        by_type.setdefault(r["type"], []).append(r["score"])
    type_scores = {t: round(sum(v) / len(v) * 100, 2) for t, v in by_type.items()}

    return {
        "total_score": total_score,
        "passed": passed,
        "total": n,
        "scored": len(scored),
        "skipped": n - len(scored),
        "expected_domain_hit_rate": expected_domain_hit_rate,
        "must_terms_hit_rate": must_terms_hit_rate,
        "avoid_domain_violation_rate": avoid_violation_rate,
        "official_source_rate": official_source_rate,
        "extraction_quality_rate": extraction_quality_rate,
        "ask_source_rate": ask_source_rate,
        "avg_latency_ms": avg_latency,
        "type_scores": type_scores,
        "failed_cases": [r["id"] for r in rows if not r["passed"]],
    }


def main():
    ap = argparse.ArgumentParser(description="Agent Search 回测评测器")
    ap.add_argument("--offline", action="store_true", help="只用缓存，绝不联网")
    ap.add_argument("--refresh", action="store_true", help="忽略缓存重新联网")
    ap.add_argument("--limit", type=int, default=None, help="只跑前 N 个 case")
    ap.add_argument("--case", default=None, help="只跑某个 case id")
    ap.add_argument("--type", default=None, help="只跑某类型 search/ask/extract/map/github")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    ap.add_argument("--max-network-calls", type=int, default=None)
    ap.add_argument("--max-ask-calls", type=int, default=None)
    ap.add_argument("--out", default=None, help="把结果写到指定路径(相对 bench/)")
    args = ap.parse_args()

    with open(CASES_PATH, encoding="utf-8") as f:
        cases = json.load(f)
    if args.type:
        cases = [c for c in cases if c["type"] == args.type]
    if args.case:
        cases = [c for c in cases if c["id"] == args.case]
    if args.limit:
        cases = cases[: args.limit]

    budget = evalcache.Budget(max_network=args.max_network_calls,
                              max_ask=args.max_ask_calls,
                              offline=args.offline, refresh=args.refresh)
    engine = AgentSearch()
    evalcache.activate(engine, budget)

    t0 = time.time()
    rows = run(cases, budget, engine)
    budget.save()
    cases_by_id = {c["id"]: c for c in cases}
    metrics = aggregate(rows, cases_by_id)
    metrics["budget"] = budget.summary()
    metrics["wall_ms"] = int((time.time() - t0) * 1000)

    report = {"metrics": metrics, "cases": rows}

    out_path = os.path.join(HERE, args.out) if args.out else os.path.join(HERE, "results", "latest.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    # 总是同步一份 latest
    with open(os.path.join(HERE, "results", "latest.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        m = metrics
        print(f"\n{'='*64}")
        print(f"  总分 total_score = {m['total_score']}   通过 {m['passed']}/{m['total']}"
              f"  (skipped {m['skipped']})")
        print(f"{'='*64}")
        print(f"  expected_domain_hit_rate   {m['expected_domain_hit_rate']}")
        print(f"  must_terms_hit_rate        {m['must_terms_hit_rate']}")
        print(f"  avoid_domain_violation     {m['avoid_domain_violation_rate']}")
        print(f"  official_source_rate       {m['official_source_rate']}")
        print(f"  extraction_quality_rate    {m['extraction_quality_rate']}")
        print(f"  ask_source_rate            {m['ask_source_rate']}")
        print(f"  avg_latency_ms             {m['avg_latency_ms']}")
        print(f"  type_scores                {m['type_scores']}")
        print(f"  budget                     {m['budget']}")
        print(f"\n  失败 case: {', '.join(m['failed_cases']) or '无'}")
        print(f"  结果已写入 {os.path.relpath(out_path, ROOT)}\n")


if __name__ == "__main__":
    main()
