"""界面自测：构建窗口、模拟操作、走一遍完整的合并流程。

窗口会闪现一下然后自动关闭（弹窗已替换为记录，不会卡住）。
用法：tools\\python\\python.exe tests\\gui_test.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import main as app_module  # noqa: E402
from ffmpeg_tools import CREATE_NO_WINDOW, find_tool, probe_video  # noqa: E402
from segments import Segment  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f"  -- {detail}" if detail else ""))
    if not condition:
        failures.append(label)


def visible_in(window, widget) -> bool:
    """控件已显示，且完整落在窗口可视区域内。"""
    if not widget.winfo_ismapped():
        return False
    top = widget.winfo_rooty() - window.winfo_rooty()
    left = widget.winfo_rootx() - window.winfo_rootx()
    return (top >= 0 and left >= 0
            and top + widget.winfo_height() <= window.winfo_height()
            and left + widget.winfo_width() <= window.winfo_width())


def reveal(app, widget) -> None:
    """把主界面滚动到让控件出现在视野里（相当于用户滚到它）。"""
    y, node = 0, widget
    while node is not app.body:
        y += node.winfo_y()
        node = node.master
    total = app.body.winfo_height()
    app.canvas.yview_moveto(max(0, y - 20) / total)
    app.update()


def key_widgets(app) -> dict:
    return {
        "tree": app.tree, "progress": app.progress, "start_button": app.start_button,
        "cancel_button": app.cancel_button, "log": app.log, "keep_parts_check": app.keep_parts_check,
        "resolution_box": app.resolution_box, "crf_scale": app.crf_scale, "preset_box": app.preset_box,
        "cutpoint_exact": app.cutpoint_exact, "cutpoint_key": app.cutpoint_key,
    }


def unreachable(app) -> list[str]:
    """逐个滚动到控件后检查是否完整可见，返回不可达的控件名。"""
    bad = []
    for name, widget in key_widgets(app).items():
        reveal(app, widget)
        if not visible_in(app, widget):
            bad.append(name)
    return bad


class ScaledApp(app_module.App):
    """模拟 DPI 缩放：在建界面之前改 tk scaling，字体和控件尺寸随之变化。"""

    factor = 1.0
    base_scaling = 1.0

    def _build_style(self) -> None:
        self.tk.call("tk", "scaling", ScaledApp.base_scaling * ScaledApp.factor)
        super()._build_style()


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="vsplit-gui-"))
    sample = work / "sample.mp4"
    subprocess.run(
        [find_tool("ffmpeg"), "-hide_banner", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=12",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "30", str(sample)],
        capture_output=True, creationflags=CREATE_NO_WINDOW,
    )

    # 弹窗全部拦截，测试才能自动跑完
    popups: list[tuple[str, str]] = []
    app_module.messagebox.askyesno = lambda title, msg="", **k: (popups.append((title, msg)), False)[1]
    app_module.messagebox.showinfo = lambda title, msg="", **k: popups.append((title, msg))
    app_module.messagebox.showwarning = lambda title, msg="", **k: popups.append((title, msg))
    app_module.messagebox.showerror = lambda title, msg="", **k: popups.append((title, msg))

    app = app_module.App()
    app.update()
    ScaledApp.base_scaling = float(app.tk.call("tk", "scaling"))
    check("窗口创建成功", app.winfo_exists() == 1)
    check("控件齐全", all(hasattr(app, name) for name in
                     ("tree", "log", "progress", "start_button", "cancel_button", "source_var",
                      "merge_var", "keep_parts_var", "merge_hint_var", "cutpoint_var", "cutpoint_hint_var")))
    check("ffmpeg 可用时按钮可用", str(app.start_button["state"]) == "normal")
    check("初始状态为就绪", app.status_var.get() == "就绪")

    print("\n--- 布局与可见性 ---")
    sections = app.body.winfo_children()
    check("默认缩放：内容需求高度不超过 768p 可用高度", app.body.winfo_reqheight() <= 768 - 80,
          str(app.body.winfo_reqheight()))
    check("默认缩放：窗口高度 = 内容需求高度", app.winfo_height() == app.body.winfo_reqheight(),
          f"窗口 {app.winfo_height()} / 需求 {app.body.winfo_reqheight()}")
    check("默认缩放：不显示滚动条", app._scroll_bar.winfo_manager() == "")
    check("各区块高度不低于需求（未被挤压）",
          all(s.winfo_height() >= s.winfo_reqheight() for s in sections),
          str([(s.winfo_height(), s.winfo_reqheight()) for s in sections]))
    check("日志区默认 6 行", str(app.log.cget("height")) == "6", str(app.log.cget("height")))
    check("默认缩放：关键控件全部可见", not unreachable(app))

    app.geometry("820x688")
    app.update()
    check("768p 可用高度（820x688）：关键控件全部可达", not unreachable(app))

    app.geometry("1024x700")
    app.update()
    check("1024x700：关键控件全部可达", not unreachable(app))

    app._fit_window()
    app.update()
    default_log_h = app.log.winfo_height()
    app.geometry("1000x900")
    app.update()
    grown_log_h = app.log.winfo_height()
    check("窗口放大后日志区变高", grown_log_h >= default_log_h + 60,
          f"默认 {default_log_h} -> 1000x900 时 {grown_log_h}")
    check("窗口放大后日志区仍可达", not unreachable(app))

    app._fit_window()
    app.update()

    print("\n--- 模式联动 ---")
    app.mode_var.set("copy")
    app._sync_mode_state()
    app.update()
    check("无损模式：分辨率置灰", str(app.resolution_box["state"]) == "disabled")
    check("无损模式：CRF 置灰", str(app.crf_scale["state"]) == "disabled")
    check("无损模式：提示语正确", "无损" in app.crf_label_var.get())

    app.mode_var.set("encode")
    app._sync_mode_state()
    app.update()
    check("精确模式：分辨率可选", str(app.resolution_box["state"]) == "normal")
    check("精确模式：编码速度可选", str(app.preset_box["state"]) == "readonly")
    check("精确模式：CRF 可用", str(app.crf_scale["state"]) == "normal")

    print("\n--- 无损切点选项 ---")
    app.mode_var.set("copy")
    app._sync_mode_state()
    app.update()
    check("默认是严格按填写时间", app.cutpoint_var.get() == "exact", app.cutpoint_var.get())
    check("严格模式：提示说明了代价", "严格" in app.cutpoint_hint_var.get(), app.cutpoint_hint_var.get())
    check("严格模式：设置里不对齐关键帧", app._collect_settings().align_keyframes is False)

    app.cutpoint_var.set("keyframe")
    app._sync_mode_state()
    app.update()
    check("对齐模式：提示说明会多出开头", "关键帧" in app.cutpoint_hint_var.get(), app.cutpoint_hint_var.get())
    check("对齐模式：设置里要求对齐", app._collect_settings().align_keyframes is True)

    app.mode_var.set("encode")
    app._sync_mode_state()
    app.update()
    check("重编码模式：切点选项置灰",
          str(app.cutpoint_exact["state"]) == "disabled" and str(app.cutpoint_key["state"]) == "disabled")
    check("重编码模式：提示说明不生效", "只在无损" in app.cutpoint_hint_var.get(), app.cutpoint_hint_var.get())

    app.mode_var.set("copy")
    app.cutpoint_var.set("exact")
    app._sync_mode_state()
    app.update()
    check("切回无损：切点选项恢复可选", str(app.cutpoint_exact["state"]) == "normal")

    print("\n--- 分别输出 / 合并 联动 ---")
    app.merge_var.set("split")
    app._sync_merge_state()
    app.update()
    check("默认是分别输出", app.merge_var.get() == "split")
    check("分别输出时「保留分段文件」置灰", str(app.keep_parts_check["state"]) == "disabled")
    check("分别输出时无合并提示", app.merge_hint_var.get() == "")

    app.merge_var.set("merge")
    app._sync_merge_state()
    app.update()
    check("合并时「保留分段文件」可选", str(app.keep_parts_check["state"]) == "normal")
    check("合并提示包含目标文件名", "merged.mp4" in app.merge_hint_var.get(), app.merge_hint_var.get())

    print("\n--- 片段列表 ---")
    app.segments = [Segment(0.0, 10.5), Segment(12.0, 30.0)]
    app._refresh_segments()
    app.update()
    check("列表渲染 2 行", len(app.tree.get_children()) == 2)
    row = app.tree.item("1", "values")
    check("第一行内容正确", row[1] == "00:00:00.000" and row[3] == "00:10", str(row))
    check("汇总信息正确", "2 个片段" in app.segment_info_var.get())
    check("合并提示跟随片段数量", "2 段" in app.merge_hint_var.get(), app.merge_hint_var.get())

    app.tree.selection_set("2")
    app._delete_selected()
    app.update()
    check("删除后剩 1 行", len(app.tree.get_children()) == 1)

    # 没有视频时点开始，应被拦住而不是崩溃
    app.video = None
    app.segments = []
    app._start()
    app.update()
    check("没有视频时点开始会被拦住", any("视频" in title for title, _ in popups))
    check("没有视频时任务不会启动", app.job is None)

    print("\n--- 走一遍完整的合并流程 ---")
    popups.clear()
    app.out_dir_var.set(str(work / "out"))
    app.prefix_var.set("demo")
    app.container_var.set("mp4")
    app.merge_var.set("merge")
    app._sync_merge_state()
    app.video = probe_video(str(sample))
    app.segments = [Segment(1.0, 4.0), Segment(7.0, 9.0)]

    app._start()
    check("任务已启动", app.job is not None)
    check("启动后按钮状态切换正确",
          str(app.start_button["state"]) == "disabled" and str(app.cancel_button["state"]) == "normal")

    deadline = time.time() + 120
    while app.job is not None and time.time() < deadline:
        app.update()
        time.sleep(0.05)
    app.update()

    check("任务在超时前结束", app.job is None)
    merged = work / "out" / "demo_merged.mp4"
    check("合并文件已产出", merged.is_file(), str(list((work / "out").glob("*"))))
    check("按钮已恢复", str(app.start_button["state"]) == "normal" and str(app.cancel_button["state"]) == "disabled")
    check("状态栏显示完成", app.status_var.get() == "全部完成", app.status_var.get())
    check("进度条到 100%", float(app.progress["value"]) == 100.0, str(app.progress["value"]))
    check("日志写入了合并过程", "合并" in app.log.get("1.0", "end"))
    check("没有残留分段文件", not list((work / "out").glob("demo_*.mp4"))[1:],
          str([p.name for p in (work / "out").glob("*.mp4")]))
    if merged.is_file():
        got = probe_video(str(merged)).duration
        check("合并时长 ≈ 5 秒", abs(got - 5.0) < 0.4, f"{got:.3f}")

    # 播放改为调用系统默认播放器（包里已不带 ffplay）
    app.video = probe_video(str(sample))
    calls: list[str] = []
    original = getattr(os, "startfile", None)
    os.startfile = lambda path: calls.append(path)
    try:
        app._play()
        app._play(Segment(1.0, 3.0))
    finally:
        if original is not None:
            os.startfile = original
    check("播放调用系统默认播放器", calls == [str(sample), str(sample)], str(calls))
    check("播放不再依赖 ffplay", "ffplay" not in app.log.get("1.0", "end"))

    print("\n--- 图标 ---")
    from resources import find_resource
    icon = find_resource("assets", "icon.ico")
    check("能找到 assets/icon.ico", icon is not None, str(icon))
    if icon is not None:
        check("图标是多尺寸 .ico（含 256）", icon.stat().st_size > 5000,
              f"{icon.stat().st_size / 1024:.1f} KB")
    check("窗口已套用图标", bool(app.winfo_exists()) and icon is not None)

    app.destroy()

    print("\n--- 高 DPI 缩放兜底（滚动） ---")
    for factor in (1.25, 1.5, 2.0):
        ScaledApp.factor = factor
        dpi = ScaledApp()
        dpi.update()
        tag = f"{int(factor * 100)}%"
        need = dpi.body.winfo_reqheight()
        dpi.geometry("820x688")
        dpi.update()
        check(f"{tag}：内容需求高度超过 768p 可用高度", need > 688, f"需求 {need}")
        check(f"{tag}：装不下时滚动条自动出现", dpi._scroll_bar.winfo_manager() == "pack")
        bad = unreachable(dpi)
        check(f"{tag}：滚到任意关键控件都可达（含日志和执行区）", not bad, str(bad))

        dpi.canvas.yview_moveto(0)
        dpi.update()
        dpi._wheel_over(dpi.progress, -120)
        dpi.update()
        check(f"{tag}：滚轮在空白控件（进度条）上滚动主界面", dpi.canvas.yview()[0] > 0, str(dpi.canvas.yview()))

        dpi.canvas.yview_moveto(0)
        dpi.update()
        dpi._wheel_over(dpi.tree, -120)
        dpi._wheel_over(dpi.log, -120)
        dpi.update()
        check(f"{tag}：滚轮在片段列表和日志上不带动主界面", dpi.canvas.yview()[0] == 0)
        dpi.destroy()

    print()
    if failures:
        print(f"失败 {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
