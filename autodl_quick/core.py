"""Local AutoDL market client. Python 3.10+, standard library only.

The market endpoints are the ones used by autodl.com's own web client.
They require a web session token, not the documented Pro developer token.
"""

from __future__ import annotations

import argparse
import copy
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field, replace
from decimal import Decimal, InvalidOperation
import getpass
import json
import os
from pathlib import Path
import re
import socket
import threading
import time
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .catalog import Catalog, parse_region_stock


ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "https://www.autodl.com"
GPU_NAME = "RTX 5090"
MILLI = Decimal(1000)
GIB = 1024 ** 3


class QuickError(Exception):
    """An error suitable for display without response bodies or credentials."""


class LoginRequired(QuickError):
    pass


class ReadUnavailable(QuickError):
    """A read can be repeated without creating another instance."""


class CreateUncertain(QuickError):
    """Creation might have reached the server; do not repeat it."""


class Cancelled(QuickError):
    pass


class APIError(QuickError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{message}（{code}）")


def positive_decimal(value: Any, label: str = "价格") -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise QuickError(f"{label}必须是大于 0 的数字") from None
    if not number.is_finite() or number <= 0:
        raise QuickError(f"{label}必须是大于 0 的有限数字")
    return number


def amount(raw: Any, label: str) -> Decimal:
    if isinstance(raw, bool):
        raise QuickError(f"{label}格式变化，已停止")
    return positive_decimal(raw, label) / MILLI


def integer(raw: Any, default: int = 0) -> int:
    try:
        return int(raw)
    except (ValueError, TypeError, OverflowError):
        return default


def expanded_disk_bytes(gb: int) -> int:
    if not isinstance(gb, int) or isinstance(gb, bool) or gb < 0:
        raise QuickError("扩容数据盘应为非负整数 GB；0 表示不扩容")
    return gb * GIB


def cuda_version(raw: Any) -> tuple[int, int]:
    match = re.fullmatch(r"\s*(\d+)\.(\d+)(?:\.\d+)?\s*", str(raw))
    return (int(match[1]), int(match[2])) if match else (0, 0)


def exact_gpu(raw: Any) -> bool:
    # Deliberately do not accept RTX 5090 D, 5090D, vGPU, or PRO substitutes.
    name = re.sub(r"\s+", " ", str(raw).upper().strip())
    return name in ("RTX 5090", "NVIDIA RTX 5090", "NVIDIA GEFORCE RTX 5090")


@dataclass
class Settings:
    max_price: str = ""
    poll_seconds: int = 30
    wait_minutes: int = 30
    regions: list[str] = field(default_factory=list)
    image_uuid: str = ""
    image_kind: str = "base"
    image_cuda: str = ""
    min_cuda: str = ""
    gpu_names: list[str] = field(default_factory=list)
    region_groups: list[str] = field(default_factory=list)
    min_vram_gb: int = 0
    strategy: str = "price"
    image_chip: str = "nvidia"
    image_arch: str = "x86"
    image_profiles: dict[str, dict] = field(default_factory=dict)
    auto_image: bool = True
    expand_disk_gb: int = 0

    def validate(self, renting: bool = False) -> None:
        expanded_disk_bytes(self.expand_disk_gb)
        if not isinstance(self.poll_seconds, int) or isinstance(self.poll_seconds, bool):
            raise QuickError("查询间隔必须是整数")
        if not isinstance(self.wait_minutes, int) or isinstance(self.wait_minutes, bool):
            raise QuickError("等待时间必须是整数")
        if not isinstance(self.regions, list) or not all(isinstance(r, str) for r in self.regions):
            raise QuickError("地区代码必须是字符串列表")
        if not 15 <= self.poll_seconds <= 3600:
            raise QuickError("查询间隔应在 15–3600 秒之间")
        if not 1 <= self.wait_minutes <= 1440:
            raise QuickError("等待时间应在 1–1440 分钟之间")
        if self.min_cuda and cuda_version(self.min_cuda) == (0, 0):
            raise QuickError("CUDA 下限应为 11.8、12.8 等版本号，也可以留空")
        if not isinstance(self.gpu_names, list) or not all(isinstance(n, str) and n for n in self.gpu_names):
            raise QuickError("型号必须是名称列表")
        if not isinstance(self.region_groups, list) or not all(isinstance(n, str) for n in self.region_groups):
            raise QuickError("地区分组格式不正确")
        if not isinstance(self.min_vram_gb, int) or not 0 <= self.min_vram_gb <= 1024:
            raise QuickError("最低显存应为 0–1024 GB 的整数")
        if self.strategy not in ("price", "gpu_priority", "region_priority"):
            raise QuickError("未知抢卡排序方式")
        if not isinstance(self.image_profiles, dict):
            raise QuickError("镜像配置格式不正确")
        if self.max_price:
            positive_decimal(self.max_price, "每小时价格上限")
        if self.image_kind not in ("base", "private"):
            raise QuickError("镜像类型应为 base 或 private")
        if renting:
            profiles = list(self.image_profiles.values())
            if self.image_uuid:
                profiles.append({"uuid": self.image_uuid, "kind": self.image_kind,
                                 "version": self.image_cuda, "chip": self.image_chip, "arch": self.image_arch})
            if not profiles and not self.auto_image:
                raise QuickError("请先为所选平台配置镜像")
            for profile in profiles:
                if not isinstance(profile, dict) or not re.fullmatch(r"[A-Za-z0-9_-]+", str(profile.get("uuid", ""))):
                    raise QuickError("镜像 UUID 不正确")
                if profile.get("kind", "base") not in ("base", "private"):
                    raise QuickError("镜像类型不正确")
                if profile.get("chip", "nvidia") == "nvidia" and cuda_version(profile.get("version", "")) == (0, 0):
                    raise QuickError("NVIDIA 镜像请填写正确的 CUDA 版本")

    @classmethod
    def load(cls, path: Path = ROOT / "config.json") -> "Settings":
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if "gpu_names" not in data:
                # Preserve a legacy single-5090 profile's intended model scope.
                data["gpu_names"] = ["RTX 5090"]
                data["min_cuda"] = ""
            result = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
            result.validate()
            return result
        except (ValueError, TypeError, AttributeError) as error:
            raise QuickError("config.json 格式不正确") from error

    def save(self, path: Path = ROOT / "config.json") -> None:
        self.validate()
        atomic_json(path, asdict(self))


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        target = urllib.parse.urlsplit(newurl)
        if (target.scheme, target.netloc) != ("https", "www.autodl.com"):
            raise QuickError("AutoDL 返回跨站重定向，已停止以保护登录凭据")
        return super().redirect_request(request, fp, code, msg, headers, newurl)


@dataclass(frozen=True)
class PriceQuote:
    instance_hourly: Decimal
    disk_daily: Decimal = Decimal(0)


class AutoDL:
    def __init__(self, token: str, timeout: int = 25, *, read_only: bool = False):
        token = token.strip()
        if not token or any(c in token for c in "\r\n"):
            raise LoginRequired("请在本地输入 AutoDL 网页登录 Token")
        self._token = token
        self.timeout = timeout
        self.read_only = read_only
        self.audit: list[dict] = []
        self.opener = urllib.request.build_opener(SameOriginRedirect())

    def request(self, method: str, path: str, body: dict | None = None,
                *, creating: bool = False) -> Any:
        for attempt in range(1 if creating else 3):
            try:
                return self._request_once(method, path, body, creating=creating)
            except ReadUnavailable:
                if creating or attempt == 2:
                    raise
                time.sleep(attempt + 1)

    def _request_once(self, method: str, path: str, body: dict | None = None,
                      *, creating: bool = False) -> Any:
        if not path.startswith("/api/v1/") or ".." in path or "?" in path:
            raise QuickError("无效 API 路径")
        if self.read_only and (method, path) not in {
            ("POST", "/api/v1/user/machine/list"),
            ("POST", "/api/v1/machine/search"),
            ("POST", "/api/v1/order/price/preview"),
            ("POST", "/api/v1/image/all"),
            ("POST", "/api/v1/machine/check_machine_online"),
            ("POST", "/api/v1/machine/region/gpu_type"),
            ("GET", "/api/v1/region/list"),
            ("GET", "/api/v1/machine/gpu_type"),
        }:
            raise QuickError("当前会话为只读测试，已禁止下单和实例操作")
        self.audit.append({"method": method, "path": path})
        request = urllib.request.Request(
            BASE_URL + path,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            method=method,
            headers={"Authorization": self._token, "Content-Type": "application/json",
                     "Accept": "application/json", "Referer": BASE_URL + "/market/list",
                     "User-Agent": "AutoDLQuick/1.0 (local client)"},
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                envelope = json.loads(response.read(8 * 1024 * 1024).decode("utf-8"))
        except urllib.error.HTTPError as error:
            if creating:
                raise CreateUncertain("创建请求返回 HTTP 错误；请先核对控制台，工具不会自动重试") from None
            if error.code in (401, 403):
                raise LoginRequired("登录失效或无权限，请重新登录 AutoDL") from None
            if error.code == 429 or error.code >= 500:
                raise ReadUnavailable(f"AutoDL 暂不可用（HTTP {error.code}）") from None
            raise QuickError(f"AutoDL 接口返回 HTTP {error.code}，可能已变更") from None
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError,
                ValueError, UnicodeError):
            if creating:
                raise CreateUncertain("创建结果未确认；可能已创建成功，请核对控制台，工具不会自动重试") from None
            raise ReadUnavailable("网络或响应异常，可稍后重试查询") from None
        if not isinstance(envelope, dict) or not isinstance(envelope.get("code"), str):
            if creating:
                raise CreateUncertain("创建响应格式变化，请先核对控制台")
            raise QuickError("AutoDL 响应格式变化，已停止")
        code = envelope["code"]
        if code in ("AuthorizeFailed", "Unauthorized", "TokenExpired"):
            raise LoginRequired("网页登录已失效，请重新获取网页 Token；开发者 Token 不适用于此入口")
        if code != "Success":
            # Never log the full envelope, which can contain credentials.
            message = str(envelope.get("msg") or "AutoDL 拒绝请求").replace(self._token, "[已隐藏]")[:400]
            raise APIError(code, message)
        return envelope.get("data")

    def machine_batches(self, settings: Settings, cancel: threading.Event):
        rows: list[dict] = []
        seen: set[str] = set()
        for page in range(1, 101):
            check_cancel(cancel)
            data = self.request("POST", "/api/v1/user/machine/list", {
                "charge_type": "payg", "region_sign": "",
                "region_sign_list": settings.regions, "gpu_type_name": settings.gpu_names,
                "machine_tag_name": [], "gpu_idle_num": 1, "mount_net_disk": False,
                "cpu_arch": [], "chip_corp": [],
                "instance_disk_size_order": "", "date_range": "", "date_from": "", "date_to": "",
                "page_index": page, "page_size": 90, "pay_price_order": "",
                "gpu_idle_type": "desc", "default_order": False,
            })
            if not isinstance(data, dict) or not isinstance(data.get("list"), list):
                raise QuickError("库存响应格式变化，已停止")
            rows.extend(data["list"])
            batch = []
            for row in data["list"]:
                if isinstance(row, dict) and row.get("machine_id") and str(row["machine_id"]) not in seen:
                    seen.add(str(row["machine_id"]))
                    batch.append(row)
            yield batch
            max_page = integer(data.get("max_page"))
            total = integer(data.get("result_total"), -1)
            if max_page and page >= max_page:
                break
            if total >= 0 and len(rows) >= total:
                break
            if not max_page and total < 0 and len(data["list"]) < 90:
                break
        else:
            raise QuickError("库存超过 100 页；请指定地区后重试")
    def machines(self, settings: Settings, cancel: threading.Event) -> list[dict]:
        return [row for batch in self.machine_batches(settings, cancel) for row in batch]

    def region_stock(self, regions: list[str]) -> dict[str, dict]:
        data = self.request("POST", "/api/v1/machine/region/gpu_type", {"region_sign_list": regions})
        if not isinstance(data, list):
            raise QuickError("地区型号库存响应格式变化")
        try:
            return parse_region_stock(data)
        except (ValueError, TypeError):
            raise QuickError("地区型号库存响应格式变化") from None

    def quote(self, machine_id: str) -> Decimal:
        return self.quote_detail(machine_id).instance_hourly

    def quote_detail(self, machine_id: str, expand_disk_gb: int = 0) -> PriceQuote:
        data = self.request("POST", "/api/v1/order/price/preview", price_payload(machine_id, expand_disk_gb))
        if not isinstance(data, dict) or "origin_price" not in data:
            raise QuickError("未获得有效的每小时原价，禁止下单")
        daily = Decimal(0)
        if expand_disk_gb:
            info = data.get("data_disk_info")
            raw_daily = info.get("data_disk_deal_daily_price") if isinstance(info, dict) else None
            try:
                daily = Decimal(str(raw_daily)) / MILLI
            except InvalidOperation:
                raise QuickError("无法核对扩容数据盘日费，禁止下单") from None
            if isinstance(raw_daily, bool) or not daily.is_finite() or daily < 0:
                raise QuickError("扩容数据盘日费格式异常，禁止下单")
        # The pay-as-you-go preview returns payg_price; deal_price is often an unused 0.
        for key in ("payg_price", "deal_price"):
            raw = data.get(key)
            if raw is None:
                continue
            if isinstance(raw, bool):
                raise QuickError("平台报价字段格式异常")
            try:
                if Decimal(str(raw)) == 0:
                    continue
            except InvalidOperation:
                raise QuickError("平台报价字段格式异常") from None
            return PriceQuote(amount(raw, "当前每小时报价"), daily)
        adjustments = [item for item in data.get("discount_list", [])
                       if isinstance(item, dict) and item.get("product_type") == "instance"]
        if adjustments:
            final = adjustments[-1]
            if "origin_price" in final and "total_reduction" in final:
                return PriceQuote(amount(Decimal(str(final["origin_price"])) - Decimal(str(final["total_reduction"])), "当前每小时报价"), daily)
        return PriceQuote(amount(data["origin_price"], "每小时原价"), daily)

    def online(self, machine_id: str) -> bool:
        data = self.request("POST", "/api/v1/machine/check_machine_online", {"machine_id": machine_id})
        if not isinstance(data, int) or isinstance(data, bool):
            raise QuickError("主机在线状态响应格式变化，禁止下单")
        return data not in (0, 1)

    def images(self, chip: str = "nvidia", arch: str = "x86") -> list[dict]:
        data = self.request("POST", "/api/v1/image/all", {"cpu_arch": [arch], "chip_corp": [] if chip == "cpu" else [chip]})
        if not isinstance(data, list):
            raise QuickError("镜像目录响应格式变化")
        images = []

        def visit(node: Any, trail: list[str]) -> None:
            if not isinstance(node, dict):
                return
            label = node.get("label_name", "")
            if isinstance(label, str):
                trail = trail + [label]
            elif isinstance(label, dict) and label.get("uuid"):
                cuda = str(label.get("v", ""))
                if (chip == "cpu" or label.get("chip_corp") == chip) and label.get("cpu_arch") == arch:
                    images.append({"uuid": label["uuid"], "cuda": cuda,
                                   "label": " / ".join(trail + [(("MUSA " if chip == "mthreads" else "CUDA ") + cuda) if cuda else "CPU"]),
                                   "kind": "base", "chip": chip, "arch": arch})
            for child in node.get("children") or []:
                visit(child, trail)

        for node in data:
            visit(node, [])
        # Prefer recent PyTorch versions; 5090 needs a Blackwell-compatible build.
        def rank(image: dict):
            numbers = tuple(int(x) for x in re.findall(r"\d+", image["label"]))
            return ("pytorch" in image["label"].lower(), numbers, cuda_version(image["cuda"]))
        return sorted(images, key=rank, reverse=True)

    def create(self, payload: dict) -> str:
        data = self.request("POST", "/api/v1/order/instance/create/payg", payload, creating=True)
        if not isinstance(data, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", data):
            raise CreateUncertain("平台未返回实例 ID，请先核对控制台")
        return data

    def snapshot(self, instance_id: str) -> dict:
        # GET query parameters follow the website's snapshot client.
        path = "/api/v1/instance/snapshot"
        request = urllib.request.Request(
            BASE_URL + path + "?" + urllib.parse.urlencode({"instance_uuid": instance_id}),
            headers={"Authorization": self._token, "Accept": "application/json"},
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                envelope = json.loads(response.read(2 * 1024 * 1024))
        except (urllib.error.URLError, OSError, ValueError):
            raise ReadUnavailable("实例已创建；暂时无法读取 SSH 信息，请查看控制台") from None
        if not isinstance(envelope, dict) or envelope.get("code") != "Success" or not isinstance(envelope.get("data"), dict):
            raise ReadUnavailable("实例已创建；SSH 信息尚未就绪，请查看控制台")
        data = envelope["data"]
        # Explicit whitelist: do not persist root_password, Jupyter tokens, etc.
        return {key: data[key] for key in ("ssh_command", "proxy_host", "ssh_port") if key in data}


def price_payload(machine_id: str, expand_disk_gb: int = 0) -> dict:
    return {"charge_type": "payg", "coupon_id_list": [], "duration": 1,
            "expand_data_disk": expanded_disk_bytes(expand_disk_gb), "machine_id": machine_id, "num": 1}


def create_payload(machine_id: str, settings: Settings, name: str) -> dict:
    return {
        "instance_info": {
            "machine_id": machine_id, "charge_type": "payg", "req_gpu_amount": 1,
            "base_image_uuid": settings.image_uuid if settings.image_kind == "base" else "",
            "private_image_uuid": settings.image_uuid if settings.image_kind == "private" else "",
            "reproduction_uuid": "", "reproduction_id": 0,
            "cg_application_uuid": "", "cg_application_info": {
                "image_name": "", "current_version_id": 0, "current_version": "", "image_id": 0,
            },
            "instance_name": name, "expand_data_disk": expanded_disk_bytes(settings.expand_disk_gb),
        },
        "price_info": price_payload(machine_id, settings.expand_disk_gb),
    }


@dataclass
class Candidate:
    machine_id: str
    region: str
    region_sign: str
    idle: int
    cuda: str
    gpu: str
    list_price: Decimal
    quote: Decimal | None = None
    chip: str = "nvidia"
    arch: str = "x86"
    memory_gb: float = 0
    cpu_cores: int = 0
    cpu_name: str = ""
    ram_gb: float = 0
    driver: str = ""
    disk_gb: float = 0
    disk_type: str = ""
    image_uuid: str = ""
    image_label: str = ""
    image_runtime: str = ""
    image_kind: str = "base"
    disk_expandable_gb: int | None = None
    expand_disk_gb: int = 0
    disk_daily_price: Decimal | None = None

    def display(self) -> dict:
        values = asdict(self)
        values["list_price"] = str(self.list_price)
        values["quote"] = str(self.quote) if self.quote is not None else "待查询"
        values["disk_daily_price"] = str(self.disk_daily_price) if self.disk_daily_price is not None else None
        values["disk_total_gb"] = self.disk_gb + self.expand_disk_gb
        return values


def candidates(rows: list[dict], settings: Settings) -> list[Candidate]:
    result = []
    for row in rows:
        gpu = str(row.get("gpu_name", ""))
        is_cpu = gpu.upper().startswith("CPU")
        if not gpu or not row.get("machine_id"):
            continue
        if settings.gpu_names and gpu not in settings.gpu_names:
            continue
        if not settings.gpu_names and is_cpu:
            continue
        idle = integer(row.get("gpu_idle_num"))
        if idle < 1 or integer(row.get("on_line"), -1) != 2:
            continue
        # Same instance-slot filter as AutoDL's market page; absent limits fail closed.
        if integer(row.get("max_instance_num")) <= integer(row.get("binding_instance_num")):
            continue
        chip = "cpu" if is_cpu else str(row.get("chip_corp") or "nvidia")
        if settings.min_cuda and chip == "nvidia" and cuda_version(row.get("highest_cuda_version")) < cuda_version(settings.min_cuda):
            continue
        memory_gb = integer(row.get("gpu_memory")) / 1024 ** 3
        if memory_gb < settings.min_vram_gb:
            continue
        if settings.regions and row.get("region_sign") not in settings.regions:
            continue
        raw_capacity = row.get("max_data_disk_expand_size")
        capacity_bytes = integer(raw_capacity, -1) if not isinstance(raw_capacity, bool) else -1
        if settings.expand_disk_gb and capacity_bytes < expanded_disk_bytes(settings.expand_disk_gb):
            continue
        skus = row.get("machine_sku_info") or []
        if not isinstance(skus, list):
            raise QuickError("库存价格格式变化，已停止")
        # The website indexes its SKU array by payg/daily/weekly/monthly/yearly.
        payg = next((sku for sku in skus if isinstance(sku, dict) and sku.get("type") == "payg"), None)
        if payg is None and skus and isinstance(skus[0], dict) and not skus[0].get("type"):
            payg = skus[0]
        if not payg or payg.get("current_price") is None:
            continue
        price = amount(payg["current_price"], "库存原价")
        result.append(Candidate(str(row["machine_id"]),
                                str(row.get("region_name") or row.get("region_sign", "")),
                                str(row.get("region_sign", "")), idle,
                                str(row.get("highest_cuda_version") or ""), gpu, price,
                                chip=chip, arch=str(row.get("cpu_arch") or "x86"), memory_gb=memory_gb,
                                cpu_cores=integer(row.get("cpu_per_gpu")),
                                cpu_name=str((row.get("machine_base_info") or {}).get("cpu_name", "")),
                                ram_gb=integer(row.get("mem_per_gpu")) / 1024 ** 3,
                                driver=str(row.get("driver_version") or ""),
                                disk_gb=integer(row.get("max_instance_disk_size")) / 1024 ** 3,
                                disk_type=str(row.get("disk_type") or ""),
                                disk_expandable_gb=capacity_bytes // GIB if capacity_bytes >= 0 else None,
                                expand_disk_gb=settings.expand_disk_gb))
    def order(candidate):
        primary = 0
        if settings.strategy == "gpu_priority":
            primary = settings.gpu_names.index(candidate.gpu) if candidate.gpu in settings.gpu_names else len(settings.gpu_names)
        elif settings.strategy == "region_priority":
            primary = settings.regions.index(candidate.region_sign) if candidate.region_sign in settings.regions else len(settings.regions)
        return primary, candidate.list_price, -candidate.idle, candidate.machine_id
    return sorted(result, key=order)


def image_for_candidate(settings: Settings, candidate: Candidate) -> Settings | None:
    profile = settings.image_profiles.get("gpu:" + candidate.gpu) or settings.image_profiles.get(f"{candidate.chip}:{candidate.arch}")
    if not profile and settings.image_uuid and settings.image_chip == candidate.chip and settings.image_arch == candidate.arch:
        profile = {"uuid": settings.image_uuid, "kind": settings.image_kind, "version": settings.image_cuda}
    if not profile:
        return None
    if profile.get("chip", candidate.chip) != candidate.chip or profile.get("arch", candidate.arch) != candidate.arch:
        return None
    version = str(profile.get("version", ""))
    if candidate.chip == "nvidia":
        series = Catalog.load().model(candidate.gpu).get("series", "")
        if any(legacy in series for legacy in ("Maxwell", "Pascal", "Volta")) and cuda_version(version) > (12, 6):
            return None
        minimum = (12, 8) if any(name in candidate.gpu for name in ("5090", "PRO 6000", "PRO6000", "RTX 6000D", "PRO 5000")) else (0, 0)
        if cuda_version(version) < minimum or cuda_version(version) == (0, 0):
            return None
        if cuda_version(candidate.cuda) < cuda_version(version):
            return None
    return replace(settings, image_uuid=profile["uuid"], image_kind=profile.get("kind", "base"),
                   image_cuda=version, image_chip=candidate.chip, image_arch=candidate.arch)


def check_cancel(cancel: threading.Event) -> None:
    if cancel.is_set():
        raise Cancelled("已停止等待")


class RentalJournal:
    """Durable write-before-submit journal + OS lock across windows/processes."""

    def __init__(self, directory: Path = ROOT / ".state"):
        self.directory = directory
        self.path = directory / "rental.json"
        self._lock = None

    def __enter__(self):
        if self._lock is not None:
            raise QuickError("当前进程已经持有租卡锁")
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = (self.directory / "rent.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self._lock.seek(0, os.SEEK_END)
                if self._lock.tell() == 0:
                    self._lock.write(b"0")
                    self._lock.flush()
                self._lock.seek(0)
                msvcrt.locking(self._lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock.close()
            self._lock = None
            raise QuickError("已有另一个窗口或进程正在自动租卡") from None
        return self

    def __exit__(self, *_):
        if self._lock:
            self._lock.close()
            self._lock = None

    def read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or "status" not in data:
                raise ValueError()
            return data
        except (ValueError, OSError):
            raise QuickError("租卡记录无法读取；请核对控制台后再处理 .state/rental.json") from None

    def ensure_new(self) -> None:
        previous = self.read()
        if previous and previous.get("status") != "rejected_no_stock":
            name = previous.get("instance_name", "")
            instance = previous.get("instance_uuid", "尚未确认 ID")
            raise QuickError(f"已有租卡记录：{name} / {instance}。请核对控制台；需要另一台时先手动归档记录")

    def write(self, **data) -> None:
        atomic_json(self.path, data)

    def archive(self) -> None:
        previous = self.read()
        if previous:
            destination = self.directory / f"rental-{time.time_ns()}.json"
            os.replace(self.path, destination)


class Engine:
    def __init__(self, api: AutoDL, emit: Callable[[str, Any], None] | None = None,
                 journal: RentalJournal | None = None):
        self.api = api
        self.emit = emit or (lambda *_: None)
        self.journal = journal or RentalJournal()
        self._image_cache: dict[tuple[str, str], list[dict]] = {}
        self._stock_rejections: set[str] = set()

    def quote_candidate(self, settings: Settings, candidate: Candidate) -> Decimal:
        if settings.expand_disk_gb:
            detail = self.api.quote_detail(candidate.machine_id, settings.expand_disk_gb)
            candidate.quote, candidate.disk_daily_price = detail.instance_hourly, detail.disk_daily
        else:
            candidate.quote, candidate.disk_daily_price = self.api.quote(candidate.machine_id), Decimal(0)
        return candidate.quote

    def prepare_image(self, settings: Settings, candidate: Candidate) -> Settings | None:
        selected = image_for_candidate(settings, candidate)
        label = "已配置镜像"
        if selected is None and settings.auto_image:
            key = (candidate.chip, candidate.arch)
            if key not in self._image_cache:
                self._image_cache[key] = self.api.images(*key)
            for image in self._image_cache[key]:
                auto = replace(settings, image_uuid=image["uuid"], image_cuda=image["cuda"],
                               image_kind=image["kind"], image_chip=candidate.chip, image_arch=candidate.arch,
                               image_profiles={})
                selected = image_for_candidate(auto, candidate)
                if selected is not None:
                    label = image["label"]
                    break
        if selected is not None:
            candidate.image_uuid = selected.image_uuid
            candidate.image_kind = selected.image_kind
            candidate.image_runtime = selected.image_cuda
            candidate.image_label = label
        else:
            candidate.image_label = "未找到兼容镜像"
        return selected

    def discover(self, settings: Settings, cancel: threading.Event, *, limit: int = 20) -> list[Candidate]:
        """Use region stock to query only models that currently have remaining GPUs."""
        check_cancel(cancel)
        stock = self.api.region_stock(settings.regions)
        self.emit("region_stock", stock)
        catalog = Catalog.load()
        names = [name for name, values in stock.items() if values["idle"] > 0
                 and (name in settings.gpu_names if settings.gpu_names else not catalog.model(name).get("is_cpu", name.upper().startswith("CPU")))]
        self.emit("log", f"地区实时库存：{len(names)} 种型号有余量，自动加载主机、报价和镜像")
        if not names:
            self.emit("found_cards", [])
            return []
        chosen = replace(settings, gpu_names=names)
        found = []
        for batch in self.api.machine_batches(chosen, cancel):
            for candidate in candidates(batch, chosen):
                check_cancel(cancel)
                if candidate.machine_id in self._stock_rejections:
                    continue
                self.quote_candidate(settings, candidate)
                if settings.max_price and candidate.quote > positive_decimal(settings.max_price):
                    continue
                self.prepare_image(settings, candidate)
                found.append(candidate)
                self.emit("found_cards", list(found))
                if len(found) >= limit:
                    return found
        if not found:
            self.emit("log", "地区库存有余量，但当前主机未满足在线、容器名额或显存条件；稍后重查")
            self.emit("found_cards", [])
        return found

    def auto_wait(self, settings: Settings, cancel: threading.Event, *, execute=False) -> dict:
        settings = copy.deepcopy(settings)
        settings.validate(renting=execute)
        deadline = time.monotonic() + settings.wait_minutes * 60
        with self.journal if execute else nullcontext():
            if execute:
                self.journal.ensure_new()
            while time.monotonic() < deadline:
                check_cancel(cancel)
                try:
                    rejected_before = len(self._stock_rejections)
                    found = self.discover(settings, cancel, limit=1)
                    for card in found:
                        if not execute:
                            return {"status": "rehearsed", "gpu": card.gpu, "region": card.region,
                                    "machine_id": card.machine_id, "current_price": str(card.quote),
                                    "image_uuid": card.image_uuid, "expand_disk_gb": card.expand_disk_gb,
                                    "disk_total_gb": card.disk_gb + card.expand_disk_gb,
                                    "disk_daily_price": str(card.disk_daily_price)}
                        result = self.try_candidate(settings, card, cancel, True, deadline)
                        if result:
                            return result
                    if len(self._stock_rejections) > rejected_before:
                        continue
                except ReadUnavailable as error:
                    self.emit("log", f"查询暂时失败，继续等待：{error}")
                self.emit("log", f"当前无满足条件的剩余卡，{settings.poll_seconds} 秒后自动重查")
                if cancel.wait(min(settings.poll_seconds, max(0, deadline - time.monotonic()))):
                    check_cancel(cancel)
                self._stock_rejections.clear()
        raise QuickError("已达到等待时限，尚未创建实例")

    def scan(self, settings: Settings, cancel: threading.Event | None = None) -> list[Candidate]:
        settings.validate()
        cancel = cancel or threading.Event()
        rows = candidates(self.api.machines(settings, cancel), settings)
        self.emit("log", f"找到 {len(rows)} 台符合型号和地区条件的主机，共 {sum(c.idle for c in rows)} 张空闲卡")
        self.emit("candidates", rows)
        return rows

    def match_batches(self, settings: Settings, cancel: threading.Event):
        scopes = [settings]
        if settings.strategy == "gpu_priority" and settings.gpu_names:
            scopes = [replace(settings, gpu_names=[name]) for name in settings.gpu_names]
        elif settings.strategy == "region_priority" and settings.region_groups:
            catalog = Catalog.load()
            scopes = [replace(settings, regions=catalog.resolve_regions([group])) for group in settings.region_groups]
        visible = []
        for scope in scopes:
            for batch in self.api.machine_batches(scope, cancel):
                check_cancel(cancel)
                matching = candidates(batch, scope)
                visible.extend(matching)
                self.emit("candidates", visible[:300])
                yield matching

    def try_candidate(self, settings: Settings, candidate: Candidate, cancel: threading.Event,
                      execute: bool, deadline: float) -> dict | None:
        check_cancel(cancel)
        selected = self.prepare_image(settings, candidate) if execute else image_for_candidate(settings, candidate)
        if execute and selected is None:
            return None
        selected = selected or settings
        quote = self.quote_candidate(settings, candidate)
        cap = positive_decimal(settings.max_price) if settings.max_price else None
        if (cap is not None and quote > cap) or not self.api.online(candidate.machine_id):
            return None
        check_cancel(cancel)
        if time.monotonic() >= deadline:
            raise QuickError("已达到等待时限，尚未创建实例")
        quote = self.quote_candidate(settings, candidate)
        if cap is not None and quote > cap:
            self.emit("log", "下单前报价升高，继续寻找备选主机")
            return None
        operation = uuid.uuid4().hex
        name = "quick-rent-" + operation[:10]
        if not execute:
            self.emit("log", f"演练命中：{candidate.gpu} / {candidate.region} / {candidate.machine_id}，平台当前价 ¥{quote}/小时；没有下单")
            return {"status": "rehearsed", "machine_id": candidate.machine_id, "gpu": candidate.gpu,
                    "region": candidate.region, "quoted_yuan_per_hour": str(quote),
                    "payload": create_payload(candidate.machine_id, selected, "quick-rent-PREVIEW")}
        record = {"status": "submitting", "operation": operation, "gpu": candidate.gpu,
                  "region_sign": candidate.region_sign, "instance_name": name,
                  "machine_id": candidate.machine_id, "quoted_yuan_per_hour": str(quote),
                  "expand_disk_gb": settings.expand_disk_gb, "disk_total_gb": candidate.disk_gb + settings.expand_disk_gb,
                  "disk_daily_price": str(candidate.disk_daily_price),
                  "max_price": str(cap) if cap is not None else None, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        check_cancel(cancel)
        self.journal.write(**record)
        self.emit("log", f"提交单张 {candidate.gpu} / {candidate.region}，GPU ¥{quote}/小时；扩容 {settings.expand_disk_gb} GB，数据盘 ¥{candidate.disk_daily_price}/日")
        try:
            instance_id = self.api.create(create_payload(candidate.machine_id, selected, name))
        except APIError as error:
            # Retry only explicit, unambiguous inventory rejection responses.
            if error.code in {"NoStock", "GPUNotEnough", "GpuNotEnough"}:
                self._stock_rejections.add(candidate.machine_id)
                record.update(status="rejected_no_stock", rejection_code=error.code)
                self.journal.write(**record)
                self.emit("log", "平台明确拒绝：空闲卡已被占用。继续寻找备选主机")
                return None
            raise CreateUncertain("平台拒绝创建，已保留记录；请核对控制台后再继续") from None
        except BaseException as error:
            self.emit("log", f"已保留提交记录 {name}；请核对控制台，禁止盲目重试")
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise CreateUncertain("创建结果未确认，已停止并保留记录；不会自动重试") from None
        record.update(status="created", instance_uuid=instance_id)
        self.journal.write(**record)
        self.emit("created", record)
        self.emit("log", f"租用成功：{candidate.gpu} / {instance_id}；已停止租卡，实例需在控制台管理")
        try:
            details = self.api.snapshot(instance_id)
            record.update(details)
            self.journal.write(**record)
            if details.get("ssh_command"):
                self.emit("log", details["ssh_command"])
        except QuickError as error:
            self.emit("log", str(error))
        return record

    def rent(self, settings: Settings, cancel: threading.Event) -> dict:
        return self.wait_for_match(settings, cancel, execute=True)

    def wait_for_match(self, settings: Settings, cancel: threading.Event, *, execute: bool = False) -> dict:
        # Freeze the request: editing GUI fields while waiting cannot change it.
        settings = copy.deepcopy(settings)
        settings.validate(renting=execute)
        cap = positive_decimal(settings.max_price) if settings.max_price else None
        with self.journal if execute else nullcontext():
            if execute:
                self.journal.ensure_new()
            deadline = time.monotonic() + settings.wait_minutes * 60
            failures = 0
            while time.monotonic() < deadline:
                check_cancel(cancel)
                try:
                    for batch in self.match_batches(settings, cancel):
                        for candidate in batch:
                            record = self.try_candidate(settings, candidate, cancel, execute, deadline)
                            if record:
                                return record
                    failures = 0
                    price_condition = f" ¥{cap}/小时以内" if cap is not None else "按平台当前价"
                    self.emit("log", f"暂时没有符合条件的可用主机（{price_condition}），{settings.poll_seconds} 秒后重查")
                    delay = settings.poll_seconds
                except ReadUnavailable as error:
                    failures += 1
                    if failures >= 5:
                        raise QuickError("连续 5 次查询失败，已停止等待；未提交创建") from error
                    delay = min(settings.poll_seconds * 2 ** (failures - 1), 300)
                    self.emit("log", f"{error}，{delay} 秒后重查（{failures}/5）")
                remaining = max(0, deadline - time.monotonic())
                if cancel.wait(min(delay, remaining)):
                    check_cancel(cancel)
            raise QuickError("已达到等待时限，尚未创建实例")


def print_event(kind: str, value: Any) -> None:
    if kind == "log":
        print(value, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="AutoDL 多型号、多地区单卡等待工具")
    parser.add_argument("command", nargs="?", choices=["gui", "catalog", "scan", "images", "plan", "watch", "rent", "state", "archive-state"], default="gui")
    parser.add_argument("--max-price", help="可选：单卡每小时最高可接受价；不填按平台当前报价")
    parser.add_argument("--gpus", help="型号名称，用英文逗号分隔；all 表示全部 GPU，CPU 需明确选中")
    parser.add_argument("--min-vram", type=int, help="最低显存 GB，0 为不限")
    parser.add_argument("--expand-disk", type=int, help="新增数据盘 GB；筛选可扩容量并实际扩容，0 为不扩容")
    parser.add_argument("--strategy", choices=["price", "gpu_priority", "region_priority"])
    parser.add_argument("--image-platform", choices=["nvidia", "mthreads", "cpu"])
    parser.add_argument("--interval", type=int, help="查询间隔秒数，至少 15")
    parser.add_argument("--wait-minutes", type=int, help="最多等待分钟数")
    parser.add_argument("--regions", help="官网地区名称 / 数据中心 / 机房代码，用英文逗号分隔")
    parser.add_argument("--image", help="从 images 命令或控制台获取的镜像 UUID")
    parser.add_argument("--image-cuda", help="所选 NVIDIA 镜像 CUDA 版本，例如 11.8 或 12.8")
    parser.add_argument("--private-image", action="store_true", help="镜像是私有镜像")
    parser.add_argument("--machine", help="plan 命令使用的主机 ID")
    parser.add_argument("--execute", action="store_true", help="明确执行一次付费创建")
    parser.add_argument("--checked-console", action="store_true", help="已核对控制台，可归档本地租卡记录")
    parser.add_argument("--demo", action="store_true", help="GUI 演示数据，完全不联网")
    parser.add_argument("--read-only", action="store_true", help="只读会话：禁止调用创建或实例修改接口")
    args = parser.parse_args()
    try:
        settings = Settings.load()
        for key, value in (("max_price", args.max_price), ("poll_seconds", args.interval),
                           ("wait_minutes", args.wait_minutes), ("image_uuid", args.image),
                           ("image_cuda", args.image_cuda), ("min_vram_gb", args.min_vram),
                           ("strategy", args.strategy), ("image_chip", args.image_platform),
                           ("expand_disk_gb", args.expand_disk)):
            if value is not None:
                setattr(settings, key, value)
        if args.regions is not None:
            settings.region_groups = [s.strip() for s in args.regions.split(",") if s.strip()]
            settings.regions = Catalog.load().resolve_regions(settings.region_groups)
        if args.gpus is not None:
            settings.gpu_names = [] if args.gpus.lower() == "all" else [s.strip() for s in args.gpus.split(",") if s.strip()]
        if args.private_image:
            settings.image_kind = "private"
        if args.command == "gui":
            from .ui import run
            run(settings, demo=args.demo, read_only=args.read_only)
            return 0
        if args.command == "catalog":
            catalog = Catalog.refresh()
            print(json.dumps({"models": catalog.models, "regions": catalog.regions}, ensure_ascii=False, indent=2))
            return 0
        if args.demo:
            raise QuickError("--demo 仅用于 GUI，不会执行真实租卡")
        if args.read_only and args.command in ("rent", "archive-state"):
            raise QuickError("只读测试模式禁止租卡或修改租卡记录")
        if args.command == "state":
            print(json.dumps(RentalJournal().read(), ensure_ascii=False, indent=2))
            return 0
        if args.command == "archive-state":
            if not args.checked_console:
                raise QuickError("请先在控制台核对实例，再加 --checked-console 归档；归档不会关机")
            with RentalJournal() as journal:
                journal.archive()
            print("本地记录已归档；实例状态由 AutoDL 控制台管理")
            return 0
        settings.validate(renting=args.command in ("rent", "plan"))
        if not settings.regions:
            settings.regions = Catalog.load().resolve_regions(settings.region_groups)
        if args.command == "rent" and not args.execute:
            raise QuickError("实际租卡需要 --execute；可先运行 scan、images 或 plan")
        if args.command == "plan":
            if not args.machine:
                raise QuickError("plan 需要 --machine 主机ID")
            print(json.dumps(create_payload(args.machine, settings, "quick-rent-PREVIEW"), ensure_ascii=False, indent=2))
            print("以上仅为预览，没有请求创建接口。实际执行前仍将重新查询报价。")
            return 0
        api = AutoDL(os.environ.get("AUTODL_WEB_TOKEN") or getpass.getpass("AutoDL 网页 Token（本地输入，不回显）: "), read_only=args.read_only)
        engine = Engine(api, print_event)
        if args.command == "images":
            for image in api.images(settings.image_chip, settings.image_arch):
                print(f"{image['uuid']}\t{image['label']}")
        elif args.command == "scan":
            rows = engine.discover(settings, threading.Event())
            for row in rows:
                print(json.dumps(row.display(), ensure_ascii=False))
            print("已自动加载剩余 GPU 的平台价格、硬件配置和兼容镜像；没有创建实例。")
        elif args.command == "rent":
            print(json.dumps(engine.auto_wait(settings, threading.Event(), execute=True), ensure_ascii=False, indent=2))
        elif args.command == "watch":
            print(json.dumps(engine.auto_wait(settings, threading.Event()), ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt:
        print("已中断；若已提交创建，请查看 state 和 AutoDL 控制台。")
        return 130
    except (QuickError, OSError) as error:
        print(f"提示：{error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
