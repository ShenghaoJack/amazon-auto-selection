# -*- coding: utf-8 -*-
"""cat_report — 整合市场报告引擎(契约见 references/report-spec.md)。

对外只暴露 engine / metrics / readiness / scoring / build。
"""
from . import engine, metrics, readiness, scoring, build  # noqa: F401

__all__ = ["engine", "metrics", "readiness", "scoring", "build"]
