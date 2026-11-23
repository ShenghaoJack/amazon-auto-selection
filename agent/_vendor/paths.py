"""agent 层类目路径定位(自 `core/paths.py` 复制改造: 只读精简版)。

与主流水线 `core/paths.py` 的区别:
- 仅保留 agent 需要的: 数据根/类目根/监控目录/类目相对路径解析;
- 移除 Profile、下载目录、归档快照、步骤子目录等与"只读分析"无关的方法。

⚠️ 这是 `core/Paths` 的独立拷贝; 改动这里不影响主流水线, 反之亦然。
"""

from __future__ import annotations

import os
from pathlib import Path

from .config import Config

STEP_17_DIR = "17_备份与监控"


class Paths:
    """按类目隔离的路径集合(agent 精简版)。

    所有方法返回 str 路径。目录不存在时自动创建(ensure=True 默认)。
    仅在真正需要写(如服务缓存/日志)时使用, 默认对产物只读。
    """

    def __init__(self, config: Config):
        self.config = config
        root = config.resolved_data_root()
        self.data_root = Path(root)
        self.cat_root = self.data_root / config.cat

    def ensure(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        return path

    def monitor_dir(self) -> str:
        return str(self.ensure(self.cat_root / STEP_17_DIR))

    def monitor_file(self, name: str) -> str:
        return str(self.ensure(self.cat_root / STEP_17_DIR) / name)

    def resolve_cat_relative(self, value: str) -> str:
        """解析相对类目根目录的文件名。"""
        if os.path.isabs(value):
            return value
        return str(self.ensure(self.cat_root) / value)

    def cat_file(self, name: str) -> str:
        """类目根目录下带 <类目名>- 前缀的平铺文件。"""
        return str(self.ensure(self.cat_root) / name)
