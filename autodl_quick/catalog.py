"""Public AutoDL model catalog and website region-to-machine mapping."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
BUNDLED_CATALOG = Path(__file__).resolve().parent / "data" / "market-catalog.json"
CACHE = ROOT / ".state" / "market-catalog.json"


@dataclass
class Catalog:
    regions: list[dict]
    models: list[dict]
    updated_at: str = ""

    @classmethod
    def from_responses(cls, region_rows: list[dict], gpu_rows: list[dict]) -> "Catalog":
        groups: dict[str, dict] = {}
        for row in region_rows:
            if not row.get("data_center") or not row.get("region_sign"):
                continue
            key = row["data_center"]
            group = groups.setdefault(key, {
                "id": key, "name": row["region_name"], "signs": [],
                "visible": row.get("visible", "all"), "used_for": row.get("used_for", "all"),
                "kind": row.get("region_class") or ("special" if "专区" in row["region_name"] else "normal"),
            })
            signs = row["region_sign"] if isinstance(row["region_sign"], list) else [row["region_sign"]]
            group["signs"] = list(dict.fromkeys(group["signs"] + signs))
        models = []
        for row in gpu_rows:
            if not row.get("gpu_name"):
                continue
            cpu = row["gpu_name"].upper().startswith("CPU")
            models.append({"name": row["gpu_name"], "chip": "cpu" if cpu else row.get("chip_corp", ""),
                           "arch": row.get("cpu_arch", "x86"),
                           "memory_gb": round(int(row.get("gpu_memory") or 0) / 1024 ** 3, 2),
                           "is_cpu": cpu, "series": row.get("gpu_series_type", ""),
                           "rank": int(row.get("gpu_rank") or 0)})
        return cls(list(groups.values()), models, datetime.now().astimezone().isoformat())

    @classmethod
    def load(cls, path: Path | None = None) -> "Catalog":
        if path is None:
            path = CACHE if CACHE.exists() else BUNDLED_CATALOG
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(data["regions"], data["models"], data.get("updated_at", ""))

    @classmethod
    def refresh(cls, path: Path | None = None) -> "Catalog":
        if path is None:
            path = CACHE
        responses = []
        for endpoint in ("/api/v1/region/list", "/api/v1/machine/gpu_type"):
            request = urllib.request.Request("https://www.autodl.com" + endpoint,
                                             headers={"Accept": "application/json"})
            with urllib.request.urlopen(request, timeout=20) as response:
                envelope = json.load(response)
            if envelope.get("code") != "Success" or not isinstance(envelope.get("data"), list):
                raise ValueError("AutoDL 公共型号 / 地区目录响应异常")
            responses.append(envelope["data"])
        catalog = cls.from_responses(*responses)
        if not catalog.regions or not catalog.models:
            raise ValueError("目录为空，保留上一次目录")
        from .core import atomic_json
        atomic_json(path, {"regions": catalog.regions, "models": catalog.models, "updated_at": catalog.updated_at})
        return catalog

    def model(self, name: str) -> dict:
        return next((model for model in self.models if model["name"] == name), {})

    def resolve_regions(self, selected: list[str] | None = None) -> list[str]:
        """A website region is a union of physical region_sign values, not a DC code."""
        if not selected:
            return list(dict.fromkeys(sign for group in self.regions
                                      if group["visible"] == "all" and group["used_for"] == "all"
                                      for sign in group["signs"]))
        result = []
        for value in selected:
            group = next((g for g in self.regions if value in (g["id"], g["name"])), None)
            if group:
                result.extend(group["signs"])
            elif any(value in g["signs"] for g in self.regions):
                result.append(value)
            else:
                raise ValueError(f"未知地区：{value}；请刷新目录并重新选择")
        return list(dict.fromkeys(result))

    def group_for_sign(self, sign: str) -> str:
        return next((g["id"] for g in self.regions if sign in g["signs"]), "")

    def gpu_names(self, include_cpu: bool = False) -> list[str]:
        return [model["name"] for model in self.models if include_cpu or not model["is_cpu"]]


def parse_region_stock(data: list[dict]) -> dict[str, dict]:
    result = {}
    for group in data:
        if not isinstance(group, dict):
            raise ValueError("地区库存格式变化")
        for name, stock in group.items():
            if isinstance(stock, dict):
                idle = next((stock[key] for key in ("gpu_idle_num", "idle_gpu_num", "idle") if key in stock), None)
                total = next((stock[key] for key in ("gpu_total_num", "total_gpu_num", "gpu_total_number", "total_gpu_number", "gpu_number", "gpu_num", "total") if key in stock), None)
                if idle is None or total is None:
                    raise ValueError("地区库存缺少空闲数或总量，不能当作 0")
                result[name] = {"idle": int(idle), "total": int(total)}
                if result[name]["idle"] < 0 or result[name]["total"] < 0:
                    raise ValueError("地区库存不能为负数")
            else:
                raise ValueError("地区库存格式变化")
    return result
