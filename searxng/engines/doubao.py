# SPDX-License-Identifier: AGPL-3.0-or-later
"""火山引擎豆包搜索 (融合信息搜索) —— SearXNG 自定义 engine。

把豆包搜索 API 接进 SearXNG，结果会和 Brave/Startpage 等一起聚合显示，
也可用 !db 关键词 单独搜。

API Key 通过容器环境变量 WEB_SEARCH_API_KEY 注入（见 docker-compose.yml）。
"""

import os
from json import dumps, loads

# ---- engine 元信息 ----
about = {
    "website": "https://www.volcengine.com/product/web-search",
    "official_api_documentation": "https://www.volcengine.com/docs/84296",
    "use_official_api": True,
    "require_api_key": True,
    "results": "JSON",
}

categories = ["general"]
paging = False
language = "zh"   # 豆包搜索默认中文

# 可被 settings.yml 里同名字段覆盖
search_url = "https://open.feedcoopapi.com/search_api/web_search"
api_key = ""          # 留空则读环境变量 WEB_SEARCH_API_KEY
count = 10


def _resolve_key():
    return api_key or os.environ.get("WEB_SEARCH_API_KEY", "")


def request(query, params):
    key = _resolve_key()
    params["url"] = search_url
    params["method"] = "POST"
    params["headers"]["Authorization"] = f"Bearer {key}"
    params["headers"]["Content-Type"] = "application/json"
    params["data"] = dumps({
        "Query": query,
        "SearchType": "web",
        "Count": count,
        "QueryControl": {"QueryRewrite": True},
    })
    return params


def response(resp):
    results = []
    try:
        data = resp.json()
    except ValueError:
        data = loads(resp.text or "{}")

    web_results = (data.get("Result") or {}).get("WebResults") or []
    for item in web_results:
        url = item.get("Url")
        if not url:
            continue
        results.append({
            "url": url,
            "title": item.get("Title", ""),
            "content": item.get("Summary") or item.get("Snippet", ""),
        })
    return results
