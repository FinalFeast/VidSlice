"""诊断五（决定性）：候选命令的内容准确性、帧数、体积、可解码性。

画面用灰度递增（第 i 秒灰度 = 12*i），读首帧灰度即可判断切点落在哪一秒。
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from ffmpeg_tools import CREATE_NO_WINDOW, find_tool, probe_keyframes  # noqa: E402

FFMPEG = find_tool("ffmpeg")
FFPROBE = find_tool("ffprobe")
STEP = 12.0


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, creationflags=CREATE_NO_WINDOW)


def make_ramp(path: Path, gop: int, seconds: int = 20) -> None:
    vf = f"geq=r='{STEP}*floor(T)':g='{STEP}*floor(T)':b='{STEP}*floor(T)'"
    run([FFMPEG, "-hide_banner", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"nullsrc=s=8x8:r=30:d={seconds}",
         "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
         "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0", str(path)])


def first_gray(path: Path) -> int:
    proc = run([FFMPEG, "-v", "error", "-i", str(path), "-frames:v", "1",
                "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"])
    return proc.stdout[0] if proc.stdout else -1


def count_frames(path: Path) -> int:
    proc = run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-count_frames",
                "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)])
    try:
        return int(proc.stdout.decode().strip())
    except ValueError:
        return -1


def fmt_duration(path: Path) -> float:
    proc = run([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)])
    try:
        return float(proc.stdout.decode().strip())
    except ValueError:
        return -1.0


def decode_errors(path: Path) -> int:
    proc = run([FFMPEG, "-v", "error", "-err_detect", "+bitstream+buffer", "-i", str(path), "-f", "null", "-"])
    return len([l for l in proc.stderr.decode("utf-8", "replace").splitlines() if l.strip()])


CANDIDATES = {
    "B 当前写法 -ss前": lambda s, e, src, out: [
        "-ss", f"{s}", "-i", str(src), "-t", f"{e - s:.3f}", "-c", "copy", str(out)],
    "G 加 -copyts": lambda s, e, src, out: [
        "-copyts", "-ss", f"{s}", "-i", str(src), "-t", f"{e - s:.3f}", "-c", "copy", str(out)],
    "E -copyts -avoid": lambda s, e, src, out: [
        "-ss", f"{s}", "-i", str(src), "-t", f"{e - s:.3f}", "-c", "copy",
        "-copyts", "-avoid_negative_ts", "make_zero", str(out)],
}


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="vs-final-"))
    srcs = {}
    for gop in (30, 250):
        p = work / f"ramp{gop}.mp4"
        make_ramp(p, gop)
        srcs[gop] = p
        print(f"源 gop={gop}  关键帧 {[round(k, 2) for k in probe_keyframes(str(p))]}")

    for gop, src in srcs.items():
        print(f"\n===== 源 gop={gop} =====")
        for start, end in ((2.0, 5.0), (4.6, 7.0)):
            want_sec = int(start)
            want_gray = int(STEP * want_sec)
            want_frames = int(round((end - start) * 30))
            print(f"  请求 {start} → {end}（{end - start:.1f}s，{want_frames} 帧，首帧灰度应为 {want_gray}）")
            for label, builder in CANDIDATES.items():
                out = work / f"{gop}-{start}-{label[0]}.mp4"
                proc = run([FFMPEG, "-hide_banner", "-y", "-loglevel", "error"] + builder(start, end, src, out))
                if not out.is_file():
                    print(f"    {label:20s} 失败: {proc.stderr.decode('utf-8','replace').strip()[-100:]}")
                    continue
                gray = first_gray(out)
                frames = count_frames(out)
                ok = "内容对" if abs(gray - want_gray) <= 6 else "!!内容错"
                print(f"    {label:20s} 首帧灰度 {gray:3d}({ok})  帧数 {frames:>4}/{want_frames}"
                      f"  时长 {fmt_duration(out):.3f}s  体积 {out.stat().st_size / 1024:>6.0f}KB  解码报错 {decode_errors(out)}")
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
