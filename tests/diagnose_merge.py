"""诊断：两种无损切点模式下，「切割 + 合并」的画面准确性、帧数与体积对照。

画面用灰度递增的素材（第 i 秒灰度 = 12*i），读帧灰度即可反推画面落在第几秒。
只比对时长是不够的——切错位置时长也可能对得上。

用法：tools\\python\\python.exe tests\\diagnose_merge.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from ffmpeg_tools import CREATE_NO_WINDOW, find_tool, probe_keyframes  # noqa: E402
from segments import Segment  # noqa: E402
from splitter import OutputSettings, build_command, build_concat_command, previous_keyframe  # noqa: E402

FFMPEG = find_tool("ffmpeg")
FFPROBE = find_tool("ffprobe")
STEP = 12.0
SEGMENTS = [(2.0, 4.0), (10.0, 12.0)]


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, creationflags=CREATE_NO_WINDOW)


def make_ramp(path: Path, gop: int, seconds: int = 20) -> None:
    vf = f"geq=r='{STEP}*floor(T)':g='{STEP}*floor(T)':b='{STEP}*floor(T)'"
    run([FFMPEG, "-hide_banner", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"nullsrc=s=8x8:r=30:d={seconds}",
         "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
         "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0", str(path)])


def gray_at(path: Path, t: float) -> int:
    proc = run([FFMPEG, "-v", "error", "-ss", f"{t:.3f}", "-i", str(path),
                "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"])
    return proc.stdout[0] if proc.stdout else -1


def duration(path: Path) -> float:
    proc = run([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)])
    try:
        return float(proc.stdout.decode().strip())
    except ValueError:
        return -1.0


def frames(path: Path) -> int:
    proc = run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-count_frames",
                "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)])
    try:
        return int(proc.stdout.decode().strip())
    except ValueError:
        return -1


def trial(src: Path, work: Path, mode: str, gop: int) -> None:
    aligned = mode == "对齐关键帧"
    keys = probe_keyframes(str(src)) if aligned else ()
    settings = OutputSettings(mode="copy", container="mp4", out_dir=str(work), prefix=mode[:2],
                              align_keyframes=aligned)
    tag = "align" if aligned else "strict"
    parts, spans, cursor, bad = [], [], 0.0, 0

    print(f"  【{mode}】")
    for i, (s, e) in enumerate(SEGMENTS, 1):
        start = previous_keyframe(s, keys) if aligned else s
        p = work / f"{tag}_{gop}_{i}.mp4"
        run(build_command(str(src), Segment(start, e), settings, str(p)))
        d = duration(p)
        # 片段自身开头应当落在实际使用的起点上
        got, want = gray_at(p, 0.0), int(STEP * start)
        ok = abs(got - want) <= 6
        bad += 0 if ok else 1
        print(f"    片段{i}: 填写 {s}-{e} → 实际从 {start:.2f}s 起  "
              f"时长 {d:.3f}s 帧数 {frames(p)}（应 {int((e - start) * 30)}）"
              f" 体积 {p.stat().st_size / 1024:.0f}KB  开头画面 {got}(应 {want}) {'OK' if ok else '!!不对'}")
        spans.append((cursor, d, start))
        cursor += d
        parts.append(p)

    merged = work / f"{tag}_{gop}_merged.mp4"
    result = run(build_concat_command([str(p) for p in parts], str(merged), settings, copy=True))
    if not merged.is_file() or merged.stat().st_size == 0:
        print(f"    合并失败: {result.stderr.decode('utf-8', 'replace').strip()[-150:]}\n")
        return
    print(f"    合并: 时长 {duration(merged):.3f}s（各段之和 {cursor:.3f}s）"
          f" 帧数 {frames(merged)} 体积 {merged.stat().st_size / 1024:.0f}KB")

    for i, (offset, d, s) in enumerate(spans, 1):
        # 校验点按实测时长推导，末尾留出 -c copy 多出的 1~3 帧
        for local, note in ((0.15, "开头"), (d / 2, "中间"), (max(0.0, d - 0.25), "结尾")):
            got, want = gray_at(merged, offset + local), int(STEP * int(s + local))
            ok = abs(got - want) <= 6
            bad += 0 if ok else 1
            print(f"    片段{i}{note}: t={offset + local:5.2f}s 画面 {got:3d} 应 {want:3d}"
                  f"（源 {s + local:.2f}s）{'OK' if ok else '!!不对'}")
    print(f"    → {'画面全部正确' if bad == 0 else f'{bad} 处画面不对'}\n")


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="vs-merge-diag-"))
    print(f"工作目录 {work}\n")
    for gop, note in ((30, "关键帧每秒一个"), (250, "关键帧稀疏 ≈8.3s")):
        src = work / f"ramp{gop}.mp4"
        make_ramp(src, gop)
        print(f"源 gop={gop}（{note}）关键帧 {[round(k, 2) for k in probe_keyframes(str(src))]}")
        for mode in ("严格按填写时间", "对齐关键帧"):
            trial(src, work, mode, gop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
