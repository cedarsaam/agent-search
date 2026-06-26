#!/bin/bash
# Agent Search — 启动/停止/状态管理 / start-stop-status helper
# Usage: ./run.sh [start|stop|restart|status|logs|pull]

set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# 优先用项目 venv 的 python
if [ -x "$DIR/.venv/bin/python" ]; then
  PY="$DIR/.venv/bin/python"
else
  PY="python3"
fi

case "${1:-status}" in
  start)
    echo "[*] 启动 SearXNG..."
    docker compose up -d
    echo "[*] 等待就绪..."
    for i in $(seq 1 30); do
      if curl -sf "http://localhost:8888/search?q=test&format=json" > /dev/null 2>&1; then
        echo "[✓] SearXNG 就绪 → http://localhost:8888"
        echo "[*] 可用引擎:"
        curl -s "http://localhost:8888/search?q=test&format=json" | python3 -m json.tool 2>/dev/null | grep '"engine"' | sort -u
        exit 0
      fi
      sleep 1
    done
    echo "[!] SearXNG 启动超时，检查: docker compose logs"
    ;;
  stop)
    echo "[*] 停止 SearXNG..."
    docker compose down
    echo "[✓] 已停止"
    ;;
  restart)
    docker compose restart
    echo "[*] 已重启"
    ;;
  status)
    if curl -sf "http://localhost:8888/search?q=ping&format=json" > /dev/null 2>&1; then
      echo "[✓] SearXNG: 运行中 (http://localhost:8888)"
      echo ""
      echo "  SEARXNG_URL=http://localhost:8888"
      echo "  在 search.py 中自动使用"
    else
      echo "[✗] SearXNG: 未运行"
      echo "  执行 ./run.sh start 启动，或设置 SEARXNG_URL 指向其他实例"
    fi
    ;;
  logs)
    docker compose logs -f
    ;;
  pull)
    echo "[*] 拉取 SearXNG 镜像..."
    docker compose pull
    echo "[✓] 镜像已更新"
    ;;
  search)
    shift
    if [ "$1" = "-f" ] || [ "$1" = "--flaresolverr" ]; then
      "$PY" search.py "$@"
    else
      "$PY" search.py "$@"
    fi
    ;;
  ask)
    shift
    # 检查是否用了 -f
    for arg in "$@"; do
      if [ "$arg" = "-f" ] || [ "$arg" = "--flaresolverr" ]; then
        "$PY" search.py --answer "$@"
        exit $?
      fi
    done
    "$PY" search.py --answer "$@"
    ;;
  serve)
    # HTTP API (给自建 agent / function calling)。8000 常被本机 Qwen3-ASR 占用，默认用 8077
    PORT="${2:-8077}"
    echo "[*] 启动 HTTP API → http://localhost:$PORT  (文档 /docs, 工具 schema /openai-tools)"
    exec "$DIR/.venv/bin/uvicorn" server:app --host 0.0.0.0 --port "$PORT"
    ;;
  mcp)
    # MCP stdio server (给 Claude Code / Desktop，一般由客户端拉起，这里仅供手动调试)
    exec "$PY" mcp_server.py
    ;;
  bridge)
    case "$2" in
      start)
        echo "[*] 启动 FlareSolverr..."
        docker compose up -d flaresolverr
        echo "[*] 等待就绪..."
        for i in $(seq 1 60); do
          # /v1 只收 POST，GET 返回 405 → 只要不是 000(无响应)就算端口已就绪
          code=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:8191/v1" 2>/dev/null)
          if [ "$code" != "000" ]; then
            echo "[✓] FlareSolverr 就绪 → http://localhost:8191"
            exit 0
          fi
          sleep 1
        done
        echo "[!] FlareSolverr 启动超时"
        ;;
      stop)
        docker compose stop flaresolverr
        echo "[✓] FlareSolverr 已停止"
        ;;
      restart)
        docker compose restart flaresolverr
        echo "[*] FlareSolverr 已重启"
        ;;
      status)
        code=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:8191/v1" 2>/dev/null)
        if [ "$code" != "000" ]; then
          echo "[✓] FlareSolverr: 运行中 (:8191)"
        else
          echo "[✗] FlareSolverr: 未运行"
        fi
        ;;
      *)
        echo "用法: $0 bridge [start|stop|restart|status]"
        ;;
    esac
    ;;
  *)

    echo "Agent Search"
    echo "================================"
    echo "用法: $0 [start|stop|restart|status|logs|pull|search|ask|bridge]"
    echo ""
    echo "  search <词>         纯搜索               例: $0 search \"deepseek 价格\""
    echo "  search -f <词>      绕过 CAPTCHA 搜索    例: $0 search -f \"关键词\""
    echo "  ask <问题>          DeepSeek 问答        例: $0 ask \"deepseek 怎么计费\""
    echo "  ask -f <问题>       绕过 CAPTCHA + 问答  例: $0 ask -f \"关键词\""
    echo "  bridge start        启动 FlareSolverr    例: $0 bridge start"
    echo "  bridge stop         停止 FlareSolverr"
    echo "  bridge status       查看状态"
    echo ""
    ./run.sh status
    ;;
esac
