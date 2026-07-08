#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# 私有代理端点/上游/服务器等敏感值从 gitignored 的 .env 读取, 公开仓不含真实值。
# .env 里用单引号包住带 &/? 的 URL, 例: AGENT_SEARCH_711_GEN_URL='http://.../gen?a=1&b=2'
if [ -f "$ROOT/.env" ]; then
  set -a; . "$ROOT/.env" 2>/dev/null || true; set +a
fi

CONFIG="$ROOT/config/sing-box/agent-search-711.json"
PIDFILE="$ROOT/logs/agent-search-711-singbox.pid"
LOGFILE="$ROOT/logs/agent-search-711-singbox.log"
LABEL="com.agent-search.711-proxy"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LISTEN_PORT="${AGENT_SEARCH_711_LISTEN_PORT:-17110}"
UPSTREAM_PROXY="${AGENT_SEARCH_711_UPSTREAM_PROXY:-127.0.0.1:7890}"
# 轮换代理的 gen(生成)API 端点 —— 属私有基建, 不硬编码进公开仓; 在 .env 设 AGENT_SEARCH_711_GEN_URL。
GEN_URL="${AGENT_SEARCH_711_GEN_URL:-}"
SING_BOX="${SING_BOX_BIN:-sing-box}"
PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  PY="python3"
fi

mkdir -p "$ROOT/logs" "$(dirname "$CONFIG")"

is_running() {
  [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" >/dev/null 2>&1
  return $?
}

process_running() {
  pgrep -f "sing-box run -c $CONFIG" >/dev/null 2>&1
}

write_launch_agent() {
  bin_path="$SING_BOX"
  if [[ "$bin_path" != /* ]]; then
    bin_path="$(command -v "$bin_path")"
  fi
  "$PY" - "$PLIST" "$LABEL" "$bin_path" "$CONFIG" "$LOGFILE" <<'PY'
import plistlib
import sys
from pathlib import Path

plist, label, bin_path, config, logfile = sys.argv[1:6]
data = {
    "Label": label,
    "ProgramArguments": [bin_path, "run", "-c", config],
    "RunAtLoad": True,
    "KeepAlive": True,
    "StandardOutPath": logfile,
    "StandardErrorPath": logfile,
    "WorkingDirectory": str(Path(config).parent.parent.parent),
}
Path(plist).parent.mkdir(parents=True, exist_ok=True)
with open(plist, "wb") as f:
    plistlib.dump(data, f)
PY
}

refresh() {
  if [ -z "$GEN_URL" ]; then
    echo "[!] 未配置 AGENT_SEARCH_711_GEN_URL(轮换代理 gen 端点)。请在 .env 里设置后重试。" >&2
    echo "    例: AGENT_SEARCH_711_GEN_URL='http://<你的代理>/gen?...'" >&2
    exit 1
  fi
  "$PY" - "$GEN_URL" "$CONFIG" "$LISTEN_PORT" "$UPSTREAM_PROXY" <<'PY'
import json
import os
import sys
import tempfile
import urllib.request

gen_url, config_path, listen_port, upstream = sys.argv[1:5]
up_host, up_port = upstream.rsplit(":", 1)

raw = urllib.request.urlopen(gen_url, timeout=30).read().decode()
try:
    payload = json.loads(raw)
    data = payload.get("data") or payload.get("list") or payload
    if isinstance(data, dict):
        data = [data]
    item = data[0]
    server = item.get("ip") or item.get("server") or item.get("host")
    port = item.get("port") or item.get("server_port")
    username = item.get("username") or item.get("user")
    password = item.get("password") or item.get("pass")
except Exception:
    line = raw.strip().splitlines()[0].strip()
    if "://" in line:
        from urllib.parse import urlparse
        parsed = urlparse(line)
        server, port = parsed.hostname, parsed.port
        username, password = parsed.username, parsed.password
    else:
        server, port = line.rsplit(":", 1)
        username = password = None

if not server or not port:
    raise SystemExit("711 proxy response missing server/port")

out_711 = {
    "type": "http",
    "tag": "711-http",
    "server": str(server),
    "server_port": int(port),
    "detour": "bwh-http",
}
if username:
    out_711["username"] = str(username)
if password:
    out_711["password"] = str(password)

config = {
    "log": {"level": "info", "timestamp": True},
    "inbounds": [{
        "type": "mixed",
        "tag": "agent-search-in",
        "listen": "127.0.0.1",
        "listen_port": int(listen_port),
        "tcp_keep_alive": "30s",
    }],
    "outbounds": [
        out_711,
        {
            "type": "http",
            "tag": "bwh-http",
            "server": up_host,
            "server_port": int(up_port),
        },
        {"type": "direct", "tag": "direct"},
    ],
    "route": {"final": "711-http"},
}

fd, tmp = tempfile.mkstemp(prefix=".agent-search-711.", suffix=".json", dir=os.path.dirname(config_path))
with os.fdopen(fd, "w") as f:
    json.dump(config, f, ensure_ascii=False, indent=2)
    f.write("\n")
os.replace(tmp, config_path)
print(f"{server}:{port}")
PY
}

start() {
  if process_running; then
    echo "[✓] Agent Search 7-11 proxy already running on 127.0.0.1:$LISTEN_PORT"
    return
  fi
  proxy="$(refresh)"
  "$SING_BOX" check -c "$CONFIG" >/dev/null
  if [[ "$(uname -s)" == "Darwin" ]] && command -v launchctl >/dev/null 2>&1; then
    write_launch_agent
    launchctl bootout "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    launchctl kickstart -k "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
  else
    nohup "$SING_BOX" run -c "$CONFIG" >"$LOGFILE" 2>&1 &
    echo $! > "$PIDFILE"
  fi
  sleep 1
  if ! process_running; then
    echo "[!] failed to start; log: $LOGFILE" >&2
    exit 1
  fi
  echo "[✓] Agent Search 7-11 proxy started on 127.0.0.1:$LISTEN_PORT via $proxy"
}

stop() {
  if [[ "$(uname -s)" == "Darwin" ]] && command -v launchctl >/dev/null 2>&1; then
    launchctl bootout "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || true
  fi
  if process_running; then
    pkill -f "sing-box run -c $CONFIG" || true
  fi
  rm -f "$PIDFILE"
  echo "[✓] Agent Search 7-11 proxy stopped"
}

status() {
  if process_running; then
    echo "[✓] running on 127.0.0.1:$LISTEN_PORT"
  else
    echo "[✗] not running"
  fi
  if [[ -f "$PLIST" ]]; then
    echo "[*] launch agent: $PLIST"
  fi
  [ -f "$CONFIG" ] && "$SING_BOX" check -c "$CONFIG" >/dev/null && echo "[✓] config ok: $CONFIG"
}

case "${1:-status}" in
  refresh) refresh ;;
  start) start ;;
  stop) stop ;;
  restart) stop; start ;;
  status) status ;;
  logs) tail -f "$LOGFILE" ;;
  *)
    echo "用法: $0 [start|stop|restart|refresh|status|logs]"
    exit 2
    ;;
esac
