"""Select a region/model; load configuration and rent automatically in that scope."""
from __future__ import annotations
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
import os
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
import webbrowser

from ..core import AutoDL, Candidate, Cancelled, Engine, QuickError, ROOT, Settings, atomic_json
from ..catalog import Catalog
from ..i18n import Translator
from .theme import COLORS, configure_theme, wrapped_label
from .display import configure_window, display_scale, enable_high_dpi, scale_layout

ALL_REGIONS = "全部公开地区"
ALL_GPUS = "全部 GPU 型号"
CUSTOM_REGIONS = "高级偏好中的多个地区 / 机房"
CUSTOM_GPUS = "高级偏好中的多个型号"


class App:
    def __init__(self, root, settings=None, demo=False, read_only=False, language="zh"):
        self.root, self.demo, self.read_only = root, demo, read_only
        self.language = language
        self.tr = Translator(language)
        self.all_regions_label = self.tr(ALL_REGIONS)
        self.all_gpus_label = self.tr(ALL_GPUS)
        self.custom_regions_label = self.tr(CUSTOM_REGIONS)
        self.custom_gpus_label = self.tr(CUSTOM_GPUS)
        self.preferences = settings or Settings()
        self.catalog = Catalog.load()
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.busy = self.closing = False
        self.rows = []
        self._timer = None
        self._stock_timer = None
        self._stock_generation = 0
        self.stock = None
        root.title(self.tr('AutoDL Quick · 自动找剩余卡') + (self.tr(' · 演示') if demo else self.tr(' · 只读测试') if read_only else ""))
        self.display_scale = display_scale(root)
        configure_window(root, 1240, 860, 1040, 740)
        root.configure(bg=COLORS["background"])
        root.protocol("WM_DELETE_WINDOW", self.close)
        configure_theme(root)
        self._build_interface()
        scale_layout(root)
        self.sync_selectors()
        self.clear_results()
        self.write_log(self.tr('按所选地区和型号自动找卡，绝不扩大范围。目录 {p0} 种 GPU；Token 不保存。', p0=len(self.catalog.gpu_names())))
        if demo:
            self.show_cards([
                Candidate("demo-host-4090", self.tr('西北B区（示例）'), "west-B", 2, "12.8", "RTX 4090", Decimal("2.5"), Decimal("2.38"), memory_gb=24, cpu_cores=16, ram_gb=80, cpu_name=self.tr('示例 CPU'), driver="580.82", disk_gb=50, disk_type="SSD", expand_disk_gb=self.preferences.expand_disk_gb, disk_expandable_gb=1000, disk_daily_price=Decimal("0.67") if self.preferences.expand_disk_gb else Decimal("0"), image_uuid="base-image-DEMO", image_label=self.tr('示例 PyTorch / CUDA 12.8'), image_runtime="12.8"),
                Candidate("demo-host-5090", self.tr('北京B区（示例）'), "bj-B1", 1, "12.8", "RTX 5090", Decimal("3.9"), Decimal("3.68"), memory_gb=32, cpu_cores=20, ram_gb=100, cpu_name=self.tr('示例 CPU'), driver="580.82", disk_gb=100, disk_type="SSD", expand_disk_gb=self.preferences.expand_disk_gb, disk_expandable_gb=1500, disk_daily_price=Decimal("0.67") if self.preferences.expand_disk_gb else Decimal("0"), image_uuid="base-image-DEMO", image_label=self.tr('示例 PyTorch / CUDA 12.8'), image_runtime="12.8"),
            ])
            self.status.set(self.tr('演示数据 · 完全不联网'))
        self._timer = root.after(100, self.drain)
        self._token_trace = self.token.trace_add("write", self.token_changed)
        self._disk_trace = self.disk.trace_add("write", self.disk_changed)
        if self.token.get().strip():
            self.schedule_stock()

    def _build_interface(self):
        wrap = ttk.Frame(self.root, padding=(24, 20, 24, 12))
        wrap.pack(fill="both", expand=True)
        wrap.columnconfigure(1, weight=1)
        wrap.rowconfigure(1, weight=1)

        header = ttk.Frame(wrap)
        header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 20))
        mark = tk.Canvas(header, width=46, height=46, background=COLORS["background"], highlightthickness=0)
        mark.pack(side="left", padx=(0, 14))
        mark.create_rectangle(8, 8, 38, 38, fill=COLORS["accent"], outline="")
        mark.create_rectangle(16, 16, 30, 30, outline="white", width=2)
        for position in (14, 23, 32):
            for coords in ((position, 3, position, 7), (position, 39, position, 43), (3, position, 7, position), (39, position, 43, position)):
                mark.create_line(*coords, fill=COLORS["accent"], width=2)
        titles = ttk.Frame(header)
        titles.pack(side="left")
        ttk.Label(titles, text=self.tr('AutoDL 自动找卡'), style="Heading.TLabel").pack(anchor="w")
        ttk.Label(titles, text=self.tr('选好算力，让工具替你等待。'), style="Muted.TLabel").pack(anchor="w", pady=(3, 0))
        ttk.Button(header, text=self.tr('打开市场'), command=lambda: webbrowser.open("https://www.autodl.com/market/list")).pack(side="right")
        mode = self.tr('离线演示') if self.demo else self.tr('只读模式') if self.read_only else self.tr('本地运行')
        ttk.Label(header, text=mode, style="Warning.Badge.TLabel" if self.demo or self.read_only else "Ready.Badge.TLabel").pack(side="right", padx=16)

        self.sidebar = ttk.Frame(wrap, style="Panel.TFrame", width=302)
        self.sidebar.grid(row=1, column=0, sticky="nsew", padx=(0, 18))
        self.sidebar.grid_propagate(False)
        self.sidebar.columnconfigure(0, weight=1)
        self.sidebar.rowconfigure(0, weight=1)
        # Scroll the controls on short displays and when Windows uses a larger text scale.
        self.settings_canvas = tk.Canvas(self.sidebar, bg=COLORS["surface"], highlightthickness=0, width=300)
        self.settings_canvas.grid(row=0, column=0, sticky="nsew", padx=1, pady=1)
        self.settings_scroll = ttk.Scrollbar(self.sidebar, command=self.settings_canvas.yview)
        self.settings_canvas.configure(yscrollcommand=self.settings_scroll.set)
        controls = self.settings_controls = ttk.Frame(self.settings_canvas, style="Surface.TFrame", padding=(18, 10))
        control_window = self.settings_canvas.create_window((0, 0), window=controls, anchor="nw")
        def resize_canvas(event):
            self.settings_canvas.itemconfigure(control_window, width=event.width)
            self._resize_settings()
        self.settings_canvas.bind("<Configure>", resize_canvas)
        controls.bind("<Configure>", self._resize_settings)
        self.root.bind("<MouseWheel>", self._scroll_settings, add="+")
        self.root.bind("<FocusIn>", self._focus_setting, add="+")
        self._build_settings(controls)
        self._build_actions(self.sidebar)

        self.workspace = ttk.Frame(wrap)
        self.workspace.grid(row=1, column=1, sticky="nsew")
        self.workspace.columnconfigure(0, weight=1)
        self.workspace.rowconfigure(0, weight=1)
        self._build_results(self.workspace)
        self._build_details(self.workspace)
        self._build_log(self.workspace)
        self.workspace.bind("<Configure>", self._resize_workspace)

        footer = ttk.Frame(wrap)
        footer.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Label(footer, text=self.tr('Token 仅留在窗口内存'), style="Muted.TLabel").pack(side="left")
        ttk.Label(footer, text=self.tr('每次一台单卡实例，租用成功即停止'), style="Muted.TLabel").pack(side="right")

    def _build_settings(self, controls):
        login = ttk.Frame(controls, style="Surface.TFrame")
        login.pack(fill="x", pady=(0, 6))
        ttk.Label(login, text=self.tr('网页 Token'), style="Surface.TLabel").pack(side="left")
        ttk.Button(login, text=self.tr('获取方法'), style="Quiet.TButton", command=self.token_help).pack(side="right")
        self.token = tk.StringVar(value="" if self.demo else os.environ.get("AUTODL_WEB_TOKEN", ""))
        self.token_entry = ttk.Entry(controls, textvariable=self.token, show="•")
        self.token_entry.pack(fill="x")
        ttk.Separator(controls).pack(fill="x", pady=14)
        scope_heading = ttk.Frame(controls, style="Surface.TFrame")
        scope_heading.pack(fill="x")
        ttk.Label(scope_heading, text=self.tr('租卡范围'), style="Title.Surface.TLabel").pack(side="left")
        self.advanced_button = ttk.Button(scope_heading, text=self.tr('高级偏好'), style="Quiet.TButton", command=self.advanced)
        self.advanced_button.pack(side="right")

        ttk.Label(controls, text=self.tr('地区'), style="Surface.TLabel").pack(anchor="w", pady=(9, 4))
        self.region = tk.StringVar()
        self.region_combo = ttk.Combobox(controls, textvariable=self.region, state="readonly", width=20)
        self.region_combo.pack(fill="x")
        self.region_combo.bind("<<ComboboxSelected>>", self.region_changed)
        ttk.Label(controls, text=self.tr('卡型号'), style="Surface.TLabel").pack(anchor="w", pady=(9, 4))
        self.gpu = tk.StringVar()
        self.gpu_combo = ttk.Combobox(controls, textvariable=self.gpu, state="readonly", width=20)
        self.gpu_combo.pack(fill="x")
        self.gpu_combo.bind("<<ComboboxSelected>>", self.gpu_changed)
        ttk.Label(controls, text=self.tr('扩容数据盘'), style="Surface.TLabel").pack(anchor="w", pady=(9, 4))
        disk_row = ttk.Frame(controls, style="Surface.TFrame")
        disk_row.pack(fill="x")
        self.disk = tk.StringVar(value=str(self.preferences.expand_disk_gb))
        self.disk_entry = ttk.Spinbox(disk_row, textvariable=self.disk, from_=0, to=1048576, increment=50, width=7)
        self.disk_entry.pack(side="left", fill="x", expand=True)
        ttk.Label(disk_row, text="GB", style="Muted.Surface.TLabel").pack(side="left", padx=(8, 2))
        self.disk_info = tk.StringVar(value=self.tr('新增 {p0} GB，叠加免费容量。\n扩容费用按日另计。', p0=self.preferences.expand_disk_gb) if self.preferences.expand_disk_gb else self.tr('0 表示不扩容，扩容费用按日另计。'))
        wrapped_label(controls, textvariable=self.disk_info, style="Muted.Surface.TLabel", wraplength=260).pack(fill="x", pady=(6, 10))
        self.stock_button = ttk.Button(controls, text=self.tr('刷新地区库存'), command=self.refresh_stock)
        self.stock_button.pack(fill="x")
        self.stock_info = tk.StringVar(value=self.tr('登录后读取库存；无余量的型号也可等待。'))
        wrapped_label(controls, textvariable=self.stock_info, style="Muted.Surface.TLabel", wraplength=260).pack(fill="x", pady=(8, 0))

    def _build_actions(self, parent):
        controls = ttk.Frame(parent, style="Surface.TFrame", padding=(18, 12, 18, 16))
        controls.grid(row=1, column=0, columnspan=2, sticky="ew", padx=1, pady=(0, 1))
        ttk.Separator(controls).pack(fill="x", pady=(0, 12))
        actions = ttk.Frame(controls, style="Surface.TFrame")
        actions.pack(fill="x", pady=(0, 10))
        actions.columnconfigure((0, 1), weight=1, uniform="action")
        self.scan_button = ttk.Button(actions, text=self.tr('扫描当前余量'), command=self.scan, padding=(4, 8))
        self.scan_button.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.wait_button = ttk.Button(actions, text=self.tr('持续自动找卡'), command=self.wait, padding=(4, 8))
        self.wait_button.grid(row=0, column=1, sticky="ew")
        self.rent_button = ttk.Button(controls, text=self.tr('开始自动租卡（计费）'), style="Accent.TButton", command=self.rent)
        self.rent_button.pack(fill="x")
        if self.read_only:
            self.rent_button.configure(state="disabled")
        wrapped_label(controls, text=self.tr('扫描与等待仅查询；自动租卡会产生费用。'), style="Muted.Surface.TLabel", wraplength=260).pack(fill="x", pady=(9, 0))

    def _resize_settings(self, _=None):
        self.settings_canvas.configure(scrollregion=self.settings_canvas.bbox("all"))
        content_height = self.settings_canvas.bbox("all")[3]
        if content_height > self.settings_canvas.winfo_height():
            self.settings_scroll.grid(row=0, column=1, sticky="ns", pady=1)
        else:
            self.settings_scroll.grid_remove()
            self.settings_canvas.yview_moveto(0)

    def _scroll_settings(self, event):
        widget = event.widget
        if not isinstance(widget, tk.Misc):
            return
        if widget.winfo_class() in ("TCombobox", "TSpinbox"):
            return
        while widget is not None:
            if widget == self.sidebar:
                if self.settings_canvas.yview() != (0.0, 1.0):
                    self.settings_canvas.yview_scroll(-int(event.delta / 120), "units")
                return
            widget = widget.master

    def _focus_setting(self, event):
        widget = event.widget
        if not isinstance(widget, tk.Misc):
            return
        while widget is not None:
            if widget == self.settings_controls:
                canvas = self.settings_canvas
                top = event.widget.winfo_rooty() - canvas.winfo_rooty()
                bottom = top + event.widget.winfo_height()
                offset = top - 8 if top < 8 else bottom - canvas.winfo_height() + 8 if bottom > canvas.winfo_height() - 8 else 0
                if offset:
                    content_height = canvas.bbox("all")[3]
                    canvas.yview_moveto(canvas.yview()[0] + offset / content_height)
                return
            widget = widget.master

    def _build_results(self, parent):
        results = ttk.Frame(parent, style="Panel.TFrame")
        results.grid(row=0, column=0, sticky="nsew")
        results.columnconfigure(0, weight=1)
        results.rowconfigure(2, weight=1)
        heading = ttk.Frame(results, style="Surface.TFrame", padding=(18, 14))
        heading.grid(row=0, column=0, sticky="ew")
        ttk.Label(heading, text=self.tr('可用主机'), style="Title.Surface.TLabel").pack(side="left")
        self.result_count = tk.StringVar(value=self.tr('等待扫描'))
        ttk.Label(heading, textvariable=self.result_count, style="Muted.Surface.TLabel").pack(side="left", padx=12)
        self.stop_button = ttk.Button(heading, text=self.tr('停止'), command=self.stop, state="disabled", style="Stop.TButton", padding=(12, 5))
        self.stop_button.pack(side="right", padx=(10, 0))
        self.activity = ttk.Label(heading, text=self.tr('就绪'), style="Neutral.Badge.TLabel")
        self.activity.pack(side="right")
        self.status = tk.StringVar(value=self.tr('只读测试：自动租卡按钮已禁用；正常使用请通过 start.cmd 打开。') if self.read_only else self.tr('选择地区与型号，然后扫描当前余量。'))
        wrapped_label(results, textvariable=self.status, style="Muted.Subtle.TLabel", padding=(18, 10), wraplength=740).grid(row=1, column=0, sticky="ew")
        self.table_wrap = ttk.Frame(results, style="Surface.TFrame")
        self.table_wrap.grid(row=2, column=0, sticky="nsew")
        self.table_wrap.columnconfigure(0, weight=1)
        self.table_wrap.rowconfigure(0, weight=1)
        columns = ("gpu", "region", "price", "vram", "cpu", "ram", "disk", "disk_price", "idle", "image")
        self.table = ttk.Treeview(self.table_wrap, columns=columns, displaycolumns=("gpu", "region", "price", "vram", "idle", "image", "cpu", "ram", "disk", "disk_price"), show="headings", height=4, selectmode="browse")
        for key, label, width in (("gpu", self.tr('剩余型号'), 155), ("region", self.tr('地区'), 130), ("price", self.tr('GPU 元/时'), 110),
                                  ("vram", self.tr('显存 GB'), 85), ("cpu", self.tr('CPU 核'), 80), ("ram", self.tr('内存 GB'), 90),
                                  ("disk", self.tr('数据盘 GB'), 125), ("disk_price", self.tr('数据盘 元/日'), 115),
                                  ("idle", self.tr('空闲卡'), 85), ("image", self.tr('自动镜像'), 115)):
            self.table.heading(key, text=label, anchor="w")
            self.table.column(key, width=width, minwidth=width, anchor="w", stretch=key == "gpu")
        scroll = ttk.Scrollbar(self.table_wrap, command=self.table.yview)
        horizontal = ttk.Scrollbar(self.table_wrap, orient="horizontal", command=self.table.xview)
        self.table.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self.table.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        self.table.tag_configure("even", background=COLORS["surface"])
        self.table.tag_configure("odd", background=COLORS["subtle"])
        self.table.bind("<<TreeviewSelect>>", self.show_selected)
        self.empty_state = ttk.Frame(self.table_wrap, style="Surface.TFrame", padding=16)
        self.empty_state.columnconfigure(0, weight=1)
        self.empty_title = tk.StringVar(value=self.tr('从一张合适的 GPU 开始'))
        self.empty_hint = tk.StringVar(value=self.tr('在左侧填写 Token，选好地区和型号，再点击“扫描当前余量”。'))
        ttk.Label(self.empty_state, textvariable=self.empty_title, style="Title.Surface.TLabel", anchor="center").pack(fill="x")
        wrapped_label(self.empty_state, textvariable=self.empty_hint, style="Muted.Surface.TLabel", wraplength=460, anchor="center", justify="center").pack(fill="x", pady=(10, 0))

    def _build_details(self, parent):
        self.detail_panel = ttk.Frame(parent, style="Panel.TFrame", padding=(18, 14))
        self.detail_panel.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        self.detail_panel.columnconfigure(0, weight=1)
        title = ttk.Frame(self.detail_panel, style="Surface.TFrame")
        title.grid(row=0, column=0, sticky="ew")
        ttk.Label(title, text=self.tr('主机配置'), style="Muted.Surface.TLabel").pack(side="left")
        self.copy_button = ttk.Button(title, text=self.tr('复制配置'), style="Quiet.TButton", command=self.copy_details, state="disabled")
        self.copy_button.pack(side="right")
        self.details = tk.StringVar(value=self.tr('发现剩余卡后，第一台的完整配置会自动显示在这里。'))
        self.detail_empty = wrapped_label(self.detail_panel, textvariable=self.details, style="Muted.Surface.TLabel", wraplength=700)
        self.detail_empty.grid(row=1, column=0, sticky="ew", pady=(10, 8))
        self.detail_content = ttk.Frame(self.detail_panel, style="Surface.TFrame")
        self.detail_content.columnconfigure(0, weight=1)
        top = ttk.Frame(self.detail_content, style="Surface.TFrame")
        top.grid(row=0, column=0, sticky="ew")
        self.host_title = tk.StringVar()
        ttk.Label(top, textvariable=self.host_title, style="Value.TLabel").pack(side="left")
        self.host_price = tk.StringVar()
        ttk.Label(top, textvariable=self.host_price, style="Price.TLabel").pack(side="right")
        self.host_subtitle = tk.StringVar()
        wrapped_label(self.detail_content, textvariable=self.host_subtitle, style="Muted.Surface.TLabel", wraplength=700).grid(row=1, column=0, sticky="ew", pady=(0, 10))
        specs = self.detail_specs = ttk.Frame(self.detail_content, style="Surface.TFrame")
        specs.grid(row=2, column=0, sticky="ew")
        self.spec_values = []
        for index, label in enumerate(('显存', 'CPU 核心', '内存', '空闲 GPU')):
            specs.columnconfigure(index, weight=1, uniform="spec")
            cell = ttk.Frame(specs, style="Subtle.TFrame", padding=(12, 8))
            cell.grid(row=0, column=index, sticky="ew", padx=(0, 6 if index < 3 else 0))
            ttk.Label(cell, text=self.tr(label), style="Muted.Subtle.TLabel").pack(anchor="w")
            value = tk.StringVar(value="—")
            self.spec_values.append(value)
            ttk.Label(cell, textvariable=value, style="Title.Subtle.TLabel").pack(anchor="w", pady=(3, 0))
        self.disk_summary, self.image_summary, self.host_meta = tk.StringVar(), tk.StringVar(), tk.StringVar()
        for row, variable in enumerate((self.disk_summary, self.image_summary, self.host_meta), 3):
            label = wrapped_label(self.detail_content, textvariable=variable, style="Muted.Surface.TLabel", wraplength=700)
            label.grid(row=row, column=0, sticky="ew", pady=(8 if row == 3 else 4, 0))
            if variable == self.host_meta:
                self.host_meta_label = label

    def _resize_workspace(self, event):
        # Leave room for the inventory on short screens; full details can be copied.
        if event.height < 660 * self.display_scale:
            self.detail_specs.grid_remove()
            self.host_meta_label.grid_remove()
        else:
            self.detail_specs.grid()
            self.host_meta_label.grid()

    def _build_log(self, parent):
        log_panel = ttk.Frame(parent, style="Panel.TFrame", padding=(14, 10))
        log_panel.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        title = ttk.Frame(log_panel, style="Surface.TFrame")
        title.pack(fill="x", pady=(0, 6))
        ttk.Label(title, text=self.tr('运行记录'), style="Title.Surface.TLabel").pack(side="left")
        body = ttk.Frame(log_panel, style="Surface.TFrame")
        body.pack(fill="x")
        self.log = tk.Text(body, height=3, wrap="word", bg=COLORS["surface"], fg=COLORS["muted"], relief="flat", borderwidth=0, highlightthickness=0, font="TkTextFont", state="disabled", padx=0)
        self.log.pack(side="left", fill="x", expand=True)
        scroll = ttk.Scrollbar(body, command=self.log.yview)
        scroll.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=scroll.set)
        self.log.tag_configure("time", foreground=COLORS["accent"])

    def clear_results(self, *, searched=False):
        self.rows = []
        self.table.delete(*self.table.get_children())
        self.result_count.set(self.tr('暂无匹配主机') if searched else self.tr('等待扫描'))
        self.empty_title.set(self.tr('当前范围暂无可用主机') if searched else self.tr('从一张合适的 GPU 开始'))
        self.empty_hint.set(self.tr('点击“持续自动找卡”等待补货，工具会保持所选地区与型号。') if searched else self.tr('在左侧填写 Token，选好地区和型号，再点击“扫描当前余量”。'))
        self.empty_state.place(relx=0.5, rely=0.58, anchor="center", relwidth=0.92)
        self.detail_content.grid_remove()
        self.detail_empty.grid()
        self.copy_button.configure(state="disabled", text=self.tr('复制配置'))
        if not self.busy:
            self.activity.configure(text=self.tr('就绪'), style="Neutral.Badge.TLabel")

    def copy_details(self):
        if not self.rows:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.details.get())
        self.copy_button.configure(text=self.tr('已复制'))

    def sync_selectors(self):
        chosen = self.preferences
        signs = chosen.regions or self.catalog.resolve_regions(chosen.region_groups)
        if signs == self.catalog.resolve_regions() and not chosen.region_groups:
            region = self.all_regions_label
        else:
            region = next((g["name"] for g in self.catalog.regions if set(g["signs"]) == set(signs)), self.custom_regions_label)
        values = [self.all_regions_label] + [g["name"] for g in self.catalog.regions]
        if region == self.custom_regions_label:
            values.append(self.custom_regions_label)
        self.region_combo.configure(values=values)
        self.region.set(region)
        self.gpu.set(chosen.gpu_names[0] if len(chosen.gpu_names) == 1 else self.custom_gpus_label if chosen.gpu_names else self.all_gpus_label)
        self.disk.set(str(chosen.expand_disk_gb))
        self.render_gpu_choices()

    def disk_changed(self, *_):
        if self.busy:
            return
        self.clear_results()
        raw = self.disk.get().strip()
        if raw and not re.fullmatch(r"[0-9]+", raw):
            self.disk_info.set(self.tr('扩容容量应为非负整数 GB；0 表示不扩容。'))
            self.details.set(self.tr('请填写有效的扩容容量后重新扫描。'))
            return
        size = int(raw or 0)
        self.preferences = replace(self.preferences, expand_disk_gb=size)
        self.disk_info.set(self.tr('新增 {p0} GB，叠加免费容量。\n扩容费用按日另计。', p0=size) if size else self.tr('0 表示不扩容，扩容费用按日另计。'))
        self.details.set(self.tr('已更新数据盘条件，重新扫描后自动加载对应容量和费用。'))

    def render_gpu_choices(self):
        names = self.catalog.gpu_names()
        if self.stock is not None:
            names = [name for name in self.stock if not self.catalog.model(name).get("is_cpu", name.upper().startswith("CPU"))]
        names = list(dict.fromkeys(names + self.preferences.gpu_names))
        self.gpu_combo.configure(values=[self.all_gpus_label] + names + ([self.custom_gpus_label] if len(self.preferences.gpu_names) > 1 else []))
        if self.stock is None:
            return
        selected = self.preferences.gpu_names
        if selected:
            summary = "；".join(f"{name}：{self.stock[name]['idle']} / {self.stock[name]['total']}" if name in self.stock else self.tr('{p0}：本地区未提供', p0=name) for name in selected)
        else:
            remaining = [(name, self.stock[name]) for name in names if name in self.stock]
            summary = self.tr('{p0} 种 GPU 型号，{p1} 张空闲卡', p0=len(remaining), p1=sum((v['idle'] for _, v in remaining)))
        self.stock_info.set(self.tr('{p0} · {p1}（空闲 / 总量）；无余量会继续等待，不换地区或型号。', p0=self.region.get(), p1=summary))

    def cancel_stock_refresh(self):
        self._stock_generation += 1
        if self._stock_timer is not None:
            self.root.after_cancel(self._stock_timer)
            self._stock_timer = None

    def scope_changed(self):
        self.cancel_stock_refresh()
        self.stock = None
        self.render_gpu_choices()
        self.clear_results()
        self.activity.configure(text=self.tr('就绪'), style="Neutral.Badge.TLabel")
        self.details.set(self.tr('已切换租卡范围，扫描后自动加载该范围内的配置、价格和镜像。'))
        self.status.set(self.tr('当前范围：{p0} / {p1}；点击扫描或自动租卡。', p0=self.region.get(), p1=self.gpu.get()))
        self.stock_info.set(self.tr('正在刷新该地区库存…') if self.token.get().strip() else self.tr('登录后自动读取该地区库存。'))
        self.schedule_stock()

    def region_changed(self, _=None):
        if self.busy:
            return
        value = self.region.get()
        if value == self.custom_regions_label:
            return
        groups = [] if value == self.all_regions_label else [value]
        self.preferences = replace(self.preferences, region_groups=groups, regions=self.catalog.resolve_regions(groups))
        self.sync_selectors()
        self.scope_changed()

    def gpu_changed(self, _=None):
        if self.busy or self.gpu.get() == self.custom_gpus_label:
            return
        self.preferences = replace(self.preferences, gpu_names=[] if self.gpu.get() == self.all_gpus_label else [self.gpu.get()])
        self.clear_results()
        self.activity.configure(text=self.tr('就绪'), style="Neutral.Badge.TLabel")
        self.details.set(self.tr('已切换型号，扫描后自动加载该型号的配置、价格和镜像。'))
        self.status.set(self.tr('当前范围：{p0} / {p1}；点击扫描或自动租卡。', p0=self.region.get(), p1=self.gpu.get()))
        self.render_gpu_choices()

    def token_changed(self, *_):
        self.cancel_stock_refresh()
        self.stock = None
        self.render_gpu_choices()
        self.stock_info.set(self.tr('正在刷新该地区库存…') if self.token.get().strip() else self.tr('登录后自动读取该地区库存。'))
        self.schedule_stock()

    def schedule_stock(self):
        if self._stock_timer is not None:
            self.root.after_cancel(self._stock_timer)
        self._stock_timer = self.root.after(400, self.refresh_stock)

    def refresh_stock(self):
        self.cancel_stock_refresh()
        if self.demo or self.busy or not self.token.get().strip():
            return
        generation = self._stock_generation
        credential = self.token.get().strip()
        try:
            signs = self.settings().regions
            api = AutoDL(credential, read_only=True)
        except QuickError as error:
            self.stock_info.set(self.tr('地区库存未刷新：') + self.tr(str(error)).replace(credential, self.tr('[已隐藏]')))
            return
        self.stock_info.set(self.tr('正在刷新该地区库存…'))
        def work():
            try:
                self.events.put(("scope_stock", (generation, api.region_stock(signs))))
            except Exception as error:
                message = self.tr(str(error)).replace(credential, self.tr('[已隐藏]'))
                self.events.put(("scope_stock_error", (generation, message)))
        threading.Thread(target=work, daemon=True).start()

    def settings(self):
        raw = self.disk.get().strip()
        if raw and not re.fullmatch(r"[0-9]+", raw):
            raise QuickError(self.tr('扩容数据盘应为非负整数 GB；0 表示不扩容'))
        chosen = replace(self.preferences, auto_image=True, expand_disk_gb=int(raw or 0))
        if not chosen.regions:
            chosen.regions = self.catalog.resolve_regions(chosen.region_groups)
        return chosen

    def api(self):
        if self.demo:
            raise QuickError(self.tr('演示模式不联网'))
        return AutoDL(self.token.get(), read_only=self.read_only)

    def write_log(self, text):
        token = self.token.get().strip()
        translated = self.tr(str(text))
        value = translated.replace(token, self.tr('[已隐藏]')) if token else translated
        self.log.configure(state="normal")
        self.log.insert("end", time.strftime("%H:%M:%S") + "  ", "time")
        self.log.insert("end", value + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def start(self, waiting, *, execute=False):
        if self.busy:
            return
        if self.demo:
            self.write_log(self.tr('当前是演示模式，不发出网络请求。'))
            return
        if execute and self.read_only:
            self.status.set(self.tr('当前为只读测试，无法租卡；请通过 start.cmd 打开正常窗口。'))
            return
        try:
            chosen, api = self.settings(), self.api()
            chosen.validate(renting=execute)
        except QuickError as error:
            messagebox.showerror(self.tr('请检查设置'), self.tr(str(error)), parent=self.root)
            return
        self.busy = True
        self.activity.configure(text=self.tr('自动租卡中') if execute else self.tr('等待库存') if waiting else self.tr('正在扫描'), style="Busy.Badge.TLabel")
        self.cancel_stock_refresh()
        generation = self._stock_generation
        self.cancel.clear()
        self.status.set(self.tr('正在找卡：{p0} / {p1}；', p0=self.region.get(), p1=self.gpu.get()) + (self.tr('命中后按平台当前价租用一台、单张 GPU，成功后停止。') if execute else self.tr('仅查询，自动加载价格、配置和镜像…')))
        for widget in (self.scan_button, self.wait_button, self.rent_button, self.token_entry, self.advanced_button, self.region_combo, self.gpu_combo, self.stock_button, self.disk_entry):
            widget.configure(state="disabled")
        self.stop_button.configure(state="normal")
        credential = self.token.get().strip()
        def work():
            report = {"started_at": datetime.now().astimezone().isoformat(), "mode": "automatic_finder",
                      "read_only": not execute, "status": "running", "cards": [],
                      "expand_disk_gb": chosen.expand_disk_gb,
                      "region_groups": list(chosen.region_groups), "region_signs": list(chosen.regions), "gpu_names": list(chosen.gpu_names)}
            def emit(kind, value):
                if kind == "found_cards":
                    report["cards"] = [card.display() for card in value]
                elif kind == "region_stock":
                    self.events.put(("scope_stock", (generation, value)))
                    return
                self.events.put((kind, value))
            engine = Engine(api, emit)
            try:
                try:
                    fresh = Catalog.refresh()
                    if chosen.region_groups or chosen.regions == self.catalog.resolve_regions():
                        chosen.regions = fresh.resolve_regions(chosen.region_groups)
                    self.events.put(("catalog", fresh))
                    report["region_signs"] = list(chosen.regions)
                except Exception:
                    self.events.put(("log", self.tr('官网目录暂未刷新，使用最近的已验证目录。')))
                if waiting:
                    report["result"] = engine.auto_wait(chosen, self.cancel, execute=execute)
                else:
                    cards = engine.discover(chosen, self.cancel)
                    report["cards"] = [card.display() for card in cards]
                report["status"] = "complete"
            except Cancelled:
                report["status"] = "cancelled"
            except Exception as error:
                report.update(status="error", error=self.tr(str(error)).replace(credential, self.tr('[已隐藏]')))
                self.events.put(("error", report["error"]))
            finally:
                report["requests"] = api.audit
                try:
                    atomic_json(ROOT / ".state" / "auto-finder-query.json", report)
                finally:
                    self.events.put(("done", report["status"]))
        threading.Thread(target=work, daemon=False).start()

    def scan(self):
        self.start(False)

    def wait(self):
        self.start(True)

    def rent(self):
        self.start(True, execute=True)

    def show_cards(self, cards):
        if not cards:
            self.clear_results(searched=True)
            self.details.set(self.tr('当前没有满足平台可出租条件的剩余 GPU；持续查找会自动等待下一轮。'))
            return
        self.rows = cards
        self.empty_state.place_forget()
        self.result_count.set(self.tr('{p0} 台主机 / {p1} 张空闲卡', p0=len(cards), p1=sum(card.idle for card in cards)))
        self.table.delete(*self.table.get_children())
        for index, card in enumerate(cards):
            self.table.insert("", "end", iid=str(index), tags=("odd" if index % 2 else "even",), values=(card.gpu, card.region, str(card.quote) if card.quote is not None else self.tr('待查询'),
                             card.memory_gb or "—", card.cpu_cores or "—", card.ram_gb or "—",
                             f"{card.disk_gb:g}+{card.expand_disk_gb}={card.disk_gb + card.expand_disk_gb:g}" if card.expand_disk_gb else card.disk_gb or "—",
                             str(card.disk_daily_price) if card.disk_daily_price is not None else self.tr('待报价'), card.idle,
                             self.tr('已匹配') if card.image_uuid else self.tr('未匹配')))
        self.table.selection_set("0")
        self.show_selected()
        if not self.busy:
            self.activity.configure(text=self.tr('已找到余量'), style="Ready.Badge.TLabel")
        self.status.set(self.tr('发现 {p0} 台有余量的主机，已自动加载配置；当前仅查询。', p0=len(cards)) if self.read_only else self.tr('发现 {p0} 台有余量的主机。', p0=len(cards)))

    def show_selected(self, _=None):
        selection = self.table.selection()
        if not selection:
            return
        card = self.rows[int(selection[0])]
        self.details.set(self.tr('{p0} · {p1} · {p2} / {p3} · 当前 ¥{p4}/小时\nCPU：{p5} 核 {p6}    内存：{p7} GB    显存：{p8} GB\n驱动：{p9}    空闲卡：{p10}    主机：{p11}\n数据盘：免费 {p12:g} + 扩容 {p13} = {p14:g} GB {p15}；最多可增 {p16} GB；费用 ¥{p17}/日\n自动镜像：{p18}  {p19}', p0=card.gpu, p1=card.region, p2=card.chip, p3=card.arch, p4=card.quote, p5=card.cpu_cores or self.tr('未提供'), p6=card.cpu_name, p7=card.ram_gb or self.tr('未提供'), p8=card.memory_gb or self.tr('未提供'), p9=card.driver or self.tr('未提供'), p10=card.idle, p11=card.machine_id, p12=card.disk_gb, p13=card.expand_disk_gb, p14=card.disk_gb + card.expand_disk_gb, p15=card.disk_type, p16=card.disk_expandable_gb if card.disk_expandable_gb is not None else self.tr('未提供'), p17=card.disk_daily_price if card.disk_daily_price is not None else self.tr('待报价'), p18=card.image_label or self.tr('未找到兼容镜像'), p19=card.image_uuid))
        self.detail_empty.grid_remove()
        self.detail_content.grid(row=1, column=0, sticky="ew", pady=(round(6 * self.display_scale), 0))
        self.copy_button.configure(state="normal", text=self.tr('复制配置'))
        self.host_title.set(card.gpu)
        self.host_price.set(self.tr('¥{p0} / 时', p0=card.quote) if card.quote is not None else self.tr('待报价'))
        self.host_subtitle.set(card.region + "  /  " + card.chip + "  /  " + card.arch)
        for variable, value, unit in zip(self.spec_values, (card.memory_gb, card.cpu_cores, card.ram_gb, card.idle), (" GB", "", " GB", "")):
            variable.set(str(value) + unit if value is not None and value != 0 else "—")
        self.disk_summary.set(self.tr('数据盘 {p0:g} GB（免费 {p1:g} + 扩容 {p2}）  /  ¥{p3} / 日', p0=card.disk_gb + card.expand_disk_gb, p1=card.disk_gb, p2=card.expand_disk_gb, p3=card.disk_daily_price) if card.disk_daily_price is not None else self.tr('数据盘 {p0:g} GB，扩容日费待报价', p0=card.disk_gb + card.expand_disk_gb))
        self.image_summary.set(self.tr('自动镜像：{p0}', p0=card.image_label or self.tr('未找到兼容镜像')))
        self.host_meta.set(self.tr('主机 {p0}  /  {p1}  /  驱动 {p2}', p0=card.machine_id, p1=card.cpu_name or self.tr('未提供'), p2=card.driver or self.tr('未提供')))

    def advanced(self):
        from .advanced import App as AdvancedApp
        window = tk.Toplevel(self.root)
        editor = AdvancedApp(window, self.settings(), read_only=True, language=self.language)
        editor.token.set(self.token.get())
        def apply():
            try:
                self.preferences = editor.get_settings()
                self.preferences.auto_image = True
                self.sync_selectors()
                self.scope_changed()
                editor.close()
            except QuickError as error:
                messagebox.showerror(self.tr('请检查偏好'), self.tr(str(error)), parent=window)
        editor.save_button.configure(text=self.tr('应用偏好'), command=apply)

    def stop(self):
        self.cancel.set()
        self.activity.configure(text=self.tr('正在停止'), style="Warning.Badge.TLabel")
        self.status.set(self.tr('正在停止，等待当前请求结束'))

    def close(self):
        if self.busy:
            self.closing = True
            self.stop()
            return
        if self._timer is not None:
            self.root.after_cancel(self._timer)
            self._timer = None
        self.cancel_stock_refresh()
        self.token.trace_remove("write", self._token_trace)
        self.disk.trace_remove("write", self._disk_trace)
        self.root.destroy()

    def drain(self):
        if self._timer is not None:
            self.root.after_cancel(self._timer)
            self._timer = None
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "log":
                    self.write_log(value)
                elif kind == "catalog":
                    previous = self.catalog
                    self.catalog = value
                    if self.preferences.region_groups or not self.preferences.regions or self.preferences.regions == previous.resolve_regions():
                        self.preferences = replace(self.preferences, regions=self.catalog.resolve_regions(self.preferences.region_groups))
                    self.sync_selectors()
                elif kind == "scope_stock":
                    generation, stock = value
                    if generation == self._stock_generation:
                        self.stock = stock
                        self.render_gpu_choices()
                elif kind == "scope_stock_error":
                    generation, message = value
                    if generation == self._stock_generation:
                        self.stock_info.set(self.tr('地区库存未刷新：') + message)
                elif kind == "found_cards":
                    self.show_cards(value)
                elif kind == "created":
                    self.activity.configure(text=self.tr('租用成功'), style="Ready.Badge.TLabel")
                    self.status.set(self.tr('已租用一台，自动停止：') + value["instance_uuid"])
                elif kind == "error":
                    self.write_log(value)
                    self.activity.configure(text=self.tr('需要处理'), style="Error.Badge.TLabel")
                    self.status.set(self.tr('查询未完成，请查看下方记录'))
                elif kind == "done":
                    self.busy = False
                    for widget in (self.scan_button, self.wait_button, self.token_entry, self.advanced_button, self.stock_button, self.disk_entry):
                        widget.configure(state="normal")
                    self.region_combo.configure(state="readonly")
                    self.gpu_combo.configure(state="readonly")
                    self.stop_button.configure(state="disabled")
                    self.rent_button.configure(state="disabled" if self.read_only else "normal")
                    if value == "cancelled":
                        self.activity.configure(text=self.tr('已停止'), style="Neutral.Badge.TLabel")
                        self.status.set(self.tr('已停止查找，可调整条件后重新开始。'))
                    if value == "complete" and not self.rows:
                        self.clear_results(searched=True)
                        self.details.set(self.tr('当前没有满足平台可出租条件的剩余 GPU；持续查找会自动等待下一轮。'))
                        self.status.set(self.tr('查询完成：当前没有符合平台可出租条件的余量。'))
                    if value == "complete" and self.activity.cget("text") != self.tr('租用成功'):
                        self.activity.configure(text=self.tr('查询完成'), style="Ready.Badge.TLabel")
                    if self.closing:
                        self.close()
                        return
        except queue.Empty:
            pass
        self._timer = self.root.after(100, self.drain)

    def token_help(self):
        messagebox.showinfo(self.tr('获取网页 Token'), self.tr('登录 AutoDL → F12 → Application / 应用 → Local Storage / 本地存储 → https://www.autodl.com → token。\n\n只粘贴到此窗口，不保存、不发到聊天。'), parent=self.root)


def run(settings=None, demo=False, read_only=False, language="zh"):
    enable_high_dpi()
    root = tk.Tk()
    App(root, settings, demo=demo, read_only=read_only, language=language)
    root.mainloop()
