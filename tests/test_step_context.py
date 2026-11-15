# 单元测试: StepContext 步骤等待计时(等待不计入步骤活动耗时)
from core.config import Config
from core.paths import Paths
from steps.base import StepContext


def _ctx(ws_tmp):
    return StepContext(Config({"CAT": "t", "DATA_ROOT": ws_tmp, "DRY_RUN": True}), Paths(Config({"CAT": "t", "DATA_ROOT": ws_tmp})))


def test_step_wait_accumulator(ws_tmp):
    ctx = _ctx(ws_tmp)
    assert ctx.step_wait_seconds() == 0.0
    ctx.add_step_wait(300)
    ctx.add_step_wait(45.5)
    assert ctx.step_wait_seconds() == 345.5
    ctx.add_step_wait(-10)  # 负数忽略
    assert ctx.step_wait_seconds() == 345.5
    ctx.reset_step_wait()
    assert ctx.step_wait_seconds() == 0.0
