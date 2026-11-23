# agent 层: 自包含基础模块(从现有 core/ 复制改造, 只读精简版)。
#
# ⚠️ 本目录是 main 仓库 core/ 的**独立拷贝**, 与 core/ 无任何 import 关系,
#    专供 agent 层只读加载与解析产物使用。改动 core/ 不会影响这里, 反之亦然。
# 未改动现有任何代码。
#
# 导出:
#   - Config        配置加载/类型化访问(只保留 agent 需要的最小集)
#   - Paths         类目路径定位(只保留数据根/类目/监控目录)
#   - 读文件工具     read_json / read_jsonl / read_txt_lines / read_excel_sheet / ...
from .config import Config
from .paths import Paths
from .errors import ConfigError, StepDataError, StepFailedError, PipelineError
from .io_utils import (
    read_json,
    read_jsonl,
    read_txt_lines,
    read_excel_sheet,
    read_excel_all_sheets,
    ensure_dir,
)

__all__ = [
    "Config",
    "Paths",
    "ConfigError",
    "StepDataError",
    "StepFailedError",
    "PipelineError",
    "read_json",
    "read_jsonl",
    "read_txt_lines",
    "read_excel_sheet",
    "read_excel_all_sheets",
    "ensure_dir",
]
