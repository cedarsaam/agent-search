<div align="center">

# 🔎 Agent Search

**A self-hosted, MCP-native web-search backend for AI agents** — meta-search, clean extraction, RAG with citations, GitHub project selection, and a Tavily-compatible API. All free, all local.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)
![MCP](https://img.shields.io/badge/MCP-native-success.svg)
![Self-hosted](https://img.shields.io/badge/self--hosted-100%25-orange.svg)
![No API key required](https://img.shields.io/badge/search-no%20API%20key-green.svg)

**English** · [简体中文](README.zh-CN.md)

</div>

---

## Why?

Built-in `WebSearch` / `WebFetch` give you links and snippets. Your agent still has to search → fetch → read → reconcile by hand, and the results are easily polluted by SEO blogs and inflated stars.

**Agent Search turns "search primitives" into "search outcomes":** aggregate many engines, rank with official-source priority, extract clean text, and answer with **chunk-level citations** — exposed as **one MCP server** any agent (Claude Code, Codex, Cursor, …) can call by default. It also does the things the built-ins can't: **typed GitHub search**, **first-party project comparison for tech selection**, **site mapping**, and a **Tavily-compatible** endpoint.

## ✨ Features

- **Meta-search over 9 engines** via [SearXNG](https://github.com/searxng/searxng) (Google/Bing/DDG/Brave/Wikipedia/GitHub/StackOverflow/Reddit/News) with URL dedup.
- **Smart local reranking** — boosts official docs / API / pricing / changelog pages, **down-weights SEO content farms**, multi-query expansion for doc & pricing intent.
- **Robust extraction** — `trafilatura → Jina Reader → requests` fallback chain, ratio-based noise cleaning (keeps tables/code/prices/dates), optional **Crawl4AI** for JS-heavy pages.
- **RAG with citations** — search → parallel multi-source fetch → LLM summary with `[1][2]` references and **per-source excerpts** (chunk-level evidence); bad body falls back to snippet.
- **GitHub, done right** — typed `repos/code/issues/prs` search via the `gh` CLI, returning `license / last-commit / archived / forks` for real evaluation, not just stars.
- **🆕 Tech-selection compare** — `github_compare` pulls **first-party facts** (`gh api`) + **OpenSSF Scorecard** health (via the free [deps.dev](https://deps.dev) API) and flags *archived / stale / no-release / copyleft*. Evidence, not verdicts.
- **Site mapping** — `sitemap.xml` first, page-link fallback, same-domain dedup.
- **Tavily-compatible API** — drop-in `/tavily/search` with stable `include_raw_content`.
- **Caching** — SQLite TTL cache; works offline against the cache.

## 🆚 How it compares

No single OSS project covers this niche — most are end-user apps, single-capability tools, or higher-level orchestrators.

| Project | Multi-engine | Extract (JS) | RAG + cites | GitHub typed | Site map | Native MCP | Tavily-compat |
|---|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| Firecrawl | ⚠️ single-src | ✅✅ | ✅ | ⚠️ | ✅ | ✅ | ❌ |
| Crawl4AI | ❌ | ✅✅ | ⚠️ | ❌ | ✅ | ✅ | ❌ |
| Perplexica | ✅ | ⚠️ | ✅ | ❌ | ❌ | ❌ | ❌ |
| GPT Researcher | ⚠️ | ✅ | ✅ report | ❌ | ❌ | ❌ | ❌ |
| SearXNG | ✅✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| mcp-searxng | ✅ | ⚠️ | ❌ | ❌ | ❌ | ✅ | ❌ |
| **Agent Search** | **✅ 9** | ⚠️/✅ opt | **✅ chunk** | **✅✅** | **✅** | **✅ 6 tools** | **✅ only one** |

## 🏗️ Architecture

```
Agent / MCP client
        │  web_search · web_ask · web_extract · web_map · github_search · github_compare
        ▼
   Agent Search  (FastAPI / MCP / CLI)
        ├─ SearXNG (9 engines, local Docker)      → meta-search + rerank
        ├─ trafilatura / Jina / requests / Crawl4AI → clean extraction
        ├─ LLM (OpenAI-compatible, e.g. DeepSeek)  → RAG with citations
        ├─ gh CLI                                   → typed GitHub search
        └─ deps.dev + OpenSSF Scorecard            → project selection
```

## 🚀 Quickstart

**1. Start SearXNG (and optional FlareSolverr):**
```bash
cp .env.example .env          # then edit: SEARXNG_SECRET_KEY, (optional) LLM key
docker compose up -d searxng  # add `flaresolverr` only if you need anti-bot handling
```

**2. Install the Python side:**
```bash
python -m venv .venv && source .venv/bin/activate
pip install requests beautifulsoup4 lxml fastapi uvicorn "mcp[cli]" trafilatura
```

**3. Use it** — three ways:
```bash
# CLI
python search.py "python asyncio tutorial"
python search.py "Anthropic Claude API pricing" --answer

# HTTP API (binds 127.0.0.1 by default)
python server.py          # → http://127.0.0.1:8077/docs

# MCP (Claude Code / Cursor / Codex …)
cp .mcp.json.example .mcp.json   # set the absolute path to this repo
```

## 🧰 MCP tools

| Tool | What it does |
|---|---|
| `web_search` | Meta-search, ranked results |
| `web_ask` | RAG answer with `[n]` citations + per-source excerpts |
| `web_extract` | Fetch a page → clean Markdown |
| `web_map` | Discover a site's links (sitemap-first) |
| `github_search` | Typed `repos/code/issues/prs` search |
| `github_compare` | First-party tech-selection comparison (facts + OpenSSF Scorecard) |

> 💡 **Western/English sources:** the local SearXNG leans toward broad coverage but can be thin on some Western authoritative sources. For those, have your agent run its **native** `WebSearch`/`WebFetch` **in parallel** and merge — Agent Search for aggregation/RAG/GitHub, native search for reach.

## ⚠️ Notes & limitations

- `web_ask` (RAG) needs an OpenAI-compatible LLM key; everything else (search/extract/map/github) needs **no API key**.
- Extraction does **not** render JS by default — install the optional `crawl4ai` and use `deep=True` for JS-heavy pages.
- Built for **local / trusted use**: the HTTP server binds `127.0.0.1` by default and extraction has an SSRF guard (blocks localhost / private / cloud-metadata IPs). Add auth + a reverse proxy before exposing it.
- This is a personal project, maintained best-effort. Issues/PRs welcome but no SLA.

## 🙏 Acknowledgements

Stands on the shoulders of: [SearXNG](https://github.com/searxng/searxng) · [trafilatura](https://github.com/adbar/trafilatura) · [Jina Reader](https://github.com/jina-ai/reader) · [Crawl4AI](https://github.com/unclecode/crawl4ai) · [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr) · [OpenSSF Scorecard](https://github.com/ossf/scorecard) + [deps.dev](https://deps.dev) · [GitHub CLI](https://github.com/cli/cli) · FastAPI · the [Model Context Protocol](https://modelcontextprotocol.io). RAG summaries via any OpenAI-compatible endpoint (e.g. DeepSeek).

## 📄 License

[MIT](LICENSE) — do whatever, no warranty. Agent Search orchestrates SearXNG as a separate service (it does not bundle or modify SearXNG's source), so its AGPL does not extend to this project.
