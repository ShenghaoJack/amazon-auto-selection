# -*- coding: utf-8 -*-
"""只读冒烟(在**类目副本**上做, 不碰真数据): 仅注入词端到端。

验证四件事:
  ① 面板保存仅注入词 -> 文件出现 `[仅注入]` 分区;
  ② 步骤10 `_final_keywords()` 注入 核心大词 + 仅注入词;
  ③ 步骤9 的抓取口径(plan.core)**不含**仅注入词;
  ④ 面板 API 回的 inject_only / search_plan 正确(仅注入不增加搜索页数)。
"""
from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import Config              # noqa: E402
from core.keyword_lists import read_core_sections, resolve  # noqa: E402
from core.paths import Paths                # noqa: E402
from steps.base import StepContext          # noqa: E402
from steps.step10_variants import Step10Variants   # noqa: E402

SRC_CAT = "Salt and Pepper Mill Sets-US-202608"
TMP_ROOT = ROOT / "scripts" / "_tmp_smoke"
INJECT_WORDS = ["仅注入测试词A", "only inject B"]


def _load_server():
    spec = importlib.util.spec_from_file_location("ws_smoke", ROOT / "webui" / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    # ---- 建副本: 只拷该步骤需要的 4 个文件 ----
    shutil.rmtree(TMP_ROOT, ignore_errors=True)
    dst = TMP_ROOT / SRC_CAT
    dst.mkdir(parents=True)
    src = ROOT / "data" / SRC_CAT
    for name in (f"{SRC_CAT}-keywords.txt", f"{SRC_CAT}-screen-results.json",
                 f"filter-{SRC_CAT}-prompt.json", "config.yaml"):
        s = src / name
        if s.is_file():
            shutil.copy2(s, dst / name)
    # config 的 DATA_ROOT 指向副本根
    (dst / "config.yaml").write_text(
        f'CAT: "{SRC_CAT}"\nDATA_ROOT: "{TMP_ROOT.as_posix()}"\n'
        'SITE: "美国"\nMARKET: "amazon.com"\nGATE_MIN_QUALIFIED: 2\n', encoding="utf-8")

    S = _load_server()
    S.PROJECT_ROOT = ROOT

    cfg = Config({"CAT": SRC_CAT, "DATA_ROOT": str(TMP_ROOT), "DRY_RUN": True,
                  "SITE": "美国", "MARKET": "amazon.com"})
    ctx = StepContext(cfg, Paths(cfg))
    s10 = Step10Variants()
    logs: list[str] = []
    s10.log = lambda c, t, _l=logs: _l.append(t)          # type: ignore[assignment]

    core_file = dst / f"filter-{SRC_CAT}-core-words.txt"
    print("① 保存前 核心清单文件存在?", core_file.exists())

    before = s10._final_keywords(ctx, SRC_CAT)
    p0 = resolve(dst, SRC_CAT, auto_core=[], auto_longtail=[], gate=2, gate_applies=True)
    print(f"   自动态(无人工清单): 注入 {len(before)} 个 -> {before}")
    print(f"   其中 plan.core(供步骤9抓取) = 自动核心词, 与注入一致: {len(p0.inject_only) == 0}")

    # ---- 保存仅注入词(经面板 API) ----
    out = S.save_keyword_lists(SRC_CAT, str(TMP_ROOT), "inject", "manual", INJECT_WORDS)
    print(f"\n② 保存仅注入词: ok={out['ok']} saved={out['saved']}")
    print(f"   文件内容: {core_file.read_text(encoding='utf-8')!r}")
    print(f"   分区解析: {read_core_sections(core_file)}")

    after = s10._final_keywords(ctx, SRC_CAT)
    print(f"\n③ 步骤10 注入词 {len(after)} 个 -> {after}")
    for ln in logs[-3:]:
        print("   日志: " + ln)
    p1 = resolve(dst, SRC_CAT, auto_core=[w for w in before], auto_longtail=[],
                 gate=2, gate_applies=True)
    print(f"   ④ 步骤9 抓取口径 plan.core = {p1.core}")
    want = [w.lower() for w in INJECT_WORDS]
    print(f"   ④ 仅注入词在 plan.core 里吗? {[w for w in want if w in p1.core]} (期望 [])")
    print(f"   ④ 仅注入词在 plan.inject_words 里吗? {[w for w in want if w in p1.inject_words]} (期望全部)")
    print(f"   ④ plan.inject_only = {p1.inject_only}")

    # ---- 面板 ----
    panel = S.keyword_lists_panel(SRC_CAT, str(TMP_ROOT))
    io = panel.get("inject_only") or {}
    print(f"\n⑤ 面板 inject_only.words = {io.get('words')}")
    print(f"   面板 effective_inject = {len(io.get('effective_inject') or [])} 个")
    print(f"   预计搜索: {panel['search_plan']['text']}  <- 仅注入词**不增加**页数")

    # ---- 清回自动: 仅注入词应被清掉, 文件删除 ----
    S.save_keyword_lists(SRC_CAT, str(TMP_ROOT), "inject", "auto")
    print(f"\n⑥ 清空仅注入后 文件还在? {core_file.exists()} (期望 False — 两分区都空才删)")

    shutil.rmtree(TMP_ROOT, ignore_errors=True)
    print("\n冒烟完成(真数据未改动; 副本已删除)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
