"""切割与合并任务：命令行拼装与后台执行。"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass, replace
from pathlib import Path

from ffmpeg_tools import CREATE_NO_WINDOW, VideoInfo, find_tool, probe_keyframes, probe_video, scan_keyframes
from segments import Segment, format_clock

# 预设分辨率 -> scale 过滤器参数（-2 表示按比例自动对齐偶数）
_SCALE_PRESETS = {
    "1080p": "-2:1080",
    "720p": "-2:720",
    "480p": "-2:480",
    "360p": "-2:360",
}

# 进度条分配：片段切割占前 88%，合并占后 12%
PARTS_SHARE = 0.88

# 无损切割时，开头最多允许多出多少秒才提示用户
RUNUP_WARN = 2.0

# 合并后与"各段之和"的最大允许偏差（秒）；每多一段再放宽一点，
# 用于容忍拼接处 1~3 帧的取整误差。实测正常拼接偏差约 0.07s，
# 而隐藏预滚被暴露时会达到 1s 以上，所以这个容差能有效区分两者。
MERGE_TOLERANCE = 0.2
MERGE_TOLERANCE_PER_PART = 0.05

# 智能切割：切点与关键帧相差不超过它时，视为切点已落在关键帧上
KEYFRAME_EPS = 0.001
# 智能切割后的片段：首帧需要在这个误差内是关键帧，时长需要在 CUT_TOLERANCE 内
CUT_START_TOLERANCE = 0.05
CUT_TOLERANCE = 0.2
# 智能切割时，头部重编码所用的编码器，按源视频编码选择（必须与源一致）
SMART_ENCODERS = {"h264": "libx264", "hevc": "libx265"}
# 智能切割中头部与尾部共占的进度比例，其余留给最后的拼接
SMART_PROGRESS_SHARE = 0.9


@dataclass
class OutputSettings:
    """用户在界面上选定的输出参数。"""

    mode: str = "copy"  # copy = 无损快速，encode = 精确重编码
    resolution: str = "保持原始"
    fps: str = "保持原始"
    crf: int = 23
    preset: str = "veryfast"
    container: str = "mp4"
    out_dir: str = ""
    prefix: str = "clip"
    # 无损模式下是否把起点向前对齐到关键帧（默认严格按填写时间）
    align_keyframes: bool = False
    # 合并相关
    merge: bool = False
    merged_path: str = ""
    keep_parts: bool = False
    parts_dir: str = ""


def scale_filter(resolution: str) -> str | None:
    """把界面选项翻译成 ``scale=`` 过滤器；返回 None 表示不缩放。"""
    text = (resolution or "").strip()
    if not text or text == "保持原始":
        return None
    if text in _SCALE_PRESETS:
        return f"scale={_SCALE_PRESETS[text]}"
    if "x" in text.lower():
        width, _, height = text.lower().partition("x")
        width, height = width.strip(), height.strip()
        if width and height:
            return f"scale={width}:{height}"
    raise ValueError(f"无法识别的分辨率：{resolution}")


def previous_keyframe(start: float, keyframes) -> float:
    """取不超过 ``start`` 的最后一个关键帧。

    只有用户选择「向前对齐到关键帧」时才会用到。好处是切点正好落在关键帧上，
    ffmpeg 不必为了精确切点把关键帧到切点之间的预滚数据一并写进文件，因此
    文件体积最小、没有隐藏数据；代价是片段开头最多多出一个关键帧间隔。
    """
    if not keyframes:
        return start
    earlier = [k for k in keyframes if k <= start + 1e-6]
    return max(earlier) if earlier else start


def plan_cut(start: float, end: float, keyframes) -> tuple[str, float]:
    """无损模式下决定一段的切法，返回 ``(方式, 关键帧时间)``。

    - ``("copy", 0.0)``：起点已在关键帧上（或读不到关键帧），整段直接复制；
    - ``("split", N)``：起点不在关键帧上，``[start, N)`` 重编码，``[N, end)`` 复制；
    - ``("reencode", 0.0)``：起点之后片段内没有关键帧，整段重编码。
    """
    if not keyframes:
        return "copy", 0.0
    if any(abs(k - start) <= KEYFRAME_EPS for k in keyframes):
        return "copy", 0.0
    later = [k for k in keyframes if k > start]
    if not later or later[0] >= end - KEYFRAME_EPS:
        return "reencode", 0.0
    return "split", later[0]


def smart_block_reason(info: VideoInfo | None) -> str:
    """源视频是否允许智能切割；返回空串表示可以，否则返回原因。"""
    if info is None:
        return "读取源视频信息失败"
    if info.video_codec not in SMART_ENCODERS:
        return f"视频编码 {info.video_codec} 没有对应的重编码器"
    if info.pix_fmt != "yuv420p":
        return f"像素格式 {info.pix_fmt or '未知'} 不是 yuv420p"
    if info.audio_codec not in ("aac", "无"):
        return f"音频编码 {info.audio_codec} 不是 aac"
    return ""


def build_command(
    src: str, segment: Segment, settings: OutputSettings, out_path: str, *, encoder: str = "libx264"
) -> list[str]:
    """拼出一条完整的 ffmpeg 切割命令。"""
    ffmpeg = find_tool("ffmpeg")
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-y",
        "-nostdin",
        "-loglevel", "error",
        "-progress", "pipe:1",
        "-nostats",
        # -ss 放在 -i 之前：快速定位
        "-ss", f"{segment.start:.3f}",
        "-i", str(src),
        "-t", f"{segment.duration:.3f}",
    ]

    if settings.mode == "copy":
        # 不要加 -avoid_negative_ts make_zero：它会把 edit list 去掉，
        # 导致隐藏的预滚数据暴露出来，3 秒的请求切出 7 秒。
        cmd += ["-c", "copy"]
    else:
        cmd += ["-c:v", encoder, "-preset", settings.preset, "-crf", str(settings.crf), "-pix_fmt", "yuv420p"]
        vf = scale_filter(settings.resolution)
        if vf:
            cmd += ["-vf", vf]
        fps = (settings.fps or "").strip()
        if fps and fps != "保持原始":
            cmd += ["-r", fps]
        cmd += ["-c:a", "aac", "-b:a", "192k"]

    if settings.container in ("mp4", "mov"):
        cmd += ["-movflags", "+faststart"]

    cmd.append(str(out_path))
    return cmd


def build_tasks(
    src: str, segments: list[Segment], settings: OutputSettings, target_dir: str | None = None
) -> list[tuple[int, Segment, str]]:
    """为每个片段生成 ``(序号, 片段, 输出路径)``。"""
    directory = Path(target_dir or settings.out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stem = Path(src).stem
    prefix = (settings.prefix or "").strip() or stem
    tasks: list[tuple[int, Segment, str]] = []
    for index, segment in enumerate(segments, 1):
        name = f"{prefix}_{index:03d}.{settings.container}"
        tasks.append((index, segment, str(directory / name)))
    return tasks


def build_concat_command(parts: list[str], out_path: str, settings: OutputSettings, copy: bool) -> list[str]:
    """用 concat demuxer 把多个片段拼成一个文件。"""
    ffmpeg = find_tool("ffmpeg")
    listing = Path(parts[0]).with_name("concat-list.txt")
    lines = []
    for part in parts:
        # concat demuxer 的清单格式：单引号包裹，内部的单引号需要转义
        escaped = str(Path(part).as_posix()).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    listing.write_text("\n".join(lines) + "\n", encoding="utf-8")

    cmd = [
        ffmpeg,
        "-hide_banner",
        "-y",
        "-nostdin",
        "-loglevel", "error",
        "-progress", "pipe:1",
        "-nostats",
        "-f", "concat",
        "-safe", "0",
        "-i", str(listing),
    ]
    if copy:
        cmd += ["-c", "copy"]
    else:
        cmd += ["-c:v", "libx264", "-preset", settings.preset, "-crf", str(settings.crf),
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k"]
    if settings.container in ("mp4", "mov"):
        cmd += ["-movflags", "+faststart"]
    cmd.append(str(out_path))
    return cmd


class SplitJob:
    """在后台线程里依次切割所有片段；需要时再合并成一个文件。"""

    def __init__(self, src, tasks, settings: OutputSettings, on_log, on_progress, on_finished):
        self.src = str(src)
        self.tasks = tasks
        self.settings = settings
        self.on_log = on_log
        self.on_progress = on_progress
        self.on_finished = on_finished
        self.total_duration = sum(seg.duration for _, seg, _ in tasks) or 1.0
        self._proc: subprocess.Popen | None = None
        self._cancelled = False
        self._thread: threading.Thread | None = None
        self._facts: tuple[tuple[float, ...], VideoInfo | None] | None = None

    # ---- 对外接口 -------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancelled = True
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    # ---- 切点对齐 -------------------------------------------------
    def _align_to_keyframes(self) -> None:
        """按用户选择，把无损切点向前对齐到关键帧（默认不做对齐）。"""
        if not self.settings.align_keyframes or self.settings.mode != "copy" or not self.tasks:
            return
        self.on_log("无损模式：正在扫描关键帧…")
        keyframes = probe_keyframes(self.src)
        if not keyframes:
            self.on_log("    读不到关键帧列表，切点将原样交给 ffmpeg 处理。")
            return

        aligned: list[tuple[int, Segment, str]] = []
        for index, segment, out_path in self.tasks:
            snapped = previous_keyframe(segment.start, keyframes)
            new_segment = Segment(snapped, segment.end)
            runup = segment.start - snapped
            if runup > 0.001:
                note = ""
                if runup > RUNUP_WARN:
                    note = "（多出的开头较长，如需精确请改用「精确重编码」）"
                self.on_log(
                    f"    片段 {index}: {format_clock(segment.start)} 往前对齐到关键帧 "
                    f"{format_clock(snapped)}，开头多出 {runup:.2f}s{note}"
                )
            if new_segment.duration < 0.05:
                self.on_log(f"    片段 {index} 长度为 0，已跳过。")
                continue
            aligned.append((index, new_segment, out_path))

        if not aligned:
            raise RuntimeError("对齐后没有可用的片段，请调整时间段。")
        self.tasks = aligned
        self.total_duration = sum(seg.duration for _, seg, _ in aligned) or 1.0

    # ---- 单段切割 -------------------------------------------------
    def _source_facts(self) -> tuple[tuple[float, ...], VideoInfo | None]:
        """源视频的关键帧与基本信息，每个任务只读一次。"""
        if self._facts is None:
            try:
                info = probe_video(self.src)
            except Exception:
                info = None
            self._facts = (probe_keyframes(self.src), info)
        return self._facts

    def _cut(self, index: int, segment: Segment, out_path: str, low: float, high: float) -> tuple[int, str]:
        """切出一个片段。无损模式下切点不在关键帧上时走智能切割。"""
        if self.settings.mode != "copy":
            cmd = build_command(self.src, segment, self.settings, out_path)
            return self._execute(cmd, segment.duration, low, high)

        keyframes, info = self._source_facts()
        kind, pivot = plan_cut(segment.start, segment.end, keyframes)
        if kind == "copy":
            cmd = build_command(self.src, segment, self.settings, out_path)
            return self._execute(cmd, segment.duration, low, high)

        reason = smart_block_reason(info)
        if kind == "split" and not reason:
            self.on_log(f"    片段 {index}: 切点 {format_clock(segment.start)} 不在关键帧上，"
                        f"开头 {pivot - segment.start:.2f}s 重编码，其余无损复制（智能切割）")
            return self._smart_cut(index, segment, pivot, out_path, low, high, info)

        if kind == "reencode":
            reason = "切点之后到结尾之间没有关键帧"
        self.on_log(f"    片段 {index}: 整段重编码（{reason}）")
        return self._reencode(segment, out_path, low, high, info)

    def _reencode(self, segment: Segment, out_path: str, low: float, high: float,
                  info: VideoInfo | None) -> tuple[int, str]:
        """整段重编码：编码器尽量与源视频一致，分辨率和帧率保持原样。"""
        encoder = SMART_ENCODERS.get(info.video_codec, "libx264") if info else "libx264"
        settings = replace(self.settings, mode="encode", resolution="保持原始", fps="保持原始")
        cmd = build_command(self.src, segment, settings, out_path, encoder=encoder)
        return self._execute(cmd, segment.duration, low, high)

    def _smart_cut(self, index: int, segment: Segment, pivot: float, out_path: str,
                   low: float, high: float, info: VideoInfo) -> tuple[int, str]:
        """智能切割：[start, 关键帧) 重编码成自包含的首个 GOP，[关键帧, end) 无损复制，再拼接。

        中间文件放在独立临时目录里，无论成功、失败还是取消都会删除。
        """
        head_len = pivot - segment.start
        tail_len = segment.end - pivot
        span = high - low
        head_high = low + span * SMART_PROGRESS_SHARE * head_len / segment.duration
        tail_high = head_high + span * SMART_PROGRESS_SHARE * tail_len / segment.duration
        encoder = SMART_ENCODERS[info.video_codec]
        head_settings = replace(self.settings, mode="encode", resolution="保持原始", fps="保持原始",
                                container="mkv")
        tail_settings = replace(self.settings, container="mkv")

        workdir = Path(tempfile.mkdtemp(prefix="vsplit-smart-"))
        try:
            head = workdir / "head.mkv"
            tail = workdir / "tail.mkv"
            code, detail = self._execute(
                build_command(self.src, Segment(segment.start, pivot), head_settings, str(head), encoder=encoder),
                head_len, low, head_high,
            )
            if code != 0 or self._cancelled:
                return code, detail
            code, detail = self._execute(
                build_command(self.src, Segment(pivot, segment.end), tail_settings, str(tail)),
                tail_len, head_high, tail_high,
            )
            if code != 0 or self._cancelled:
                return code, detail
            code, detail = self._execute(
                build_concat_command([str(head), str(tail)], str(out_path), self.settings, copy=True),
                segment.duration, tail_high, high,
            )
            if self._cancelled:
                return code, detail
            if code == 0 and self._verify_cut(out_path, segment):
                return 0, ""
            reason = "拼接失败" if code != 0 else "首帧或时长校验不符"
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

        if self._cancelled:
            return 1, "已取消"
        self.on_log(f"    片段 {index}: 智能切割结果未通过校验（{reason}），改为整段重编码")
        return self._reencode(segment, out_path, low, high, info)

    @staticmethod
    def _verify_cut(out_path: str, segment: Segment) -> bool:
        """智能切割的结果必须：首帧是关键帧（没有隐藏预滚），时长与请求一致。"""
        keyframes = scan_keyframes(str(out_path))
        if not keyframes or keyframes[0] > CUT_START_TOLERANCE:
            return False
        try:
            got = probe_video(str(out_path)).duration
        except Exception:
            return False
        return abs(got - segment.duration) <= CUT_TOLERANCE

    # ---- 主流程 ---------------------------------------------------
    def _run(self) -> None:
        completed = 0.0
        ok = True
        message = "全部完成"
        parts: list[str] = []
        try:
            self._align_to_keyframes()
            part_share = PARTS_SHARE if self.settings.merge else 1.0

            for position, (index, segment, out_path) in enumerate(self.tasks, 1):
                if self._cancelled:
                    ok, message = False, "已取消"
                    break
                self.on_log(
                    f"[{position}/{len(self.tasks)}] {Path(out_path).name}  "
                    f"{format_clock(segment.start)} → {format_clock(segment.end)}"
                )
                low = completed / self.total_duration * part_share
                high = (completed + segment.duration) / self.total_duration * part_share
                code, detail = self._cut(index, segment, out_path, low, high)
                if self._cancelled:
                    ok, message = False, "已取消"
                    break
                if code != 0:
                    ok = False
                    message = f"第 {position} 个片段切割失败"
                    self.on_log(f"    失败：{detail}")
                    break
                parts.append(out_path)
                completed += segment.duration
            else:
                if self.settings.merge:
                    self._merge(parts)
                self.on_progress(1.0)
        except Exception as exc:  # 兜底，保证界面能恢复
            ok, message = False, str(exc)
            self.on_log(f"    出错：{exc}")
        finally:
            if self.settings.merge:
                self._finish_parts(parts, ok)
        self.on_finished(ok, message)

    def _merge(self, parts: list[str]) -> None:
        """把切好的片段拼成一个文件，并校验结果。"""
        if self._cancelled:
            return
        out_path = Path(self.settings.merged_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        expected = 0.0
        for part in parts:
            try:
                expected += probe_video(part).duration
            except Exception:
                pass
        tolerance = MERGE_TOLERANCE + MERGE_TOLERANCE_PER_PART * max(0, len(parts) - 1)
        self.on_log(f"正在合并 {len(parts)} 个片段 → {out_path.name}"
                    f"（预期时长 {expected:.2f}s，容差 {tolerance:.2f}s）")

        for copy_mode, note in ((True, "无损"), (False, "重编码")):
            if self._cancelled:
                return
            code, detail = self._execute(
                build_concat_command(parts, str(out_path), self.settings, copy=copy_mode),
                expected, PARTS_SHARE, 1.0,
            )
            if self._cancelled:
                return
            if code != 0:
                self.on_log(f"    {note}合并失败：{detail}")
            else:
                try:
                    actual = probe_video(str(out_path)).duration
                except Exception as exc:
                    actual = -1.0
                    self.on_log(f"    合并结果无法读取：{exc}")
                if actual >= 0 and abs(actual - expected) <= tolerance:
                    self.on_log(f"    {note}合并完成，实际时长 {actual:.2f}s")
                    if note == "重编码":
                        self.on_log("    无损拼接会把片段开头隐藏的预滚数据暴露出来，"
                                    "本次已自动改用重编码合并，内容仍与各片段一致。")
                    return
                self.on_log(f"    {note}合并结果时长不符（预期 {expected:.2f}s，实得 {actual:.2f}s）")

        raise RuntimeError("合并失败：无损与重编码两种方式都没能通过校验。")

    def _finish_parts(self, parts: list[str], ok: bool) -> None:
        """决定分段文件的去向：保留则搬到输出目录，否则删掉临时目录。"""
        parts_dir = self.settings.parts_dir
        if not parts_dir:
            return
        source = Path(parts_dir)
        if not source.is_dir():
            return
        if ok and self.settings.keep_parts and parts:
            target = Path(self.settings.out_dir) / f"{(self.settings.prefix or 'clip')}_parts"
            try:
                target.mkdir(parents=True, exist_ok=True)
                for part in parts:
                    shutil.move(str(part), str(target / Path(part).name))
                self.on_log(f"    分段文件已保留在 {target}")
            except OSError as exc:
                self.on_log(f"    分段文件搬移失败：{exc}")
        try:
            shutil.rmtree(source, ignore_errors=True)
        except OSError:
            pass

    # ---- 进程执行 -------------------------------------------------
    def _execute(self, cmd: list[str], span_total: float, span_from: float, span_to: float) -> tuple[int, str]:
        """跑一条 ffmpeg 命令，把进度映射到 [span_from, span_to] 区间。"""
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        stderr_lines: list[str] = []
        collector = threading.Thread(target=self._drain, args=(self._proc.stderr, stderr_lines), daemon=True)
        collector.start()

        for line in self._proc.stdout:
            line = line.strip()
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                try:
                    # ffmpeg 的 out_time_ms 实际单位是微秒
                    seconds = float(line.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    continue
                if span_total > 0:
                    frac = min(1.0, seconds / span_total)
                    self.on_progress(span_from + (span_to - span_from) * frac)
            elif line == "progress=end":
                break

        code = self._proc.wait()
        collector.join(timeout=2)
        self._proc = None
        return code, "\n".join(stderr_lines[-8:]).strip()

    @staticmethod
    def _drain(stream, sink: list[str]) -> None:
        if stream is None:
            return
        for line in stream:
            line = line.rstrip()
            if line:
                sink.append(line)
