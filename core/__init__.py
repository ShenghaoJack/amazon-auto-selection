"""core 包: 流水线核心基础设施。

模块清单:
- config     唯一配置入口(3.5)
- paths      路径管理/类目隔离/归档规范(3.4)
- logger     进度日志(17 目录)
- io_utils   文件 I/O(JSONL 容错/Excel/合并/去重)
- state      步骤状态/断点续跑(7.6)
- validation 上游产物校验(7.3 硬性)
- llm        LLM 文本+视觉客户端(非思考+JSON 模式)
- http_client HTTP 直连抓取(重试/限流/状态分类)
- browser    浏览器自动化封装(持久 Profile/真实点击/下载监听)
- mcp        卖家精灵 MCP 客户端(挖词/趋势)
- mock       dry-run 模拟数据源
- notify     预警记录 + 企业微信推送
- archive    归档同步/快照
- errors     统一异常
"""
from .archive import create_snapshot, sync_step_dir, sync_step_files
from .browser import BrowserAutomation
from .config import Config
from .errors import (
    BrowserError,
    ConfigError,
    DownloadError,
    LLMError,
    PipelineError,
    StepDataError,
    StepFailedError,
)
from .http_client import HttpFetcher, HttpResponse, ProductPage
from .io_utils import (
    append_jsonl,
    atomic_write_text,
    dedup_keep_best,
    df_to_excel,
    ensure_dir,
    merge_excel_by_rows,
    read_excel_all_sheets,
    read_excel_sheet,
    read_json,
    read_jsonl,
    read_txt_lines,
    write_json,
    write_txt_lines,
)
from .llm import LLMClient, extract_json
from .logger import get_logger, setup_logger
from .mcp import MCPClient
from .paths import STEP_DIR_NAMES, Paths
from .state import RunState
from .validation import ensure_non_empty, require_file, require_files, warn_missing

__all__ = [
    "Config",
    "Paths",
    "STEP_DIR_NAMES",
    "RunState",
    "LLMClient",
    "extract_json",
    "HttpFetcher",
    "HttpResponse",
    "ProductPage",
    "BrowserAutomation",
    "MCPClient",
    "append_jsonl",
    "read_jsonl",
    "read_json",
    "write_json",
    "read_txt_lines",
    "write_txt_lines",
    "atomic_write_text",
    "df_to_excel",
    "read_excel_sheet",
    "read_excel_all_sheets",
    "merge_excel_by_rows",
    "dedup_keep_best",
    "ensure_dir",
    "setup_logger",
    "get_logger",
    "require_file",
    "require_files",
    "warn_missing",
    "ensure_non_empty",
    "sync_step_files",
    "sync_step_dir",
    "create_snapshot",
    "PipelineError",
    "ConfigError",
    "StepDataError",
    "StepFailedError",
    "LLMError",
    "BrowserError",
    "DownloadError",
]
