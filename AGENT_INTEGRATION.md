# 让 Agent 默认调用本搜索服务

一个后端(`search.py` 的 `AgentSearch`),三个出口。下面是三种接入方式。
**"默认调用"的关键 = 工具描述写清楚(已写好) + system prompt 立规矩。**

---

**暴露的工具**（8 个）：`web_search`(搜索，含豆包引擎、时间/分类过滤、本地重排；`expand_mode` 识别"对比/选型"意图自动多角度并发扇出找全候选；拼写容错 L0-L3：消费 SearXNG corrections → rapidfuzz 编辑距离 → 模糊重排 → LLM 纠拼写)、`web_ask`(自建 SearXNG+DeepSeek RAG)、`web_crawl`(递归深抓 2–6 级，沿链接 BFS/best-first 抓多级正文，带预算与逐链接 SSRF 护栏；先 `web_map` 探路再 `web_crawl` 深抓)、`compare_solutions`(通用方案对比矩阵：任意候选拉齐，GitHub 候选取一手事实、非 GitHub 走官方页规则/可选 LLM 抽取，每格带 source_url+证据+置信度可追溯)、`github_search`(查 GitHub，走 gh cli)、`github_compare`(技术选型对比：GitHub API 一手事实 + OpenSSF Scorecard 健康分 via deps.dev，不下结论)、`web_extract`(抓正文)、`web_map`(发现站点链接)。

## 方式一：Claude Code / Codex / Claude Desktop / Cursor（MCP）⭐ 最省事

已生成项目级配置 `.mcp.json`，在本项目目录启动 `claude` 会自动加载上述工具。

**Codex CLI**：已写入 `~/.codex/config.toml` 的 `[mcp_servers.agent-search]`，重启 codex 即可用。

**加到全局（任意目录都能用）：**
```bash
claude mcp add agent-search \
  ~/Developer/tools/agent-search-service/.venv/bin/python \
  ~/Developer/tools/agent-search-service/mcp_server.py
```

**Claude Desktop**：把 `.mcp.json` 里的 `mcpServers` 块合并进
`~/Library/Application Support/Claude/claude_desktop_config.json`，重启 App。

**让它"默认调用"**：在 Claude Code 的 `CLAUDE.md` 或 Desktop 的自定义指令里加一句：
> 凡是涉及实时信息、新闻、版本/价格、或我给的网址，先调用 web_search / web_ask，不要凭记忆回答。
> 找开源库/做技术选型时优先用 `github_search`，并结合维护活跃度(最近提交/是否归档) + license(规避 GPL/AGPL) + star 综合判断，别只看 star。
> 只想拿链接/摘要用 `web_search`(轻)，需要综合多源给带引用的结论才用 `web_ask`(重、有成本)。

> ⚠️ 新增/改动 MCP 配置后需要**重启 Claude 会话**才生效。

### 国际/英文权威源：并行用宿主自带搜索补全

本服务底座是本地 SearXNG，偏**中文/国内索引**，对国际/英文权威源（stackoverflow、owasp.org、reuters、英文官方文档、GitHub discussions 等）召回较弱（这是回测里唯一过不去的点）。**最省事的解法不是换搜索基建，而是让 agent 在这类查询上并行用它自身的联网搜索**，两边结果合并取舍：

- **Claude Code / Desktop** → 在 `CLAUDE.md` 加：
  > 查国际/英文权威源、或 web_search/web_ask 结果明显偏国内站时，同时并行调用 Claude 自带的 `WebSearch` / `WebFetch` 交叉补全。
- **Codex** → 在 `~/.codex/AGENTS.md` 加同样意思的一句，把"Claude 自带 WebSearch/WebFetch"换成"Codex 内置的联网搜索/网页工具"。注意区分二者：MCP 的 `web_search`/`web_ask` 是本服务，Codex 内置搜索是另一套，两边都跑。
- **Hermes / OpenCode** → 若已关掉内置 web 强制走 agent-search，则保持现状（无国际/英文源时本就以国内源为主）；需要国际/英文源可临时放开其内置 web 工具并行。

原则：**国内/中文用 agent-search，国际/英文权威源并行加一手宿主自带搜索**，互补而非二选一。

---

## 方式二：自建 LLM agent（function calling，走 HTTP API）

先起服务：
```bash
./run.sh serve            # → http://localhost:8077 （文档 /docs）
```

工具 schema 直接拿现成的：`curl http://localhost:8077/openai-tools`

兼容 Tavily 风格的调用也可直接用：
```bash
curl -X POST http://localhost:8077/tavily/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"OpenAI API pricing","max_results":5,"include_raw_content":false}'
```

站点链接发现：
```bash
curl -X POST http://localhost:8077/map \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://platform.openai.com/docs","max_links":20}'
```

**Python 示例（DeepSeek/OpenAI 通用）：**
```python
import requests
from openai import OpenAI

client = OpenAI(api_key="sk-...", base_url="https://api.deepseek.com")
tools = requests.get("http://localhost:8077/openai-tools").json()

SYSTEM = "你是联网助手。涉及实时信息/新闻/资料时，必须先调用 web_search 或 web_ask，不要凭记忆答。"
messages = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": "deepseek v4 现在怎么收费？"}]

resp = client.chat.completions.create(model="deepseek-chat", messages=messages, tools=tools)
msg = resp.choices[0].message

# 模型决定调用工具 → 打到本地 HTTP API
for call in (msg.tool_calls or []):
    import json
    args = json.loads(call.function.arguments)
    endpoint = "/ask" if call.function.name == "web_ask" else "/search"
    result = requests.post(f"http://localhost:8077{endpoint}", json=args).json()
    messages.append(msg)
    messages.append({"role": "tool", "tool_call_id": call.id,
                     "content": json.dumps(result, ensure_ascii=False)})

# 再让模型基于工具结果作答
final = client.chat.completions.create(model="deepseek-chat", messages=messages)
print(final.choices[0].message.content)
```

---

## 方式三：命令行 / 脚本类 agent

最轻量，不用起服务，agent 直接 subprocess 调：
```bash
./run.sh search "关键词" --json        # 纯搜索，JSON 输出
./run.sh ask "问题"                    # DeepSeek 问答
./run.sh search -f "关键词" --json     # 绕过 CAPTCHA
./run.sh search "关键词" --time-range day --categories news --json
./run.sh search --map "https://example.com/docs" --top-k 20 --json
```
在 agent 的 system prompt 里告诉它这条命令格式即可。

---

## 端口/服务一览

| 服务 | 端口 | 启动 |
|------|------|------|
| SearXNG | 8888 | `./run.sh start` |
| FlareSolverr | 8191 | `./run.sh bridge start` |
| HTTP API | 8077 | `./run.sh serve`（含 /search /ask /github /extract /openai-tools） |
| MCP server | stdio | 由 MCP 客户端拉起（Claude `.mcp.json` / Codex `~/.codex/config.toml`） |

可选增强依赖见 `requirements-optional.txt`。当前已支持 `trafilatura` 自动增强正文抽取；没安装时会回退 Jina Reader / requests。

## GitHub 搜索（gh cli）

`github_search` 工具走本机已登录的 `gh`，无需额外 key、比网页搜索精准。
- MCP：`github_search(query, kind, limit)`，kind ∈ repos/code/issues/prs
- HTTP：`POST /github  {"query":"...","kind":"repos","limit":5}`
- CLI：直接 `gh search repos/code/issues "..."`
