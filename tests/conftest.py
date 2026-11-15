# 共享测试 fixture: 在 tests/ 下建独立临时目录
# 说明: 沙箱拒绝访问系统临时目录(如 C:\Users\...\AppData\Local\Temp\pytest-of-<用户>),
#       pytest 内置的 tmp_path/tmp_path_factory 会因此在 fixture 初始化阶段直接报
#       PermissionError(WinError 5) —— 表现为 13 个 "ERROR at setup"(测试体根本没跑)。
#       故这里用 os.makedirs + uuid 自建目录, 并**覆盖** pytest 内置同名 fixture;
#       清理失败时忽略(沙箱禁止删除目录, 目录已被 .gitignore 忽略)。
import os
import shutil
import uuid
from pathlib import Path

import pytest


def _tmp_base() -> str:
    # ⚠️ 目录名**不能以点开头**(沙箱拒绝在仓库内创建隐藏目录, 实测 `.pytest-tmp` 会
    #    PermissionError)。沿用与 ws_tmp 同级的普通目录名。
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pytest_tmp")
    os.makedirs(base, exist_ok=True)
    return base


@pytest.fixture
def ws_tmp():
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pytest_ws")
    os.makedirs(base, exist_ok=True)
    d = os.path.join(base, "ws-" + uuid.uuid4().hex[:12])
    os.makedirs(d, exist_ok=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def tmp_path_factory():
    """覆盖 pytest 内置 fixture: 把临时根目录换到 tests/.pytest-tmp(沙箱可写)。

    保持与内置语义一致: mktemp() 每次返回**全新**目录; getbasetemp() 返回根目录。
    """
    base = _tmp_base()
    created: list[str] = []

    class _Factory:
        def mktemp(self, basename: str = "", numbered: bool = True) -> Path:
            name = (basename or "tmp") + "-" + uuid.uuid4().hex[:12]
            d = os.path.join(base, name)
            os.makedirs(d, exist_ok=True)
            created.append(d)
            return Path(d)

        def getbasetemp(self) -> Path:
            return Path(base)

    yield _Factory()
    for d in created:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def tmp_path(tmp_path_factory) -> Path:
    """覆盖 pytest 内置 fixture(原因见文件头注释), 语义保持一致: 每个用例一个全新目录。"""
    return tmp_path_factory.mktemp("test")
