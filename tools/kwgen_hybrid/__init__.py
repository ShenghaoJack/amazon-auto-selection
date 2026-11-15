# -*- coding: utf-8 -*-
"""融合版关键词生成引擎（步骤4 接入用）。引擎在 `engine.py`，接线在 `glue.py`。"""
from .engine import HybridPipeline, write_outputs  # noqa: F401
from . import glue  # noqa: F401
