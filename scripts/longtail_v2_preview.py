# FR-04 五维长尾 + FR-09 抓取词 预览(真实数据 + 真实 LLM, **不写任何管线产物、不抓页面**)
#
# 用途: 改版后人工核对 —— 每个类目打印:
#   ① 基座词(核心根词, 根词规则"乙"选出)
#   ② 五维高频实体(商品数/出现次数/来源) + 全部长尾词
#   ③ FR-09 抓取词: 每维频率最高的实体 × **搜索量最高的达标词**(基座) = 5 条
#   ④ 收集清单预览: 全部达标核心词 + 那 5 条
#
# 用法: python scripts/longtail_v2_preview.py [类目名 ...]   (缺省=所有有真实数据的类目)
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import Config                                    # noqa: E402
from core.io_utils import read_json, read_jsonl                   # noqa: E402
from core.paths import Paths                                      # noqa: E402
from steps._keywords import (                                    # noqa: E402
    DIM_RULES,
    crawl_words,
    parse_entity_artifact,
)
from steps.base import StepContext                                # noqa: E402
from steps.step04_keywords import Step04Keywords                  # noqa: E402
from steps.step09_search_collect import Step09SearchCollect, _accessory_terms_from, _matches_accessory  # noqa: E402


class _Capture:
    """假 logger: 收步骤日志, 避免与控制台表格混在一起。"""

    def __init__(self):
        self.msgs: list[str] = []

    def info(self, msg):
        self.msgs.append(str(msg))


def _categories(argv: list[str]) -> list[Path]:
    out = []
    for cfg_path in sorted((ROOT / "data").glob("*/config.yaml")):
        if argv and cfg_path.parent.name not in argv:
            continue
        if not (cfg_path.parent / f"{cfg_path.parent.name}-dual-details-filtered.jsonl").exists():
            continue
        try:
            if Config.load(cfg_path).dry_run:
                continue
        except Exception:
            continue
        out.append(cfg_path)
    return out


def _preview(cfg_path: Path) -> str:
    cfg = Config.load(cfg_path)
    cat = cfg.cat
    ctx = StepContext(cfg, Paths(cfg))
    log = _Capture()
    ctx.log = log
    records = read_jsonl(ctx.resolve_artifact(f"{cat}-dual-details-filtered.jsonl"))
    s4 = Step04Keywords()
    s4._cat = cat
    titles = [s4._normalize_text(str(r.get("title", ""))) for r in records]
    texts = [s4._fulltext_text(r) for r in records]
    per_dim = s4._longtail_per_dim(ctx)

    lines = [f"## {cat}", ""]
    root = s4._pick_root(cat, titles)
    lines.append(f"- 语料: 过滤后榜单详情 {len(records)} 条(标题+五点)")
    lines.append(f"- 基座词(核心根词, 规则乙): **{root}**")
    lines.append(f"- 每维条数: {per_dim}(5 维共 {per_dim * 5} 条)")
    lines.append("")

    words, report = s4._generate_longtail(ctx, cat, root, texts, {root}, per_dim)
    report_text = s4._entity_report_text(cat, root, len(records), per_dim, report)
    parsed = parse_entity_artifact(report_text.split("\n"))       # 回读校验(FR-09 就是这么做)
    assert sum(len(v["longtails"]) for v in parsed.values()) == len(words), "台账回读数量不一致"

    lines.append("### 五维实体(按『提到它的商品数』降序)与长尾词")
    lines.append("")
    lines.append("| 维度 | 实体(商品数/出现次数/来源) | 长尾词 |")
    lines.append("|---|---|---|")
    for dim in DIM_RULES:
        rows = (report.get(dim) or {}).get("entities") or []
        ents = "、".join(f"{r['entity']}({r['records']}/{r['occ']}/{r['source']})" for r in rows) or "(无)"
        tails = "<br>".join((report.get(dim) or {}).get("longtails") or []) or "(无)"
        fb = (report.get(dim) or {}).get("fallback", 0)
        if fb:
            tails += f"<br>*(其中 {fb} 条为通用模板兜底)*"
        lines.append(f"| {dim} | {ents} | {tails} |")
    lines.append("")

    # ---------- FR-09 抓取词 ----------
    s9 = Step09SearchCollect()
    s9._cat = cat
    screen = read_json(ctx.resolve_artifact(f"{cat}-screen-results.json"), default=[])
    qualified = [str(r.get("keyword") or "") for r in screen
                 if isinstance(r, dict) and str(r.get("verdict") or "") == "达标"]
    qualified = [w for w in qualified if w]
    base = s9._top_volume_core(ctx, qualified) if qualified else ""
    lines.append("### FR-09 抓取词(每维最高频实体 × 搜索量最高的达标词)")
    lines.append("")
    lines.append(f"- 达标核心词 {len(qualified)} 个: " + ("、".join(qualified) or "(无, 需先跑 FR-07)"))
    lines.append(f"- 抓取词基座(搜索量最高的达标词): **{base or '(无)'}**")
    lines.append("")
    if base:
        s9._cat = cat
        bad = _accessory_terms_from(
            read_json(ctx.resolve_artifact(f"{cat}-filter-prompt.json"), default={}))
        real = s9._crawl_keywords(ctx, qualified, bad,
                                  report_lines=report_text.split("\n"))   # 真实 FR-09 代码路径
        pairs = crawl_words(parsed, base, reject=lambda w: _matches_accessory(w, bad))
        lines.append("| 维度 | 选用实体 | 抓取词 |")
        lines.append("|---|---|---|")
        for dim in DIM_RULES:
            hit = next((p for p in pairs if p[0] == dim), None)
            lines.append(f"| {dim} | {hit[1] if hit else '(无可用实体)'} | "
                         f"**{hit[2]}** |" if hit else f"| {dim} | (无) | (跳过) |")
        lines.append("")
        lines.append(f"- 页面抓取清单 = 达标核心词 {len(qualified)} 个 + 抓取词 {len(real)} 个")
        lines.append("  = " + "、".join(qualified + [w for w in real if w not in qualified]))
        if real != [p[2] for p in pairs]:
            lines.append(f"- ⚠️ 预览组装与 FR-09 代码路径结果不一致: preview={[p[2] for p in pairs]} vs step09={real}")
    else:
        lines.append("(无达标词 → 无法组装抓取词; 正常管线在 FR-07 之后才有达标词)")
    lines.append("")
    lines.append("<details><summary>步骤日志(实体抽取/长尾组装)</summary>")
    lines.append("")
    lines.append("```")
    lines.extend(log.msgs)
    lines.append("```")
    lines.append("</details>")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    targets = _categories(sys.argv[1:])
    if not targets:
        print("没有找到符合条件(非 dry-run + 有过滤后榜单详情)的类目")
        return 1
    print(f"待预览类目 {len(targets)} 个\n")
    out = ["# FR-04 五维长尾 + FR-09 抓取词 预览(真实数据/真实 LLM)", ""]
    for cfg_path in targets:
        print(f"=== {cfg_path.parent.name} ===", flush=True)
        try:
            block = _preview(cfg_path)
        except Exception as exc:                                   # 单个类目失败不中断
            print(f"  [失败] {type(exc).__name__}: {exc}", flush=True)
            block = f"## {cfg_path.parent.name}\n\n(失败: {type(exc).__name__}: {exc})\n"
        out.append(block)
        print("\n".join(block.split("\n")[:-1])[:400], "...\n", flush=True)
    dest = ROOT / "长尾五维与抓取词-预览-真实LLM-20260913.md"
    dest.write_text("\n".join(out), encoding="utf-8", newline="\n")
    print(f"\n完整报告已写入: {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
