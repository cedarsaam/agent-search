#!/usr/bin/env python3
"""用 Agent Search 自己做轻量联网核验(不使用内置 Web Fetch)。

目标:
  - 对失败/低置信 case 做二次检查: 当前权威源是什么? expected_domains 是否合理?
  - 发现重复 case、过时 case、错误 expected_terms。
  - 输出 recommended_case_updates(只建议, 不自动改)。

预算:
  - 每轮最多真实联网 N 次(默认 20), 与 run_eval 共享 budget.json 累计上限。
  - 优先复用 eval cache(同 query 不重复联网); 需要新证据时才发起 refined 探针。

用法:
  .venv/bin/python bench/verify_online.py --round 0 --max-network-calls 20
  .venv/bin/python bench/verify_online.py --ids s_docs_x,a_cross_y
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import evalcache  # noqa: E402
from search import AgentSearch, text_quality_ok  # noqa: E402
from run_eval import (domain_of, host_matches, classify_source, authoritative,  # noqa: E402
                      any_domain_match, fraction_terms)

CASES_PATH = os.path.join(HERE, "eval_cases.json")
LATEST_PATH = os.path.join(HERE, "results", "latest.json")

GENERIC_HINTS = {
    "official": "official documentation",
    "docs": "official documentation",
    "news": "news",
    "github": "github",
    "any": "",
    "community": "",
}


def authoritative_domains(results, top=6):
    """从结果中收集 top 区间内被判为权威(official/docs/github)的域名 + 出现次序。"""
    out = []
    for i, r in enumerate(results[:top]):
        host = domain_of(r["url"])
        stype = classify_source(r["url"])
        if authoritative(stype):
            out.append({"domain": host, "rank": i + 1, "type": stype})
    return out


def verify_search_like(engine, case, budget, do_probe):
    """search / ask 共用: 用纯 search(不触发 LLM, 不耗 ask 预算)检查
    expected_domains 与 must_terms 是否被真实结果支持。"""
    q = case["query"]
    res = engine.search(q, top_k=10)
    results = res.get("results", [])
    text_blob = " ".join(f"{r.get('title','')} {r.get('snippet','')} {r['url']}" for r in results)

    hosts = [domain_of(r["url"]) for r in results]
    expected = case.get("expected_domains") or []
    must = case.get("must_include_terms") or []

    present_expected = [e for e in expected if any(host_matches(h, e) for h in hosts)]
    auth = authoritative_domains(results, top=6)
    frac, matched = fraction_terms(must, text_blob)
    missing_terms = [t for t in must if t not in matched]

    recs = []
    confidence = "low"

    # 1) expected_domains 一个都没出现, 但有稳定的权威源在 top3
    if expected and not present_expected:
        top3_auth = [a for a in auth if a["rank"] <= 3]
        if top3_auth:
            cand = sorted({a["domain"] for a in top3_auth})
            recs.append({
                "field": "expected_domains",
                "issue": "expected_domains 在真实 top10 完全未出现",
                "suggestion": f"考虑改为实际权威源: {cand}",
                "evidence": [a for a in auth],
            })
            confidence = "high"
        else:
            recs.append({
                "field": "expected_domains",
                "issue": "expected_domains 未出现, 且 top 区无明显权威源",
                "suggestion": "可能 query 表述需调整, 或放宽 preferred_source_type",
                "evidence": [{"domain": h, "rank": i + 1} for i, h in enumerate(hosts[:5])],
            })
            confidence = "medium"

    # 2) must_terms 部分/全部缺失 → 可能过时或过严
    if must and frac < 0.5:
        recs.append({
            "field": "must_include_terms",
            "issue": f"must_include_terms 命中率低({round(frac,2)}), 缺失 {missing_terms}",
            "suggestion": "这些词可能过时或过于具体, 建议换成更稳定的词",
            "evidence": {"matched": matched, "missing": missing_terms},
        })
        confidence = "high" if frac == 0 else max(confidence, "medium", key=_clevel)

    # 3) 可选 refined 探针: 加通用 hint 看是否能找到更权威的域
    probe_info = None
    if do_probe and budget.can_fetch() and expected and not present_expected:
        hint = GENERIC_HINTS.get(case.get("preferred_source_type", "any"), "")
        if hint:
            probe_q = f"{q} {hint}"
            pres = engine.search(probe_q, top_k=8)
            pres_results = pres.get("results", [])
            pauth = authoritative_domains(pres_results, top=5)
            probe_info = {"probe_query": probe_q,
                          "authoritative": pauth,
                          "top_domains": [domain_of(r["url"]) for r in pres_results[:5]]}
            if pauth:
                recs.append({
                    "field": "expected_domains",
                    "issue": "refined 探针发现权威源",
                    "suggestion": f"探针 top 权威源: {sorted({a['domain'] for a in pauth})}",
                    "evidence": pauth,
                })
                confidence = "high"

    return {
        "present_expected": present_expected,
        "authoritative_top": auth,
        "must_terms_frac": round(frac, 2),
        "missing_terms": missing_terms,
        "observed_domains": hosts[:8],
        "probe": probe_info,
        "recommendations": recs,
        "confidence": confidence,
    }


def verify_extract(engine, case, budget):
    url = case["url"]
    res = engine.extract(url)
    md = res.get("markdown", "") or ""
    must = case.get("must_include_terms") or []
    frac, matched = fraction_terms(must, md)
    recs = []
    confidence = "low"
    quality = text_quality_ok(md)
    if not md.strip() or res.get("error"):
        recs.append({"field": "url", "issue": f"抽取失败/空正文 (method={res.get('method_used')}, err={res.get('error')})",
                     "suggestion": "URL 可能失效或强 JS 渲染, 建议更换稳定 URL 或标记 requires_deep", "evidence": {}})
        confidence = "high"
    elif not quality:
        recs.append({"field": "extract", "issue": "正文质量差(疑似 CSS/JS 噪声)",
                     "suggestion": "这是清洗优化目标; 若 URL 本身 JS-heavy 可标记 requires_deep", "evidence": {"len": len(md)}})
        confidence = "medium"
    if must and frac < 0.5:
        recs.append({"field": "must_include_terms", "issue": f"正文中 must_terms 命中率低({round(frac,2)}), 缺 {[t for t in must if t not in matched]}",
                     "suggestion": "词可能过时或不在该页, 建议核对页面实际内容后更新", "evidence": {"matched": matched}})
        confidence = max(confidence, "medium", key=_clevel)
    return {"md_len": len(md), "quality_ok": quality, "method": res.get("method_used"),
            "must_terms_frac": round(frac, 2), "recommendations": recs, "confidence": confidence}


def verify_github(engine, case, budget):
    res = engine.github_search(case["query"], kind=case.get("kind", "repos"), limit=case.get("limit", 5))
    items = res.get("results", []) or []
    expected = case.get("expected_domains") or []
    blob = json.dumps(items, ensure_ascii=False).lower()
    present = [e for e in expected if e.lower() in blob]
    recs = []
    confidence = "low"
    if not items:
        recs.append({"field": "query", "issue": f"gh search 0 结果 (err={res.get('error')})",
                     "suggestion": "query 可能过窄/语法不当, 建议放宽或调整 kind", "evidence": {}})
        confidence = "high"
    elif expected and not present:
        recs.append({"field": "expected_domains", "issue": "期望仓库未出现在结果中",
                     "suggestion": f"实际 top 仓库: {[it.get('fullName') or it.get('repository') for it in items[:5]]}",
                     "evidence": {}})
        confidence = "medium"
    return {"total": len(items), "present_expected": present, "recommendations": recs, "confidence": confidence}


def verify_map(engine, case, budget):
    res = engine.map_site(case["url"], max_links=case.get("max_links", 50),
                          same_domain=case.get("same_domain", True))
    links = res.get("links", []) or []
    recs = []
    confidence = "low"
    if not links:
        recs.append({"field": "url", "issue": f"map 0 链接 (err={res.get('error')})",
                     "suggestion": "站点无 sitemap 且页面链接抓取失败, 建议更换站点", "evidence": {}})
        confidence = "high"
    return {"link_count": len(links), "sources": sorted(set(l.get("source", "") for l in links)),
            "recommendations": recs, "confidence": confidence}


_CLEVELS = {"low": 0, "medium": 1, "high": 2}


def _clevel(c):
    return _CLEVELS.get(c, 0)


def main():
    ap = argparse.ArgumentParser(description="Agent Search 联网核验")
    ap.add_argument("--round", type=int, default=0, help="轮次(写 case_updates_round_N.json)")
    ap.add_argument("--ids", default=None, help="逗号分隔的 case id; 不给则取 latest.json 失败+低分 case")
    ap.add_argument("--max-network-calls", type=int, default=20)
    ap.add_argument("--low-score", type=float, default=0.7, help="低于该分视为低置信也核验")
    ap.add_argument("--probe", action="store_true", help="允许 refined 探针(额外联网)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    with open(CASES_PATH, encoding="utf-8") as f:
        cases = json.load(f)
    by_id = {c["id"]: c for c in cases}

    if args.ids:
        target_ids = [i.strip() for i in args.ids.split(",") if i.strip()]
    else:
        with open(LATEST_PATH, encoding="utf-8") as f:
            latest = json.load(f)
        target_ids = []
        for r in latest["cases"]:
            if (not r["passed"]) or r["score"] < args.low_score:
                target_ids.append(r["id"])
    target_ids = [i for i in target_ids if i in by_id]

    budget = evalcache.Budget(max_network=args.max_network_calls, max_ask=6,
                              offline=False, refresh=False)
    engine = AgentSearch()
    evalcache.activate(engine, budget)

    updates = []
    for cid in target_ids:
        case = by_id[cid]
        t = case["type"]
        budget.begin_case("network")  # 核验一律按 network 计费, 不触发 ask
        if t in ("search", "ask"):
            v = verify_search_like(engine, case, budget, do_probe=args.probe)
        elif t == "extract":
            v = verify_extract(engine, case, budget)
        elif t == "github":
            v = verify_github(engine, case, budget)
        elif t == "map":
            v = verify_map(engine, case, budget)
        else:
            budget.end_case()
            continue
        budget.end_case()
        if v.get("recommendations"):
            updates.append({"id": cid, "type": t, "query": case.get("query"),
                            "url": case.get("url"), "confidence": v["confidence"],
                            "findings": v})

    budget.save()
    out = {
        "round": args.round,
        "verified_count": len(target_ids),
        "budget": budget.summary(),
        "recommended_case_updates": updates,
    }
    out_path = os.path.join(HERE, "results", f"case_updates_round_{args.round}.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(f"\n核验 {len(target_ids)} 个 case, 产出 {len(updates)} 条建议 "
              f"(联网 {budget.run_network}, 累计 {budget.cum['network_total']}/{evalcache.HARD_NETWORK_LIMIT})")
        for u in updates:
            print(f"\n[{u['id']}] ({u['type']}, conf={u['confidence']})")
            for r in u["findings"]["recommendations"]:
                print(f"  - {r['field']}: {r['issue']}")
                print(f"    → {r['suggestion']}")
        print(f"\n写入 {os.path.relpath(out_path, ROOT)}\n")


if __name__ == "__main__":
    main()
