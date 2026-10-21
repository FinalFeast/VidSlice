"""定位 ffmpeg / ffprobe，并读取视频信息。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from resources import resource_dirs

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Windows 下避免子进程弹出黑窗口
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class FFmpegNotFound(RuntimeError):
    """找不到可用的 ffmpeg 可执行文件。"""


def search_dirs() -> list[Path]:
    """ffmpeg 可执行文件可能所在的目录，按优先级排列。

    冻结运行时 ffmpeg 被放在包的 ``ffmpeg/`` 下；项目里则在 ``tools/ffmpeg/``。
    """
    dirs: list[Path] = []
    for root in resource_dirs():
        dirs.append(root / "tools" / "ffmpeg" if root == PROJECT_ROOT else root / "ffmpeg")
    return dirs


@lru_cache(maxsize=None)
def find_tool(name: str) -> str:
    """优先使用随程序分发的版本，其次回退到系统 PATH。"""
    exe = f"{name}.exe" if os.name == "nt" else name
    for directory in search_dirs():
        local = directory / exe
        if local.is_file():
            return str(local)
    found = shutil.which(exe)
    if found:
        return found
    raise FFmpegNotFound(
        f"没有找到 {exe}。\n"
        f"请把它放到下列任一目录：\n"
        + "\n".join(f"  {d}" for d in search_dirs())
        + "\n或加入系统 PATH。"
    )


@dataclass
class VideoInfo:
    path: str
    duration: float
    width: int
    height: int
    fps: float
    video_codec: str
    audio_codec: str
    pix_fmt: str = ""

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}"

    @property
    def fps_text(self) -> str:
        if self.fps <= 0:
            return "未知"
        return f"{self.fps:.3f}".rstrip("0").rstrip(".")


def _parse_fps(value: str) -> float:
    if not value or value == "0/0":
        return 0.0
    if "/" in value:
        num, _, den = value.partition("/")
        try:
            den_f = float(den)
            return float(num) / den_f if den_f else 0.0
        except ValueError:
            return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def _to_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


@lru_cache(maxsize=4)
def probe_keyframes(path: str) -> tuple[float, ...]:
    """列出视频流全部关键帧的时间点（秒），结果按路径缓存。

    无损切割（-c copy）的切点必须落在关键帧上，所以需要这份清单来
    把用户填写的起点对齐过去。读取失败时返回空元组，调用方需自行兜底。
    """
    return scan_keyframes(path)


def scan_keyframes(path: str) -> tuple[float, ...]:
    """同 ``probe_keyframes``，但每次都真正去读文件（不走缓存）。

    用于校验刚生成的输出文件：同一路径可能被覆盖重写，缓存会读到旧结果。
    """
    ffprobe = find_tool("ffprobe")
    cmd = [
        ffprobe,
        "-v", "error",
        "-select_streams", "v:0",
        "-skip_frame", "nokey",
        "-show_entries", "frame=pts_time",
        "-of", "csv=p=0",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, creationflags=CREATE_NO_WINDOW)
    except OSError:
        return ()
    if proc.returncode != 0:
        return ()

    times: list[float] = []
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        line = line.strip().rstrip(",")
        if not line:
            continue
        try:
            times.append(float(line))
        except ValueError:
            continue
    times.sort()
    return tuple(times)


def probe_video(path: str) -> VideoInfo:
    """用 ffprobe 读取时长、分辨率、帧率和编码格式。"""
    ffprobe = find_tool("ffprobe")
    cmd = [
        ffprobe,
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, creationflags=CREATE_NO_WINDOW)
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"读取视频信息失败：{detail or '未知错误'}")

    try:
        data = json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"解析视频信息失败：{exc}") from None

    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise RuntimeError("这个文件里没有视频轨道。")

    duration = 0.0
    for source in (data.get("format", {}).get("duration"), video.get("duration")):
        duration = _to_float(source)
        if duration > 0:
            break

    fps = _parse_fps(video.get("avg_frame_rate") or video.get("r_frame_rate") or "")

    return VideoInfo(
        path=str(path),
        duration=duration,
        width=int(_to_float(video.get("width"))),
        height=int(_to_float(video.get("height"))),
        fps=fps,
        video_codec=str(video.get("codec_name") or "未知"),
        audio_codec=str(audio.get("codec_name")) if audio else "无",
        pix_fmt=str(video.get("pix_fmt") or ""),
    )
