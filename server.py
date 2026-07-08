#!/usr/bin/env python3
"""
HTTP API — 把 AgentSearch 暴露成 REST 接口，供自建 LLM agent (function calling) 调用。

启动:
  ./run.sh serve            # 或 .venv/bin/uvicorn server:app --port 8000

端点:
  GET  /health              健康检查
  POST /search              纯搜索
  POST /ask                 DeepSeek RAG 问答
  POST /research            深度研究(分章带引用的 Markdown 调研报告)
  POST /extract             单 URL 抓正文
  POST /map                 发现站点链接
  POST /tavily/search       Tavily-compatible 搜索响应
  GET  /openai-tools        返回 OpenAI function-calling 的 tool schema(直接拿去注册)
  GET  /docs                Swagger UI(FastAPI 自带)
"""

from fastapi import FastAPI
from pydantic import BaseModel
from typing import Optional

from search import AgentSearch

# 全局单例：构造时会做一次后端探测(SearXNG/FlareSolverr 是否在线)，不要每请求都建
_engine: Optional[AgentSearch] = None


def get_engine() -> AgentSearch:
    global _engine
    if _engine is None:
        _engine = AgentSearch()
    return _engine


app = FastAPI(
    title="Agent Search Service",
    description="本地搜索服务：SearXNG + FlareSolverr + DeepSeek RAG",
    version="1.0",
)


# ---------- 请求模型 ----------

class SearchReq(BaseModel):
    query: str
    engines: Optional[list[str]] = None      # 如 ["brave","github"]，留空用默认
    top_k: int = 10
    deep: bool = False                        # Crawl4AI 深度抓取
    use_flaresolverr: bool = False            # 绕过 CAPTCHA 搜 Google/Bing/DDG
    time_range: Optional[str] = None           # day / month / year
    categories: Optional[list[str]] = None
    language: Optional[str] = None
    safe_search: Optional[int] = None
    rerank: bool = True
    expand_mode: str = "auto"                  # off / auto / compare 多查询扩展策略


class AskReq(BaseModel):
    query: str
    engines: Optional[list[str]] = None
    num_sources: int = 4
    deep: bool = False
    use_flaresolverr: bool = False
    time_range: Optional[str] = None
    categories: Optional[list[str]] = None
    language: Optional[str] = None
    safe_search: Optional[int] = None


class ResearchReq(BaseModel):
    query: str
    report_type: str = "standard"       # standard / detailed / comparison / outline
    num_sections: int = 0               # 0=按报告类型自动
    sources_per_section: int = 0        # 0=按报告类型自动, 上限 5
    render_charts: bool = True          # vega-lite 块渲染成内联 SVG(装了 vl-convert 才生效)
    engines: Optional[list[str]] = None
    time_range: Optional[str] = None
    categories: Optional[list[str]] = None
    language: Optional[str] = None


class ExtractReq(BaseModel):
    url: str
    deep: bool = False


class MapReq(BaseModel):
    url: str
    max_links: int = 50
    same_domain: bool = True


class CrawlReq(BaseModel):
    url: str
    max_depth: int = 2                  # 起始页之外的层数(2~6 级), 硬上限 6
    max_pages: int = 30                 # 总页数上限, 硬上限 120
    scope: str = "same-domain"          # same-domain / path-prefix / any
    include: Optional[list[str]] = None
    exclude: Optional[list[str]] = None
    concurrency: int = 4
    relevance: str = ""                 # 给了则按 URL 相关性 best-first
    return_markdown: bool = True


class GitHubReq(BaseModel):
    query: str
    kind: str = "repos"          # repos / code / issues / prs
    limit: int = 5


class GitHubCompareReq(BaseModel):
    repos: Optional[list[str]] = None   # ["owner/name", ...] 或 GitHub URL
    query: Optional[str] = None          # 不给 repos 时按 star 搜 top N 再比
    limit: int = 5


class CompareReq(BaseModel):
    topic: str = ""
    candidates: Optional[list[str]] = None
    dimensions: Optional[list[str]] = None
    num_sources: int = 3
    use_llm: bool = False
    deep: bool = False


class TavilySearchReq(BaseModel):
    query: str
    max_results: int = 10
    include_raw_content: bool = False
    engines: Optional[list[str]] = None
    categories: Optional[list[str]] = None
    time_range: Optional[str] = None
    include_answer: bool = False
    search_depth: str = "basic"


# ---------- 端点 ----------

@app.get("/health")
def health():
    return {"status": "ok", **get_engine().diagnostics()}


@app.post("/search")
def search(req: SearchReq):
    return get_engine().search(
        req.query, engines=req.engines, top_k=req.top_k,
        deep=req.deep, use_flaresolverr=req.use_flaresolverr,
        time_range=req.time_range, categories=req.categories,
        language=req.language, safe_search=req.safe_search,
        rerank=req.rerank, expand_mode=req.expand_mode,
    )


@app.post("/ask")
def ask(req: AskReq):
    return get_engine().answer(
        req.query, engines=req.engines, num_sources=req.num_sources,
        deep=req.deep, use_flaresolverr=req.use_flaresolverr,
        time_range=req.time_range, categories=req.categories,
        language=req.language, safe_search=req.safe_search,
    )


@app.post("/research")
def research(req: ResearchReq):
    """深度研究: 规划大纲→扇出检索→取证→分章综合, 返回带 [n] 引用的完整 Markdown 报告。"""
    return get_engine().research(
        req.query, report_type=req.report_type, num_sections=req.num_sections,
        sources_per_section=req.sources_per_section, render_charts=req.render_charts,
        engines=req.engines, time_range=req.time_range,
        categories=req.categories, language=req.language,
    )


@app.post("/extract")
def extract(req: ExtractReq):
    return get_engine().extract(req.url, deep=req.deep)


@app.post("/map")
def map_site(req: MapReq):
    return get_engine().map_site(req.url, max_links=req.max_links, same_domain=req.same_domain)


@app.post("/crawl")
def crawl(req: CrawlReq):
    """递归深抓 2~6 级正文(web_map 的递归版 + 抓正文)。"""
    return get_engine().crawl(
        req.url, max_depth=req.max_depth, max_pages=req.max_pages, scope=req.scope,
        include=req.include, exclude=req.exclude, concurrency=req.concurrency,
        relevance=req.relevance, return_markdown=req.return_markdown,
    )


@app.post("/github")
def github(req: GitHubReq):
    return get_engine().github_search(req.query, kind=req.kind, limit=req.limit)


@app.post("/github/compare")
def github_compare(req: GitHubCompareReq):
    """技术选型对比：GitHub API 一手事实 + OpenSSF Scorecard 健康分，不下结论。"""
    return get_engine().github_compare(repos=req.repos, query=req.query, limit=req.limit)


@app.post("/compare")
def compare_solutions(req: CompareReq):
    """通用方案对比矩阵：任意候选拉齐成矩阵，每格带来源可追溯(GitHub 候选取一手事实)。"""
    return get_engine().compare_solutions(
        topic=req.topic, candidates=req.candidates, dimensions=req.dimensions,
        num_sources=req.num_sources, use_llm=req.use_llm, deep=req.deep)


@app.post("/tavily/search")
def tavily_search(req: TavilySearchReq):
    """Tavily-compatible search response for drop-in agent integrations."""
    import time

    t0 = time.time()
    eng = get_engine()
    res = eng.search(
        req.query,
        engines=req.engines,
        top_k=req.max_results,
        time_range=req.time_range,
        categories=req.categories,
        rerank=True,
    )
    results = []
    for item in res.get("results", []):
        raw_content = None
        if req.include_raw_content:
            ext = eng.extract(item["url"])
            raw_content = ext.get("markdown") or None
        results.append({
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "content": item.get("snippet", ""),
            "score": item.get("rank_score", item.get("score", 0)),
            "raw_content": raw_content,
        })

    answer = None
    if req.include_answer:
        asked = eng.answer(req.query, engines=req.engines, num_sources=min(4, req.max_results))
        answer = asked.get("answer")

    return {
        "query": req.query,
        "answer": answer,
        "results": results,
        "response_time": round(time.time() - t0, 3),
    }


@app.get("/openai-tools")
def openai_tools():
    """返回 OpenAI / DeepSeek function-calling 的 tools 定义，自建 agent 直接拿去注册。"""
    return [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "联网搜索，返回标题/URL/摘要列表。需要实时信息、新闻、文档、技术资料时调用。底座为本地 SearXNG(偏中文/国内源)，查国际/英文权威源(stackoverflow/owasp/reuters/英文官方文档等)覆盖弱，建议并行调用宿主自带的 WebSearch/WebFetch 交叉补全。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索关键词"},
                        "top_k": {"type": "integer", "description": "返回结果数", "default": 10},
                        "time_range": {"type": "string", "enum": ["day", "month", "year"], "description": "时间范围过滤"},
                        "categories": {"type": "array", "items": {"type": "string"}, "description": "分类，如 general/news/images"},
                        "language": {"type": "string", "description": "语言代码，如 zh-CN / en-US"},
                        "use_flaresolverr": {
                            "type": "boolean",
                            "description": "是否绕过 CAPTCHA 搜 Google/Bing/DuckDuckGo(慢 5-15s，结果更全)",
                            "default": False,
                        },
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "web_ask",
                "description": "联网搜索并由 DeepSeek 总结出带引用来源的答案(RAG)。需要综合多源给出有出处的结论时调用。本工具较重(并行抓多页+调 LLM，慢且有成本)：只想要链接/摘要用更轻的 web_search，需要带引用的综合结论才用本工具。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "要回答的问题"},
                        "num_sources": {"type": "integer", "description": "参考来源数量", "default": 4},
                        "use_flaresolverr": {"type": "boolean", "default": False},
                        "time_range": {"type": "string", "enum": ["day", "month", "year"]},
                        "categories": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "web_research",
                "description": "深度研究：规划大纲→并发扇出检索→抓正文取证→分章综合，产出带 [n] 引用的完整 Markdown 调研报告(标题/分章正文/结论与建议/参考来源)，数值数据渲染为内联 SVG 矢量图。需要可直接交付的调研报告(技术选型/领域综述/竞品分析)时调用。最重的工具(多路搜索+抓十余页正文+多次 LLM，约 1~3 分钟)：只要链接用 web_search，一个问题要一段结论用 web_ask，要整篇报告才用本工具。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "调研主题，越具体越好"},
                        "report_type": {"type": "string", "enum": ["standard", "detailed", "comparison", "outline"],
                                        "description": "报告类型：standard 标准/detailed 深度详报/comparison 选型对比/outline 大纲预览(只规划秒回)", "default": "standard"},
                        "num_sections": {"type": "integer", "description": "章节数，0=按类型自动", "default": 0},
                        "sources_per_section": {"type": "integer", "description": "每章引用来源数，0=按类型自动(上限 5)", "default": 0},
                        "time_range": {"type": "string", "enum": ["day", "month", "year"]},
                        "language": {"type": "string", "description": "语言代码，如 zh-CN / en-US"},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "github_search",
                "description": "在 GitHub 搜索仓库/代码/issue/PR(优先 gh CLI，失败时走 GitHub API)。查开源项目、代码用法、issue 时用，比网页搜索精准。repos 结果含 license/pushedAt(最近提交)/isArchived(是否归档)/forksCount；做开源选型时综合维护活跃度+license(规避 GPL/AGPL)+star 判断，别只看 star。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索词，code 类型支持 GitHub 搜索语法"},
                        "kind": {"type": "string", "enum": ["repos", "code", "issues", "prs"], "default": "repos"},
                        "limit": {"type": "integer", "default": 5},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "github_compare",
                "description": "技术选型对比：拉齐多个 GitHub 项目的一手事实(star/license/最近提交/release/归档/issues)+ OpenSSF Scorecard 健康分(经 deps.dev 免费 API)，并标注 已归档/近N天无提交/无release/copyleft 等信号。对比开源库做选型时用，比看网页 best/top 软文可靠。只给证据，不替你下结论。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "repos": {"type": "array", "items": {"type": "string"},
                                  "description": "候选仓库 owner/name 或 GitHub URL 列表"},
                        "query": {"type": "string", "description": "不给 repos 时按 star 搜 top N 再比"},
                        "limit": {"type": "integer", "default": 5},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "web_extract",
                "description": "抓取指定网页正文并转成 Markdown。用户给出 URL，或需要读取某个网页全文时调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "目标网页 URL"},
                        "deep": {
                            "type": "boolean",
                            "description": "是否用 Crawl4AI 深度抓取 JS 渲染页面",
                            "default": False,
                        },
                    },
                    "required": ["url"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "web_map",
                "description": "发现一个站点的 sitemap 或页面链接。需要先了解站点结构、找文档页、批量抓取入口时调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "站点或页面 URL"},
                        "max_links": {"type": "integer", "default": 50},
                        "same_domain": {"type": "boolean", "default": True},
                    },
                    "required": ["url"],
                },
            },
        },
    ]


def main():
    import os
    import uvicorn
    # 默认只绑本机(127.0.0.1)。要对外暴露请显式设 HOST=0.0.0.0, 并自行加认证/反代。
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8077"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
