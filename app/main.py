"""视频分割小工具 —— 图形界面入口。

从源视频中切出若干片段：手动添加时间段，或按固定时长等分生成。
"""

from __future__ import annotations

import os
import queue
import subprocess
import tempfile
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from ffmpeg_tools import CREATE_NO_WINDOW, FFmpegNotFound, VideoInfo, find_tool, probe_video
from resources import find_resource
from segments import Segment, equal_segments, format_clock, format_short, parse_time
from splitter import OutputSettings, SplitJob, build_tasks, scale_filter

APP_TITLE = "视频分割小工具"
RESOLUTION_VALUES = ["保持原始", "1080p", "720p", "480p", "360p"]
FPS_VALUES = ["保持原始", "24", "25", "30", "50", "60"]
PRESET_VALUES = ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow"]
CONTAINER_VALUES = ["mp4", "mkv", "mov"]


class SegmentDialog(tk.Toplevel):
    """添加 / 编辑片段的输入框。"""

    def __init__(self, master, title: str, start: str = "", end: str = "", max_duration: float = 0.0):
        super().__init__(master)
        self.title(title)
        self.resizable(False, False)
        self.transient(master)
        self.result: tuple[float, float] | None = None
        self.max_duration = max_duration

        body = ttk.Frame(self, padding=14)
        body.grid(row=0, column=0, sticky="nsew")

        ttk.Label(body, text="开始时间（HH:MM:SS，也可直接填秒数）").grid(row=0, column=0, sticky="w")
        self.start_var = tk.StringVar(value=start)
        ttk.Entry(body, textvariable=self.start_var, width=28).grid(row=1, column=0, sticky="ew", pady=(2, 10))

        ttk.Label(body, text="结束时间").grid(row=2, column=0, sticky="w")
        self.end_var = tk.StringVar(value=end)
        ttk.Entry(body, textvariable=self.end_var, width=28).grid(row=3, column=0, sticky="ew", pady=(2, 10))

        if max_duration > 0:
            ttk.Label(
                body, text=f"视频总时长：{format_clock(max_duration)}", foreground="#666666"
            ).grid(row=4, column=0, sticky="w", pady=(0, 10))

        buttons = ttk.Frame(body)
        buttons.grid(row=5, column=0, sticky="e")
        ttk.Button(buttons, text="取消", command=self.destroy).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(buttons, text="确定", command=self._confirm).grid(row=0, column=1)

        self.bind("<Return>", lambda _event: self._confirm())
        self.bind("<Escape>", lambda _event: self.destroy())
        self.grab_set()
        self.after(50, self.focus_force)
        self.wait_window(self)

    def _confirm(self) -> None:
        try:
            start = parse_time(self.start_var.get())
            end = parse_time(self.end_var.get())
        except ValueError as exc:
            messagebox.showerror("时间格式不对", str(exc), parent=self)
            return
        if end <= start:
            messagebox.showerror("时间不对", "结束时间必须大于开始时间。", parent=self)
            return
        if self.max_duration and start >= self.max_duration:
            messagebox.showerror("超出范围", "开始时间已经超过视频总时长。", parent=self)
            return
        self.result = (start, end)
        self.destroy()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)

        self.video: VideoInfo | None = None
        self.segments: list[Segment] = []
        self.job: SplitJob | None = None
        # 后台线程只往队列里放事件，由主线程取出来更新界面（Tkinter 不是线程安全的）
        self._events: queue.Queue = queue.Queue()

        self._build_style()
        self._build_scroll_body()
        self._build_source_section()
        self._build_segment_section()
        self._build_output_section()
        self._build_run_section()
        self._fit_window()
        self._apply_icon()
        self._check_ffmpeg()
        self._pump_job = self.after(80, self._pump)

    # ---- 界面搭建 -------------------------------------------------
    def _build_scroll_body(self) -> None:
        """界面内容放进可垂直滚动的容器：装得下时不显示滚动条，装不下时自动出现。"""
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0, yscrollincrement=24)
        self._scroll_bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self._scroll_bar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.body = ttk.Frame(self.canvas)
        self._body_id = self.canvas.create_window(0, 0, window=self.body, anchor="nw")
        self.body.bind("<Configure>", lambda _event: self._sync_scroll())
        self.canvas.bind("<Configure>", lambda _event: self._sync_scroll())
        self.bind_all("<MouseWheel>", self._on_mousewheel)

    def _sync_scroll(self) -> None:
        need = self.body.winfo_reqheight()
        view_h = self.canvas.winfo_height()
        width = self.canvas.winfo_width()
        self.canvas.itemconfigure(self._body_id, width=width, height=max(view_h, need))
        self.canvas.configure(scrollregion=(0, 0, width, max(view_h, need)))
        shown = self._scroll_bar.winfo_manager() == "pack"
        if need > view_h and not shown:
            self._scroll_bar.pack(side="right", fill="y", before=self.canvas)
        elif need <= view_h and shown:
            self._scroll_bar.pack_forget()

    def _on_mousewheel(self, event) -> None:
        self._wheel_over(self.winfo_containing(event.x_root, event.y_root), event.delta)

    def _wheel_over(self, widget, delta: int) -> None:
        if widget is None or widget.winfo_toplevel() is not self:
            return  # 弹窗或其他窗口打开时，不滚动主界面
        if isinstance(widget, (ttk.Treeview, tk.Text)):
            return  # 列表和日志在自己上面滚动时，由它们自己处理
        if self._scroll_bar.winfo_manager() == "pack":
            self.canvas.yview_scroll(-1 if delta > 0 else 1, "units")

    def _fit_window(self) -> None:
        """窗口高度取内容需求高度（不超过屏幕可用高度）；装不下时由滚动条兜底。"""
        self.update_idletasks()
        need = self.body.winfo_reqheight()
        height = min(need, self.winfo_screenheight() - 80)
        self.minsize(760, 480)
        self.geometry(f"820x{height}")

    def _build_style(self) -> None:
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Hint.TLabel", foreground="#666666")
        style.configure("Title.TLabel", font=("", 10, "bold"))

    def _build_source_section(self) -> None:
        frame = ttk.LabelFrame(self.body, text=" 1. 源视频 ", padding=(10, 6))
        frame.pack(fill="x", padx=12, pady=(8, 4))

        row = ttk.Frame(frame)
        row.pack(fill="x")
        self.source_var = tk.StringVar()
        entry = ttk.Entry(row, textvariable=self.source_var)
        entry.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="浏览…", command=self._choose_source).pack(side="left", padx=(6, 0))
        self.play_button = ttk.Button(row, text="播放", command=lambda: self._play())
        self.play_button.pack(side="left", padx=(6, 0))

        self.source_info_var = tk.StringVar(value="尚未选择视频文件")
        ttk.Label(frame, textvariable=self.source_info_var, style="Hint.TLabel").pack(
            anchor="w", pady=(4, 0)
        )

    def _build_segment_section(self) -> None:
        frame = ttk.LabelFrame(self.body, text=" 2. 片段时间段 ", padding=(10, 4))
        frame.pack(fill="both", expand=True, padx=12, pady=4)

        table = ttk.Frame(frame)
        table.pack(fill="both", expand=True)

        columns = ("index", "start", "end", "duration")
        self.tree = ttk.Treeview(table, columns=columns, show="headings", height=3, selectmode="extended")
        headings = {"index": ("#", 50), "start": ("开始", 130), "end": ("结束", 130), "duration": ("时长", 100)}
        for key, (text, width) in headings.items():
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, anchor="center")
        self.tree.pack(side="left", fill="both", expand=True)

        scrollbar = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        scrollbar.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind("<Double-1>", lambda _event: self._play_selected())

        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=(6, 0))
        ttk.Button(actions, text="添加片段", command=self._add_segment).pack(side="left")
        ttk.Button(actions, text="编辑选中", command=self._edit_selected).pack(side="left", padx=(6, 0))
        ttk.Button(actions, text="删除选中", command=self._delete_selected).pack(side="left", padx=(6, 0))
        ttk.Button(actions, text="清空", command=self._clear_segments).pack(side="left", padx=(6, 0))

        self.segment_info_var = tk.StringVar(value="共 0 个片段")
        ttk.Label(actions, textvariable=self.segment_info_var, style="Hint.TLabel").pack(side="right")

        equal = ttk.LabelFrame(frame, text=" 按固定时长等分 ", padding=(8, 2))
        equal.pack(fill="x", pady=(6, 0))

        line = ttk.Frame(equal)
        line.pack(fill="x")
        ttk.Label(line, text="每段时长").pack(side="left")
        self.equal_length_var = tk.StringVar(value="60")
        ttk.Entry(line, textvariable=self.equal_length_var, width=6).pack(side="left", padx=(4, 4))
        ttk.Label(line, text="秒").pack(side="left")
        ttk.Button(line, text="生成片段（覆盖现有列表）", command=self._generate_equal).pack(side="left", padx=(12, 16))
        ttk.Label(line, text="范围").pack(side="left")
        self.equal_start_var = tk.StringVar()
        ttk.Entry(line, textvariable=self.equal_start_var, width=9).pack(side="left", padx=(4, 4))
        ttk.Label(line, text="到").pack(side="left")
        self.equal_end_var = tk.StringVar()
        ttk.Entry(line, textvariable=self.equal_end_var, width=9).pack(side="left", padx=(4, 4))
        ttk.Label(line, text="（留空表示整段视频）", style="Hint.TLabel").pack(side="left")

    def _build_output_section(self) -> None:
        frame = ttk.LabelFrame(self.body, text=" 3. 输出设置 ", padding=(10, 4))
        frame.pack(fill="x", padx=12, pady=4)

        mode_row = ttk.Frame(frame)
        mode_row.pack(fill="x")
        ttk.Label(mode_row, text="切割方式").pack(side="left")
        self.mode_var = tk.StringVar(value="copy")
        ttk.Radiobutton(
            mode_row, text="无损快速（只重编开头一小段）",
            variable=self.mode_var, value="copy", command=self._sync_mode_state,
        ).pack(side="left", padx=(8, 0))
        ttk.Radiobutton(
            mode_row, text="精确重编码", variable=self.mode_var, value="encode",
            command=self._sync_mode_state,
        ).pack(side="left", padx=(8, 0))
        ttk.Label(mode_row, text="无损切点").pack(side="left", padx=(18, 0))
        self.cutpoint_var = tk.StringVar(value="exact")
        self.cutpoint_exact = ttk.Radiobutton(
            mode_row, text="严格按填写时间", variable=self.cutpoint_var,
            value="exact", command=self._sync_mode_state,
        )
        self.cutpoint_exact.pack(side="left", padx=(8, 0))
        self.cutpoint_key = ttk.Radiobutton(
            mode_row, text="向前对齐关键帧", variable=self.cutpoint_var,
            value="keyframe", command=self._sync_mode_state,
        )
        self.cutpoint_key.pack(side="left", padx=(8, 0))

        options = ttk.Frame(frame)
        options.pack(fill="x", pady=(6, 0))
        ttk.Label(options, text="分辨率").grid(row=0, column=0, sticky="w")
        self.resolution_var = tk.StringVar(value="保持原始")
        self.resolution_box = ttk.Combobox(
            options, textvariable=self.resolution_var, values=RESOLUTION_VALUES, width=12
        )
        self.resolution_box.grid(row=0, column=1, sticky="w", padx=(6, 18))

        ttk.Label(options, text="帧率").grid(row=0, column=2, sticky="w")
        self.fps_var = tk.StringVar(value="保持原始")
        self.fps_box = ttk.Combobox(options, textvariable=self.fps_var, values=FPS_VALUES, width=10)
        self.fps_box.grid(row=0, column=3, sticky="w", padx=(6, 18))

        ttk.Label(options, text="编码速度").grid(row=0, column=4, sticky="w")
        self.preset_var = tk.StringVar(value="veryfast")
        self.preset_box = ttk.Combobox(
            options, textvariable=self.preset_var, values=PRESET_VALUES, width=11, state="readonly"
        )
        self.preset_box.grid(row=0, column=5, sticky="w", padx=(6, 0))

        quality_row = ttk.Frame(frame)
        quality_row.pack(fill="x", pady=(6, 0))
        ttk.Label(quality_row, text="画质 CRF").pack(side="left")
        self.crf_var = tk.IntVar(value=23)
        self.crf_scale = ttk.Scale(
            quality_row, from_=16, to=32, orient="horizontal",
            variable=self.crf_var, command=lambda _v: self._update_crf_label(),
        )
        self.crf_scale.pack(side="left", padx=(6, 6), fill="x", expand=True)
        self.crf_label_var = tk.StringVar(value="23（数值越小画质越好、体积越大）")
        ttk.Label(quality_row, textvariable=self.crf_label_var).pack(side="left")

        dest_row = ttk.Frame(frame)
        dest_row.pack(fill="x", pady=(6, 0))
        ttk.Label(dest_row, text="输出目录").pack(side="left")
        self.out_dir_var = tk.StringVar()
        ttk.Entry(dest_row, textvariable=self.out_dir_var).pack(side="left", fill="x", expand=True, padx=(6, 6))
        ttk.Button(dest_row, text="浏览…", command=self._choose_out_dir).pack(side="left")
        ttk.Button(dest_row, text="打开", command=self._open_out_dir).pack(side="left", padx=(6, 0))

        merge_row = ttk.Frame(frame)
        merge_row.pack(fill="x", pady=(6, 0))
        ttk.Label(merge_row, text="输出方式").pack(side="left")
        self.merge_var = tk.StringVar(value="split")
        ttk.Radiobutton(
            merge_row, text="分别输出", variable=self.merge_var,
            value="split", command=self._sync_merge_state,
        ).pack(side="left", padx=(8, 0))
        ttk.Radiobutton(
            merge_row, text="合并成一个视频", variable=self.merge_var,
            value="merge", command=self._sync_merge_state,
        ).pack(side="left", padx=(8, 0))
        self.keep_parts_var = tk.BooleanVar(value=False)
        self.keep_parts_check = ttk.Checkbutton(
            merge_row, text="同时保留分段文件", variable=self.keep_parts_var
        )
        self.keep_parts_check.pack(side="left", padx=(8, 0))
        ttk.Label(merge_row, text="文件名前缀").pack(side="left", padx=(18, 0))
        self.prefix_var = tk.StringVar(value="clip")
        self.prefix_var.trace_add("write", lambda *_: self._sync_merge_state())
        ttk.Entry(merge_row, textvariable=self.prefix_var, width=14).pack(side="left", padx=(6, 0))
        ttk.Label(merge_row, text="格式").pack(side="left", padx=(12, 0))
        self.container_var = tk.StringVar(value="mp4")
        ttk.Combobox(
            merge_row, textvariable=self.container_var, values=CONTAINER_VALUES, width=6, state="readonly"
        ).pack(side="left", padx=(6, 0))

        self.merge_hint_var = tk.StringVar()
        ttk.Label(frame, textvariable=self.merge_hint_var, style="Hint.TLabel").pack(anchor="w", pady=(4, 0))

        self.cutpoint_hint_var = tk.StringVar()
        ttk.Label(frame, textvariable=self.cutpoint_hint_var, style="Hint.TLabel").pack(anchor="w")

        self._sync_mode_state()
        self._sync_merge_state()

    def _build_run_section(self) -> None:
        frame = ttk.Frame(self.body, padding=(12, 0, 12, 6))
        frame.pack(fill="both", expand=True)

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        self.start_button = ttk.Button(buttons, text="开始切割", command=self._start)
        self.start_button.pack(side="left")
        self.cancel_button = ttk.Button(buttons, text="取消", command=self._cancel, state="disabled")
        self.cancel_button.pack(side="left", padx=(8, 0))
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(buttons, textvariable=self.status_var, style="Hint.TLabel").pack(side="right")
        self.progress = ttk.Progressbar(buttons, mode="determinate", maximum=100)
        self.progress.pack(side="left", fill="x", expand=True, padx=(12, 12))

        log_frame = ttk.LabelFrame(frame, text=" 日志 ", padding=(6, 2))
        log_frame.pack(fill="both", expand=True, pady=(6, 0))
        self.log = tk.Text(log_frame, height=6, wrap="none", state="disabled", background="#fbfbfb")
        self.log.pack(side="left", fill="both", expand=True)
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        log_scroll.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=log_scroll.set)

    # ---- 状态辅助 -------------------------------------------------
    def _apply_icon(self) -> None:
        """设置窗口/任务栏图标；找不到图标文件时静默跳过。"""
        icon = find_resource("assets", "icon.ico")
        if icon is None:
            return
        try:
            self.iconbitmap(str(icon))
        except tk.TclError:
            pass

    def _check_ffmpeg(self) -> None:
        try:
            find_tool("ffmpeg")
            find_tool("ffprobe")
        except FFmpegNotFound as exc:
            messagebox.showerror("缺少 ffmpeg", str(exc))
            self.start_button.configure(state="disabled")

    def _sync_mode_state(self) -> None:
        """无损模式下，分辨率/帧率/CRF/编码速度都无意义，置灰。"""
        copy_mode = self.mode_var.get() == "copy"
        state = "disabled" if copy_mode else "normal"
        self.resolution_box.configure(state=state)
        self.fps_box.configure(state=state)
        self.preset_box.configure(state="disabled" if copy_mode else "readonly")
        self.crf_scale.configure(state=state)
        cut_state = "normal" if copy_mode else "disabled"
        self.cutpoint_exact.configure(state=cut_state)
        self.cutpoint_key.configure(state=cut_state)
        self._update_crf_label()
        self._update_cutpoint_hint()
        self._sync_merge_state()

    def _update_cutpoint_hint(self) -> None:
        if self.mode_var.get() != "copy":
            self.cutpoint_hint_var.set("「无损切点」只在无损快速模式下起作用")
        elif self.cutpoint_var.get() == "exact":
            self.cutpoint_hint_var.set("起点严格按填写的时间；不在关键帧上时只把开头一小段重新编码（智能切割）")
        else:
            self.cutpoint_hint_var.set("起点向前对齐到关键帧，整段不重新编码；开头最多多出一个关键帧间隔")

    def _update_crf_label(self) -> None:
        value = int(round(self.crf_var.get()))
        if self.mode_var.get() == "copy":
            self.crf_label_var.set("无损模式不使用该选项")
        else:
            self.crf_label_var.set(f"{value}（数值越小画质越好、体积越大）")

    def _sync_merge_state(self) -> None:
        """合并模式下提示最终文件名，并决定「保留分段文件」是否可选。"""
        merging = self.merge_var.get() == "merge"
        self.keep_parts_check.configure(state="normal" if merging else "disabled")
        if not merging:
            self.merge_hint_var.set("")
            return
        prefix = self.prefix_var.get().strip() or "clip"
        count = len(self.segments) or "若干"
        hint = f"先切成 {count} 段再拼接，最终输出 {prefix}_merged.{self.container_var.get()}"
        if self.mode_var.get() == "copy" and self.cutpoint_var.get() == "exact":
            hint += "；严格切点下无损拼接可能不准，校验不过会自动改用重编码"
        self.merge_hint_var.set(hint)

    def _log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _require_video(self) -> bool:
        if self.video is None:
            messagebox.showwarning("还没有选视频", "请先选择一个源视频文件。")
            return False
        return True

    # ---- 源视频 ---------------------------------------------------
    def _choose_source(self) -> None:
        path = filedialog.askopenfilename(
            title="选择源视频",
            filetypes=[
                ("视频文件", "*.mp4 *.mkv *.mov *.avi *.flv *.wmv *.webm *.ts *.m4v"),
                ("所有文件", "*.*"),
            ],
        )
        if not path:
            return
        self._load_video(path)

    def _load_video(self, path: str) -> None:
        self.status_var.set("正在读取视频信息…")
        self.update_idletasks()
        try:
            info = probe_video(path)
        except Exception as exc:
            self.status_var.set("就绪")
            messagebox.showerror("读取失败", str(exc))
            return

        self.video = info
        self.source_var.set(path)
        self.source_info_var.set(
            f"时长 {format_clock(info.duration)}　|　{info.resolution}　|　{info.fps_text} fps　|　"
            f"视频 {info.video_codec} / 音频 {info.audio_codec}"
        )
        if not self.out_dir_var.get():
            self.out_dir_var.set(str(Path(path).parent / "segments"))
        self.status_var.set("视频已载入")
        self._log(f"已载入：{Path(path).name}（{format_clock(info.duration)}）")

    def _play(self, segment: Segment | None = None) -> None:
        """用系统默认播放器打开源视频。

        为了把包体压到最小，不再内置 ffplay；因此无法只播放某一段，
        双击片段时只提示该片段的起点，由用户在自己的播放器里定位。
        """
        if not self._require_video():
            return
        if segment is not None:
            self._log(
                f"已用系统播放器打开源视频；该片段起点为 {format_clock(segment.start)}，"
                f"结束于 {format_clock(segment.end)}"
            )
        path = self.video.path
        try:
            if os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", path])
        except OSError as exc:
            messagebox.showerror("播放失败", f"无法打开视频：{exc}")

    def _play_selected(self) -> None:
        index = self._selected_index()
        if index is None:
            return
        self._play(self.segments[index])

    # ---- 片段管理 -------------------------------------------------
    def _selected_index(self) -> int | None:
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo("没有选中", "请先在列表里选中一个片段。")
            return None
        return int(selection[0]) - 1

    def _refresh_segments(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for position, segment in enumerate(self.segments, 1):
            self.tree.insert(
                "", "end", iid=str(position),
                values=(position, format_clock(segment.start), format_clock(segment.end), format_short(segment.duration)),
            )
        total = sum(s.duration for s in self.segments)
        self.segment_info_var.set(f"共 {len(self.segments)} 个片段，合计 {format_short(total)}")
        self._sync_merge_state()

    def _add_segment(self) -> None:
        if not self._require_video():
            return
        last_end = self.segments[-1].end if self.segments else 0.0
        dialog = SegmentDialog(
            self, "添加片段",
            start=format_clock(last_end),
            end=format_clock(min(last_end + 30, self.video.duration)),
            max_duration=self.video.duration,
        )
        if dialog.result:
            self.segments.append(Segment(*dialog.result))
            self._refresh_segments()

    def _edit_selected(self) -> None:
        index = self._selected_index()
        if index is None:
            return
        current = self.segments[index]
        dialog = SegmentDialog(
            self, "编辑片段",
            start=format_clock(current.start),
            end=format_clock(current.end),
            max_duration=self.video.duration if self.video else 0.0,
        )
        if dialog.result:
            self.segments[index] = Segment(*dialog.result)
            self._refresh_segments()

    def _delete_selected(self) -> None:
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo("没有选中", "请先在列表里选中要删除的片段。")
            return
        for iid in sorted((int(s) for s in selection), reverse=True):
            del self.segments[iid - 1]
        self._refresh_segments()

    def _clear_segments(self) -> None:
        if self.segments and messagebox.askyesno("清空", "确定要清空所有片段吗？"):
            self.segments.clear()
            self._refresh_segments()

    def _generate_equal(self) -> None:
        if not self._require_video():
            return
        try:
            length = parse_time(self.equal_length_var.get())
        except ValueError as exc:
            messagebox.showerror("时长不对", str(exc))
            return
        if length <= 0:
            messagebox.showerror("时长不对", "每段时长必须大于 0。")
            return

        try:
            start_text = self.equal_start_var.get().strip()
            end_text = self.equal_end_var.get().strip()
            start = parse_time(start_text) if start_text else 0.0
            end = parse_time(end_text) if end_text else self.video.duration
        except ValueError as exc:
            messagebox.showerror("范围不对", str(exc))
            return

        if self.video.duration and end > self.video.duration:
            end = self.video.duration
        try:
            generated = equal_segments(start, end, length)
        except ValueError as exc:
            messagebox.showerror("无法生成", str(exc))
            return
        if not generated:
            messagebox.showinfo("没有片段", "该范围内无法生成任何片段。")
            return

        self.segments = generated
        self._refresh_segments()
        self._log(f"已按每段 {format_short(length)} 生成 {len(generated)} 个片段。")

    # ---- 输出设置 -------------------------------------------------
    def _choose_out_dir(self) -> None:
        initial = self.out_dir_var.get() or str(Path.home())
        path = filedialog.askdirectory(title="选择输出目录", initialdir=initial)
        if path:
            self.out_dir_var.set(path)

    def _open_out_dir(self) -> None:
        path = self.out_dir_var.get().strip()
        if not path:
            messagebox.showinfo("没有目录", "还没有设置输出目录。")
            return
        target = Path(path)
        if not target.is_dir():
            messagebox.showinfo("目录不存在", f"目录还不存在：\n{path}\n切割完成后会自动创建。")
            return
        if os.name == "nt":
            os.startfile(target)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(target)])

    def _collect_settings(self) -> OutputSettings:
        return OutputSettings(
            mode=self.mode_var.get(),
            resolution=self.resolution_var.get(),
            fps=self.fps_var.get(),
            crf=int(round(self.crf_var.get())),
            preset=self.preset_var.get(),
            container=self.container_var.get(),
            out_dir=self.out_dir_var.get().strip(),
            prefix=self.prefix_var.get().strip(),
            align_keyframes=self.cutpoint_var.get() == "keyframe",
            merge=self.merge_var.get() == "merge",
            keep_parts=bool(self.keep_parts_var.get()),
        )

    # ---- 执行 -----------------------------------------------------
    def _start(self) -> None:
        if not self._require_video():
            return
        if not self.segments:
            messagebox.showwarning("没有片段", "请先添加或生成至少一个片段。")
            return

        settings = self._collect_settings()
        if not settings.out_dir:
            messagebox.showwarning("没有输出目录", "请先选择输出目录。")
            return
        try:
            if settings.mode == "encode":
                scale_filter(settings.resolution)
        except ValueError as exc:
            messagebox.showerror("参数不对", str(exc))
            return

        try:
            out_dir = Path(settings.out_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("无法创建输出目录", str(exc))
            return

        # 合并模式下先把片段切到临时目录，合并成功后再决定去留
        settings.parts_dir = ""
        target_dir = None
        if settings.merge:
            prefix = settings.prefix or "clip"
            settings.merged_path = str(out_dir / f"{prefix}_merged.{settings.container}")
            settings.parts_dir = tempfile.mkdtemp(prefix="vsplit-parts-")
            target_dir = settings.parts_dir

        try:
            tasks = build_tasks(self.video.path, self.segments, settings, target_dir=target_dir)
        except OSError as exc:
            messagebox.showerror("无法准备输出目录", str(exc))
            return

        # 合并模式只需确认最终文件是否会被覆盖，分段是临时的
        if settings.merge:
            paths = [Path(settings.merged_path)]
        else:
            paths = [Path(p) for _, _, p in tasks]
        existing = [p.name for p in paths if p.exists()]
        if existing:
            preview = "、".join(existing[:3]) + ("…" if len(existing) > 3 else "")
            if not messagebox.askyesno("文件已存在", f"以下文件会被覆盖：\n{preview}\n\n继续吗？"):
                return

        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        if settings.merge:
            self._log(f"开始切割 {len(tasks)} 个片段，然后合并为一个视频。")
        else:
            self._log(f"开始切割，共 {len(tasks)} 个片段，输出到 {settings.out_dir}")

        self.start_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.progress["value"] = 0
        self.status_var.set("正在切割…")

        self.job = SplitJob(
            src=self.video.path,
            tasks=tasks,
            settings=settings,
            on_log=lambda msg: self._events.put(("log", msg)),
            on_progress=lambda ratio: self._events.put(("progress", ratio)),
            on_finished=lambda ok, msg: self._events.put(("finished", (ok, msg))),
        )
        self.job.start()

    def _pump(self) -> None:
        """在主线程里把后台任务发来的事件取出来更新界面。"""
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "progress":
                    self._set_progress(payload)
                elif kind == "finished":
                    self._on_finished(*payload)
        except queue.Empty:
            pass
        except tk.TclError:
            return  # 窗口已销毁
        self._pump_job = self.after(80, self._pump)

    def destroy(self) -> None:
        if getattr(self, "_pump_job", None):
            self.after_cancel(self._pump_job)
        super().destroy()

    def _set_progress(self, ratio: float) -> None:
        self.progress["value"] = max(0.0, min(1.0, ratio)) * 100

    def _cancel(self) -> None:
        if self.job:
            self.status_var.set("正在取消…")
            self.job.cancel()

    def _on_finished(self, ok: bool, message: str) -> None:
        self.job = None
        self.start_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        self.status_var.set(message)
        self._log(message)
        if ok:
            self.progress["value"] = 100
            if messagebox.askyesno("完成", f"{message}\n\n要打开输出目录吗？"):
                self._open_out_dir()


def main() -> None:
    if os.name == "nt":
        try:
            from ctypes import windll

            windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
