#!/usr/bin/env bash
# 固定主目录 -> 固定 NAS 目录；只同步 Git 跟踪文件，保留两边运行数据。
set -euo pipefail

source_dir=/home/duanqw/Kami/video_clip_reviewer
nas_mount=/mnt/27_1-B2B-data
target_dir="$nas_mount/2026/duanqingwu/qwen3vl_vllm/datasets/video_clip_reviewer"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

if [[ "$script_dir" != "$source_dir" ]]; then
    echo "请在本机主目录运行：$source_dir/sync_to_nas.sh" >&2
    exit 1
fi
if [[ $# -gt 1 || ( $# -eq 1 && "$1" != --dry-run ) ]]; then
    echo "用法：./sync_to_nas.sh [--dry-run]" >&2
    exit 1
fi
for dependency in git rsync mountpoint; do
    command -v "$dependency" >/dev/null || { echo "缺少命令：$dependency" >&2; exit 1; }
done
cd -- "$source_dir"
git rev-parse --is-inside-work-tree >/dev/null
# 先访问父目录以触发自动挂载；挂载缺失时不在本机建立假的 NAS 目录。
[[ -d "$(dirname -- "$target_dir")" ]] && mountpoint -q "$nas_mount" || {
    echo "NAS 尚未挂载或 datasets 目录不存在。" >&2
    exit 1
}
[[ ! -L "$target_dir" ]] || { echo "目标不能是符号链接。" >&2; exit 1; }
dry_run=()
if [[ $# -eq 1 ]]; then
    dry_run=(--dry-run)
else
    mkdir -p -- "$target_dir"
fi
git ls-files -z | rsync -rt --checksum --omit-dir-times --from0 --files-from=- \
    --itemize-changes "${dry_run[@]}" ./ "$target_dir/"
if [[ $# -eq 0 ]]; then
    changes="$(git ls-files -z | rsync -rcn --from0 --files-from=- --itemize-changes ./ "$target_dir/")"
    if [[ -n "$changes" ]]; then
        echo "同步后校验未通过：" >&2
        echo "$changes" >&2
        exit 1
    fi
    echo "同步完成，程序文件内容校验一致：$target_dir"
fi
