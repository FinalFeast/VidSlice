"""时间段：解析、格式化、等分生成。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Segment:
    """一个待切割的片段，单位为秒。"""

    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def parse_time(text: str | None) -> float:
    """把 ``90``、``1:30``、``00:01:30.5`` 这类写法统一转成秒。"""
    if text is None:
        raise ValueError("时间不能为空")
    raw = str(text).strip().replace("：", ":")
    if not raw:
        raise ValueError("时间不能为空")
    parts = raw.split(":")
    if len(parts) > 3:
        raise ValueError(f"无法识别的时间：{text}")
    try:
        values = [float(p) for p in parts]
    except ValueError:
        raise ValueError(f"无法识别的时间：{text}") from None
    seconds = 0.0
    for value in values:
        seconds = seconds * 60 + value
    if seconds < 0:
        raise ValueError("时间不能为负数")
    return seconds


def format_clock(seconds: float) -> str:
    """``00:01:30.250``，用于输入框回填。"""
    seconds = max(0.0, float(seconds))
    total_ms = int(round(seconds * 1000))
    hours, rest = divmod(total_ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def format_short(seconds: float) -> str:
    """``01:30`` 或 ``1:01:30``，用于列表展示。"""
    total = int(round(max(0.0, float(seconds))))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def equal_segments(start: float, end: float, length: float) -> list[Segment]:
    """在 ``[start, end)`` 区间内，按固定时长切成若干段。"""
    if length <= 0:
        raise ValueError("每段时长必须大于 0")
    if end <= start:
        raise ValueError("结束时间必须大于开始时间")
    result: list[Segment] = []
    cursor = start
    while cursor < end - 1e-6:
        result.append(Segment(cursor, min(cursor + length, end)))
        cursor += length
    return result
