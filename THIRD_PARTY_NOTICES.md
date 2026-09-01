# 第三方软件说明

发布版内置以下第三方软件，用户不需要另行安装。

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
