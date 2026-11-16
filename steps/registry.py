"""步骤注册表: 步骤号 -> 步骤类。

新增步骤模块后在 STEPS 列表中登记, 编排器按步骤号排序执行。
"""
from __future__ import annotations

from steps.ids import PIPELINE_STEP_NUMBERS

from steps.step00_bestsellers import Step00Bestsellers
from steps.step01_details import Step01Details
from steps.step02_filter_prompt import Step02FilterPrompt
from steps.step03_filter_details import Step03FilterDetails
from steps.step04_keywords import Step04Keywords
from steps.step05_keyword_data import Step05KeywordData
from steps.step06_trend import Step06Trend
from steps.step07_screen import Step07Screen
from steps.step08_keyword_report import Step08KeywordReport
from steps.step09_search_collect import Step09SearchCollect
from steps.step10_variants import Step10Variants
from steps.step11_traffic_source import Step11TrafficSource
from steps.step12_competitor_submit import Step12CompetitorSubmit
from steps.step13_competitor_download import Step13CompetitorDownload
from steps.step14_traffic_split import Step14TrafficSplit
from steps.step15_category_report import Step15CategoryReport
from steps.step16_attribute_annotation import Step16AttributeAnnotation
from steps.step17_attr_annotation import Step17AttrAnnotation
from steps.step18_standardization import Step18Standardization
from steps.step17_backup_monitor import Step17BackupMonitor

STEPS: list = [
    Step00Bestsellers,
    Step01Details,
    Step02FilterPrompt,
    Step03FilterDetails,
    Step04Keywords,
    Step05KeywordData,
    Step06Trend,
    Step07Screen,
    Step08KeywordReport,
    Step09SearchCollect,
    Step10Variants,
    Step11TrafficSource,
    Step12CompetitorSubmit,
    Step13CompetitorDownload,
    Step14TrafficSplit,
    Step15CategoryReport,
    Step16AttributeAnnotation,
    Step17AttrAnnotation,
    Step18Standardization,
    Step17BackupMonitor,
]


def get_steps() -> list:
    """返回按步骤号排序的步骤实例列表。"""
    steps = sorted((cls() for cls in STEPS), key=lambda s: s.number)
    if tuple(step.number for step in steps) != PIPELINE_STEP_NUMBERS:
        raise ValueError("步骤注册表必须完整且唯一地覆盖 0–19，最终体检必须位于最后")
    return steps
