"""项目统一异常定义。"""


class PipelineError(Exception):
    """流程级错误基类。"""


class ConfigError(PipelineError):
    """配置加载或校验错误。"""


class StepDataError(PipelineError):
    """上游产物缺失/格式错误(硬性校验失败)。"""


class StepFailedError(PipelineError):
    """步骤执行失败, 编排器应阻断流程。"""


class WaitingForHuman(PipelineError):
    """步骤主动让路：**等人工确认后再继续**（这不是失败）。

    2026-10-09 用户口径（方案 A 的两个停点）：
      16 提取属性 →[停①]确认属性集 → 17 标注 + 产词表 →[停②]确认词表 → 18 应用词表

    为什么单独立一个类型：原先这两处都抛 `StepDataError`，编排器一律按
    「失败/产物缺失」处理 —— 发 CRIT 告警、弹窗、标 FAILED。
    **可这根本不是失败，是在等人**；当成失败会让人去查一个不存在的故障。

    编排器捕获它之后：不标 FAILED、不发告警/弹窗、退出码用 **3**（≠2 失败），
    日志写「⏸ 等待人工确认」，步骤状态留在 PENDING（续跑语义天然正确）。

    约定：`hint` 要说明**去哪、做什么**，让人能直接照着做。
    """

    def __init__(self, message: str, *, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint or ""

    def __str__(self) -> str:                    # noqa: D105
        base = super().__str__()
        return "%s%s" % (base, ("｜下一步: " + self.hint) if self.hint else "")


class EmptyExportPause(WaitingForHuman):
    """A file was empty twice or a retry's submission state is uncertain."""



class LLMError(PipelineError):
    """LLM 调用失败。"""


class LLMTruncatedError(LLMError):
    """LLM 输出被截断(finish_reason=length 且 max_tokens 已达上限)。

    与普通 LLMError 区分: 上层(如 CallableProducer)据此把输入拆成更小的批重试,
    而不是当作一般失败重试/放弃。
    """


class BrowserError(PipelineError):
    """浏览器自动化失败。"""


class DownloadError(PipelineError):
    """下载失败。"""
