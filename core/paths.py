"""路径管理。

对应需求文档 3.4 目录与文件命名规范:
- 实时数据目录: 数据根/<类目名>/ (中间产物平铺, 靠文件名区分步骤)
- 步骤子目录: 11_查流量来源 / 12_查竞品 / 13_查竞品下载 / 14_流量拆分 / 20_备份与监控
- 归档目录: 数据根/archive/<类目名>-<时间戳>/ 内按 00-20 固定表分文件夹
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

from .config import Config
from steps.ids import MONITOR_STEP

STEP_DIR_NAMES: dict[int, str] = {
    0: "00_榜单收集",
    1: "01_榜单详情与第一遍过滤",
    2: "02_过滤提示词",
    3: "03_榜单详情过滤",
    4: "04_关键词",
    5: "05_关键词挖词",
    6: "06_趋势",
    7: "07_筛选",
    8: "08_关键词报告",
    9: "09_搜索收集",
    10: "10_父项变体",
    11: "11_查流量来源",
    12: "12_查竞品",
    13: "13_查竞品下载",
    14: "14_流量拆分",
    15: "15_初步类目分析报告",
    # 16 号目录名 = **实际落盘名**(2026-10-14 修正): 代码与实时目录一直是
    # `16_产品属性标注`(见 step16 的 `archive_dirs` / `resolve_artifact`), 而这里写的
    # `16_属性集提取与确认` 是**死条目** —— 归档器(archive_step_dir)按本表命名, 于是
    # 归档目录名与实时目录名不一致, 按本表找目录的工具(sync_artifacts manifest /
    # Web 步骤文件面板)在步骤16 上会指向一个不存在的实时目录。
    16: "16_产品属性标注",
    17: "17_属性标注",
    18: "18_标准化",
    19: "19_VOC报告",
    # 备份与最终体检: **必须是最后一步**(2026-10-09 用户口径) —— 它排在 VOC 之后,
    # 这样体检报告才能覆盖 VOC 产物与费用。目录名沿用遗留的「20_备份与监控」(全库按路径引用,
    # 改名会牵连日志/告警/账本/状态文件, 故只改号不改名)。
    20: "20_备份与监控",
}

# 备份与最终体检的步骤号(唯一真值: monitor_dir()/monitor_file() 等都用它, 别再散落 19/20 字面量)

# 有独立实时子目录的步骤(19=VOC 报告, 20=备份与监控)
REALTIME_SUBDIRS: set[int] = {11, 12, 13, 14, 19, 20}


class Paths:
    """按类目隔离的路径集合。

    对外方法返回 str 路径（`ensure_definition` 例外：返回 `Path`，供内部拼接）。
    目录不存在时自动创建(ensure=True 默认)。
    """

    def __init__(self, config: Config):
        self.config = config
        root = config.resolved_data_root()
        self.data_root = Path(root)
        self.cat_root = self.data_root / config.cat
        self.archive_root = self.data_root / "archive"
        self.profile_root = Path(config.profile_dir).resolve() if config.profile_dir else self.data_root / "profiles"

    def ensure(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        return path

    def step_dir(self, number: int) -> str:
        """实时数据目录中步骤对应子目录(仅 REALTIME_SUBDIRS 有独立目录)。"""
        if number in REALTIME_SUBDIRS:
            return str(self.ensure(self.cat_root / STEP_DIR_NAMES[number]))
        return str(self.ensure(self.cat_root))

    def cat_file(self, name: str) -> str:
        """类目根目录下带 <类目名>- 前缀的平铺文件。

        ★ 2026-10-10 接线（方案甲）：**定义层文件**（属性集 / 规则 / 学习映射）
          走 `catalog.resolve_definition_file` —— 它实现「**自己的目录第一优先**，
          自己目录没有再从共享沉淀层继承（`_catalog/<类目>/`）」。
          其他名字**原样返回**，行为逐字不变。
        """
        return str(self.ensure_definition(name))

    def ensure_definition(self, name: str) -> Path:
        """解析定义层/普通文件名（见 `cat_file` 说明）。

        ⚠ **必须保证父目录存在**：原实现是 `self.ensure(self.cat_root) / name` ——
          `ensure` 会建目录，有些调用方靠它保证后续可写。这里改成**建解析结果的父目录**
          （解析结果可能落在 `_catalog/<类目>/`，那才是要写的目标）。
        """
        try:
            from tools.attr_stability.catalog import resolve_definition_file
            p = resolve_definition_file(self.cat_root, name)
        except Exception:                                        # noqa: BLE001 — 兜底原行为
            p = self.cat_root / name
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return p

    def monitor_dir(self) -> str:
        return self.step_dir(MONITOR_STEP)

    def monitor_file(self, name: str) -> str:
        return str(self.ensure(self.cat_root / STEP_DIR_NAMES[MONITOR_STEP]) / name)

    def snapshot_dir(self, timestamp: str | None = None) -> str:
        """本轮归档目录: **复用该类目最新归档**(断点续跑/重跑共用同一轮),
        不存在时新建; 传入 timestamp 则强制新建指定时间戳目录。"""
        if timestamp is None:
            latest = self.latest_snapshot()
            if latest:
                return latest
        ts = timestamp or datetime.now().strftime("%Y%m%d-%H%M%S")
        return str(self.ensure(self.archive_root / f"{self.config.cat}-{ts}"))

    def new_snapshot_dir(self) -> str:
        """显式新建归档目录(手动快照/重置类目时用), 保证不与已有目录重名。"""
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        candidate = self.archive_root / f"{self.config.cat}-{ts}"
        n = 1
        while candidate.exists():
            candidate = self.archive_root / f"{self.config.cat}-{ts}-{n}"
            n += 1
        return str(self.ensure(candidate))

    def latest_snapshot(self) -> str | None:
        """该类目最新归档目录(按名字时间戳排序取最后一个)。

        ⚠️ 必须**精确匹配** `<CAT>-<YYYYMMDD-HHMMSS>[-N]`(2026-09-15 修):
        原实现用 `name.startswith(cat + "-")`, 于是**类目名互为前缀时会串档** —— 实测
        `CAT=Cutting-Boards` 会把 `Cutting-Boards-US-202608-20260915-120000` 当成自己的最新
        归档; 而本项目类目命名的常见形态正是 `data/<基类目>-<站点>-<月份>`, 一旦同时存在
        `Cutting-Boards` 与 `Cutting-Boards-US-202608` 两个类目, 前者的产物就会被归档/同步进
        **后者**的归档目录(数据串档)。现在只认"类目名 + 标准时间戳后缀"。
        """
        if not self.archive_root.is_dir():
            return None
        pat = re.compile(rf"^{re.escape(str(self.config.cat))}-\d{{8}}-\d{{6}}(?:-\d+)?$")
        dirs = sorted(
            (p for p in self.archive_root.iterdir() if p.is_dir() and pat.match(p.name)),
            key=lambda p: p.name,
        )
        return str(dirs[-1]) if dirs else None

    def archive_step_dir(self, snapshot: str, number: int) -> str:
        """归档快照内某步骤的固定目录。"""
        return str(self.ensure(Path(snapshot) / STEP_DIR_NAMES[number]))

    def screenshot_dir(self) -> str:
        return str(self.ensure(Path(self.monitor_dir()) / "screenshots"))

    def profile_amz(self) -> str:
        """亚马逊 profile 目录(2026-09-09 起默认按类目隔离: amz-profile-<CAT>,
        首次使用自动克隆共享种子; 显式自定义 AMZ_PROFILE 则不隔离)。"""
        from .profile_isolate import resolve_amz_profile_dir
        return str(self.ensure(resolve_amz_profile_dir(self.config, self.profile_root)))

    def profile_ss(self) -> str:
        """卖家精灵 profile 目录(2026-09-15 起默认按类目隔离: ss-profile-<CAT>,
        首次使用自动克隆共享种子; 显式自定义 SS_PROFILE 则不隔离)。"""
        from .profile_isolate import resolve_ss_profile_dir
        return str(self.ensure(resolve_ss_profile_dir(self.config, self.profile_root)))

    def profile_ss_backup(self, index: int) -> str:
        """备用账号独立 Profile(多账号配额切换用): <主Profile目录>-b<index>。

        按类目隔离(2026-09-15): 主 profile 隔离后, 备用也是 `<隔离主目录>-b<index>`,
        并从用户既有 `<配置目录>-b<index>` 克隆登录态。
        """
        from .profile_isolate import resolve_ss_backup_profile_dir
        return str(self.ensure(resolve_ss_backup_profile_dir(self.config, self.profile_root, index)))

    def download_dir_11(self) -> str:
        return self.step_dir(11)

    def download_dir_12(self) -> str:
        return self.step_dir(12)

    def download_dir_13(self) -> str:
        return self.step_dir(13)

    def resolve(self, value: str) -> str:
        """把相对路径解析到项目根目录(用于步骤产物校验)。"""
        if os.path.isabs(value):
            return value
        return str(Path(value).resolve())

    def resolve_cat_relative(self, value: str) -> str:
        """解析相对类目根目录的文件名。

        ★ 接线（2026-10-10）：定义层文件走 `ensure_definition`（自己的目录优先 →
          共享沉淀层继承）；其他名字行为逐字不变。
        """
        if os.path.isabs(value):
            return value
        return str(self.ensure_definition(value))
