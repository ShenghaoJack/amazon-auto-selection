# -*- coding: utf-8 -*-
"""一次性补丁: step10 词源改为「关键词清单 核心大词 + 长尾词」。

按行号精确替换, 避免长 docstring 里全角/半角引号导致的匹配歧义。
执行前请确认 steps/step10_variants.py.bak-before-inject-sync 存在。
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "steps" / "step10_variants.py"

HEADER = '''    def _auto_core(self, ctx: StepContext, cat: str) -> list[str]:
        """**自动态**核心大词(与步骤9 的"自动核心词"完全同一批口径)。

        - **US 站**: FR-07 对**全部候选词**判定为"达标"的词全集
          (`<cat>-screen-results.json` 中 verdict=达标; step04 核心分区=全部候选,
          达标不限个数)——与 step08 报告核心分析 / step09 搜索收集口径一致,
          不再用 search-asins.json 键(那只是 达标核心∩搜索过的子集, 会漏 grinder 等);
        - **非 US 站(2026-09-15 用户口径)**: 不套 SCREEN_REQUIREMENT, 直接取
          **站内月搜索量前 NON_US_QUALIFIED_TOP(5) 个**候选词。
        """
'''

NEW_METHODS = '''
    def _screen_verdict(self, ctx: StepContext, cat: str) -> dict[str, str]:
        """`{关键词: FR-07 判定}`(供三态门槛口径复用)。"""
        out: dict[str, str] = {}
        screen = read_json(ctx.resolve_artifact(f"{cat}-screen-results.json"), default=[])
        if isinstance(screen, list):
            for rec in screen:
                if not isinstance(rec, dict):
                    continue
                w = str(rec.get("keyword") or "").strip().lower()
                if w:
                    out[w] = str(rec.get("verdict") or "").strip()
        return out

    def _final_keywords(self, ctx: StepContext, cat: str) -> list[str]:
        """步骤10 判定参考词源 = 关键词清单最终生效的「核心大词 + 长尾词」。

        2026-10-06 用户口径: 不再单独维护"步骤10 注入词"清单, 直接与 webui
        「关键词清单」面板里选择的词同步 —— 不需要再手动选一遍。三态统一由
        `core.keyword_lists.resolve()` 裁决(与步骤9 完全同一套逻辑):
          自动 = FR-07 达标核心词(US) / 站内搜索量前 N(非 US) + FR-04 长尾词;
          人工 = `filter-<CAT>-core-words.txt` / `filter-<CAT>-longtail-words.txt`;
          停止 = 只影响步骤9(不抓取); 此处核心词仍回落自动口径, 不会变空。
        `resolve()` 读不到文件一律退化为自动态, 不抛异常。
        """
        auto_core = self._auto_core(ctx, cat)
        auto_tail: list[str] = []
        try:
            _core_part, auto_tail = read_keywords_file(
                ctx.resolve_artifact(f"{cat}-keywords.txt"))
        except Exception:                                    # noqa: BLE001 — 缺文件按无长尾
            auto_tail = []
        us_site = _is_us_site(
            site=str(ctx.config.get("SITE", "") or ""),
            market=str(ctx.config.market or ""),
            cat=cat,
        )
        try:
            gate = max(1, int(ctx.config.get("GATE_MIN_QUALIFIED", 2) or 2))
        except (TypeError, ValueError):
            gate = 1
        plan = resolve_keywords(
            ctx.paths.cat_root, cat,
            auto_core=auto_core,
            auto_longtail=list(auto_tail or []),
            screen_verdict=self._screen_verdict(ctx, cat),
            gate=gate,
            gate_applies=us_site,
        )
        self.log(ctx, "步骤10 判定参考词源(与「关键词清单」同步): " + plan.source_text())
        self.log(ctx, "  " + plan.longtail_text())
        out: list[str] = []
        for w in list(plan.core) + list(plan.longtail):
            wl = str(w).strip().lower()
            if wl and wl not in out:
                out.append(wl)
        return out

'''


def main() -> int:
    bak = TARGET.with_name(TARGET.name + ".bak-before-inject-sync")
    if not bak.is_file():
        print(f"缺少备份 {bak}, 中止")
        return 1
    lines = TARGET.read_text(encoding="utf-8").splitlines(keepends=True)
    n = len(lines)

    # --- 定位三处锚点(1-based) ---
    def find(pred, start=0):
        for i in range(start, n):
            if pred(lines[i]):
                return i
        return -1

    i_def = find(lambda s: s.startswith("    def _final_keywords(self, ctx: StepContext, cat: str)"))
    i_auto = find(lambda s: s.startswith("        # ② 自动: US 站用 FR-07 达标全集"))
    i_sep = find(lambda s: s.startswith("    # ---- 2026-09-09 用户定稿:"))
    i_ret = find(lambda s: s == "        return []\n", i_auto)
    print(f"锚点(1-based): _final_keywords={i_def + 1}  # ②自动={i_auto + 1}  "
          f"return[]={i_ret + 1}  分隔注释={i_sep + 1}")
    if min(i_def, i_auto, i_sep, i_ret) < 0:
        print("锚点未找齐, 中止")
        return 1
    if not (i_def < i_auto < i_ret < i_sep):
        print("锚点顺序异常, 中止")
        return 1

    # --- 段1: 替换 810..839(0-based i_def..i_auto-1) 为新的 _auto_core 头 ---
    seg1 = [HEADER]
    # --- 段2: 在 i_sep 之前插入 _screen_verdict + _final_keywords ---
    new = lines[:i_def] + seg1 + lines[i_auto:i_sep] + [NEW_METHODS] + lines[i_sep:]

    out = "".join(new)
    if "_final_keywords" in out.split("def _auto_core")[0]:
        pass
    TARGET.write_text(out, encoding="utf-8")
    print(f"写入完成: {n} -> {len(new)} 行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
