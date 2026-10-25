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

from autodl_quick import AutoDL, Candidate, Cancelled, Engine, QuickError, ROOT, Settings, atomic_json
from market_catalog import Catalog

ALL_REGIONS = "全部公开地区"
ALL_GPUS = "全部 GPU 型号"
CUSTOM_REGIONS = "高级偏好中的多个地区 / 机房"
CUSTOM_GPUS = "高级偏好中的多个型号"


class App:
    def __init__(self, root, settings=None, demo=False, read_only=False):
        self.root, self.demo, self.read_only = root, demo, read_only
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
        root.title("AutoDL Quick · 自动找剩余卡" + (" · 演示" if demo else " · 只读测试" if read_only else ""))
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
        ttk.Label(wrap, text="AutoDL 自动找卡", font=("Microsoft YaHei UI", 22, "bold"), foreground="#17233d").pack(anchor="w")
        ttk.Label(wrap, text="选定地区与型号，自动找余卡、读取平台价格并匹配镜像。", foreground="#52617b").pack(anchor="w", pady=(4, 14))
        login = ttk.Frame(wrap)
        login.pack(fill="x")
        ttk.Label(login, text="网页 Token").pack(side="left")
        self.token = tk.StringVar(value="" if demo else os.environ.get("AUTODL_WEB_TOKEN", ""))
        self.token_entry = ttk.Entry(login, textvariable=self.token, show="•")
        self.token_entry.pack(side="left", fill="x", expand=True, padx=10)
        ttk.Button(login, text="获取方法", command=self.token_help).pack(side="left")
        ttk.Button(login, text="打开市场", command=lambda: webbrowser.open("https://www.autodl.com/market/list")).pack(side="left", padx=(8, 0))
        scope = ttk.LabelFrame(wrap, text="租卡范围", padding=(10, 6))
        scope.pack(fill="x", pady=(12, 0))
        selectors = ttk.Frame(scope)
        selectors.pack(fill="x")
        ttk.Label(selectors, text="地区").pack(side="left")
        self.region = tk.StringVar()
        self.region_combo = ttk.Combobox(selectors, textvariable=self.region, state="readonly", width=20)
        self.region_combo.pack(side="left", padx=(8, 20))
        self.region_combo.bind("<<ComboboxSelected>>", self.region_changed)
        ttk.Label(selectors, text="卡型号").pack(side="left")
        self.gpu = tk.StringVar()
        self.gpu_combo = ttk.Combobox(selectors, textvariable=self.gpu, state="readonly", width=24)
        self.gpu_combo.pack(side="left", padx=8)
        ttk.Label(selectors, text="扩容数据盘").pack(side="left", padx=(8, 0))
        self.disk = tk.StringVar(value=str(self.preferences.expand_disk_gb))
        self.disk_entry = ttk.Spinbox(selectors, textvariable=self.disk, from_=0, to=1048576, increment=50, width=7)
        self.disk_entry.pack(side="left", padx=(6, 3))
        ttk.Label(selectors, text="GB").pack(side="left")
        self.stock_button = ttk.Button(selectors, text="刷新地区库存", command=self.refresh_stock)
        self.stock_button.pack(side="right")
        self.disk_info = tk.StringVar(value="填写新增容量，0 表示不扩容；自动筛选可扩容主机，数据盘费用按日单独计算。")
        ttk.Label(scope, textvariable=self.disk_info, foreground="#52617b", wraplength=1070).pack(anchor="w", pady=(5, 0))
        self.stock_info = tk.StringVar(value="登录后自动加载所选地区的型号库存；无余量的型号也可选，便于等待。")
        ttk.Label(scope, textvariable=self.stock_info, foreground="#52617b", wraplength=1070).pack(anchor="w", pady=(5, 0))
        self.gpu_combo.bind("<<ComboboxSelected>>", self.gpu_changed)
        self.sync_selectors()
        actions = ttk.Frame(wrap)
        actions.pack(fill="x", pady=12)
        self.scan_button = ttk.Button(actions, text="扫描当前余量", command=self.scan)
        self.scan_button.pack(side="left")
        self.wait_button = ttk.Button(actions, text="持续自动找卡", command=self.wait)
        self.wait_button.pack(side="left", padx=8)
        self.rent_button = ttk.Button(actions, text="开始自动租卡（计费）", command=self.rent, style="Accent.TButton")
        self.rent_button.pack(side="left", padx=(0, 8))
        if read_only:
            self.rent_button.configure(state="disabled")
        self.stop_button = ttk.Button(actions, text="停止", command=self.stop, state="disabled")
        self.stop_button.pack(side="left")
        self.advanced_button = ttk.Button(actions, text="高级偏好（可选）", command=self.advanced)
        self.advanced_button.pack(side="right")
        self.status = tk.StringVar(value="只读测试：自动租卡按钮已禁用；正常使用请通过 start.cmd 打开。" if read_only else "扫描和持续找卡仅查询；点击“开始自动租卡（计费）”会按平台当前价租用一台、单张 GPU，成功后停止。")
        ttk.Label(wrap, textvariable=self.status, foreground="#3057d5", wraplength=1080).pack(anchor="w", pady=(0, 9))
        table_wrap = ttk.Frame(wrap)
        table_wrap.pack(fill="both", expand=True)
        columns = ("gpu", "region", "price", "vram", "cpu", "ram", "disk", "disk_price", "idle", "image")
        self.table = ttk.Treeview(table_wrap, columns=columns, show="headings", height=7)
        for key, label, width in (("gpu", "剩余型号", 170), ("region", "地区", 88), ("price", "GPU 元/时", 110),
                                  ("vram", "显存 GB", 65), ("cpu", "CPU 核", 60), ("ram", "内存 GB", 70),
                                  ("disk", "数据盘 GB", 120), ("disk_price", "数据盘 元/日", 100),
                                  ("idle", "空闲卡", 60), ("image", "自动镜像", 90)):
            self.table.heading(key, text=label)
            self.table.column(key, width=width, anchor="w")
        scroll = ttk.Scrollbar(table_wrap, command=self.table.yview)
        horizontal = ttk.Scrollbar(table_wrap, orient="horizontal", command=self.table.xview)
        horizontal.pack(side="bottom", fill="x")
        self.table.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        self.table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.table.bind("<<TreeviewSelect>>", self.show_selected)
        details = ttk.LabelFrame(wrap, text="自动加载的信息", padding=10)
        details.pack(side="bottom", fill="x", pady=10, before=table_wrap)
        self.details = tk.StringVar(value="发现剩余卡后，第一台的完整配置会自动显示在这里。")
        ttk.Label(details, textvariable=self.details, wraplength=1070, justify="left").pack(anchor="w")
        self.log = scrolledtext.ScrolledText(wrap, height=5, wrap="word", bg="#152036", fg="#dce5f6", relief="flat", font=("Microsoft YaHei UI", 9), state="disabled")
        self.log.pack(side="bottom", fill="x", before=details)
        self.write_log(f"按所选地区和型号自动找卡，绝不扩大范围。目录 {len(self.catalog.gpu_names())} 种 GPU；Token 不保存。")
        if demo:
            self.show_cards([Candidate("demo-host", "西北B区（示例）", "west-B", 1, "12.8", "RTX 4090", Decimal("2.5"), Decimal("2.38"), memory_gb=24, cpu_cores=16, ram_gb=80, cpu_name="示例 CPU", driver="示例驱动", disk_gb=100, image_uuid="base-image-DEMO", image_label="示例 PyTorch / CUDA 12.8", image_runtime="12.8")])
            self.status.set("演示数据 · 完全不联网")
        self._timer = root.after(100, self.drain)
        self._token_trace = self.token.trace_add("write", self.token_changed)
        self._disk_trace = self.disk.trace_add("write", self.disk_changed)
        if self.token.get().strip():
            self.schedule_stock()

    def sync_selectors(self):
        chosen = self.preferences
        signs = chosen.regions or self.catalog.resolve_regions(chosen.region_groups)
        if signs == self.catalog.resolve_regions() and not chosen.region_groups:
            region = ALL_REGIONS
        else:
            region = next((g["name"] for g in self.catalog.regions if set(g["signs"]) == set(signs)), CUSTOM_REGIONS)
        values = [ALL_REGIONS] + [g["name"] for g in self.catalog.regions]
        if region == CUSTOM_REGIONS:
            values.append(CUSTOM_REGIONS)
        self.region_combo.configure(values=values)
        self.region.set(region)
        self.gpu.set(chosen.gpu_names[0] if len(chosen.gpu_names) == 1 else CUSTOM_GPUS if chosen.gpu_names else ALL_GPUS)
        self.disk.set(str(chosen.expand_disk_gb))
        self.render_gpu_choices()

    def disk_changed(self, *_):
        if self.busy:
            return
        self.rows = []
        self.table.delete(*self.table.get_children())
        raw = self.disk.get().strip()
        if raw and not re.fullmatch(r"[0-9]+", raw):
            self.disk_info.set("扩容容量应为非负整数 GB；0 表示不扩容。")
            self.details.set("请填写有效的扩容容量后重新扫描。")
            return
        size = int(raw or 0)
        self.preferences = replace(self.preferences, expand_disk_gb=size)
        self.disk_info.set(f"新增 {size} GB；目标容量 = 主机免费数据盘 + {size} GB。自动筛掉扩容不足的主机，日费另列。" if size else "填写新增容量，0 表示不扩容；自动筛选可扩容主机，数据盘费用按日单独计算。")
        self.details.set("已更新数据盘条件，重新扫描后自动加载对应容量和费用。")

    def render_gpu_choices(self):
        names = self.catalog.gpu_names()
        if self.stock is not None:
            names = [name for name in self.stock if not self.catalog.model(name).get("is_cpu", name.upper().startswith("CPU"))]
        names = list(dict.fromkeys(names + self.preferences.gpu_names))
        self.gpu_combo.configure(values=[ALL_GPUS] + names + ([CUSTOM_GPUS] if len(self.preferences.gpu_names) > 1 else []))
        if self.stock is None:
            return
        selected = self.preferences.gpu_names
        if selected:
            summary = "；".join(f"{name}：{self.stock[name]['idle']} / {self.stock[name]['total']}" if name in self.stock else f"{name}：本地区未提供" for name in selected)
        else:
            remaining = [(name, self.stock[name]) for name in names if name in self.stock]
            summary = f"{len(remaining)} 种 GPU 型号，{sum(v['idle'] for _, v in remaining)} 张空闲卡"
        self.stock_info.set(f"{self.region.get()} · {summary}（空闲 / 总量）；无余量会继续等待，不换地区或型号。")

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
        self.details.set("已切换租卡范围，扫描后自动加载该范围内的配置、价格和镜像。")
        self.status.set(f"当前范围：{self.region.get()} / {self.gpu.get()}；点击扫描或自动租卡。")
        self.stock_info.set("正在刷新该地区库存…" if self.token.get().strip() else "登录后自动读取该地区库存。")
        self.schedule_stock()

    def region_changed(self, _=None):
        if self.busy:
            return
        value = self.region.get()
        if value == CUSTOM_REGIONS:
            return
        groups = [] if value == ALL_REGIONS else [value]
        self.preferences = replace(self.preferences, region_groups=groups, regions=self.catalog.resolve_regions(groups))
        self.sync_selectors()
        self.scope_changed()

    def gpu_changed(self, _=None):
        if self.busy or self.gpu.get() == CUSTOM_GPUS:
            return
        self.preferences = replace(self.preferences, gpu_names=[] if self.gpu.get() == ALL_GPUS else [self.gpu.get()])
        self.rows = []
        self.table.delete(*self.table.get_children())
        self.details.set("已切换型号，扫描后自动加载该型号的配置、价格和镜像。")
        self.status.set(f"当前范围：{self.region.get()} / {self.gpu.get()}；点击扫描或自动租卡。")
        self.render_gpu_choices()

    def token_changed(self, *_):
        self.cancel_stock_refresh()
        self.stock = None
        self.render_gpu_choices()
        self.stock_info.set("正在刷新该地区库存…" if self.token.get().strip() else "登录后自动读取该地区库存。")
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
            self.stock_info.set("地区库存未刷新：" + str(error).replace(credential, "[已隐藏]"))
            return
        self.stock_info.set("正在刷新该地区库存…")
        def work():
            try:
                self.events.put(("scope_stock", (generation, api.region_stock(signs))))
            except Exception as error:
                message = str(error).replace(credential, "[已隐藏]")
                self.events.put(("scope_stock_error", (generation, message)))
        threading.Thread(target=work, daemon=True).start()

    def settings(self):
        raw = self.disk.get().strip()
        if raw and not re.fullmatch(r"[0-9]+", raw):
            raise QuickError("扩容数据盘应为非负整数 GB；0 表示不扩容")
        chosen = replace(self.preferences, auto_image=True, expand_disk_gb=int(raw or 0))
        if not chosen.regions:
            chosen.regions = self.catalog.resolve_regions(chosen.region_groups)
        return chosen

    def api(self):
        if self.demo:
            raise QuickError("演示模式不联网")
        return AutoDL(self.token.get(), read_only=self.read_only)

    def write_log(self, text):
        token = self.token.get().strip()
        value = str(text).replace(token, "[已隐藏]") if token else str(text)
        self.log.configure(state="normal")
        self.log.insert("end", time.strftime("%H:%M:%S") + "  " + value + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def start(self, waiting, *, execute=False):
        if self.busy:
            return
        if self.demo:
            self.write_log("当前是演示模式，不发出网络请求。")
            return
        if execute and self.read_only:
            self.status.set("当前为只读测试，无法租卡；请通过 start.cmd 打开正常窗口。")
            return
        try:
            chosen, api = self.settings(), self.api()
            chosen.validate(renting=execute)
        except QuickError as error:
            messagebox.showerror("请检查设置", str(error), parent=self.root)
            return
        self.busy = True
        self.cancel_stock_refresh()
        generation = self._stock_generation
        self.cancel.clear()
        self.status.set(f"正在找卡：{self.region.get()} / {self.gpu.get()}；" + ("命中后按平台当前价租用一台、单张 GPU，成功后停止。" if execute else "仅查询，自动加载价格、配置和镜像…"))
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
                    self.events.put(("log", "官网目录暂未刷新，使用最近的已验证目录。"))
                if waiting:
                    report["result"] = engine.auto_wait(chosen, self.cancel, execute=execute)
                else:
                    cards = engine.discover(chosen, self.cancel)
                    report["cards"] = [card.display() for card in cards]
                report["status"] = "complete"
            except Cancelled:
                report["status"] = "cancelled"
            except Exception as error:
                report.update(status="error", error=str(error).replace(credential, "[已隐藏]"))
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
            self.table.insert("", "end", iid=str(index), values=(card.gpu, card.region, str(card.quote) if card.quote is not None else "待查询",
                             card.memory_gb or "—", card.cpu_cores or "—", card.ram_gb or "—",
                             f"{card.disk_gb:g}+{card.expand_disk_gb}={card.disk_gb + card.expand_disk_gb:g}" if card.expand_disk_gb else card.disk_gb or "—",
                             str(card.disk_daily_price) if card.disk_daily_price is not None else "待报价", card.idle,
                             "已匹配" if card.image_uuid else "未匹配"))
        if cards:
            self.table.selection_set("0")
            self.show_selected()
            self.status.set(f"发现 {len(cards)} 台有余量的主机，已自动加载配置；当前仅查询。" if self.read_only else f"发现 {len(cards)} 台有余量的主机。")
        else:
            self.details.set("当前没有满足平台可出租条件的剩余 GPU；持续查找会自动等待下一轮。")

    def show_selected(self, _=None):
        selection = self.table.selection()
        if not selection:
            return
        card = self.rows[int(selection[0])]
        self.details.set(f"{card.gpu} · {card.region} · {card.chip} / {card.arch} · 当前 ¥{card.quote}/小时\n"
                         f"CPU：{card.cpu_cores or '未提供'} 核 {card.cpu_name}    内存：{card.ram_gb or '未提供'} GB    显存：{card.memory_gb or '未提供'} GB\n"
                         f"驱动：{card.driver or '未提供'}    空闲卡：{card.idle}    主机：{card.machine_id}\n"
                         f"数据盘：免费 {card.disk_gb:g} + 扩容 {card.expand_disk_gb} = {card.disk_gb + card.expand_disk_gb:g} GB {card.disk_type}；最多可增 {card.disk_expandable_gb if card.disk_expandable_gb is not None else '未提供'} GB；费用 ¥{card.disk_daily_price if card.disk_daily_price is not None else '待报价'}/日\n"
                         f"自动镜像：{card.image_label or '未找到兼容镜像'}  {card.image_uuid}")

    def advanced(self):
        from market_gui import App as AdvancedApp
        window = tk.Toplevel(self.root)
        editor = AdvancedApp(window, self.settings(), read_only=True)
        editor.token.set(self.token.get())
        def apply():
            try:
                self.preferences = editor.get_settings()
                self.preferences.auto_image = True
                self.sync_selectors()
                self.scope_changed()
                editor.close()
            except QuickError as error:
                messagebox.showerror("请检查偏好", str(error), parent=window)
        editor.save_button.configure(text="应用偏好", command=apply)

    def stop(self):
        self.cancel.set()
        self.status.set("正在停止，等待当前请求结束")

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
                        self.stock_info.set("地区库存未刷新：" + message)
                elif kind == "found_cards":
                    self.show_cards(value)
                elif kind == "created":
                    self.status.set("已租用一台，自动停止：" + value["instance_uuid"])
                elif kind == "error":
                    self.write_log(value)
                    self.status.set("查询未完成，请查看下方记录")
                elif kind == "done":
                    self.busy = False
                    for widget in (self.scan_button, self.wait_button, self.token_entry, self.advanced_button, self.stock_button, self.disk_entry):
                        widget.configure(state="normal")
                    self.region_combo.configure(state="readonly")
                    self.gpu_combo.configure(state="readonly")
                    self.stop_button.configure(state="disabled")
                    self.rent_button.configure(state="disabled" if self.read_only else "normal")
                    if value == "complete" and not self.rows:
                        self.status.set("查询完成：当前没有符合平台可出租条件的余量。")
                    if self.closing:
                        self.close()
                        return
        except queue.Empty:
            pass
        self._timer = self.root.after(100, self.drain)

    def token_help(self):
        messagebox.showinfo("获取网页 Token", "登录 AutoDL → F12 → Application / 应用 → Local Storage / 本地存储 → https://www.autodl.com → token。\n\n只粘贴到此窗口，不保存、不发到聊天。", parent=self.root)


def run(settings=None, demo=False, read_only=False):
    root = tk.Tk()
    App(root, settings, demo=demo, read_only=read_only)
    root.mainloop()
