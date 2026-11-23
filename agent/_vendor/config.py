"""agent 层唯一配置入口(自 `core/config.py` 复制改造: 只读精简版)。

与主流水线 `core/config.py` 的区别:
- 移除 COOKIE 引号自动修复、webui 相关、浏览器/卖家精灵/MCP 等与"只读分析"无关的键;
- 保留 agent 层确实需要的: 类目标识/站点/月份、LLM 密钥与模型、筛选与报告阈值、属性集。

⚠️ 这是 `core/Config` 的独立拷贝; 改动这里不影响主流水线, 反之亦然。
"""

from __future__ import annotations

import copy
import os
from pathlib import Path

import yaml

from .errors import ConfigError


class Config:
    """配置包装器(agent 精简版)。

    dict 承载配置项, 提供类型化访问与列表/字典解析辅助。
    默认值取自主流水线 `core/config.py` 中 agent 层用到的键。
    """

    DEFAULTS: dict = {
        "DATA_ROOT": "data",
        "DRY_RUN": False,
        "CAT": "",
        "LIST_URLS": "",
        "SITE": "美国",
        "MARKET": "amazon.com",
        "TRAFFIC_MONTH": "",
        "COMPETITOR_MONTH": "",
        "TREND_MONTH": "",
        # LLM: 供问答/报告 crew 复用同一套密钥与模型
        "LLM_API_KEY": "",
        "LLM_BASE_URL": "https://api.deepseek.com",
        "LLM_MODEL": "deepseek-chat",
        "VISION_API_KEY": "",
        "VISION_BASE_URL": "https://api.deepseek.com",
        "VISION_MODEL": "deepseek-vl2",
        # 筛选/报告阈值(供分析说明使用)
        "SEARCH_VOLUME_THRESHOLD": 30000,
        "MOM_YOY_MIN": -20.0,
        "GOOGLE_YOY_MIN": -20.0,
        "CPC_MAX": 3.0,
        "GATE_TOP5_REV_SHARE": 60.0,
        "GATE_MEDIAN_PRICE": 35.0,
        "GATE_AMZ_BRAND_SHARE": 20.0,
        "GATE_NEW_SELLERS": 10,
        # 属性标注
        "ATTR_KEYS": "",
        "ATTR_DESC": "",
        "ATTR_MODE": "auto",
    }

    def __init__(self, data: dict):
        merged = copy.deepcopy(self.DEFAULTS)
        merged.update(data or {})
        self.data = merged

    @classmethod
    def load(cls, path: str | os.PathLike) -> "Config":
        path = Path(path)
        if not path.exists():
            raise ConfigError(f"配置文件不存在: {path}")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"配置 YAML 解析失败: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"配置文件格式错误(应为映射): {path}")
        return cls(data)

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value):
        self.data[key] = value

    def as_dict(self) -> dict:
        return copy.deepcopy(self.data)

    # ---- 解析辅助(自 core 复制) ----
    def _split_list(self, key: str) -> list[str]:
        raw = str(self.data.get(key, "") or "")
        parts = [p.strip() for p in raw.replace("；", ";").split(";")]
        return [p for p in parts if p]

    def _split_desc(self, key: str) -> dict:
        result = {}
        for item in self._split_list(key):
            if "=" in item:
                k, v = item.split("=", 1)
                result[k.strip()] = v.strip()
        return result

    # ---- 类型化访问器 ----
    @property
    def cat(self) -> str:
        return str(self.data.get("CAT", "") or "").strip()

    @property
    def market(self) -> str:
        return str(self.data.get("MARKET", "") or "amazon.com")

    @property
    def site(self) -> str:
        return str(self.data.get("SITE", "") or "").strip()

    @property
    def trend_month(self) -> str:
        return str(self.data.get("TREND_MONTH", "") or "").strip()

    @property
    def traffic_month(self) -> str:
        return str(self.data.get("TRAFFIC_MONTH", "") or "").strip()

    @property
    def competitor_month(self) -> str:
        return str(self.data.get("COMPETITOR_MONTH", "") or "").strip()

    @property
    def dry_run(self) -> bool:
        return bool(self.data.get("DRY_RUN", False))

    @property
    def data_root(self) -> str:
        return str(self.data.get("DATA_ROOT", "data") or "data")

    @property
    def attr_keys(self) -> list[str]:
        return self._split_list("ATTR_KEYS")

    @property
    def attr_desc(self) -> dict:
        return self._split_desc("ATTR_DESC")

    def resolve_path(self, value: str) -> str:
        """把配置中的相对路径解析为相对项目根目录的绝对路径。"""
        if not value:
            return value
        if os.path.isabs(value):
            return value
        root = Path(__file__).resolve().parent.parent.parent
        return str(root / value)

    def resolved_data_root(self) -> str:
        return self.resolve_path(self.data_root)
