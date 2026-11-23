"""agent 层统一异常定义。

自 `core/errors.py` 复制(原样, 无改动)。agent 层只读加载产物,
仅在配置加载/产物读取失败时抛这些异常。
"""


class PipelineError(Exception):
    """流程级错误基类。"""


class ConfigError(PipelineError):
    """配置加载或校验错误。"""


class StepDataError(PipelineError):
    """上游产物缺失/格式错误(硬性校验失败)。"""


class StepFailedError(PipelineError):
    """步骤执行失败(本层不触发, 保留供兼容)。"""


class LLMError(PipelineError):
    """LLM 调用失败(供问答层复用)。"""


class BrowserError(PipelineError):
    """浏览器自动化失败(本层不触发, 保留占位)。"""


class DownloadError(PipelineError):
    """下载失败(本层不触发, 保留占位)。"""
