#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
echo "远程模式只监听服务器 127.0.0.1。"
echo "VS Code Remote 会自动转发；普通 SSH 请在本机建立对应的 -L 端口转发。"
if [[ $# -gt 0 ]]; then
  exec python3 start.py --no-browser --port "$1"
fi
exec python3 start.py --no-browser
