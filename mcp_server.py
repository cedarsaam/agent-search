#!/usr/bin/env python3
"""
MCP server — 把本地搜索服务暴露给 Claude Code / Claude Desktop / Cursor 等 MCP 客户端。

配置好后，agent 工具列表里会自动出现 web_search / web_ask / web_extract / web_map，
可被"默认调用"(由 system prompt + 工具描述驱动)。

本地用 stdio 传输运行：
  .venv/bin/python mcp_server.py

依赖：mcp[cli]（已装在 .venv）
"""

from mcp.server.fastmcp import FastMCP

from search import AgentSearch

mcp = FastMCP("agent-search")

# 全局单例：构造时探测后端，只做一次
_engine = None


def engine() -> AgentSearch:
    global _engine
    if _engine is None:
        _engine = AgentSearch()
    return _engine


@mcp.tool()
def web_search(query: str, top_k: int = 8, use_flaresolverr: bool = False,
               time_range: str = "", categories: str = "", language: str = "",
               expand_mode: str = "auto") -> list:
    """联网搜索，返回结果列表(标题/URL/摘要)。

    需要实时信息、新闻、技术文档、查资料时调用本工具，而不是凭记忆回答。

    注意：本服务底座是本地 SearXNG，偏中文/国内索引；查国际/英文权威源
    (stackoverflow、owasp.org、reuters、英文官方文档、GitHub discussions 等)时覆盖较弱，
    建议同时并行调用宿主自带的联网搜索(如 Claude 的 WebSearch/WebFetch、Codex 内置搜索)交叉补全。

    Args:
        query: 搜索关键词
        top_k: 返回结果数(默认 8)
        use_flaresolverr: 是否绕过 CAPTCHA 搜 Google/Bing/DuckDuckGo
                          (慢 5-15 秒但结果更全；默认 False，用 Brave/Startpage 等直连引擎)
        time_range: 时间范围 day/month/year，留空不过滤
        categories: SearXNG 分类，逗号分隔，如 general,news
        language: 语言代码，如 zh-CN / en-US
        expand_mode: 多查询扩展策略 off/auto/compare。auto(默认)识别"对比/选型"意图后
                     自动多角度并发扇出(alternatives/comparison/benchmark/best)把候选找全；
                     compare 强制扇出(找方案做选型时用)；off 只搜原词。
    """
    cats = [c.strip() for c in categories.split(",") if c.strip()] if categories else None
    res = engine().search(
        query,
        top_k=top_k,
        use_flaresolverr=use_flaresolverr,
        time_range=time_range or None,
        categories=cats,
        language=language or None,
        expand_mode=expand_mode or "auto",
    )
    return [
        {"title": r["title"], "url": r["url"], "snippet": r.get("snippet", ""), "rank_score": r.get("rank_score")}
        for r in res.get("results", [])
    ]


@mcp.tool()
def web_ask(query: str, num_sources: int = 4, use_flaresolverr: bool = False,
            time_range: str = "", categories: str = "", language: str = "") -> dict:
    """联网搜索 + DeepSeek 总结，返回带引用来源的答案(RAG)。

    需要对一个问题给出综合性、有出处的结论时调用。注意本工具较重(会并行抓多个网页正文 + 调用 LLM，
    慢且有成本)：只想拿链接/摘要/清单，或自己读原文，请用更轻的 web_search；需要"综合多源给结论 + 引用"才用 web_ask。

    Args:
        query: 要回答的问题
        num_sources: 参考来源数量(默认 4)
        use_flaresolverr: 是否绕过 CAPTCHA 搜 Google/Bing/DuckDuckGo
        time_range: 时间范围 day/month/year，留空不过滤
        categories: SearXNG 分类，逗号分隔，如 general,news
        language: 语言代码，如 zh-CN / en-US
    """
    cats = [c.strip() for c in categories.split(",") if c.strip()] if categories else None
    res = engine().answer(
        query,
        num_sources=num_sources,
        use_flaresolverr=use_flaresolverr,
        time_range=time_range or None,
        categories=cats,
        language=language or None,
    )
    return {
        "answer": res.get("answer", ""),
        "sources": res.get("sources", []),
        "error": res.get("error"),
    }


@mcp.tool()
def web_map(url: str, max_links: int = 50, same_domain: bool = True) -> dict:
    """发现站点链接，用于先了解站点结构，再决定抓哪些页面。

    Args:
        url: 站点或页面 URL
        max_links: 最多返回链接数
        same_domain: 是否只返回同域链接
    """
    return engine().map_site(url, max_links=max_links, same_domain=same_domain)


@mcp.tool()
def github_search(query: str, kind: str = "repos", limit: int = 5) -> dict:
    """在 GitHub 上搜索仓库/代码/issue/PR(走本机已登录的 gh CLI，比网页搜索精准)。

    查开源项目、找代码用法、查 issue/PR 时调用本工具，优先于 web_search。

    评估某个开源项目是否值得采用时(prior-art-first / 缝合优于从零造)：
    repos 结果已附带 license / pushedAt(最近提交) / isArchived(是否归档) / forksCount，
    请综合“维护活跃度(最近是否有提交、是否归档)+ license(规避 GPL/AGPL 这类强限制)+ star”
    一起判断，优先成熟且活跃的项目，不要只看 star 数。

    Args:
        query: 搜索词(code 类型可用 GitHub 搜索语法，如 'language:python asyncio')
        kind: repos(仓库) / code(代码) / issues(问题) / prs(拉取请求)
        limit: 返回条数(默认 5)
    """
    return engine().github_search(query, kind=kind, limit=limit)


@mcp.tool()
def github_compare(repos: list[str] = [], query: str = "", limit: int = 5) -> dict:
    """技术选型对比：拉齐多个 GitHub 项目的一手事实 + 成熟度信号，给证据但不下结论。

    专为"对比选型"设计：用 GitHub API（一手字节）+ OpenSSF Scorecard（经 deps.dev 免费 API）
    取每个候选的 star/fork/license/最近提交/最新 release/open issues/是否归档 + 健康分，
    并给出事实性标记（已归档 / 近 N 天无提交 / 无 release / copyleft 许可证）。
    比看网页"best/top 10"软文可靠得多（规避 star 虚高与 SEO 污染）。结论由你/用户判断，本工具不替你拍板。

    Args:
        repos: 候选仓库列表，"owner/name" 或完整 GitHub URL（给了就直接对比这些）
        query: 关键词；不给 repos 时，先按 star 搜 top N 再对比
        limit: query 模式下取多少个候选（默认 5）
    """
    return engine().github_compare(repos=list(repos) or None, query=query or None, limit=limit)


@mcp.tool()
def web_extract(url: str, deep: bool = False) -> dict:
    """抓取指定网页正文并转成 Markdown。

    Args:
        url: 目标网页地址
        deep: 是否用 Crawl4AI 深度抓取(处理 JS 渲染页面)
    """
    res = engine().extract(url, deep=deep)
    return {
        "url": res.get("url", url),
        "title": res.get("title", ""),
        "markdown": res.get("markdown", ""),
        "method": res.get("method_used", ""),
        "error": res.get("error"),
    }


def main():
    mcp.run()  # 默认 stdio 传输


if __name__ == "__main__":
    main()
