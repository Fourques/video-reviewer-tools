#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
if ! command -v python3 >/dev/null 2>&1; then
  echo "错误：找不到 Python 3，请先安装 Python 3.10 或更高版本。"
  exit 2
fi
if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
  echo "错误：找不到 FFmpeg/FFprobe，请先安装 FFmpeg 并加入 PATH。"
  exit 2
fi
exec python3 start.py "$@"
