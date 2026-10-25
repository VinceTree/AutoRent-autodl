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
from tkinter import messagebox, scrolledtext, ttk
import webbrowser

from ..core import AutoDL, Candidate, Cancelled, Engine, QuickError, ROOT, Settings, atomic_json
from ..catalog import Catalog
from ..i18n import Translator

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
        root.geometry("1160x820")
        root.minsize(940, 720)
        root.configure(bg="#f3f5f8")
        root.protocol("WM_DELETE_WINDOW", self.close)
        style = ttk.Style()
        style.theme_use("clam")
        for name in ("TFrame", "TLabel", "TLabelframe", "TCheckbutton"):
            style.configure(name, background="#f3f5f8")
        style.configure("TLabel", font=("Microsoft YaHei UI", 10))
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(12, 8))
        style.configure("Accent.TButton", background="#3057d5", foreground="white")
        style.map("Accent.TButton", background=[("disabled", "#9aa9d0"), ("active", "#2444ae")])
        style.configure("Treeview", font=("Microsoft YaHei UI", 9), rowheight=29)
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))
        root.option_add("*Font", ("Microsoft YaHei UI", 10))
        wrap = ttk.Frame(root, padding=20)
        wrap.pack(fill="both", expand=True)
        ttk.Label(wrap, text=self.tr('AutoDL 自动找卡'), font=("Microsoft YaHei UI", 22, "bold"), foreground="#17233d").pack(anchor="w")
        ttk.Label(wrap, text=self.tr('选定地区与型号，自动找余卡、读取平台价格并匹配镜像。'), foreground="#52617b").pack(anchor="w", pady=(4, 14))
        login = ttk.Frame(wrap)
        login.pack(fill="x")
        ttk.Label(login, text=self.tr('网页 Token')).pack(side="left")
        self.token = tk.StringVar(value="" if demo else os.environ.get("AUTODL_WEB_TOKEN", ""))
        self.token_entry = ttk.Entry(login, textvariable=self.token, show="•")
        self.token_entry.pack(side="left", fill="x", expand=True, padx=10)
        ttk.Button(login, text=self.tr('获取方法'), command=self.token_help).pack(side="left")
        ttk.Button(login, text=self.tr('打开市场'), command=lambda: webbrowser.open("https://www.autodl.com/market/list")).pack(side="left", padx=(8, 0))
        scope = ttk.LabelFrame(wrap, text=self.tr('租卡范围'), padding=(10, 6))
        scope.pack(fill="x", pady=(12, 0))
        selectors = ttk.Frame(scope)
        selectors.pack(fill="x")
        ttk.Label(selectors, text=self.tr('地区')).pack(side="left")
        self.region = tk.StringVar()
        self.region_combo = ttk.Combobox(selectors, textvariable=self.region, state="readonly", width=20)
        self.region_combo.pack(side="left", padx=(8, 20))
        self.region_combo.bind("<<ComboboxSelected>>", self.region_changed)
        ttk.Label(selectors, text=self.tr('卡型号')).pack(side="left")
        self.gpu = tk.StringVar()
        self.gpu_combo = ttk.Combobox(selectors, textvariable=self.gpu, state="readonly", width=24)
        self.gpu_combo.pack(side="left", padx=8)
        ttk.Label(selectors, text=self.tr('扩容数据盘')).pack(side="left", padx=(8, 0))
        self.disk = tk.StringVar(value=str(self.preferences.expand_disk_gb))
        self.disk_entry = ttk.Spinbox(selectors, textvariable=self.disk, from_=0, to=1048576, increment=50, width=7)
        self.disk_entry.pack(side="left", padx=(6, 3))
        ttk.Label(selectors, text="GB").pack(side="left")
        self.stock_button = ttk.Button(selectors, text=self.tr('刷新地区库存'), command=self.refresh_stock)
        self.stock_button.pack(side="right")
        self.disk_info = tk.StringVar(value=self.tr('填写新增容量，0 表示不扩容；自动筛选可扩容主机，数据盘费用按日单独计算。'))
        ttk.Label(scope, textvariable=self.disk_info, foreground="#52617b", wraplength=1070).pack(anchor="w", pady=(5, 0))
        self.stock_info = tk.StringVar(value=self.tr('登录后自动加载所选地区的型号库存；无余量的型号也可选，便于等待。'))
        ttk.Label(scope, textvariable=self.stock_info, foreground="#52617b", wraplength=1070).pack(anchor="w", pady=(5, 0))
        self.gpu_combo.bind("<<ComboboxSelected>>", self.gpu_changed)
        self.sync_selectors()
        actions = ttk.Frame(wrap)
        actions.pack(fill="x", pady=12)
        self.scan_button = ttk.Button(actions, text=self.tr('扫描当前余量'), command=self.scan)
        self.scan_button.pack(side="left")
        self.wait_button = ttk.Button(actions, text=self.tr('持续自动找卡'), command=self.wait)
        self.wait_button.pack(side="left", padx=8)
        self.rent_button = ttk.Button(actions, text=self.tr('开始自动租卡（计费）'), command=self.rent, style="Accent.TButton")
        self.rent_button.pack(side="left", padx=(0, 8))
        if read_only:
            self.rent_button.configure(state="disabled")
        self.stop_button = ttk.Button(actions, text=self.tr('停止'), command=self.stop, state="disabled")
        self.stop_button.pack(side="left")
        self.advanced_button = ttk.Button(actions, text=self.tr('高级偏好（可选）'), command=self.advanced)
        self.advanced_button.pack(side="right")
        self.status = tk.StringVar(value=self.tr('只读测试：自动租卡按钮已禁用；正常使用请通过 start.cmd 打开。') if read_only else self.tr('扫描和持续找卡仅查询；点击“开始自动租卡（计费）”会按平台当前价租用一台、单张 GPU，成功后停止。'))
        ttk.Label(wrap, textvariable=self.status, foreground="#3057d5", wraplength=1080).pack(anchor="w", pady=(0, 9))
        table_wrap = ttk.Frame(wrap)
        table_wrap.pack(fill="both", expand=True)
        columns = ("gpu", "region", "price", "vram", "cpu", "ram", "disk", "disk_price", "idle", "image")
        self.table = ttk.Treeview(table_wrap, columns=columns, show="headings", height=7)
        for key, label, width in (("gpu", self.tr('剩余型号'), 170), ("region", self.tr('地区'), 88), ("price", self.tr('GPU 元/时'), 110),
                                  ("vram", self.tr('显存 GB'), 65), ("cpu", self.tr('CPU 核'), 60), ("ram", self.tr('内存 GB'), 70),
                                  ("disk", self.tr('数据盘 GB'), 120), ("disk_price", self.tr('数据盘 元/日'), 100),
                                  ("idle", self.tr('空闲卡'), 60), ("image", self.tr('自动镜像'), 90)):
            self.table.heading(key, text=label)
            self.table.column(key, width=width, anchor="w")
        scroll = ttk.Scrollbar(table_wrap, command=self.table.yview)
        horizontal = ttk.Scrollbar(table_wrap, orient="horizontal", command=self.table.xview)
        horizontal.pack(side="bottom", fill="x")
        self.table.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        self.table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.table.bind("<<TreeviewSelect>>", self.show_selected)
        details = ttk.LabelFrame(wrap, text=self.tr('自动加载的信息'), padding=10)
        details.pack(side="bottom", fill="x", pady=10, before=table_wrap)
        self.details = tk.StringVar(value=self.tr('发现剩余卡后，第一台的完整配置会自动显示在这里。'))
        ttk.Label(details, textvariable=self.details, wraplength=1070, justify="left").pack(anchor="w")
        self.log = scrolledtext.ScrolledText(wrap, height=5, wrap="word", bg="#152036", fg="#dce5f6", relief="flat", font=("Microsoft YaHei UI", 9), state="disabled")
        self.log.pack(side="bottom", fill="x", before=details)
        self.write_log(self.tr('按所选地区和型号自动找卡，绝不扩大范围。目录 {p0} 种 GPU；Token 不保存。', p0=len(self.catalog.gpu_names())))
        if demo:
            self.show_cards([Candidate("demo-host", self.tr('西北B区（示例）'), "west-B", 1, "12.8", "RTX 4090", Decimal("2.5"), Decimal("2.38"), memory_gb=24, cpu_cores=16, ram_gb=80, cpu_name=self.tr('示例 CPU'), driver=self.tr('示例驱动'), disk_gb=100, image_uuid="base-image-DEMO", image_label=self.tr('示例 PyTorch / CUDA 12.8'), image_runtime="12.8")])
            self.status.set(self.tr('演示数据 · 完全不联网'))
        self._timer = root.after(100, self.drain)
        self._token_trace = self.token.trace_add("write", self.token_changed)
        self._disk_trace = self.disk.trace_add("write", self.disk_changed)
        if self.token.get().strip():
            self.schedule_stock()

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
        self.rows = []
        self.table.delete(*self.table.get_children())
        raw = self.disk.get().strip()
        if raw and not re.fullmatch(r"[0-9]+", raw):
            self.disk_info.set(self.tr('扩容容量应为非负整数 GB；0 表示不扩容。'))
            self.details.set(self.tr('请填写有效的扩容容量后重新扫描。'))
            return
        size = int(raw or 0)
        self.preferences = replace(self.preferences, expand_disk_gb=size)
        self.disk_info.set(self.tr('新增 {p0} GB；目标容量 = 主机免费数据盘 + {p1} GB。自动筛掉扩容不足的主机，日费另列。', p0=size, p1=size) if size else self.tr('填写新增容量，0 表示不扩容；自动筛选可扩容主机，数据盘费用按日单独计算。'))
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
        self.rows = []
        self.table.delete(*self.table.get_children())
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
        self.rows = []
        self.table.delete(*self.table.get_children())
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
        self.log.insert("end", time.strftime("%H:%M:%S") + "  " + value + "\n")
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
        self.rows = cards
        self.table.delete(*self.table.get_children())
        for index, card in enumerate(cards):
            self.table.insert("", "end", iid=str(index), values=(card.gpu, card.region, str(card.quote) if card.quote is not None else self.tr('待查询'),
                             card.memory_gb or "—", card.cpu_cores or "—", card.ram_gb or "—",
                             f"{card.disk_gb:g}+{card.expand_disk_gb}={card.disk_gb + card.expand_disk_gb:g}" if card.expand_disk_gb else card.disk_gb or "—",
                             str(card.disk_daily_price) if card.disk_daily_price is not None else self.tr('待报价'), card.idle,
                             self.tr('已匹配') if card.image_uuid else self.tr('未匹配')))
        if cards:
            self.table.selection_set("0")
            self.show_selected()
            self.status.set(self.tr('发现 {p0} 台有余量的主机，已自动加载配置；当前仅查询。', p0=len(cards)) if self.read_only else self.tr('发现 {p0} 台有余量的主机。', p0=len(cards)))
        else:
            self.details.set(self.tr('当前没有满足平台可出租条件的剩余 GPU；持续查找会自动等待下一轮。'))

    def show_selected(self, _=None):
        selection = self.table.selection()
        if not selection:
            return
        card = self.rows[int(selection[0])]
        self.details.set(self.tr('{p0} · {p1} · {p2} / {p3} · 当前 ¥{p4}/小时\nCPU：{p5} 核 {p6}    内存：{p7} GB    显存：{p8} GB\n驱动：{p9}    空闲卡：{p10}    主机：{p11}\n数据盘：免费 {p12:g} + 扩容 {p13} = {p14:g} GB {p15}；最多可增 {p16} GB；费用 ¥{p17}/日\n自动镜像：{p18}  {p19}', p0=card.gpu, p1=card.region, p2=card.chip, p3=card.arch, p4=card.quote, p5=card.cpu_cores or self.tr('未提供'), p6=card.cpu_name, p7=card.ram_gb or self.tr('未提供'), p8=card.memory_gb or self.tr('未提供'), p9=card.driver or self.tr('未提供'), p10=card.idle, p11=card.machine_id, p12=card.disk_gb, p13=card.expand_disk_gb, p14=card.disk_gb + card.expand_disk_gb, p15=card.disk_type, p16=card.disk_expandable_gb if card.disk_expandable_gb is not None else self.tr('未提供'), p17=card.disk_daily_price if card.disk_daily_price is not None else self.tr('待报价'), p18=card.image_label or self.tr('未找到兼容镜像'), p19=card.image_uuid))

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
                    self.status.set(self.tr('已租用一台，自动停止：') + value["instance_uuid"])
                elif kind == "error":
                    self.write_log(value)
                    self.status.set(self.tr('查询未完成，请查看下方记录'))
                elif kind == "done":
                    self.busy = False
                    for widget in (self.scan_button, self.wait_button, self.token_entry, self.advanced_button, self.stock_button, self.disk_entry):
                        widget.configure(state="normal")
                    self.region_combo.configure(state="readonly")
                    self.gpu_combo.configure(state="readonly")
                    self.stop_button.configure(state="disabled")
                    self.rent_button.configure(state="disabled" if self.read_only else "normal")
                    if value == "complete" and not self.rows:
                        self.status.set(self.tr('查询完成：当前没有符合平台可出租条件的余量。'))
                    if self.closing:
                        self.close()
                        return
        except queue.Empty:
            pass
        self._timer = self.root.after(100, self.drain)

    def token_help(self):
        messagebox.showinfo(self.tr('获取网页 Token'), self.tr('登录 AutoDL → F12 → Application / 应用 → Local Storage / 本地存储 → https://www.autodl.com → token。\n\n只粘贴到此窗口，不保存、不发到聊天。'), parent=self.root)


def run(settings=None, demo=False, read_only=False, language="zh"):
    root = tk.Tk()
    App(root, settings, demo=demo, read_only=read_only, language=language)
    root.mainloop()
