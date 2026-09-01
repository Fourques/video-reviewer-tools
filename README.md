# 视频人工审核工具

一个本地运行、浏览器操作的视频人工审核工具，支持两种相互独立的工作流：

- 固定 8 秒片段审核：为每个长视频选择一段或多段 8 秒画面，或标记整段无跌倒。
- 整段 Fall 快速分类：浏览完整视频，把人工判定为 Fall 的原视频导出。

Windows、macOS、Linux 和远程 Linux 服务器使用同一套响应式网页界面。视频不会上传到互联网。两个模式都只读取项目目录第一层的视频，不扫描子目录。

## 推荐：下载免安装版

GitHub Releases 提供四个自包含版本：

- `VideoReviewer-Windows-x64.zip`
- `VideoReviewer-macOS-AppleSilicon.zip`
- `VideoReviewer-macOS-Intel.zip`
- `VideoReviewer-Linux-x64.tar.gz`

这些版本已经内置 Python、FFmpeg 和全部网页资源，不需要安装 Python、FFmpeg，也不需要填写配置文件。解压后双击 `VideoReviewer.exe`、`VideoReviewer.app` 或 `VideoReviewer` 即可。

应用会打开默认浏览器显示项目中心。停止时点击页面中的“退出工具”，不要只关闭浏览器标签页。

未购买代码签名证书的构建属于未签名软件：Windows 首次运行可能显示 SmartScreen 提醒；macOS 首次运行可能需要按住 Control 点击应用并选择“打开”。后续可直接双击。完全消除这些系统提醒需要 Windows 代码签名证书和 Apple Developer ID 公证凭据。

## 源码版启动

只有开发或远程服务器不适合使用免安装版时才需要源码版。源码版要求 Python 3.10+ 和 FFmpeg；不再需要单独安装 FFprobe。

Windows 双击 `start_windows.bat`，或在 PowerShell 中运行：

```powershell
py -3 start.py
```

macOS / Linux：

```bash
./start_mac_linux.sh
```

也可以统一使用：

```bash
python3 start.py
```

启动后浏览器会打开“视频审核项目中心”：

1. 从最近项目中选择、直接粘贴路径，或使用服务器目录浏览器。
2. 选择固定 8 秒审核或整段 Fall 快速分类。
3. 使用自动生成的输出目录，或自行修改。
4. 点击“开始审核”。页面会等待视频扫描完成并自动进入审核界面。

端口 8765 被占用时，工具会自动尝试后续空闲端口。源码版可按 `Ctrl+C` 停止；免安装版可点击“退出工具”。审核进度会实时写入磁盘。

## 输出与进度

默认目录：

```text
项目目录/
├── output/              # 8 秒片段、clips.csv 和片段审核进度
├── no_fall_output/      # 整段无跌倒原视频和 no_fall.csv
└── fall_output/         # 整段 Fall 原视频、fall_export.csv 和快速分类进度
```

- 单段片段保持原文件名；同一视频多段时添加 `_0001`、`_0002`。
- 最终 8 秒片段使用 FFmpeg `-c copy`，不重新编码、不降低画质。
- 整段 Fall/无跌倒视频按原始字节复制，不转码。
- 工具不会覆盖内容不同的同名文件。
- 片段审核进度：`output/.clip_reviewer_state.json`。
- 快速分类进度：`fall_output/.fall_label_state.json`。

进度文件以点开头，在 macOS/Linux 中默认隐藏。每次提交标签都会先写临时文件再原子替换状态文件，因此关闭进程后重新选择相同项目和输出目录即可恢复。导出文件仍需点击页面中的导出按钮。

## 快捷键

固定 8 秒模式：

- `S`：播放/暂停。
- `←` / `→`：前后 1 秒，并把到达画面作为片段起点；按住 `Shift` 为 0.1 秒。
- `A` / `D`：整个 8 秒窗口前后移动 5 秒。
- `W`：循环预览当前 8 秒。
- `Space`：提交当前片段。
- `Enter`：完成并进入下一个；没有片段时标记整段无跌倒。

整段 Fall 分类：

- `S`：播放/暂停。
- `←` / `→`：前后 1 秒；`A` / `D`：前后 5 秒。
- `W`：循环播放。
- `Space`：标记 Fall 并进入下一个。
- `Enter`：标记非 Fall 并进入下一个。
- `Backspace`：撤销当前标签。

## 远程服务器

### VS Code Remote SSH

Linux 免安装版可直接在远程终端运行 `./VideoReviewer`。使用源码版时，在 VS Code 中打开本项目目录，然后运行：

```bash
./start_remote.sh
```

项目包含 `.vscode/settings.json`，会自动识别并私密转发工具端口，且每次启动最多自动打开一个网页。如果没有自动打开，在 VS Code 的“端口/Ports”面板中点击对应地址即可。

### 普通 SSH

服务器端指定端口启动：

```bash
./start_remote.sh 18765
```

本机另开终端建立隧道：

```bash
ssh -N -L 18765:127.0.0.1:18765 用户名@服务器地址
```

然后打开 `http://127.0.0.1:18765`。如果本机 18765 已占用，可只更换 `-L` 左侧的本机端口，例如 `28765:127.0.0.1:18765`，再访问 `http://127.0.0.1:28765`。

网页中的目录浏览器浏览的是运行脚本的远程服务器目录，因此可以直接选择 `/mnt/...` 路径，不需要服务器图形桌面。

### 可信局域网共享

也可以运行：

```bash
python3 start.py --host 0.0.0.0
```

同事通过 `http://服务器IP:端口` 访问。此模式没有登录验证，并允许访问运行账号可读取的目录，只应在可信且受控的局域网中使用。多人同时修改同一个项目状态可能相互覆盖，建议每人使用独立项目或独立输出目录。

## 高级用法

跳过项目中心，直接启动固定 8 秒审核：

```bash
python3 start.py --source "/视频目录" --mode clip \
  --output "/片段输出" --no-fall-output "/无跌倒输出"
```

直接启动整段 Fall 分类：

```bash
python3 start.py --source "/视频目录" --mode label --fall-output "/Fall输出"
```

完整参数：

```bash
python3 start.py --help
```
