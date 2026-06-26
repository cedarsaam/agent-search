# 反爬 / Cloudflare 处理（可选，FlareSolverr）— Anti-bot handling notes

> ⚠️ **负责任使用 / Use responsibly.** 本文仅为在**自己有权访问**的场景下、应对部分引擎在某些网络环境返回的 challenge 页面提供可选思路。请遵守目标网站的 `robots.txt`、服务条款与当地法律；不要用于绕过付费墙、抓取受限内容或大规模滥用。该能力在本项目中**默认关闭**(`use_flaresolverr=False`)。
> This is an optional aid for environments where some engines return challenge pages on resources you are entitled to access. Respect each site's robots.txt, ToS, and applicable law. Disabled by default.

## 问题背景

SearXNG 已跑通，Brave + Startpage 能出中文结果。但 Google、DuckDuckGo、Bing 在国内 IP 下全返回 CAPTCHA/challenge 页面。SearXNG 自己的 `google.py` engine 已经能检测到 Google CAPTCHA（抛出 `SearxEngineCaptchaException`），但没有内置的对抗能力——它需要外部手段。

---

## 方案总览

按侵入性从低到高排列。建议从 Level 1 开始试，能不碰代码就不碰。

```
Level 1: 代理 IP 池         ← 最简单，SearXNG 原生支持
Level 2: CAPTCHA 求解服务     ← 代码层面包装 search.py
Level 3: 浏览器自动化 + 隐身   ← 重武器，兜底方案
Level 4: SEO 爬虫专用引擎     ← 替换 SearXNG 的 engine
```

---

## Level 1: 代理 IP 池（SearXNG 原生支持，零代码改动）

### 原理

SearXNG 的 `outgoing.proxies` 支持 HTTP/HTTPS/SOCKS5 代理。给 Google/Bing/DuckDuckGo 配不同的出口 IP，CAPTCHA 触发概率直线下降。

### 实现

`searxng/settings.yml` 末尾已有被注释的模板：

```yaml
outgoing:
  proxies:
    all://:
      - socks5h://127.0.0.1:1080
      - http://127.0.0.1:1081
    https://:
      - http://127.0.0.1:1081
```

### 可选代理源（需付费）

| 服务 | 适用场景 | 特点 |
|------|---------|------|
| **Bright Data** (亮数据) | 企业级，住宅 IP | 贵但最稳，全球 IP 池最大 |
| **快代理** | CN 用户首选 | 国内线路优化，价格适中 |
| **芝麻代理** | CN 用户 | 便宜，短效 IP (1-5min) |
| **SmartProxy** | 国际 | 静态住宅 IP，稳定 |
| **WebShare** | 个人用 | 便宜入门，$3/100 IP |
| **Proxy-Seller** | 个人用 | 纯净 IP，搜索场景可 |

### 关键

- **住宅 IP > 机房 IP**：机房 IP（AWS/阿里云）Google 直接封，住宅 IP 存活率高
- **IP 与搜索关键词同区域**：搜中文内容出口中国大陆或台湾 IP；搜英文内容出口美国/日本
- **SearXNG 支持 per-engine 代理**：可以只给 google 和 bing 配代理，brave 和 startpage 直连

---

## Level 2: CAPTCHA 求解服务（search.py 层包装）

### 现有项目

| 项目 | ★ | 说明 |
|------|---|------|
| [2captcha/2captcha-python](https://github.com/2captcha/2captcha-python) | 764 | 2Captcha 官方 SDK，支持 reCAPTCHA v2/v3, Cloudflare Turnstile, hCaptcha, GeeTest |
| [capsolver/capsolver-python](https://github.com/capsolver/capsolver-python) | 72 | Capsolver 官方 SDK，AI 驱动，支持类型同上 |
| [Matthew17-21/Captcha-Tools](https://github.com/Matthew17-21/Captcha-Tools) | 79 | 统一接口包装 2captcha + anticaptcha + capsolver + capmonster |
| [2captcha captcha-solver-selenium-examples](https://github.com/2captcha/captcha-solver-selenium-python-examples) | 70 | Selenium 集成示例，可直接参考 |

### 接入方式

1. 在 `.env` 加 `CAPTCHA_API_KEY=xxx`
2. `search.py` 里加一个 `CaptchaSolver` 类
3. 当 SearXNG 返回 CAPTCHA 错误码时，调 CAPTCHA 求解 API → 把 token 注入搜索请求头
4. 重试搜索

但此方案有个前提：**你得先拿到 CAPTCHA 图片/challenge**。SearXNG 是服务端发 HTTP 请求，如果 Google 直接返回了 CAPTCHA 页面而不是 JSON 搜索结果，search.py 拿到的是个 HTML 验证码页面——这时候调 2captcha 确实可以解，但需要解析出 challenge 的 sitekey。

### 性价比

- 2captcha: $2.99/1000 次 reCAPTCHA 求解
- Capsolver: $2.5/1000 次（AI 模式更便宜）
- **如果每天搜几百次，月成本 < $10**

---

## Level 3: 浏览器自动化 + 指纹隐身（重武器）

### 现有项目

| 项目 | ★ | 说明 |
|------|---|------|
| [ultrafunkamsterdam/undetected-chromedriver](https://github.com/ultrafunkamsterdam/undetected-chromedriver) | 12.7k | **核心项目**。自动 patched ChromeDriver，过 Cloudflare/Distil/Imperva/DataDome。pip install 即用 |
| [seleniumbase/SeleniumBase](https://github.com/seleniumbase/SeleniumBase) | 12.8k | 全功能框架。CDP Mode 宣称过所有 bot 检测，内置 UC Mode + stealth |
| [playwright-extra](https://github.com/playwright-community/playwright-extra) + stealth plugin | 热门 | Playwright 的 stealth 插件，NodeJS 生态好 |
| [rebrowser/rebrowser-bot-detector](https://github.com/rebrowser/rebrowser-bot-detector) | 155 | 检测你的浏览器指纹泄露点，帮调试 |

### 架构方案

**不在 SearXNG 里改**——加一个 proxy 层在中间：

```
SearXNG → Google engine → [Browser Proxy] → Google.com
                              │
                         无头 Chrome
                      (undetected-chromedriver)
                       + 住宅代理 (Bright Data)
```

这个 Browser Proxy 是一个独立服务，接收 SearXNG 的请求，用真实浏览器渲染后返回 HTML。可以做成：

- **Flask/FastAPI 微服务**，跑在 Docker 里
- 服务名 `search-bridge`，暴露 `:8899` 端口
- SearXNG 的 `outgoing.proxies` 或 `settings.yml` 里配 custom engine 指向它

### 预计效果

undetected-chromedriver + 住宅代理 = **99% 搜索请求不触发 CAPTCHA**。SeleniumBase 的 CDP Mode 甚至声称 100%。

### 缺点

- 资源消耗大：每个请求要启一个无头 Chrome
- 延迟增加 2-5 秒（浏览器渲染时间）
- 需要 Docker 环境跑 Chrome（建议单独 container）

---

## Level 4: 替换引擎——绕过 Google 直接找能用的

### 策略

放弃 Google/DuckDuckGo/Bing（国内 IP 不可能绕过 CAPTCHA），用不封搜素引擎。

**SearXNG 已配置可用引擎：**

| 引擎 | 中文质量 | 被封情况 | 备注 |
|------|---------|---------|------|
| **Brave** ✅ | 中上 | 不封 | 自有索引: `https://search.brave.com` |
| **Startpage** ✅ | 中 (实质是 Google 代理) | 可能触发但概率低 | 通过代理访问 Google |
| **Wikipedia** ✅ | 高 | 不封 | 百科直接搜 |
| **Reddit** ✅ | 高 (技术类) | 不封 | 技术社区 |
| **StackOverflow** ✅ | 高 (技术) | 不封 | 纯技术 |
| **GitHub** ✅ | 高 (代码) | 不封 | |

### 建议补充的引擎/数据源

| 数据源 | 怎么接 | 被封风险 |
|--------|-------|---------|
| **百度搜索** | SearXNG 有 baidu engine（需启用） | 不封，但内容质量下降 |
| **Bilibili** | 自己写个简单的 python engine 爬 b23.tv | 不封 |
| **知乎** | SearXNG 有 zhihu engine? 检查下 | 不封 |
| **学术: arXiv/PubMed** | SearXNG 内置 | 不封 |

### 实际落地建议

`searxng/settings.yml` 里把 google/bing/duckduckgo 设 `disabled: true`，把 brave 放第一个。**Brave 的中文搜索来源其实就是增强版的 Bing 索引**，日常够用。搭配 StackOverflow + GitHub + Wikipedia，技术搜索完全足够。

---

## 推荐落地路径

### 第一阶段（今天就能做）

```yaml
# settings.yml - 关闭被 CAPTCHA 的引擎，启用替代
engines:
  - name: google
    disabled: true     # 国内 IP 没法用
  - name: bing
    disabled: true     # 同上
  - name: duckduckgo
    disabled: true     # 同上
  - name: brave        # 主搜索引擎
    disabled: false
  - name: startpage    # 备用
    disabled: false
```

### 第二阶段（买代理后）

```yaml
outgoing:
  proxies:
    all://:
      # 买一个住宅代理服务填入下面的地址
      - socks5h://user:pass@proxy-provider.com:1080
```

如果代理 IP 质量够好，Google/Bing 可以重新启用。

### 第三阶段（需要时加）

写一个 `search-bridge` 微服务（~200 行 Python）：

```python
# search_bridge.py — 无头 Chrome 渲染层
from undetected_chromedriver import Chrome
...
@app.post("/search")
def search_google(query: str):
    driver = Chrome(headless=True)
    driver.get(f"https://www.google.com/search?q={query}")
    # 等待结果加载
    html = driver.page_source
    driver.quit()
    return {"result": parse_google_html(html)}
```

然后 SearXNG 的 `outgoing.proxies` 指向 `http://search-bridge:8899` 即可。

---

## 相关 GitHub 项目汇总

| 项目 | Stars | 用途 | 推荐度 |
|------|-------|------|--------|
| [ultrafunkamsterdam/undetected-chromedriver](https://github.com/ultrafunkamsterdam/undetected-chromedriver) | 12.7k | ChromeDriver 隐身 patching | ⭐⭐⭐⭐⭐ |
| [seleniumbase/SeleniumBase](https://github.com/seleniumbase/SeleniumBase) | 12.8k | 全能浏览器框架 + stealth | ⭐⭐⭐⭐⭐ |
| [2captcha/2captcha-python](https://github.com/2captcha/2captcha-python) | 764 | 付费 CAPTCHA 求解 SDK | ⭐⭐⭐⭐ |
| [capsolver/capsolver-python](https://github.com/capsolver/capsolver-python) | 72 | AI CAPTCHA 求解 SDK | ⭐⭐⭐ |
| [Captain-Tools](https://github.com/Matthew17-21/Captcha-Tools) | 79 | 多 CAPTCHA 服务统一接口 | ⭐⭐⭐ |
| [scrapy-rotating-proxies](https://github.com/TeamHG-Memex/scrapy-rotating-proxies) | 775 | 代理轮换（参考思路） | ⭐⭐⭐ |
| [rebrowser-bot-detector](https://github.com/rebrowser/rebrowser-bot-detector) | 155 | 浏览器指纹检测（调试用） | ⭐⭐ |
| [SearXNG google.py](https://github.com/searxng/searxng/blob/master/searx/engines/google.py) | - | 已内置 CAPTCHA 检测 | - |

---

## 已集成到项目中的方案

当前 `agent-search-service` 已直接集成 **FlareSolverr**，通过 CLI 的 `-f` 标志一键调用。

### 使用方式

```bash
# 启动 FlareSolverr
./run.sh bridge start

# 绕过 CAPTCHA 搜索 Google/Bing/DDG
./run.sh search -f "你的关键词"

# CAPTCHA + DeepSeek 问答
./run.sh ask -f "你的问题"

# 等效命令
.venv/bin/python search.py "关键词" --flaresolverr
.venv/bin/python search.py "问题" --answer --flaresolverr
```

### 架构

```
search.py --flaresolverr
  → FlareSolverrEngine (FlareSolverr API :8191)
    → undetected-chromedriver (Chrome)
      → Google/Bing/DDG (自动解 CAPTCHA)
  → BeautifulSoup 解析结果
  → SQLite 缓存
```

实际效果: FlareSolverr 启动 Chrome + undetected-chromedriver，自动解 Cloudflare IUAM / Turnstile / reCAPTCHA。每个搜索请求约 5-15 秒（浏览器渲染时间），但基本能拿到完整结果。

**依赖:**
- Docker 容器: `ghcr.io/flaresolverr/flaresolverr`
- Docker compose 已配好，`./run.sh bridge start` 一键启动

---

## 下一步（给 Claude 的交接）

1. **已做完**: FlareSolverr docker-compose + search.py 集成 + run.sh 封装
2. **启动**: `./run.sh bridge start` → `./run.sh search -f "关键词"`
3. **代理增强**: 如果 FlareSolverr 还不够，配 Bright Data 住宅代理（见 outgoing.proxies）
