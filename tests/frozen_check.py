"""打包自检：在"已打包成 exe"的环境里验证内置 ffmpeg 能被找到并跑通切割与合并。

只用于验证打包结果，不参与应用的正常运行。
用法（打包后）：dist\\frozen-check.exe
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from ffmpeg_tools import CREATE_NO_WINDOW, find_tool, probe_video, search_dirs  # noqa: E402
from segments import Segment  # noqa: E402
from splitter import OutputSettings, SplitJob, build_tasks  # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  -- {detail}" if detail else ""))
    if not ok:
        failures.append(label)


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, creationflags=CREATE_NO_WINDOW)


def main() -> int:
    print(f"frozen       : {getattr(sys, 'frozen', False)}")
    print(f"_MEIPASS     : {getattr(sys, '_MEIPASS', '(无)')}")
    print(f"executable   : {sys.executable}")
    print(f"搜索目录     : {[str(d) for d in search_dirs()]}")
    print()

    try:
        ffmpeg = find_tool("ffmpeg")
        ffprobe = find_tool("ffprobe")
    except Exception as exc:
        print(f"找不到 ffmpeg：{exc}")
        return 1
    print(f"ffmpeg  : {ffmpeg}")
    print(f"ffprobe : {ffprobe}")
    check("ffmpeg 与 ffprobe 都能定位到", True)
    check("定位到的是随包分发的副本（不是系统 PATH）", "ffmpeg" in ffmpeg.lower())

    version = run([ffmpeg, "-version"]).stdout.decode("utf-8", "replace").splitlines()
    check("ffmpeg 能执行", bool(version), version[0] if version else "")
    print(f"版本: {version[0] if version else '?'}")
    print()

    work = Path(tempfile.mkdtemp(prefix="frozen-check-"))
    sample = work / "sample.mp4"
    run([ffmpeg, "-hide_banner", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=12",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=12",
         "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-g", "30",
         "-c:a", "aac", "-shortest", str(sample)])
    check("能用内置 ffmpeg 生成测试视频", sample.is_file(), str(sample))

    if not sample.is_file():
        print("生成失败，后续检查跳过")
        return 1

    info = probe_video(str(sample))
    check("ffprobe 能读取信息", info.duration > 0, f"{info.duration:.2f}s {info.resolution} {info.fps_text}fps")

    out_dir = work / "out"
    settings = OutputSettings(mode="copy", container="mp4", out_dir=str(out_dir), prefix="fz")
    logs: list[str] = []
    result: list[tuple[bool, str]] = []
    done = threading.Event()
    job = SplitJob(
        src=str(sample), tasks=build_tasks(str(sample), [Segment(1.0, 4.0), Segment(6.0, 9.0)], settings),
        settings=settings, on_log=logs.append, on_progress=lambda _r: None,
        on_finished=lambda ok, msg: (result.append((ok, msg)), done.set()),
    )
    job.start()
    done.wait(timeout=180)
    check("切割任务正常结束", bool(result) and result[0][0], str(result))
    produced = sorted(out_dir.glob("*.mp4"))
    check("产出 2 个片段文件", len(produced) == 2, str([p.name for p in produced]))
    if produced:
        check("片段时长合理", abs(probe_video(str(produced[0])).duration - 3.0) <= 0.3,
              f"{probe_video(str(produced[0])).duration:.3f}s")

    print(f"\n工作目录: {work}")
    if failures:
        print(f"\n失败 {len(failures)} 项：{failures}")
        return 1
    print("\n打包自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
