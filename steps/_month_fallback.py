# -*- coding: utf-8 -*-
"""卖家精灵"目标月下拉缺失 → 「最近30天」替代"(仅步骤11/12 触发, 2026-09)。

背景: 配置 TRAFFIC_MONTH / COMPETITOR_MONTH = 2026-08, 但卖家精灵日期下拉
(选项形如 最近30天 / 2026-07 / 2026-06 …)当前没有该月 → 原实现直接报错。
按需求:
  1) 下拉无目标月时, 询问用户是否改用「最近30天」替代, 并把 config 改掉;
  2) 可交互(CLI 直接运行)才用 input() 询问; webui 子进程不可交互(有
     PIPELINE_CONFIG_SOURCE 环境变量标记) → 自动改用并写强提示日志;
  3) 同意/自动后, 把**源配置文件**(CLI=当前配置; webui=服务器注入的
     PIPELINE_CONFIG_SOURCE 指向用户真实配置)的 TRAFFIC_MONTH /
     COMPETITOR_MONTH 持久化为 "最近30天"(2026-09 用户指定用中文值),
     内存同步置位; 也兼容手写 recent30/last30/近30天 等标记——之后每次运行
     直接选「最近30天」下拉项, 不再询问。
其余步骤只把"最近30天"(及其别名)当作"最近30天"展示/放行, 不在此触发询问与改写。
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

RECENT30 = "最近30天"   # 写入 config 的规范值(用户要求直接用中文「最近30天」)
# 归一化(去空格、小写)后等价的标记; 用户手写 recent30/last30/近30天 等也会被识别
_RECENT_TOKENS = {"最近30天", "recent30", "last30", "last-30", "近30天"}
# 卖家精灵下拉里"最近30天"选项的可能文本(按序尝试, 首个可见命中即点)
RECENT_MATCHERS = ("最近30天", "最近 30 天", "近30天", "近 30 天", "last 30 days", "last 30")


def is_recent30(value) -> bool:
    """判断配置值是否表示"最近30天"(如 最近30天 / recent30 / last30)。"""
    s = str(value or "").strip().lower().replace(" ", "")
    return s in _RECENT_TOKENS


def display(value) -> str:
    """把 recent30 之类标记转成给人看/落报告的文案: 最近30天。"""
    return RECENT30 if is_recent30(value) else str(value or "")


def config_source_path() -> Path | None:
    """源配置文件路径: webui 启动时服务器注入; CLI 由 main.py 注入。"""
    v = os.environ.get("PIPELINE_CONFIG_SOURCE", "").strip()
    return Path(v) if v else None


def rewrite_month_key(path, key: str, value: str) -> bool:
    """在 YAML 文本中整行替换 `KEY: 旧值` -> `KEY: "新值"`(保留注释与其它行)。

    返回是否找到并改写; 找不到返回 False(调用方只告警, 不影响本次运行)。
    """
    p = Path(path)
    if not p.is_file():
        return False
    text = p.read_text(encoding="utf-8-sig")
    pat = re.compile(r"^([ \t]*)" + re.escape(key) + r"\s*:.*$", re.M)
    if not pat.search(text):
        return False
    new = pat.sub(lambda m: f"{m.group(1)}{key}: \"{value}\"", text, count=1)
    p.write_text(new, encoding="utf-8", newline="\n")
    return True


class MonthUnavailableError(RuntimeError):
    """用户明确拒绝用「最近30天」替代(仅交互模式会抛出)。"""


class MonthFallbackNeedsConfirm(RuntimeError):
    """webui 子进程遇到目标月缺失: 需用户在网页弹窗确认后才允许改写配置(2026-09-30)。

    未确认前**不改写**配置, 步骤以失败结束, 由用户在弹窗选择:
      同意 → 调服务端接口把 {config_key} 写为「最近30天」;
      不同意 → 配置保持原样, 用户自行处理。
    """
    def __init__(self, config_key: str, month: str, options: list[str] | None = None):
        self.config_key = config_key
        self.month = month
        self.options = list(options or [])
        super().__init__(f"目标月份 {month} 不存在, 待用户在弹窗确认是否改用「最近30天」({config_key})")


def confirm_or_auto(ctx, config_key: str, month: str, options: list[str],
                    log) -> None:
    """目标月在下拉中不存在时的统一处理(步骤11/12 调用, 别处勿用)。

    交互(CLI 终端) → input() 询问 y/N; 同意=写回并停止, 拒绝抛 MonthUnavailableError。
    webui 子进程(不可交互) → **不改写配置**, 抛 MonthFallbackNeedsConfirm,
      由网页弹窗征求用户同意后才经服务端接口改写(不同意则保持原值, 用户自行处理)。
    同意后(CLI): 写回源配置 {config_key}="最近30天"(不备份, 改动记日志) + 本次内存置位。
    """
    env_src = os.environ.get("PIPELINE_CONFIG_SOURCE", "").strip()
    interactive = bool(env_src) is False and bool(
        getattr(sys.stdin, "isatty", lambda: False)()
    )
    opts_txt = ("；".join(options[:12]) + ("…" if len(options) > 12 else "")) \
        if options else "(无法读取可用选项)"
    if interactive:
        try:
            ans = input(
                f"\n[提示] 卖家精灵下拉中不存在目标月份 {month}。\n"
                f"  当前可用选项: {opts_txt}\n"
                f"是否改用「最近30天」替代, 并把配置 {config_key} 写为 {RECENT30!r}?\n"
                f"  (y=写入并停止本步骤, 请你重新运行该步骤; N=不改, 本步骤失败) [y/N]: "
            ).strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes"):
            raise MonthUnavailableError(
                f"目标月份 {month} 在下拉中不存在, 且未采用最近30天; "
                f"请把配置 {config_key} 改为可用月份或 {RECENT30} 后重跑"
            )
        log(f"用户确认: 目标月 {month} 不存在 → 改写 {config_key} 为 {RECENT30}(不备份); "
            f"**本步骤将停止**, 请重新运行该步骤(再次运行直接选最近30天)")
    else:
        log(f"[需要弹窗确认] 目标月份 {month} 在下拉中不存在(可用: {opts_txt}); "
            f"**未修改配置**——请在网页弹窗确认是否改写 {config_key} 为「最近30天」; "
            f"不同意则保持原值, 由你自行处理配置后再重跑本步骤")
        raise MonthFallbackNeedsConfirm(config_key, month, options)
    src = config_source_path()
    if src is not None and src.is_file():
        try:
            # 用户要求: 同类自动改写直接改原文件, 不额外备份(2026-09)
            if rewrite_month_key(src, config_key, RECENT30):
                log(f"已写入配置 {src}: {config_key}={RECENT30}(下次运行直接选最近30天)")
            else:
                log(f"配置 {src} 中未找到 {config_key} 行, 未写回(本次运行内仍生效)")
        except Exception as exc:  # noqa: BLE001
            log(f"写回配置失败(不影响本次运行): {exc}")
    else:
        log("未定位到源配置文件, 本次运行内改用最近30天, 未持久化; 建议手动把该月改为 "
            + RECENT30)
    ctx.config.set(config_key, RECENT30)


def list_options(page, selector: str, limit: int = 40) -> list[str]:
    """收集当前可见的下拉选项文本(去重), 供提示/日志。"""
    try:
        raw = page.locator(".el-select-dropdown__item:visible").all_inner_texts()
    except Exception:  # noqa: BLE001
        return []
    seen: list[str] = []
    for t in raw:
        t = (t or "").strip()
        if t and t not in seen:
            seen.append(t)
    return seen[:limit]


def select_recent30(page, selector: str, log, *, after_click=None) -> str:
    """展开月份下拉并点击「最近30天」类选项; 返回点击后输入框值。找不到抛 RuntimeError。

    **2026-09-16 修(用户报障)**: "在步骤11的查流量的时候, 在文本框中粘贴asin后, 鼠标会移到别的地方,
    出现下拉框挡住了页面, 导致按钮找不到"。**截图证据**: `step11-g1-site.png` 页面正常, 而
    `step11-g1-date.png` **顶栏「产品」超级菜单展开盖住整页**且**页面被滚动了**(滚动条 + 右下"回到顶部"
    按钮出现)。**根因**: 原实现 `page.locator(selector).first.click()` **盲取第一个匹配**、不校验可见性
    → 命中的是**视野外**元素, Playwright 先把它滚进视野(页面因此滚动), 鼠标随之移动/悬停到 sticky 顶栏
    → 顶栏菜单展开盖住"立即查询/导出"按钮。现改为: 只点**可见且不在顶栏内**的候选, 点前 `park_mouse()`
    (挪鼠标 + Esc 退遮挡)。
    """
    from steps._browser_ux import click_verified, first_visible, park_mouse

    park_mouse(page)                     # 先退遮挡: 关掉可能已被 hover 打开的菜单, 并把鼠标挪离顶栏
    if first_visible(page, selector) is None:
        raise RuntimeError(f"月份下拉展开失败: 未找到可见的日期输入框({selector})")
    if not click_verified(page, selector, what="月份下拉", timeout=8000, log=log):
        raise RuntimeError(f"月份下拉展开失败: 日期输入框可见但点不到({selector})")
    if after_click is not None:
        after_click()
    for m in RECENT_MATCHERS:
        item = page.locator(".el-select-dropdown__item:visible").filter(has_text=m).first
        try:
            item.wait_for(state="visible", timeout=3000)
            item.click(timeout=5000)
            if after_click is not None:
                after_click()
            val = ""
            try:
                _inp = first_visible(page, selector) or page.locator(selector).first
                val = _inp.input_value() or ""
            except Exception:  # noqa: BLE001
                pass
            log(f"已选择「最近30天」(命中 {m!r}); 输入框值={val!r}")
            park_mouse(page)             # 选完把鼠标挪回内容区(下拉已关)
            return val
        except Exception:  # noqa: BLE001
            continue
    opts = " / ".join(list_options(page, selector))
    raise RuntimeError(f"未在下拉中找到「最近30天」选项(可见: {opts[:200]})")
