"""核心功能自测。

重点用"灰度斜坡"视频（第 i 秒灰度 = 12*i）做逐帧内容校验：
只测时长是不够的——切错位置时长也可能对得上，必须读输出文件的实际画面。

用法：tools\\python\\python.exe tests\\smoke_test.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from ffmpeg_tools import CREATE_NO_WINDOW, find_tool, probe_keyframes, probe_video, scan_keyframes  # noqa: E402
from segments import Segment, equal_segments, format_clock, parse_time  # noqa: E402
from splitter import (  # noqa: E402
    OutputSettings, SplitJob, build_command, build_concat_command, build_tasks,
    previous_keyframe, scale_filter,
)
import splitter  # noqa: E402

FFMPEG = find_tool("ffmpeg")
FFPROBE = find_tool("ffprobe")
STEP = 12.0  # 每秒灰度增量

failures: list[str] = []
workdir: Path


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f"  -- {detail}" if detail else ""))
    if not condition:
        failures.append(label)


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, creationflags=CREATE_NO_WINDOW)


def make_ramp(path: Path, gop: int, seconds: float = 20.0, gop_min: int | None = None) -> None:
    """每秒灰度递增的视频；gop=30 时关键帧恰好落在整数秒。"""
    vf = f"geq=r='{STEP}*floor(T)':g='{STEP}*floor(T)':b='{STEP}*floor(T)'"
    run([FFMPEG, "-hide_banner", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"nullsrc=s=8x8:r=30:d={seconds}",
         "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
         "-g", str(gop), "-keyint_min", str(gop_min if gop_min is not None else gop),
         "-sc_threshold", "0", str(path)])


def gray_at(path: Path, t: float) -> int:
    """读 t 时刻那一帧的灰度，用来判断画面内容落在第几秒。"""
    proc = run([FFMPEG, "-v", "error", "-ss", f"{t:.3f}", "-i", str(path),
                "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"])
    return proc.stdout[0] if proc.stdout else -1


def duration_of(path: Path) -> float:
    proc = run([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)])
    try:
        return float(proc.stdout.decode().strip())
    except ValueError:
        return -1.0


def count_frames(path: Path) -> int:
    proc = run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-count_frames",
                "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)])
    try:
        return int(proc.stdout.decode().strip())
    except ValueError:
        return -1


def expect_second(second: float) -> int:
    return int(round(STEP * int(second)))


def check_gray(label: str, path: Path, at: float, second: float, tolerance: int = 6) -> None:
    got = gray_at(path, at)
    want = expect_second(second)
    check(label, abs(got - want) <= tolerance, f"t={at:.2f}s 灰度 {got}，应为 {want}（源 {second:.2f}s）")


# ---------------------------------------------------------------- 纯函数
def test_pure() -> None:
    print("--- 纯函数 ---")
    check("parse_time('90') == 90", parse_time("90") == 90.0)
    check("parse_time('1:30') == 90", parse_time("1:30") == 90.0)
    check("parse_time('00:01:30.5')", abs(parse_time("00:01:30.5") - 90.5) < 1e-6)
    check("parse_time 拒绝乱输入", _raises(lambda: parse_time("abc")))
    check("format_clock 往返", format_clock(parse_time("00:01:30.250")) == "00:01:30.250")

    check("保持原始 -> 不缩放", scale_filter("保持原始") is None)
    check("720p -> scale=-2:720", scale_filter("720p") == "scale=-2:720")
    check("1280x720 自定义", scale_filter("1280x720") == "scale=1280:720")
    check("非法分辨率报错", _raises(lambda: scale_filter("很大")))

    segs = equal_segments(0.0, 10.0, 4.0)
    check("10 秒按 4 秒等分 -> 3 段", len(segs) == 3, str([(s.start, s.end) for s in segs]))
    check("最后一段补齐到结尾", abs(segs[-1].end - 10.0) < 1e-6)
    check("合计时长 = 10", abs(sum(s.duration for s in segs) - 10.0) < 1e-6)

    keys = (0.0, 2.0, 8.33)
    check("previous_keyframe 往前取", previous_keyframe(3.0, keys) == 2.0)
    check("previous_keyframe 恰好命中", previous_keyframe(2.0, keys) == 2.0)
    check("previous_keyframe 早于首个关键帧", previous_keyframe(0.0, keys) == 0.0)
    check("previous_keyframe 无关键帧时原样返回", previous_keyframe(3.0, ()) == 3.0)
    check("previous_keyframe 永不越过请求点", previous_keyframe(7.9, keys) <= 7.9)


def _raises(fn) -> bool:
    try:
        fn()
    except Exception:
        return True
    return False


# ---------------------------------------------------------------- 探测
def test_probe(ramp_dense: Path, ramp_sparse: Path) -> None:
    print("\n--- 探测 ---")
    info = probe_video(str(ramp_dense))
    check("探测时长 ≈ 20 秒", abs(info.duration - 20.0) < 0.5, f"{info.duration:.3f}")
    check("探测分辨率 8x8", info.resolution == "8x8", info.resolution)
    check("探测帧率 ≈ 30", abs(info.fps - 30.0) < 0.1, info.fps_text)
    check("无音轨时如实报告", info.audio_codec == "无", info.audio_codec)

    dense_keys = probe_keyframes(str(ramp_dense))
    check("密集样本关键帧 ≈ 每秒一个", len(dense_keys) >= 19, f"{len(dense_keys)} 个")
    check("关键帧从 0 开始", bool(dense_keys) and dense_keys[0] == 0.0)
    sparse_keys = probe_keyframes(str(ramp_sparse))
    check("稀疏样本关键帧远少于密集样本", len(sparse_keys) < len(dense_keys), f"{len(sparse_keys)} vs {len(dense_keys)}")


# ---------------------------------------------------------------- 内容准确性
def test_cut_content(ramp_dense: Path, ramp_sparse: Path) -> None:
    print("\n--- 无损切割：严格按填写时间（默认）---")
    for name, sample, gop in (("密集", ramp_dense, 30), ("稀疏", ramp_sparse, 250)):
        settings = OutputSettings(mode="copy", container="mp4", out_dir=str(workdir), prefix="strict")
        check(f"{name}样本：默认不对齐关键帧", settings.align_keyframes is False)

        # 起点刻意选在关键帧之间（稀疏样本的关键帧是 0 / 8.33 / 16.67）
        out = workdir / f"strict-{gop}.mp4"
        asked = Segment(2.4, 5.0)
        run(build_command(str(sample), asked, settings, str(out)))
        check(f"{name}样本：切割成功", out.is_file() and out.stat().st_size > 0)
        check_gray(f"{name}样本：首帧正是填写的时间", out, 0.0, asked.start)
        check(f"{name}样本：时长 ≈ 请求时长", abs(duration_of(out) - asked.duration) <= 0.2,
              f"{duration_of(out):.3f} vs {asked.duration:.3f}")

    print("\n--- 无损切割：向前对齐到关键帧（可选）---")
    for name, sample, gop in (("密集", ramp_dense, 30), ("稀疏", ramp_sparse, 250)):
        keys = probe_keyframes(str(sample))
        align = OutputSettings(mode="copy", container="mp4", out_dir=str(workdir), prefix="align",
                               align_keyframes=True)
        settings = _run_job_settings(align)
        logs, progress, result = _run_job(sample, [Segment(2.4, 5.0)], settings)
        check(f"{name}样本：对齐模式正常结束", bool(result) and result[0][0], str(result))
        check(f"{name}样本：日志报告了对齐", any("对齐到关键帧" in line for line in logs), "".join(logs[-2:]))

        expected_start = previous_keyframe(2.4, keys)
        produced = Path(settings.out_dir) / f"{settings.prefix}_001.mp4"
        if produced.is_file():
            check_gray(f"{name}样本：首帧是关键帧位置", produced, 0.0, expected_start)
            check(f"{name}样本：对齐点不晚于请求起点", expected_start <= 2.4 + 1e-6,
                  f"{expected_start:.2f} <= 2.4")
            check(f"{name}样本：片段包含请求区间",
                  expected_start <= 2.4 and duration_of(produced) >= 5.0 - expected_start - 0.2,
                  f"起点 {expected_start:.2f}s 时长 {duration_of(produced):.3f}s")
        else:
            check(f"{name}样本：产出文件存在", False, str(produced))

    print("\n--- 无损切割不能凭空变长（回归：-avoid_negative_ts 的坑）---")
    out = workdir / "regress.mp4"
    settings = OutputSettings(mode="copy", container="mp4", out_dir=str(workdir), prefix="r")
    # 起点正好是关键帧 0.0，避免对齐带来的正常增长，单看命令本身是否虚增
    run(build_command(str(ramp_sparse), Segment(0.0, 3.0), settings, str(out)))
    got = duration_of(out)
    check("3 秒请求切出 2.8~3.3 秒", 2.8 <= got <= 3.3, f"{got:.3f}s")
    check("帧数接近 90 而非 200+", 85 <= count_frames(out) <= 100, f"{count_frames(out)} 帧")

    print("\n--- 精确重编码的内容准确性 ---")
    out = workdir / "encode-acc.mp4"
    enc = OutputSettings(mode="encode", resolution="保持原始", fps="保持原始", crf=28,
                         preset="ultrafast", container="mp4", out_dir=str(workdir), prefix="enc")
    run(build_command(str(ramp_sparse), Segment(2.0, 5.0), enc, str(out)))
    check("重编码切割成功", out.is_file() and out.stat().st_size > 0)
    check_gray("重编码：首帧就是请求的 2.0s", out, 0.0, 2.0)
    check("重编码片段时长 ≈ 3 秒", abs(duration_of(out) - 3.0) < 0.25, f"{duration_of(out):.3f}s")


def _run_job_settings(settings: OutputSettings) -> OutputSettings:
    """对齐模式走后台任务，需要一个真实存在的输出目录。"""
    Path(settings.out_dir).mkdir(parents=True, exist_ok=True)
    return settings


# ---------------------------------------------------------------- 合并
def test_merge(ramp_sparse: Path) -> None:
    print("\n--- 无损合并（严格切点 → 拼接 → 逐点核对画面）---")
    parts_dir = workdir / "merge-parts"
    parts_dir.mkdir(exist_ok=True)
    settings = OutputSettings(mode="copy", container="mp4", out_dir=str(workdir), prefix="m")

    asked = [(2.0, 4.0), (10.0, 12.0)]
    parts, spans, cursor = [], [], 0.0
    for i, (s, e) in enumerate(asked, 1):
        p = parts_dir / f"m_{i:03d}.mp4"
        run(build_command(str(ramp_sparse), Segment(s, e), settings, str(p)))
        d = duration_of(p)
        # 严格模式下每段自身的第一帧就该是填写的时间
        check_gray(f"片段 {i} 起点严格按填写时间", p, 0.0, s)
        parts.append(p)
        spans.append((cursor, d, s))
        cursor += d

    merged = workdir / "merged.mp4"
    result = run(build_concat_command([str(p) for p in parts], str(merged), settings, copy=True))
    check("无损合并成功", merged.is_file() and merged.stat().st_size > 0,
          result.stderr.decode("utf-8", "replace")[-200:])
    if not merged.is_file():
        return

    total = duration_of(merged)
    check("合并时长 = 各段之和（±0.2s）", abs(total - cursor) <= 0.2, f"{total:.3f} vs {cursor:.3f}")

    for index, (offset, d, s) in enumerate(spans):
        check_gray(f"第 {index + 1} 段开头画面正确", merged, offset + 0.15, s + 0.15)
        check_gray(f"第 {index + 1} 段中间画面正确", merged, offset + d / 2, s + d / 2)
        if index == len(spans) - 1:
            # 末尾留出余量：-c copy 按包切，最后会多出 1~3 帧
            check_gray(f"第 {index + 1} 段结尾画面正确", merged, offset + d - 0.25, s + d - 0.25)


# ---------------------------------------------------------------- 后台任务
def _run_job(src: Path, segments: list[Segment], settings: OutputSettings,
             ) -> tuple[list[str], list[float], list[tuple[bool, str]]]:
    """跑一次完整的后台任务，返回 (日志, 进度序列, 结束结果)。"""
    tasks = build_tasks(str(src), segments, settings, target_dir=settings.parts_dir or None)
    logs: list[str] = []
    progress: list[float] = []
    result: list[tuple[bool, str]] = []
    done = threading.Event()
    job = SplitJob(
        src=str(src), tasks=tasks, settings=settings,
        on_log=logs.append, on_progress=progress.append,
        on_finished=lambda ok, msg: (result.append((ok, msg)), done.set()),
    )
    job.start()
    done.wait(timeout=120)
    return logs, progress, result


def test_job(ramp_sparse: Path) -> None:
    print("\n--- 后台任务（SplitJob）---")

    # 分别输出
    split_dir = workdir / "job-split"
    settings = OutputSettings(mode="copy", container="mp4", out_dir=str(split_dir), prefix="job")
    logs, progress, result = _run_job(ramp_sparse, [Segment(2.0, 5.0), Segment(9.0, 11.0)], settings)
    check("分别输出：正常结束", bool(result) and result[0][0], str(result))
    check("分别输出：进度到 100%", bool(progress) and abs(progress[-1] - 1.0) < 1e-6, str(progress[-1:]))
    check("分别输出：有多个进度回调", len(progress) > 2, f"{len(progress)} 次")
    check("分别输出：默认严格切点，不往前对齐", not any("往前对齐" in line for line in logs))
    check("分别输出：产出 2 个文件", len(list(split_dir.glob("*.mp4"))) == 2,
          str([p.name for p in split_dir.glob("*.mp4")]))
    check("分别输出：无残留 parts 目录", not settings.parts_dir)

    # 勾选对齐关键帧后，日志应当报告对齐
    align_dir = workdir / "job-align"
    align_settings = OutputSettings(mode="copy", container="mp4", out_dir=str(align_dir),
                                    prefix="al", align_keyframes=True)
    logs, progress, result = _run_job(ramp_sparse, [Segment(2.4, 5.0)], align_settings)
    check("对齐模式：正常结束", bool(result) and result[0][0], str(result))
    check("对齐模式：日志报告了对齐", any("对齐到关键帧" in line for line in logs), "".join(logs[-2:]))

    # 合并输出（合并后删除分段）
    merged_dir = workdir / "job-merged"
    merged_path = merged_dir / "clip_merged.mp4"
    parts_dir = Path(tempfile.mkdtemp(prefix="vsplit-parts-test-"))
    merge_settings = OutputSettings(
        mode="copy", container="mp4", out_dir=str(merged_dir), prefix="clip",
        merge=True, merged_path=str(merged_path), keep_parts=False, parts_dir=str(parts_dir),
    )
    logs, progress, result = _run_job(ramp_sparse, [Segment(2.0, 5.0), Segment(9.0, 11.0)], merge_settings)
    check("合并：正常结束", bool(result) and result[0][0], str(result))
    check("合并：进度到 100%", bool(progress) and abs(progress[-1] - 1.0) < 1e-6, str(progress[-1:]))
    check("合并：日志提到合并", any("合并" in line for line in logs))
    check("合并：产出合并文件", merged_path.is_file())
    check("合并：临时目录已清理", not parts_dir.exists())
    if merged_path.is_file():
        expected = (5.0 - 2.0) + (11.0 - 9.0)
        check("合并：时长 = 各段之和（±0.3s）", abs(duration_of(merged_path) - expected) <= 0.3,
              f"{duration_of(merged_path):.3f} vs {expected:.3f}")

    # 合并输出（保留分段）
    keep_dir = workdir / "job-keep"
    keep_parts_dir = Path(tempfile.mkdtemp(prefix="vsplit-parts-keep-"))
    keep_settings = OutputSettings(
        mode="copy", container="mp4", out_dir=str(keep_dir), prefix="keep",
        merge=True, merged_path=str(keep_dir / "keep_merged.mp4"), keep_parts=True,
        parts_dir=str(keep_parts_dir),
    )
    logs, progress, result = _run_job(ramp_sparse, [Segment(2.0, 5.0)], keep_settings)
    check("保留分段：正常结束", bool(result) and result[0][0], str(result))
    check("保留分段：分段文件已搬到输出目录",
          len(list((keep_dir / "keep_parts").glob("*.mp4"))) == 1,
          str(list((keep_dir / "keep_parts").glob("*"))))
    check("保留分段：临时目录已清理", not keep_parts_dir.exists())


# ---------------------------------------------------------------- 音轨与路径
def test_merge_audio() -> None:
    print("\n--- 带音轨的合并 ---")
    sample = workdir / "av.mp4"
    run([FFMPEG, "-hide_banner", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=15",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=15",
         "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-g", "30",
         "-c:a", "aac", "-shortest", str(sample)])
    info = probe_video(str(sample))
    check("样本带音轨", info.audio_codec == "aac", info.audio_codec)

    keys = probe_keyframes(str(sample))
    settings = OutputSettings(mode="copy", container="mp4", out_dir=str(workdir), prefix="av")
    parts, expected = [], 0.0
    for i, (s, e) in enumerate([(1.0, 4.0), (8.0, 11.0)], 1):
        k = previous_keyframe(s, keys)
        p = workdir / f"av_{i:03d}.mp4"
        run(build_command(str(sample), Segment(k, e), settings, str(p)))
        expected += duration_of(p)
        parts.append(p)

    merged = workdir / "av-merged.mp4"
    run(build_concat_command([str(p) for p in parts], str(merged), settings, copy=True))
    check("带音轨合并成功", merged.is_file() and merged.stat().st_size > 0)
    if not merged.is_file():
        return

    merged_info = probe_video(str(merged))
    check("合并结果保留音轨", merged_info.audio_codec == "aac", merged_info.audio_codec)
    check("合并时长 ≈ 各段之和", abs(merged_info.duration - expected) <= 0.2,
          f"{merged_info.duration:.3f} vs {expected:.3f}")

    # 音轨是否完整：单独探测音频流的时长
    proc = run([FFPROBE, "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=duration", "-of", "csv=p=0", str(merged)])
    try:
        audio_duration = float(proc.stdout.decode().strip())
    except ValueError:
        audio_duration = -1.0
    check("音轨时长与视频一致（±0.3s）", audio_duration > 0 and abs(audio_duration - expected) <= 0.3,
          f"音频 {audio_duration:.3f}s vs 预期 {expected:.3f}s")


def test_chinese_path() -> None:
    print("\n--- 中文路径 ---")
    chinese_dir = workdir / "中文 输出目录" / "片段"
    chinese_dir.mkdir(parents=True, exist_ok=True)
    sample = workdir / "中文素材.mp4"
    run([FFMPEG, "-hide_banner", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=10",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(sample)])
    check("中文文件名素材可读取", probe_video(str(sample)).duration > 0)

    settings = OutputSettings(mode="copy", container="mp4", out_dir=str(chinese_dir), prefix="中文片段")
    parts_dir = Path(tempfile.mkdtemp(prefix="vsplit-cn-"))
    parts = []
    for i, (s, e) in enumerate([(0.0, 2.0), (5.0, 7.0)], 1):
        p = parts_dir / f"part_{i}.mp4"
        run(build_command(str(sample), Segment(s, e), settings, str(p)))
        parts.append(p)
    check("中文路径下切割成功", all(p.is_file() for p in parts))

    merged = chinese_dir / "中文合并.mp4"
    result = run(build_concat_command([str(p) for p in parts], str(merged), settings, copy=True))
    check("中文路径下合并成功", merged.is_file() and merged.stat().st_size > 0,
          result.stderr.decode("utf-8", "replace")[-200:])
    if merged.is_file():
        check("中文路径合并时长 ≈ 4 秒", abs(duration_of(merged) - 4.0) <= 0.2, f"{duration_of(merged):.3f}")


# ---------------------------------------------------------------- 编码参数
def test_merge_encode() -> None:
    print("\n--- 精确重编码模式的合并 ---")
    sample = workdir / "enc-src.mp4"
    run([FFMPEG, "-hide_banner", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30:duration=10",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=10",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "30",
         "-c:a", "aac", "-shortest", str(sample)])

    out_dir = workdir / "enc-merge"
    settings = OutputSettings(
        mode="encode", resolution="360p", fps="24", crf=30, preset="ultrafast",
        container="mp4", out_dir=str(out_dir), prefix="em",
        merge=True, merged_path=str(out_dir / "em_merged.mp4"),
        parts_dir=str(Path(tempfile.mkdtemp(prefix="vsplit-em-"))),
    )
    result = _run_job(sample, [Segment(1.0, 4.0), Segment(6.0, 9.0)], settings)
    logs, progress, finished = result
    check("重编码合并：正常结束", bool(finished) and finished[0][0], str(finished))
    merged = Path(settings.merged_path)
    check("重编码合并：产出文件", merged.is_file())
    if merged.is_file():
        info = probe_video(str(merged))
        check("重编码合并：时长 ≈ 6 秒", abs(info.duration - 6.0) <= 0.4, f"{info.duration:.3f}")
        check("重编码合并：分辨率为 360p", info.resolution == "640x360", info.resolution)
        check("重编码合并：帧率 ≈ 24", abs(info.fps - 24.0) < 0.3, info.fps_text)
        check("重编码合并：保留音轨", info.audio_codec == "aac", info.audio_codec)
    check("重编码合并：临时目录已清理", not Path(settings.parts_dir).exists())


def test_merge_fallback() -> None:
    """验证合并的自检与自愈：拼接结果时长不符时必须发现并降级，绝不悄悄交付错文件。"""
    print("\n--- 合并时长自检与自动降级 ---")
    part_a = workdir / "fb-a.mp4"
    part_b = workdir / "fb-b.mp4"
    for path, source in ((part_a, "testsrc=size=320x240:rate=30:duration=3"),
                         (part_b, "color=c=blue:size=320x240:rate=30:duration=3")):
        run([FFMPEG, "-hide_banner", "-y", "-loglevel", "error",
             "-f", "lavfi", "-i", source, "-c:v", "libx264", "-preset", "ultrafast",
             "-pix_fmt", "yuv420p", str(path)])

    settings = OutputSettings(mode="copy", container="mp4", out_dir=str(workdir), prefix="fb",
                              merge=True, merged_path=str(workdir / "fb-merged.mp4"))

    # 注入故障：让"无损拼接"这一步只拼第一段，制造时长不符
    real_builder = splitter.build_concat_command

    def faulty(parts, out_path, conf, copy):
        if copy:
            return real_builder(parts[:1], out_path, conf, copy=True)
        return real_builder(parts, out_path, conf, copy=False)

    logs: list[str] = []
    job = SplitJob(src=str(part_a), tasks=[], settings=settings,
                   on_log=logs.append, on_progress=lambda _r: None,
                   on_finished=lambda _ok, _msg: None)
    raised = ""
    splitter.build_concat_command = faulty
    try:
        job._merge([str(part_a), str(part_b)])
    except RuntimeError as exc:
        raised = str(exc)
    finally:
        splitter.build_concat_command = real_builder

    text = "".join(logs)
    merged = workdir / "fb-merged.mp4"
    check("发现无损拼接的时长不符", "时长不符" in text, text[-200:])
    check("自动降级为重编码", "重编码合并完成" in text, text[-200:])
    check("降级过程没有报错", not raised, raised)
    check("最终产出文件", merged.is_file() and merged.stat().st_size > 0)
    if merged.is_file():
        check("最终时长 ≈ 6 秒（含两段）", abs(duration_of(merged) - 6.0) <= 0.4, f"{duration_of(merged):.3f}")

    # 两种方式都无法得到正确结果时，必须报错而不是交付错文件
    def always_wrong(parts, out_path, conf, copy):
        return real_builder(parts[:1], out_path, conf, copy=copy)

    logs.clear()
    raised = ""
    splitter.build_concat_command = always_wrong
    try:
        job._merge([str(part_a), str(part_b)])
    except RuntimeError as exc:
        raised = str(exc)
    finally:
        splitter.build_concat_command = real_builder
    check("两种方式都失败时抛出错误", bool(raised), raised)
    check("错误信息说明了原因", "合并失败" in raised, raised)


def test_merge_strict_realistic() -> None:
    """真实素材（有音轨、关键帧每 2 秒）：严格切点 + 合并必须自动纠正泄漏。"""
    print("\n--- 严格切点合并：真实素材下的自检与纠正 ---")
    sample = workdir / "real.mp4"
    run([FFMPEG, "-hide_banner", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=40",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=40",
         "-c:v", "libx264", "-preset", "veryfast", "-b:v", "800k", "-pix_fmt", "yuv420p",
         "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
         "-c:a", "aac", "-shortest", str(sample)])

    keys = probe_keyframes(str(sample))
    asked = [(3.0, 8.0), (20.0, 25.0)]
    check("切点不在关键帧上（构成预滚）", 3.0 not in keys, str([round(k, 2) for k in keys[:5]]))
    want = sum(e - s for s, e in asked)

    out_dir = workdir / "real-strict"
    settings = OutputSettings(
        mode="copy", container="mp4", out_dir=str(out_dir), prefix="rs",
        merge=True, merged_path=str(out_dir / "rs_merged.mp4"),
        parts_dir=str(Path(tempfile.mkdtemp(prefix="vsplit-rs-"))),
    )
    logs, _progress, finished = _run_job(sample, [Segment(*s) for s in asked], settings)
    check("严格切点合并：正常结束", bool(finished) and finished[0][0], str(finished))

    merged = Path(settings.merged_path)
    check("严格切点合并：产出文件", merged.is_file())
    text = "".join(logs)
    check("严格切点合并：走了智能切割", "智能切割" in text, text[-200:])
    if merged.is_file():
        got = duration_of(merged)
        # 关键：不能是泄漏后的 11.2s，应当贴近填写总时长 10s
        check("严格切点合并：没有泄漏多出 1 秒", got < want + 0.5, f"{got:.3f}s（填写 {want:.3f}s）")
        check("严格切点合并：时长贴近填写总时长", abs(got - want) <= 0.4, f"{got:.3f} vs {want:.3f}")
        info = probe_video(str(merged))
        check("严格切点合并：保留音轨", info.audio_codec == "aac", info.audio_codec)
        check("严格切点合并：分辨率不变", info.resolution == "640x360", info.resolution)

    # 同一素材换成对齐切点，应当走通无损拼接
    align_dir = workdir / "real-align"
    align_settings = OutputSettings(
        mode="copy", container="mp4", out_dir=str(align_dir), prefix="ra", align_keyframes=True,
        merge=True, merged_path=str(align_dir / "ra_merged.mp4"),
        parts_dir=str(Path(tempfile.mkdtemp(prefix="vsplit-ra-"))),
    )
    logs, _progress, finished = _run_job(sample, [Segment(*s) for s in asked], align_settings)
    check("对齐切点合并：正常结束", bool(finished) and finished[0][0], str(finished))
    text = "".join(logs)
    check("对齐切点合并：走的是无损拼接", "无损合并完成" in text, text[-200:])
    check("对齐切点合并：没有降级", "改用重编码" not in text)


# ---------------------------------------------------------------- 智能切割
def smart_leftovers(before: set[Path]) -> list[str]:
    """本次运行新增的智能切割临时目录（应当为空）。"""
    now = set(Path(tempfile.gettempdir()).glob("vsplit-smart-*"))
    return sorted(p.name for p in now - before)


def first_key_time(path: Path) -> float:
    """输出文件的第一个真实关键帧时间（跳过非关键帧读取，不走缓存）。"""
    keys = scan_keyframes(str(path))
    return keys[0] if keys else -1.0


def container_frames(path: Path) -> int:
    """容器里记录的帧数。隐藏的预滚帧只会体现在这里，解码计数看不到。"""
    proc = run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=nb_frames",
                "-of", "csv=p=0", str(path)])
    try:
        return int(proc.stdout.decode().strip())
    except ValueError:
        return -1


def make_clip(path: Path, codec: str, seconds: float) -> None:
    """带音轨、每 2 秒一个关键帧的真实风格素材。"""
    encoder = ["-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "log-level=error"] \
        if codec == "hevc" else ["-c:v", "libx264", "-preset", "veryfast"]
    run([FFMPEG, "-hide_banner", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"testsrc2=s=640x360:r=30:d={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=440:d={seconds}",
         *encoder, "-b:v", "800k", "-pix_fmt", "yuv420p", "-g", "60", "-keyint_min", "60",
         "-sc_threshold", "0", "-c:a", "aac", "-shortest", str(path)])


def test_smart_cut(ramp_dense: Path) -> None:
    print("\n--- 无损切割：智能切割（切点不在关键帧上）---")
    before = set(Path(tempfile.gettempdir()).glob("vsplit-smart-*"))

    # 灰度斜坡：每秒一个关键帧，切点 2.4s 落在 2.0~3.0 之间
    out_dir = workdir / "smart-ramp"
    settings = OutputSettings(mode="copy", container="mp4", out_dir=str(out_dir), prefix="sc")
    logs, _p, done = _run_job(ramp_dense, [Segment(2.4, 5.0)], settings)
    out = out_dir / "sc_001.mp4"
    text = "".join(logs)
    check("智能切割：正常结束", bool(done) and done[0][0], str(done))
    check("智能切割：日志说明了头部重编码", "智能切割" in text, text[-200:])
    if out.is_file():
        check("智能切割：首个关键帧在 0 秒（无隐藏预滚）", first_key_time(out) == 0.0,
              f"{first_key_time(out):.3f}")
        check_gray("智能切割：首帧正是填写的 2.4s 所在秒", out, 0.0, 2.4)
        check("智能切割：时长 ≈ 2.6 秒", abs(duration_of(out) - 2.6) <= 0.15, f"{duration_of(out):.3f}")
        check("智能切割：容器帧数 = 解码帧数（无多余隐藏帧）",
              container_frames(out) == count_frames(out), f"{container_frames(out)} vs {count_frames(out)}")
    else:
        check("智能切割：产出文件存在", False, str(out))
    check("智能切割：临时目录已清理", not smart_leftovers(before), str(smart_leftovers(before)))

    # 切点恰好在关键帧上：保持原样，不走智能切割
    kf_dir = workdir / "smart-keyframe"
    settings = OutputSettings(mode="copy", container="mp4", out_dir=str(kf_dir), prefix="kf")
    logs, _p, done = _run_job(ramp_dense, [Segment(3.0, 5.0)], settings)
    out = kf_dir / "kf_001.mp4"
    check("关键帧起点：不走智能切割", "智能切割" not in "".join(logs))
    if out.is_file():
        check_gray("关键帧起点：首帧正是 3.0s 所在秒", out, 0.0, 3.0)
        check("关键帧起点：首个关键帧在 0 秒", first_key_time(out) == 0.0, f"{first_key_time(out):.3f}")

    # 起点之后到结尾没有关键帧：整段重编码
    tail_dir = workdir / "smart-tail"
    settings = OutputSettings(mode="copy", container="mp4", out_dir=str(tail_dir), prefix="tl")
    logs, _p, done = _run_job(ramp_dense, [Segment(19.4, 20.0)], settings)
    out = tail_dir / "tl_001.mp4"
    check("无后续关键帧：整段重编码并说明原因", "整段重编码" in "".join(logs), "".join(logs)[-200:])
    if out.is_file():
        check("无后续关键帧：首个关键帧在 0 秒", first_key_time(out) == 0.0, f"{first_key_time(out):.3f}")
        check_gray("无后续关键帧：首帧正是 19.4s 所在秒", out, 0.0, 19.4)
        check("无后续关键帧：时长 ≈ 0.6 秒", abs(duration_of(out) - 0.6) <= 0.1, f"{duration_of(out):.3f}")

    # 真实素材（h264 + aac，每 2 秒一个关键帧）
    real = workdir / "smart-real-h264.mp4"
    make_clip(real, "h264", 12.0)
    real_dir = workdir / "smart-real"
    settings = OutputSettings(mode="copy", container="mp4", out_dir=str(real_dir), prefix="rh")
    logs, _p, done = _run_job(real, [Segment(3.3, 8.0)], settings)
    out = real_dir / "rh_001.mp4"
    check("真实 h264 素材：智能切割正常结束", bool(done) and done[0][0], str(done))
    if out.is_file():
        check("真实 h264 素材：走的是智能切割", "智能切割" in "".join(logs))
        # 带音轨时，视频起点可能被音频对齐推迟约 0.02s（一帧以内），不是隐藏预滚
        check("真实 h264 素材：首个关键帧在一帧以内", 0.0 <= first_key_time(out) <= 0.05,
              f"{first_key_time(out):.3f}")
        check("真实 h264 素材：时长 ≈ 4.7 秒", abs(duration_of(out) - 4.7) <= 0.2, f"{duration_of(out):.3f}")
        check("真实 h264 素材：容器帧数 ≈ 141（无隐藏预滚）",
              abs(container_frames(out) - 141) <= 4, f"{container_frames(out)} 帧")
        check("真实 h264 素材：保留音轨", probe_video(str(out)).audio_codec == "aac")

    # hevc 素材：头部必须用 libx265 重编码，编码器与源一致
    hevc = workdir / "smart-real-hevc.mp4"
    make_clip(hevc, "hevc", 12.0)
    hevc_dir = workdir / "smart-hevc"
    settings = OutputSettings(mode="copy", container="mp4", out_dir=str(hevc_dir), prefix="hv")
    logs, _p, done = _run_job(hevc, [Segment(3.3, 8.0)], settings)
    out = hevc_dir / "hv_001.mp4"
    check("hevc 素材：正常结束", bool(done) and done[0][0], str(done))
    if out.is_file():
        info = probe_video(str(out))
        check("hevc 素材：输出仍是 hevc", info.video_codec == "hevc", info.video_codec)
        check("hevc 素材：首个关键帧在一帧以内", 0.0 <= first_key_time(out) <= 0.05,
              f"{first_key_time(out):.3f}")
        check("hevc 素材：时长 ≈ 4.7 秒", abs(info.duration - 4.7) <= 0.2, f"{info.duration:.3f}")

    # 拼接结果未通过校验：必须降级为整段重编码，而不是交付错误文件
    real_builder = splitter.build_concat_command

    def short_concat(parts, out_path, conf, copy):
        if copy:
            return real_builder(parts[:1], out_path, conf, copy=True)
        return real_builder(parts, out_path, conf, copy=False)

    fb_dir = workdir / "smart-fallback"
    settings = OutputSettings(mode="copy", container="mp4", out_dir=str(fb_dir), prefix="fb")
    splitter.build_concat_command = short_concat
    try:
        logs, _p, done = _run_job(ramp_dense, [Segment(2.4, 5.0)], settings)
    finally:
        splitter.build_concat_command = real_builder
    out = fb_dir / "fb_001.mp4"
    check("拼接校验失败：降级后正常结束", bool(done) and done[0][0], str(done))
    check("拼接校验失败：日志说明了降级", "改为整段重编码" in "".join(logs), "".join(logs)[-200:])
    if out.is_file():
        check("拼接校验失败：最终时长 ≈ 2.6 秒", abs(duration_of(out) - 2.6) <= 0.15, f"{duration_of(out):.3f}")
        check("拼接校验失败：首个关键帧在 0 秒", first_key_time(out) == 0.0, f"{first_key_time(out):.3f}")
    check("拼接校验失败：临时目录已清理", not smart_leftovers(before), str(smart_leftovers(before)))

    # 智能切割过程中取消：立即停止，并清理临时目录
    cancel_dir = workdir / "smart-cancel"
    tasks = build_tasks(str(real), [Segment(3.3, 8.0)], OutputSettings(out_dir=str(cancel_dir)))
    box: dict = {}
    result: list[tuple[bool, str]] = []
    done_evt = threading.Event()

    def on_log(line: str) -> None:
        if "智能切割" in line and "job" in box:
            box["job"].cancel()

    job = SplitJob(
        src=str(real), tasks=tasks,
        settings=OutputSettings(mode="copy", container="mp4", out_dir=str(cancel_dir), prefix="cc"),
        on_log=on_log, on_progress=lambda _r: None,
        on_finished=lambda ok, msg: (result.append((ok, msg)), done_evt.set()),
    )
    box["job"] = job
    job.start()
    done_evt.wait(timeout=120)
    check("智能切割取消：报告已取消", bool(result) and result[0] == (False, "已取消"), str(result))
    check("智能切割取消：临时目录已清理", not smart_leftovers(before), str(smart_leftovers(before)))


# ---------------------------------------------------------------- 编码参数
def test_encode_options(ramp_dense: Path) -> None:
    print("\n--- 精确重编码的分辨率与帧率 ---")
    # 用 16:9 的样本，避免正方形素材把 -2:480 的等比宽度算成 480
    wide = workdir / "wide.mp4"
    run([FFMPEG, "-hide_banner", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30:duration=6",
         "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", str(wide)])

    out = workdir / "enc-opts.mkv"
    settings = OutputSettings(mode="encode", resolution="480p", fps="24", crf=28, preset="ultrafast",
                              container="mkv", out_dir=str(workdir), prefix="e")
    run(build_command(str(wide), Segment(1.5, 4.0), settings, str(out)))
    check("产出文件", out.is_file())
    if not out.is_file():
        return
    info = probe_video(str(out))
    check("16:9 素材缩到 480p 得 854x480", info.resolution == "854x480", info.resolution)
    check("帧率 ≈ 24", abs(info.fps - 24.0) < 0.2, info.fps_text)
    check("时长 ≈ 2.5 秒", abs(info.duration - 2.5) < 0.25, f"{info.duration:.3f}")


def main() -> int:
    global workdir
    workdir = Path(tempfile.mkdtemp(prefix="vsplit-test-"))
    print(f"工作目录：{workdir}\n")

    ramp_dense = workdir / "ramp-dense.mp4"
    ramp_sparse = workdir / "ramp-sparse.mp4"
    make_ramp(ramp_dense, gop=30)
    make_ramp(ramp_sparse, gop=250)

    test_pure()
    test_probe(ramp_dense, ramp_sparse)
    test_cut_content(ramp_dense, ramp_sparse)
    test_smart_cut(ramp_dense)
    test_merge(ramp_sparse)
    test_merge_audio()
    test_merge_strict_realistic()
    test_merge_encode()
    test_merge_fallback()
    test_chinese_path()
    test_job(ramp_sparse)
    test_encode_options(ramp_dense)

    print()
    if failures:
        print(f"失败 {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        print(f"共 {len(failures)} 项失败")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
