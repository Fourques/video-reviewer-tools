# 维护约定

- 本机主目录固定为 `/home/duanqw/Kami/video_clip_reviewer`。
- NAS 同步目录固定为 `/mnt/27_1-B2B-data/2026/duanqingwu/qwen3vl_vllm/datasets/video_clip_reviewer`。
- 每次完成程序更新后，在本机主目录运行 `./sync_to_nas.sh` 并确认校验成功；新增文件先加入 Git 跟踪。NAS 不可用时明确报告未同步。
- 不创建带日期或版本号的程序副本；安装包使用 GitHub Releases。
- 同步脚本只更新 Git 跟踪的文件。若删除或改名程序文件，核实后同时清理 NAS 中对应的旧程序文件。
- 保留所有审核视频、输出、隐藏进度和用户设置；不要用全目录删除式同步覆盖这些数据。
