# -*- coding: utf-8 -*-
"""一次性补丁: 步骤10 注入源收敛为**只核心大词**(长尾词不参与注入)。

用户口径(2026-10-06): "长尾词不要了, 只要核心词, 和原来的原理一样, 但是从关键词清单
(核心大词/长尾词)模块中选择需要注入的词就行 —— 相当于在关键词清单模块中加入了
步骤10 注入词(达标关键词)的功能。"

按锚点定位、整段替换, 避免长 docstring 里的引号歧义。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "steps" / "step10_variants.py"
BAK = TARGET.with_name(TARGET.name + ".bak-before-inject-core-only")

NEW_METHOD = '''    def _final_keywords(self, ctx: StepContext, cat: str) -> list[str]:
        """步骤10 注入的判定参考词 = 「关键词清单」里最终生效的**核心大词**。

        2026-10-06 用户口径: 原「步骤10 注入词」面板已下线, 它的功能**并入
        「关键词清单」面板的核心大词列** —— 原理与原注入词完全一致:
          ① 存在人工核心清单 `filter-<CAT>-core-words.txt`(在面板里勾选/手填后保存)
             → 就用这份清单(可删掉不想要的达标词, 也可加不达标词或手填新词),
               **不再看 FR-07 判定**;
          ② 没有人工清单 → 自动: **US 站** = FR-07 判"达标"的词全集;
             **非 US 站** = 站内月搜索量前 NON_US_QUALIFIED_TOP(5) 个候选词;
          ③ 人工停止标记只影响步骤9(不抓取), 此处核心词仍回落自动口径, 不会变空。

        **长尾词不参与注入**(2026-10-06 用户明确): 长尾词只用于步骤9 抓取与报告。
        `resolve()` 读不到文件一律退化为自动态, 不抛异常。
        """
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
            auto_core=self._auto_core(ctx, cat),
            auto_longtail=[],                 # 长尾词不参与注入(只用于步骤9 抓取/报告)
            screen_verdict=self._screen_verdict(ctx, cat),
            gate=gate,
            gate_applies=us_site,
        )
        self.log(ctx, "步骤10 判定参考词源(取「关键词清单」的核心大词): " + plan.source_text())
        out: list[str] = []
        for w in plan.core:
            wl = str(w).strip().lower()
            if wl and wl not in out:
                out.append(wl)
        return out

'''


def main() -> int:
    if not BAK.is_file():
        print(f"缺少备份 {BAK}, 中止")
        return 1
    src = TARGET.read_text(encoding="utf-8")

    # 1) 整段替换 _final_keywords(从 def 行到下一个 "# ---- " 分隔注释之前)
    head = "    def _final_keywords(self, ctx: StepContext, cat: str) -> list[str]:\n"
    tail = "    # ---- 2026-09-09 用户定稿:"
    i, j = src.find(head), src.find(tail)
    if i < 0 or j < 0 or j < i:
        print(f"锚点定位失败: def@{i} sep@{j} -> 中止")
        return 1
    old_block = src[i:j]
    src = src[:i] + NEW_METHOD + src[j:]
    print(f"OK  替换 _final_keywords: {old_block.count(chr(10))} 行 -> "
          f"{NEW_METHOD.count(chr(10))} 行")

    # 2) 去掉不再使用的 read_keywords_file 导入
    old_imp = ("from core.keyword_lists import (\n"
               "    read_keywords_file,\n"
               "    resolve as resolve_keywords,\n"
               ")\n")
    new_imp = "from core.keyword_lists import resolve as resolve_keywords\n"
    if src.count(old_imp) != 1:
        print(f"导入块命中 {src.count(old_imp)} 次 -> 中止(未写盘)")
        return 1
    src = src.replace(old_imp, new_imp, 1)
    print("OK  精简 core.keyword_lists 导入")

    TARGET.write_text(src, encoding="utf-8")

    # 3) 自检
    import ast
    ast.parse(src)
    print("OK  语法通过")
    for k in ("read_keywords_file", "auto_longtail=list(auto_tail"):
        print(f"    残留 {k}: {src.count(k)} 次 (期望 0)")
    print(f"    plan.longtail 出现: {src.count('plan.longtail')} 次 (期望 0)")
    print("补丁完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
