# vidslice · 视频分割小工具

[中文](README.md) | **English**

> Cut segments out of a video, or merge clips back into one file. A Windows desktop app that is **lightweight, portable, and fully offline**.

<p align="center">  
  <img src="docs/screenshot-main.png" width="700" alt="Main window">  
</p>

<p align="center">  
  <img alt="platform" src="https://img.shields.io/badge/platform-Windows%2010%20%2F%2011%20x64-0078D6">  
  <img alt="size" src="https://img.shields.io/badge/download-15%20MB-success">  
  <img alt="deps" src="https://img.shields.io/badge/dependencies-none-brightgreen">  
  <img alt="offline" src="https://img.shields.io/badge/network-not%20required-informational">
  <img alt="license" src="https://img.shields.io/badge/license-MIT-blue">
</p>

Pick a video → add time ranges (or split at a fixed interval) → choose output settings → cut. Everything happens locally: nothing is uploaded, no network access, no runtime to install.

**Why is it so small?** The whole package is **15 MB**; the single-file exe is **17 MB**. It bundles a trimmed-down Python runtime plus a **self-compiled minimal ffmpeg** (only the components this tool actually uses, instead of the official 293 MB build). The naive packaging of the same feature set came to about 480 MB.

## Features

- **Lossless cutting** — copies the bitstream instead of re-encoding: extremely fast, zero quality loss.
- **Smart cut** — when the start time is not on a keyframe, only the first GOP is re-encoded. You get a **frame-accurate start with no stutter at the beginning**, which plain `-c copy` cannot do.
- **Precise re-encode** — change resolution, frame rate and quality (CRF) at the same time.
- **Merge into one video** — lossless concatenation, with an automatic duration check and an automatic fallback to re-encoding if the check fails. It never silently ships a broken file.
- **Split at a fixed interval** — cut a long episode into equal parts in seconds, optionally within a given range.
- Editable clip list, multi-select delete, double-click to preview.
- Window fits its content; a scrollbar appears automatically if the screen (or DPI scaling) is too small.

<p align="center">  
  <img src="docs/screenshot-done.png" width="700" alt="Log after a cut">  
</p>

The log line *"start is not on a keyframe, re-encoding the first 1.00s, copying the rest (smart cut)"* is smart cut at work.

## Download

Grab one of these from the [Releases](../../releases) page:

| File                        | Size  | Notes                                                    |
| --------------------------- | ----- | -------------------------------------------------------- |
| `vidslice-v1.0.0-win64.exe` | 17 MB | **Single file.** Double-click and run, nothing to unpack |
| `vidslice-v1.0.0-win64.zip` | 15 MB | Unpack and run. Starts about 1 second faster             |

Both have identical features. The single-file build unpacks its bundled components into the system temp folder on every launch, so the first start takes 2–5 seconds and later ones about 1.3 seconds.

## Usage

1. Choose the source video — duration, resolution, frame rate and codecs are read automatically.
2. **Add clip** to enter start/end times by hand (`90`, `1:30`, `00:01:30.500` are all accepted), or use **split at a fixed interval** to generate them in bulk.
3. Pick a cut mode and output settings, then press **Start**.

For troubleshooting, launch with `调试运行.bat` from the package — it keeps a console window open so errors are visible.

## How smart cut works

`ffmpeg -ss <start> -i in -t <dur> -c copy` only starts accurately if `<start>` lands on a keyframe. Otherwise ffmpeg writes the pre-roll from the previous keyframe into the file and hides it with an mp4 edit list. The clip then starts mid-GOP: its first frame is not a real keyframe, so players visibly stutter at the beginning, and the hidden data leaks when the clip is later concatenated.

Smart cut avoids that by splitting each segment in two:

- `[start, next keyframe)` — **re-encoded**, so the clip begins with a self-contained keyframe at exactly the requested time;
- `[next keyframe, end)` — **stream-copied**, untouched;
- then the two parts are concatenated.

Only one GOP per segment is re-encoded, so it stays fast and the quality loss is negligible. If the source codec or pixel format cannot be matched (for example VP9 or 10-bit), the tool falls back to re-encoding the whole segment and says so in the log.

There is also an alternative **align to previous keyframe** mode that does no re-encoding at all — the clip then starts at most one keyframe interval early.

## Size breakdown

| Part              | Unpacked    | Notes                                                                   |
| ----------------- | ----------- | ----------------------------------------------------------------------- |
| `tools/python/`   | 24.9 MB     | Trimmed: no pip/setuptools, no SSL, no SQLite, no Tix, no `__pycache__` |
| `tools/ffmpeg/`   | 21.5 MB     | `ffmpeg.exe` 15.1 MB, `ffprobe.exe` 6.2 MB, two runtime DLLs            |
| `app/`, `assets/` | 0.3 MB      | The actual code and icon                                                |
| **Total**         | **44.6 MB** | **15 MB** as a zip                                                      |

`ffprobe` is only 6.2 MB because it needs no encoders at all — it is built separately without libx264/libx265.

## Repository layout

```
app/
  main.py            Tkinter UI
  splitter.py        ffmpeg command building, cut/merge jobs
  ffmpeg_tools.py    locating ffmpeg, probing media, keyframe scan
  segments.py        time parsing, formatting, equal split
  resources.py       locating bundled assets (frozen-exe aware)
assets/              icon.svg + multi-size icon.ico
docs/                screenshots used by the README
tools/build/         reproducible build recipes (see below)
tests/               test suite
licenses/            GPL-2.0 text for the bundled ffmpeg
```

`tools/python/` and `tools/ffmpeg/` are **not** in the repository — they are build artifacts, shipped as Release assets instead. See `.gitignore`.

## Tests

```bat
tools\python\python.exe tests\smoke_test.py   :: core logic, real cutting and merging
tools\python\python.exe tests\gui_test.py     :: UI construction, interaction, full merge flow
```

`smoke_test.py` has 142 checks. It does not just compare durations — it reads the **actual pixels** of the output:

- A "grayscale ramp" clip is generated (second *i* has gray level 12×*i*), so reading the first frame's gray level tells you exactly which second the cut landed on. Duration alone is not enough: a wrong cut can still have the right length.
- Coverage: time parsing, resolution scaling, equal split, keyframe detection, both lossless cut modes, smart cut (first keyframe at 0, no hidden frames, fallback when there is no later keyframe, verification failure, cancellation, temp cleanup, HEVC sources), lossless/re-encode accuracy, lossless merge with per-point pixel checks, merge with audio, re-encoded merge, merge self-check and fallback, non-ASCII paths, background job progress and cleanup.

`gui_test.py` has 70 checks, including one full merge driven through the real UI, plus layout assertions (everything visible at the default size, scrollbar appears and the bottom is reachable when the content overflows, log area grows with the window, icon resource found).

## Building

### Python runtime

`https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe`

Install with `tcl/tk` and `pip`, target `tools\python`, then trim (about 25 MB saved):

```
Lib\site-packages  Lib\idlelib  Lib\lib2to3  Lib\turtledemo  Lib\venv  Lib\ensurepip
Lib\asyncio  Lib\email  Lib\xml  Lib\pydoc_data  Lib\http  Lib\unittest  Lib\logging
Lib\sqlite3  Lib\multiprocessing  Lib\wsgiref  Lib\importlib\resources
DLLs\libcrypto-3.dll  DLLs\libssl-3.dll  DLLs\_ssl.pyd  DLLs\sqlite3.dll  DLLs\_sqlite3.pyd
DLLs\unicodedata.pyd  DLLs\_decimal.pyd  DLLs\pyexpat.pyd  DLLs\_elementtree.pyd
DLLs\_lzma.pyd  DLLs\_bz2.pyd  DLLs\_msi.pyd  DLLs\_wmi.pyd  DLLs\winsound.pyd
tcl\tix8.4.3  tcl\dde1.4  tcl\reg1.3  tcl\tcl8  tcl\nmake  tcl\tk8.6\demos  tcl\*\msgs
NEWS.txt
all __pycache__ directories
```

> Two traps: `pathlib` needs `urllib.parse`, so removing all of `urllib` breaks startup. Missing modules can be restored from the CPython source tree (tag `v3.12.10`, `Lib/`).

The **embeddable** Python package has no Tkinter and cannot run this app — use the full installer.

### ffmpeg

Do not use the official builds: three static exes add up to 293 MB. Two scripts in `tools/build/` produce about 21.5 MB in total:

- `build-ffmpeg-min.sh` → `ffmpeg.exe` (with libx264/libx265, GPL)
- `build-ffprobe-min.sh` → `ffprobe.exe` (no encoders, LGPL, ~6 MB)

In an MSYS2 MINGW64 shell:

```bash
pacman -S --needed mingw-w64-x86_64-gcc mingw-w64-x86_64-nasm \
                   mingw-w64-x86_64-pkg-config mingw-w64-x86_64-cmake make git diffutils

# x264: https://github.com/mirror/x264  (--enable-static --disable-cli)
# x265 4.1: https://bitbucket.org/multicoreware/x265_git/downloads/
#   cmake 4.x needs -DCMAKE_POLICY_VERSION_MINIMUM=3.5, and the two
#   cmake_policy(... OLD) lines in CMakeLists.txt must be commented out

bash tools/build/build-ffmpeg-min.sh
bash tools/build/build-ffprobe-min.sh
```

Both scripts keep a component whitelist. After changing it, always re-run `tests/smoke_test.py` — a missing decoder or filter only shows up at runtime.

Then copy `ffmpeg.exe`, `ffprobe.exe`, `libgcc_s_seh-1.dll` and `libwinpthread-1.dll` into `tools/ffmpeg`.

### Icon

```bash
C:\pybuild\python.exe tools\build\make-icon.py assets/icon.svg \
    --ico assets/icon.ico --preview build\icon-preview.png
```

Rendering goes through the system's Chromium-based browser (Edge) headless, because it is the only readily available way to get a PNG **with an alpha channel** — otherwise the transparent rounded corners come out as a white square in the taskbar.

The `.ico` is encoded by the script rather than by Pillow, because Pillow stores every size as a PNG entry, while Windows only accepts PNG for 256px — smaller sizes must be uncompressed BMP(DIB), otherwise Explorer may fall back to the generic application icon.

### Single-file exe

```powershell
C:\pybuild\python.exe -m pip install pyinstaller
powershell -ExecutionPolicy Bypass -File tools\build\build-exe.ps1
```

## Licensing

The application code is released under the [MIT License](LICENSE).

The bundled `ffmpeg.exe` is a separate matter: it is a **GPL v2 or later** build (it links libx264/libx265). Distributing it means you must also provide the corresponding source code or a valid written offer to do so. Details and links are in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).

The application invokes ffmpeg as a **separate process** (`subprocess`), which is mere aggregation — the MIT licence of this project is not affected by the GPL build of the bundled binary.

## Notes from building this

A few things that cost real time, kept here so they are not re-learned:

1. **Never add `-avoid_negative_ts make_zero` to a copy cut.** It exposes the hidden pre-roll: a 3-second request produced 7 seconds.
2. **A cut point may only be aligned *backwards*, never to the "nearest" keyframe.** Nearest alignment pushed a 4.6s request to 8.33s, and when the nearest keyframe sat past the end it produced a 2-frame clip.
3. **A mid-GOP start makes the clip stutter.** Fix it with smart cut, not by hiding it.
4. **Merge self-checks must compare presented durations.** Container `nb_frames` counts physical frames including hidden pre-roll and cannot detect the leak.
5. **Never call Tkinter's `after()` from a worker thread.** Use a queue polled from the main thread.
6. **Keep `.bat` and `.ps1` files pure ASCII.** cmd.exe and PowerShell read them in the OEM codepage; UTF-8 comments corrupt parsing.
7. **`winfo_ismapped()` is false while a window is not really shown**, and `tk scaling` has no effect after widgets are created — enlarge fonts directly to test high DPI.
8. **ICO small sizes must be BMP, not PNG** (see above).
