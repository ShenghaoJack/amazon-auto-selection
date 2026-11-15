"""日志与进度记录。

进度日志统一写入 20_备份与监控/pipeline-progress.log (需求 3.4/FR-17)。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logger(progress_log: str | None = None, level: int = logging.INFO) -> logging.Logger:
    """初始化根日志器。

    progress_log 指向 20_备份与监控/pipeline-progress.log;
    为 None 时仅输出控制台。
    """
    logger = logging.getLogger("pipeline")
    logger.setLevel(level)
    logger.handlers.clear()
    fmt = logging.Formatter(_FORMAT, datefmt=_DATEFMT)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)
    if progress_log:
        Path(progress_log).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(progress_log, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    logger.propagate = False
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger("pipeline")
