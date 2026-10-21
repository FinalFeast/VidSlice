# 第三方组件与许可

本仓库的代码（`app/`、`tests/`、`tools/build/`、`assets/`、`docs/`）采用
[MIT 许可](LICENSE)，可自由使用、修改、商用，保留版权声明即可。

下面这些是**随发行包一起分发**的第三方组件，各自适用不同的许可。请留意尤其是 ffmpeg 的 GPL 义务。

## ffmpeg（GPL v2 或更高版本）

`tools/ffmpeg/ffmpeg.exe` 与 `tools/ffmpeg/ffprobe.exe` 不是官方构建，而是本仓库用
[tools/build/build-ffmpeg-min.sh](tools/build/build-ffmpeg-min.sh) 与
[tools/build/build-ffprobe-min.sh](tools/build/build-ffprobe-min.sh)
从 FFmpeg `n8.1.2` 源码自行编译的精简版本。

- `ffmpeg.exe` 启用了 `--enable-gpl --enable-libx264 --enable-libx265`，因此受
  **GPL v2 或更高版本**约束（许可全文见 [licenses/GPL-2.0.txt](licenses/GPL-2.0.txt)）。
- `ffprobe.exe` 不含任何编码器，也未链接 x264/x265，按 LGPL v2.1 或更高版本发布。
- 上游源码：<https://ffmpeg.org/download.html>

**发布时必须履行的 GPL 义务**：分发 ffmpeg.exe 时需要同时提供其完整对应的源代码（含所使用的
x264 / x265 源码），或给出有效的书面获取承诺。本仓库已经提供了完整的编译配方与所用版本号，
但严格来说这不等同于提供源码。发布 Release 时建议至少做到其一：

1. 在 Release 说明里附上 FFmpeg n8.1.2、x264、x265 的源码下载链接与获取方式；
2. 或把源码包作为 Release 的附加文件一并提供。

## x264 / x265（GPL v2 或更高版本）

静态链接进 `ffmpeg.exe`，用于 H.264 / H.265 编码。

- x264：<https://code.videolan.org/videolan/x264>（本仓库用的是 <https://github.com/mirror/x264> 镜像）
- x265 4.1：<https://bitbucket.org/multicoreware/x265_git/downloads/>

## Python 3.12.10（PSF License 2.0）

`tools/python/` 是 Python 官方 Windows 安装版的裁剪副本，许可见
<https://docs.python.org/3/license.html>。

## Tcl/Tk 8.6（BSD 风格许可）

随 Python 一同分发，位于 `tools/python/tcl/`，许可见
<https://www.tcl.tk/software/tcltk/license.html>。

## MinGW-w64 运行时

`tools/ffmpeg/libgcc_s_seh-1.dll` 与 `libwinpthread-1.dll` 来自 MinGW-w64（GCC 运行时，
GPL 附带的运行时例外条款），随 ffmpeg 的分发一并提供。
