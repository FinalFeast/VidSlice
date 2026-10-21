"""定位随程序分发的资源文件（图标、ffmpeg 等）。

打包成单文件 exe 后（PyInstaller），这些文件会被解压到 ``sys._MEIPASS``，
所以冻结运行时必须先到那里找，然后才回退到可执行文件所在目录和项目根目录。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def resource_dirs() -> list[Path]:
    """按优先级返回资源可能所在的根目录。"""
    dirs: list[Path] = []
    if getattr(sys, "frozen", False):
        bundle = getattr(sys, "_MEIPASS", None)
        if bundle:
            dirs.append(Path(bundle))
        dirs.append(Path(sys.executable).resolve().parent)
    dirs.append(PROJECT_ROOT)
    return dirs


def find_resource(*parts: str) -> Path | None:
    """在候选根目录下寻找 ``parts`` 组成的相对路径，找不到返回 None。"""
    for root in resource_dirs():
        candidate = root.joinpath(*parts)
        if candidate.exists():
            return candidate
    return None
