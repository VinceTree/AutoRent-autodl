"""Local model/region selection and one-instance AutoDL watch interface."""
from __future__ import annotations

import copy
from datetime import datetime
from decimal import Decimal
import json
import os
import queue
import re
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk
import time
import webbrowser

from ..core import (AutoDL, Candidate, Cancelled, Engine, QuickError, ROOT,
                          RentalJournal, Settings, atomic_json, create_payload,
                          image_for_candidate)
from ..catalog import Catalog

STRATEGIES = {"先发现可用卡，页内低价优先": "price", "按型号勾选顺序": "gpu_priority", "按地区勾选顺序": "region_priority"}
PLATFORMS = {"NVIDIA / CUDA": "nvidia", "摩尔线程 / MUSA": "mthreads", "CPU": "cpu"}


class App:
    def __init__(self, root: tk.Tk, settings: Settings, demo: bool = False, read_only: bool = False):
        self.root, self.settings, self.demo, self.read_only = root, settings, demo, read_only
        self.catalog = Catalog.load()
        self.events: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.busy = self.renting = self.close_requested = False
        self.rows: list[Candidate] = []
        self.images: list[dict] = []
        self.profiles = copy.deepcopy(settings.image_profiles)
        self.stock: dict | None = None
        self.region_generation = 0
        self._region_timer = None
        self._timer = None
        self.region_vars: dict[str, tk.BooleanVar] = {}
        self.region_order = list(settings.region_groups)
        if not self.region_order and settings.regions:
            self.region_order = list(dict.fromkeys(self.catalog.group_for_sign(sign) for sign in settings.regions))
        self.model_order = list(settings.gpu_names)
        self.all_gpus = not bool(settings.gpu_names)
        root.title("AutoDL Quick · 多型号抢卡" + (" · 演示" if demo else " · 只读测试" if read_only else ""))
        root.geometry(f"1180x{min(980, root.winfo_screenheight() - 70)}")
        root.minsize(980, 820)
        root.configure(bg="#f3f5f8")
        root.protocol("WM_DELETE_WINDOW", self.close)
        style = ttk.Style()
        style.theme_use("clam")
        for name in ("TFrame", "TLabelframe", "TLabel", "TCheckbutton"):
            style.configure(name, background="#f3f5f8")
        style.configure("TLabel", font=("Microsoft YaHei UI", 9))
        style.configure("TLabelframe.Label", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("TButton", font=("Microsoft YaHei UI", 9), padding=(8, 5))
        style.configure("Accent.TButton", background="#3057d5", foreground="white")
        style.map("Accent.TButton", background=[("active", "#2444ae"), ("disabled", "#aeb9d7")])
        style.configure("Treeview", font=("Microsoft YaHei UI", 9), rowheight=25)
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))
        root.option_add("*Font", ("Microsoft YaHei UI", 9))
        wrap = ttk.Frame(root, padding=16)
        wrap.pack(fill="both", expand=True)
        header = ttk.Frame(wrap)
        header.pack(fill="x")
        ttk.Label(header, text="AutoDL Quick", font=("Segoe UI", 22, "bold"), foreground="#17233d").pack(side="left")
        ttk.Label(header, text="多型号 · 多地区 · 每次只租一台单卡实例", foreground="#52617b").pack(side="left", padx=20)
        self.status = tk.StringVar(value="只读测试：已禁止下单和实例操作" if read_only else "默认仅演练，选择能接受的型号和地区")
        ttk.Label(wrap, textvariable=self.status, foreground="#3057d5").pack(anchor="w", pady=(2, 8))

        credentials = ttk.Frame(wrap)
        credentials.pack(fill="x", pady=(0, 8))
        ttk.Label(credentials, text="网页 Token").pack(side="left")
        self.token = tk.StringVar(value="" if demo else os.environ.get("AUTODL_WEB_TOKEN", ""))
        self.token_entry = ttk.Entry(credentials, textvariable=self.token, show="•")
        self.token_entry.pack(side="left", fill="x", expand=True, padx=10)
        ttk.Button(credentials, text="获取方法", command=self.token_help).pack(side="left")
        ttk.Button(credentials, text="打开市场", command=lambda: webbrowser.open("https://www.autodl.com/market/list")).pack(side="left", padx=(8, 0))

        market = ttk.LabelFrame(wrap, text="官网地区与型号", padding=9)
        market.pack(fill="x")
        top = ttk.Frame(market)
        top.pack(fill="x")
        self.all_regions = tk.BooleanVar(value=not bool(self.region_order))
        self.all_regions_check = ttk.Checkbutton(top, text="全部公开地区", variable=self.all_regions, command=self.select_all_regions)
        self.all_regions_check.pack(side="left")
        self.region_info = tk.StringVar()
        ttk.Label(top, textvariable=self.region_info, foreground="#52617b").pack(side="left", padx=8)
        self.catalog_button = ttk.Button(top, text="刷新官网目录", command=self.refresh_catalog)
        self.catalog_button.pack(side="right")
        self.stock_button = ttk.Button(top, text="刷新地区库存", command=self.schedule_stock)
        self.stock_button.pack(side="right", padx=8)
        self.region_frame = ttk.Frame(market)
        self.region_frame.pack(fill="x", pady=3)
        self.region_buttons = []
        self.render_regions()

        model_top = ttk.Frame(market)
        model_top.pack(fill="x", pady=(4, 3))
        ttk.Label(model_top, text="可接受型号").pack(side="left")
        self.model_buttons = []
        for label, action in (("全部 GPU", self.select_all_gpus), ("仅 NVIDIA", self.select_nvidia), ("清空", self.clear_gpus)):
            button = ttk.Button(model_top, text=label, command=action)
            button.pack(side="left", padx=(6, 0))
            self.model_buttons.append(button)
        self.model_info = tk.StringVar()
        ttk.Label(model_top, textvariable=self.model_info, foreground="#52617b").pack(side="left", padx=12)
        self.search = tk.StringVar()
        self.search_entry = ttk.Entry(model_top, textvariable=self.search, width=22)
        self.search_entry.pack(side="right")
        ttk.Label(model_top, text="搜索型号 ").pack(side="right")
        self.search.trace_add("write", lambda *_: self.render_models())
        model_frame = ttk.Frame(market)
        model_frame.pack(fill="x")
        self.model_table = ttk.Treeview(model_frame, columns=("selected", "model", "memory", "chip", "stock"), show="headings", height=5, selectmode="browse")
        for key, label, width in (("selected", "选择", 45), ("model", "型号（点击切换；支持多选）", 370), ("memory", "显存 GB", 100), ("chip", "平台", 120), ("stock", "已选地区空闲 / 总量", 220)):
            self.model_table.heading(key, text=label)
            self.model_table.column(key, width=width, anchor="w")
        model_scroll = ttk.Scrollbar(model_frame, command=self.model_table.yview)
        self.model_table.configure(yscrollcommand=model_scroll.set)
        self.model_table.pack(side="left", fill="x", expand=True)
        model_scroll.pack(side="right", fill="y")
        self.model_table.bind("<ButtonRelease-1>", self.toggle_model)
        self.model_table.bind("<space>", self.toggle_model)
        self.render_models()

        form = ttk.LabelFrame(wrap, text="等待条件与镜像", padding=9)
        form.pack(fill="x", pady=8)
        self.price = tk.StringVar(value=settings.max_price)
        self.interval = tk.StringVar(value=str(settings.poll_seconds))
        self.wait = tk.StringVar(value=str(settings.wait_minutes))
        self.vram = tk.StringVar(value=str(settings.min_vram_gb))
        self.disk = tk.StringVar(value=str(settings.expand_disk_gb))
        self.strategy = tk.StringVar(value=next(k for k, v in STRATEGIES.items() if v == settings.strategy))
        self.editable = []
        for col, (label, var, width) in enumerate((("最高价 元/时（选填）", self.price, 9), ("间隔 秒", self.interval, 6), ("最长等待 分钟", self.wait, 7), ("最低显存 GB", self.vram, 5))):
            ttk.Label(form, text=label).grid(row=0, column=col * 2, padx=(0, 5), sticky="w")
            entry = ttk.Entry(form, textvariable=var, width=width)
            entry.grid(row=0, column=col * 2 + 1, padx=(0, 12), sticky="w")
            self.editable.append(entry)
        ttk.Label(form, text="排序").grid(row=1, column=0, sticky="w", pady=(7, 0))
        self.strategy_combo = ttk.Combobox(form, textvariable=self.strategy, values=list(STRATEGIES), state="readonly", width=29)
        self.strategy_combo.grid(row=1, column=1, columnspan=3, sticky="w", pady=(7, 0))
        ttk.Label(form, text="价格留空 = 按平台当前价；显存 0 = 不限", foreground="#52617b").grid(row=1, column=4, columnspan=4, sticky="w", pady=(7, 0))
        self.platform = tk.StringVar(value=next(k for k, v in PLATFORMS.items() if v == settings.image_chip))
        self.active_platform = settings.image_chip
        self.platform_combo = ttk.Combobox(form, textvariable=self.platform, values=list(PLATFORMS), state="readonly", width=20)
        self.platform_combo.grid(row=2, column=0, columnspan=2, sticky="w", pady=(7, 0))
        self.platform_combo.bind("<<ComboboxSelected>>", self.platform_changed)
        self.image = tk.StringVar(value=settings.image_uuid)
        self.image_cuda = tk.StringVar(value=settings.image_cuda)
        self.image_kind = tk.StringVar(value=settings.image_kind)
        self.image_combo = ttk.Combobox(form, textvariable=self.image)
        self.image_combo.grid(row=2, column=2, columnspan=5, sticky="ew", pady=(7, 0), padx=(0, 8))
        self.image_combo.bind("<<ComboboxSelected>>", self.select_image)
        self.image_button = ttk.Button(form, text="加载平台镜像", command=self.load_images)
        self.image_button.grid(row=2, column=7, sticky="e", pady=(7, 0))
        ttk.Label(form, text="镜像运行时版本").grid(row=3, column=0, sticky="w", pady=(7, 0))
        image_runtime = ttk.Entry(form, textvariable=self.image_cuda, width=9)
        image_runtime.grid(row=3, column=1, sticky="w", pady=(7, 0))
        self.editable.append(image_runtime)
        self.kind_combo = ttk.Combobox(form, textvariable=self.image_kind, values=["base", "private"], state="readonly", width=9)
        self.kind_combo.grid(row=3, column=2, sticky="w", pady=(7, 0))
        self.profile_info = tk.StringVar()
        ttk.Label(form, textvariable=self.profile_info, foreground="#52617b").grid(row=3, column=3, columnspan=4, sticky="w", pady=(7, 0))
        self.model_image_button = ttk.Button(form, text="型号专用镜像", command=self.model_image_dialog)
        self.model_image_button.grid(row=3, column=7, sticky="e", pady=(7, 0))
        ttk.Label(form, text="扩容数据盘 GB").grid(row=4, column=0, sticky="w", pady=(7, 0))
        disk_entry = ttk.Entry(form, textvariable=self.disk, width=9)
        disk_entry.grid(row=4, column=1, sticky="w", pady=(7, 0))
        self.editable.append(disk_entry)
        ttk.Label(form, text="填新增容量，0 不扩容；容量不足不租用，数据盘按日单独计费", foreground="#52617b").grid(row=4, column=2, columnspan=6, sticky="w", pady=(7, 0))
        form.columnconfigure(5, weight=1)
        self.update_profile_info()

        actions = ttk.Frame(wrap)
        actions.pack(fill="x", pady=(0, 5))
        self.scan_button = ttk.Button(actions, text="查询空闲主机", command=self.scan)
        self.scan_button.pack(side="left")
        self.preview_button = ttk.Button(actions, text="预览下单", command=self.preview)
        self.preview_button.pack(side="left", padx=7)
        self.rent_button = ttk.Button(actions, text="开始库存演练", command=self.rent, style="Accent.TButton")
        self.rent_button.pack(side="left")
        self.stop_button = ttk.Button(actions, text="停止等待", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=7)
        self.real_rent = tk.BooleanVar(value=False)
        self.mode_check = ttk.Checkbutton(actions, text="真实租用（计费）", variable=self.real_rent, command=self.mode_changed)
        self.mode_check.pack(side="left", padx=8)
        self.save_button = ttk.Button(actions, text="保存条件", command=self.save)
        self.save_button.pack(side="right")
        self.mode_changed()
        ttk.Label(wrap, text="默认仅演练。镜像按平台分别保存；真实租用只选择镜像匹配的卡。成功一台即停止。", foreground="#52617b").pack(anchor="w", pady=(0, 5))

        table_wrap = ttk.Frame(wrap)
        table_wrap.pack(fill="both", expand=True)
        self.table = ttk.Treeview(table_wrap, columns=("machine", "region", "gpu", "idle", "cuda", "price"), show="headings", height=4)
        for key, label, width in (("machine", "主机 ID", 185), ("region", "地区", 130), ("gpu", "型号", 210), ("idle", "空闲卡", 65), ("cuda", "最高 CUDA", 95), ("price", "平台价 / 小时", 140)):
            self.table.heading(key, text=label)
            self.table.column(key, width=width, anchor="w")
        table_scroll = ttk.Scrollbar(table_wrap, command=self.table.yview)
        self.table.configure(yscrollcommand=table_scroll.set)
        self.table.pack(side="left", fill="both", expand=True)
        table_scroll.pack(side="right", fill="y")
        self.log = scrolledtext.ScrolledText(wrap, height=4, wrap="word", bg="#152036", fg="#dce5f6", relief="flat", font=("Microsoft YaHei UI", 9), state="disabled")
        self.log.pack(side="bottom", fill="x", pady=(7, 0), before=table_wrap)
        self.write_log("演示模式：示例数据，完全不联网。" if demo else f"目录包含 {len(self.catalog.models)} 个型号、{len(self.catalog.regions)} 个地区分组。Token 只留在当前窗口。")
        if demo:
            self.price.set("4.00")
            self.demo_rows()
        self._timer = root.after(100, self.drain)

    def render_regions(self):
        for widget in self.region_frame.winfo_children():
            widget.destroy()
        self.region_vars, self.region_buttons = {}, []
        ordered = [g for g in self.catalog.regions if g["kind"] == "normal"] + [g for g in self.catalog.regions if g["kind"] != "normal"]
        for i, group in enumerate(ordered):
            chosen = (group["visible"] == "all" and group["used_for"] == "all") if self.all_regions.get() else group["id"] in self.region_order
            var = tk.BooleanVar(value=chosen)
            self.region_vars[group["id"]] = var
            label = group["name"] + ("（企业）" if group["visible"] == "enterprise" else "")
            button = ttk.Checkbutton(self.region_frame, text=label, variable=var, command=lambda key=group["id"]: self.region_changed(key))
            button.grid(row=i // 6, column=i % 6, sticky="w", padx=(0, 18))
            self.region_buttons.append(button)
        self.update_region_info()

    def selected_regions(self):
        if self.all_regions.get():
            return []
        selected = [key for key in self.region_order if key in self.region_vars and self.region_vars[key].get()]
        if not selected:
            raise QuickError("请至少选择一个地区，或选择全部公开地区")
        return selected

    def update_region_info(self):
        try:
            groups = self.selected_regions()
            signs = self.catalog.resolve_regions(groups)
            self.region_info.set(f"覆盖 {len(signs)} 个机房；点击地区后可多选，专区单独选择")
        except QuickError:
            self.region_info.set("尚未选择地区")

    def select_all_regions(self):
        self.region_order = []
        for group in self.catalog.regions:
            self.region_vars[group["id"]].set(self.all_regions.get() and group["visible"] == "all" and group["used_for"] == "all")
        self.changed_region_scope()

    def region_changed(self, key):
        if self.all_regions.get():
            self.all_regions.set(False)
            for name, var in self.region_vars.items():
                var.set(name == key)
            self.region_order = [key]
        elif self.region_vars[key].get():
            if key not in self.region_order:
                self.region_order.append(key)
        else:
            self.region_order = [name for name in self.region_order if name != key]
        self.changed_region_scope()

    def changed_region_scope(self):
        self.region_generation += 1
        self.stock = None
        self.update_region_info()
        if hasattr(self, "model_table"):
            self.render_models()
            self.schedule_stock()

    def render_models(self):
        self.model_table.delete(*self.model_table.get_children())
        search = self.search.get().lower()
        for model in self.catalog.models:
            name = model["name"]
            if search and search not in name.lower():
                continue
            chosen = not model["is_cpu"] if self.all_gpus else name in self.model_order
            if self.stock is None:
                stock = "待刷新"
            elif name not in self.stock:
                stock = "本地区未提供"
            else:
                values = self.stock[name]
                stock = f"{values['idle']} / {values['total']}"
            self.model_table.insert("", "end", iid=name, values=("☑" if chosen else "☐", name, model["memory_gb"] or "—", model["chip"], stock))
        selected = self.catalog.gpu_names() if self.all_gpus else self.model_order
        self.model_info.set(f"已选 {len(selected)} 种；CPU 需单独勾选")

    def toggle_model(self, event=None):
        if self.busy:
            return
        item = self.model_table.identify_row(event.y) if event and hasattr(event, "y") and getattr(event, "keysym", "") != "space" else next(iter(self.model_table.selection()), "")
        if not item:
            return
        if self.all_gpus:
            self.all_gpus = False
            self.model_order = [item]
        elif item in self.model_order:
            self.model_order.remove(item)
        else:
            self.model_order.append(item)
        self.render_models()

    def select_all_gpus(self):
        self.all_gpus, self.model_order = True, []
        self.render_models()

    def select_nvidia(self):
        self.all_gpus = False
        self.model_order = [m["name"] for m in self.catalog.models if m["chip"] == "nvidia" and not m["is_cpu"]]
        self.render_models()

    def clear_gpus(self):
        self.all_gpus, self.model_order = False, []
        self.render_models()

    def store_profile(self):
        value, version, kind = self.image.get().strip(), self.image_cuda.get().strip(), self.image_kind.get()
        for image in self.images:
            if value == image["label"]:
                value, version, kind = image["uuid"], image["cuda"], image["kind"]
                break
        key = self.active_platform + ":x86"
        if value:
            self.profiles[key] = {"uuid": value, "version": version, "kind": kind, "chip": self.active_platform, "arch": "x86"}
        else:
            self.profiles.pop(key, None)
        self.update_profile_info()
        return value, version, kind

    def update_profile_info(self):
        chips = [name for name, chip in PLATFORMS.items() if chip + ":x86" in self.profiles]
        custom = sum(key.startswith("gpu:") for key in self.profiles)
        self.profile_info.set("已配置：" + ("、".join(chips) if chips else "无平台通用镜像") + (f"；{custom} 种型号专用" if custom else ""))

    def model_image_dialog(self):
        self.store_profile()
        window = tk.Toplevel(self.root)
        window.title("配置型号专用镜像")
        window.geometry("660x270")
        frame = ttk.Frame(window, padding=16)
        frame.pack(fill="both", expand=True)
        names = self.catalog.gpu_names(include_cpu=True)
        name = tk.StringVar(value=self.model_order[0] if self.model_order else names[0])
        value, version, kind = tk.StringVar(), tk.StringVar(), tk.StringVar(value="base")
        for row, (label, variable) in enumerate((("算力型号", name), ("镜像 UUID", value), ("CUDA / MUSA 版本", version), ("镜像类型", kind))):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=4)
            if row in (0, 3):
                widget = ttk.Combobox(frame, textvariable=variable, values=names if row == 0 else ["base", "private"], state="readonly")
            else:
                widget = ttk.Entry(frame, textvariable=variable)
            widget.grid(row=row, column=1, sticky="ew", pady=4)
            if row == 0:
                def load(_=None):
                    profile = self.profiles.get("gpu:" + name.get(), {})
                    value.set(profile.get("uuid", ""))
                    version.set(profile.get("version", ""))
                    kind.set(profile.get("kind", "base"))
                widget.bind("<<ComboboxSelected>>", load)
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text="此配置优先于平台通用镜像；UUID 留空可移除专用配置。", foreground="#52617b").grid(row=4, column=0, columnspan=2, sticky="w", pady=10)
        def apply():
            key = "gpu:" + name.get()
            if value.get().strip():
                if not re.fullmatch(r"[A-Za-z0-9_-]+", value.get().strip()):
                    messagebox.showerror("镜像 UUID 不正确", "请填写控制台或镜像列表中的 UUID", parent=window)
                    return
                model = self.catalog.model(name.get())
                self.profiles[key] = {"uuid": value.get().strip(), "version": version.get().strip(),
                                      "kind": kind.get(), "chip": model["chip"], "arch": model["arch"]}
            else:
                self.profiles.pop(key, None)
            self.update_profile_info()
            window.destroy()
        ttk.Button(frame, text="应用到当前配置", command=apply).grid(row=5, column=1, sticky="e")
        load()

    def platform_changed(self, _=None):
        self.store_profile()
        self.active_platform = PLATFORMS[self.platform.get()]
        profile = self.profiles.get(self.active_platform + ":x86", {})
        self.image.set(profile.get("uuid", ""))
        self.image_cuda.set(profile.get("version", ""))
        self.image_kind.set(profile.get("kind", "base"))
        self.images = []
        self.image_combo.configure(values=[])
        self.update_profile_info()

    def get_settings(self, renting=False):
        if not self.all_gpus and not self.model_order:
            raise QuickError("请至少勾选一种型号，或选择全部 GPU")
        groups = self.selected_regions()
        value, version, kind = self.store_profile()
        try:
            result = Settings(max_price=self.price.get().strip(), poll_seconds=int(self.interval.get()),
                              wait_minutes=int(self.wait.get()), min_vram_gb=int(self.vram.get()),
                              expand_disk_gb=int(self.disk.get().strip() or 0),
                              gpu_names=[] if self.all_gpus else list(self.model_order), region_groups=groups,
                              regions=self.catalog.resolve_regions(groups), strategy=STRATEGIES[self.strategy.get()],
                              image_uuid=value, image_cuda=version, image_kind=kind,
                              image_chip=self.active_platform, image_arch="x86", image_profiles=copy.deepcopy(self.profiles))
        except ValueError:
            raise QuickError("间隔、等待时长、显存、扩容容量应为整数；地区应来自官网目录") from None
        result.validate(renting)
        return result

    def api(self):
        if self.demo:
            raise QuickError("演示模式不联网，请正常启动工具")
        return AutoDL(self.token.get(), read_only=self.read_only)

    def write_log(self, text):
        credential = self.token.get().strip()
        text = str(text).replace(credential, "[已隐藏]") if credential else str(text)
        self.log.configure(state="normal")
        self.log.insert("end", time.strftime("%H:%M:%S") + "  " + text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def schedule_stock(self):
        if self._region_timer is not None:
            self.root.after_cancel(self._region_timer)
        self._region_timer = self.root.after(400, self.refresh_stock)

    def refresh_stock(self):
        self._region_timer = None
        if self.demo or not self.token.get().strip() or self.busy:
            return
        try:
            signs = self.catalog.resolve_regions(self.selected_regions())
            api = self.api()
        except QuickError:
            return
        generation = self.region_generation
        def work():
            try:
                self.events.put(("region_stock", (generation, api.region_stock(signs))))
            except Exception as error:
                self.events.put(("log", f"地区库存未刷新：{error}"))
        threading.Thread(target=work, daemon=True).start()

    def set_busy(self, busy):
        state = "disabled" if busy else "normal"
        for widget in (self.scan_button, self.rent_button, self.preview_button, self.save_button,
                       self.image_button, self.catalog_button, self.stock_button, self.all_regions_check,
                       self.model_image_button,
                       self.token_entry, self.image_combo, *self.model_buttons, *self.region_buttons, *self.editable):
            widget.configure(state=state)
        for widget in (self.strategy_combo, self.platform_combo, self.kind_combo):
            widget.configure(state="disabled" if busy else "readonly")
        self.stop_button.configure(state="normal" if busy else "disabled")
        self.mode_check.configure(state="disabled" if busy or self.read_only else "normal")

    def launch(self, action, *, renting=False):
        if self.busy:
            return
        self.busy, self.renting = True, renting
        self.cancel.clear()
        self.set_busy(True)
        self.status.set("等待可用卡，成功一台即停止" if renting else "正在查询 / 演练所选型号与地区…")
        def worker():
            try:
                action()
            except Cancelled as error:
                self.events.put(("log", str(error)))
            except Exception as error:
                self.events.put(("error", str(error)))
            finally:
                self.events.put(("done", None))
        threading.Thread(target=worker, daemon=False).start()

    def refresh_catalog(self):
        if self.demo:
            return
        self.launch(lambda: self.events.put(("catalog", Catalog.refresh())))

    def scan(self):
        if self.demo:
            self.demo_rows()
            return
        try:
            settings, api = self.get_settings(), self.api()
        except QuickError as error:
            messagebox.showerror("请检查设置", str(error), parent=self.root)
            return
        generation = self.region_generation
        credential = self.token.get().strip()
        def action():
            report = {"started_at": datetime.now().astimezone().isoformat(), "status": "running",
                      "read_only": True, "gpu_names": settings.gpu_names or "全部 GPU（不含 CPU）",
                      "region_groups": settings.region_groups or "全部公开地区", "region_signs": settings.regions,
                      "paid_requests_allowed": False}
            def save_report():
                if self.read_only:
                    report["requests"] = api.audit
                    atomic_json(ROOT / ".state" / "inventory-query.json", report)
            save_report()
            try:
                stock = api.region_stock(settings.regions)
                self.events.put(("region_stock", (generation, stock)))
                report["region_model_stock"] = stock
                rows = Engine(api, lambda *event: self.events.put(event)).scan(settings, self.cancel)
                report.update(status="inventory_received", hosts=len(rows), idle_gpus=sum(r.idle for r in rows), candidates=[r.display() for r in rows])
                save_report()
                for row in rows[:10]:
                    if self.cancel.is_set():
                        break
                    Engine(api).quote_candidate(settings, row)
                    self.events.put(("candidates", rows))
                report.update(status="complete", completed_at=datetime.now().astimezone().isoformat(), candidates=[r.display() for r in rows])
                save_report()
                self.events.put(("log", "查询完成；前 10 台已核对账户当前报价，其余标注库存原价。没有创建实例。"))
            except Exception as error:
                report.update(status="error", error=str(error).replace(credential, "[已隐藏]"))
                save_report()
                raise
        self.launch(action)

    def load_images(self):
        try:
            api = self.api()
        except QuickError as error:
            messagebox.showerror("请检查登录", str(error), parent=self.root)
            return
        chip = self.active_platform
        self.launch(lambda: self.events.put(("images", api.images(chip, "x86"))))

    def select_image(self, _=None):
        for image in self.images:
            if self.image.get() == image["label"]:
                self.image_cuda.set(image["cuda"])
                self.image_kind.set(image["kind"])
        self.store_profile()

    def rent(self):
        execute = self.real_rent.get() and not self.read_only
        try:
            settings, api = self.get_settings(execute), self.api()
            if execute:
                RentalJournal().ensure_new()
        except QuickError as error:
            messagebox.showerror("暂时无法开始", str(error), parent=self.root)
            return
        self.launch(lambda: Engine(api, lambda *event: self.events.put(event)).wait_for_match(settings, self.cancel, execute=execute), renting=execute)

    def mode_changed(self):
        if self.read_only:
            self.real_rent.set(False)
        self.rent_button.configure(text="开始自动抢卡" if self.real_rent.get() else "开始库存演练")
        if self.read_only:
            self.mode_check.configure(state="disabled")

    def preview(self):
        try:
            settings = self.get_settings(True)
            selected = self.table.selection()
            if not selected:
                raise QuickError("先查询并选中一台主机")
            row = self.rows[int(selected[0])]
            matched = image_for_candidate(settings, row)
            if matched is None:
                raise QuickError("此主机缺少匹配平台 / CUDA 的镜像，请先配置")
        except QuickError as error:
            messagebox.showinfo("预览下单", str(error), parent=self.root)
            return
        window = tk.Toplevel(self.root)
        window.title("仅预览 · 没有创建实例")
        window.geometry("760x600")
        condition = f"最高价 ¥{settings.max_price}/小时" if settings.max_price else "按平台当前报价"
        ttk.Label(window, text=f"{row.gpu} / {row.region}，{condition}", padding=12).pack(anchor="w")
        text = scrolledtext.ScrolledText(window, wrap="word")
        text.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        text.insert("end", json.dumps(create_payload(row.machine_id, matched, "quick-rent-PREVIEW"), ensure_ascii=False, indent=2))
        text.configure(state="disabled")

    def save(self):
        try:
            self.get_settings().save()
            self.write_log("型号、地区、条件和各平台镜像已保存；Token 未保存。")
        except (QuickError, OSError) as error:
            messagebox.showerror("保存失败", str(error), parent=self.root)

    def stop(self):
        self.cancel.set()
        self.status.set("正在停止等待，当前网络请求结束后生效")
        self.write_log("停止等待；已创建的实例需在控制台管理。")

    def close(self):
        if self.busy:
            self.close_requested = True
            self.stop()
            return
        for timer in (self._timer, self._region_timer):
            if timer is not None:
                self.root.after_cancel(timer)
        self._timer = self._region_timer = None
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
                elif kind == "region_stock":
                    generation, stock = value
                    if generation == self.region_generation:
                        self.stock = stock
                        self.render_models()
                elif kind == "catalog":
                    previous = {key for key, var in self.region_vars.items() if var.get()}
                    self.catalog = value
                    if not self.all_regions.get():
                        missing = previous - {g["id"] for g in value.regions}
                        if missing:
                            self.write_log("官网地区已变化，请重新核对地区选择：" + ", ".join(missing))
                    self.render_regions()
                    self.changed_region_scope()
                    self.write_log(f"已刷新官网目录：{len(value.models)} 个型号、{len(value.regions)} 个地区分组。")
                elif kind == "candidates":
                    self.rows = value
                    self.table.delete(*self.table.get_children())
                    for i, row in enumerate(value):
                        price = f"¥{row.quote}" if row.quote is not None else f"¥{row.list_price}（库存原价）"
                        self.table.insert("", "end", iid=str(i), values=(row.machine_id, row.region, row.gpu, row.idle, row.cuda if row.chip == "nvidia" else "—", price))
                elif kind == "images":
                    self.images = value
                    self.image_combo.configure(values=[image["label"] for image in value])
                    if value:
                        self.image.set(value[0]["label"])
                        self.select_image()
                    self.write_log(f"已加载 {len(value)} 个 {self.active_platform} x86 镜像；请选适合任务的版本。")
                elif kind == "created":
                    self.status.set("租用成功 · " + value["instance_uuid"])
                elif kind == "error":
                    self.status.set("已停止 · 请查看记录")
                    self.write_log(value)
                elif kind == "done":
                    self.busy = self.renting = False
                    self.set_busy(False)
                    if self.status.get().startswith(("正在", "等待")):
                        self.status.set("已停止等待" if self.cancel.is_set() else "查询 / 演练完成 · 没有新增实例")
                    if self.close_requested:
                        self.close()
                        return
        except queue.Empty:
            pass
        self._timer = self.root.after(100, self.drain)

    def token_help(self):
        messagebox.showinfo("在本机获取网页 Token", "登录 https://www.autodl.com → F12 → Application / 应用 → Local Storage / 本地存储 → https://www.autodl.com → token。\n\n复制值到本地窗口。Token 不保存，不要发到聊天里。开发者 Token 不适用于这个网页市场。", parent=self.root)

    def demo_rows(self):
        self.events.put(("candidates", [Candidate("demo-host-01", "西北B区（示例）", "west-B", 2, "13.0", "RTX 5090", Decimal("3.50"), Decimal("3.50")),
                                        Candidate("demo-host-02", "北京B区（示例）", "bj-B1", 1, "12.8", "RTX 3090", Decimal("1.80"), Decimal("1.80"))]))
        self.events.put(("images", [{"uuid": "base-image-DEMO", "cuda": "12.8", "kind": "base", "chip": "nvidia", "arch": "x86", "label": "示例 PyTorch / CUDA 12.8"}]))
        self.status.set("演示数据 · 没有连接 AutoDL · 下单禁用")


def run(settings=None, demo=False, read_only=False):
    root = tk.Tk()
    app = App(root, settings or Settings(), demo=demo, read_only=read_only)
    if not demo:
        root.after(500, app.refresh_catalog)
    root.mainloop()
