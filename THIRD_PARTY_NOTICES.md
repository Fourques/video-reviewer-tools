# 第三方软件说明

发布版内置以下第三方软件，用户不需要另行安装。

## pywebview 6.2.1 与平台窗口组件

- pywebview：https://github.com/r0x0r/pywebview/tree/6.2.1（BSD 3-Clause）
- Windows 使用系统 WebView2 / .NET，macOS 使用系统 WebKit；平台依赖的许可证随包放在 `licenses/`。
- Linux 使用 QtPy 2.4.3（MIT）、PySide6 / Shiboken6 6.11.0 和 Qt 6.11（相关运行模块采用 LGPLv3）。
- Qt 许可证：https://doc.qt.io/qt-6/licensing.html
- 对应 PySide6 / Shiboken6 源码：https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.11.0-src/
- 对应 Qt 源码：https://download.qt.io/official_releases/qt/6.11/6.11.0/single/

Linux 包采用可替换的动态库目录（不是静态链接或单文件嵌入 Qt），允许依照 LGPL 替换、修改库及为调试这些修改进行反向工程。原始许可证与第三方声明一并随发布包保留；Qt/PySide6 未作源码修改。构建方式与依赖版本见仓库 `VideoReviewer.spec` / `requirements-build.txt`。本项目没有将自身源码改成 Qt 的许可证。

## imageio-ffmpeg 0.6.0

- 项目：https://github.com/imageio/imageio-ffmpeg
- 许可证：BSD 2-Clause
- 源代码：https://github.com/imageio/imageio-ffmpeg/tree/v0.6.0

## FFmpeg

- 项目：https://ffmpeg.org/
- 源代码：https://github.com/FFmpeg/FFmpeg
- 许可证说明：https://ffmpeg.org/legal.html

FFmpeg 的具体许可证取决于发布二进制启用的组件。随 `imageio-ffmpeg`
平台轮子提供的 FFmpeg 是独立可执行程序，本工具通过子进程调用它。
运行 `VideoReviewer` 所附 FFmpeg 的 `-version` 参数可查看准确构建配置。
发布目录中的 `licenses/FFmpeg-COPYING.GPLv3` 和
`licenses/imageio-ffmpeg-LICENSE.txt` 保留了许可证全文。
