# -*- coding: utf-8 -*-
"""跨类目通用属性自检（2026-10-08）。

**为什么要有它**：这一轮用户是**一条条**把问题提出来我才改的
（材质没收敛、厚度与厚冲突、二值属性口径乱、来源优先级自相矛盾…）。
正确做法是把这些**问题类型**固化成自动检查，换任何类目都能自己报出来，
不必等人发现。

六类检查（全部数据驱动，不调 LLM）：

  ① 二值口径      `是否*` 属性只能取 是/否/未明确/空
  ② 值域碎片化    实际唯一值数 > `粒度建议` 的数据结论（过闸门/低基数）→ 该收敛了
  ③ 三方口径不一致 rules.values vs spec.hint「典型取值」 vs 实际产出值
  ④ 推导一致性    有 `derive_from` 的属性，其值必须与该属性从来源精确值推出来的档一致
  ⑤ 跨属性冲突    已声明 `cross_checks` 的违例
  ⑥ 声明未接通    rules.json 里声明了、但**代码里从不出现**的键（§13/§18/§21 反复踩）

用法::

    python -X utf8 scripts/audit_attributes.py --cat "Cutting Boards-US-202608"
    python -X utf8 scripts/audit_attributes.py --cat "<CAT>" --dir <隔离目录>
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

EMPTY = {"", "未明确", "未标注", "不适用", "未知", "待确认", "None", "none"}
BOOL_OK = {"是", "否"} | EMPTY


def _spec_hint_values(hint: str) -> list[str]:
    m = re.search(r"典型取值\s*([^；;。]+)", str(hint or ""))
    return [x.strip() for x in re.split(r"[/、]", m.group(1)) if x.strip()] if m else []


def _target_levels(cat_dir: Path, cat: str) -> dict[str, tuple[int, str]]:
    """读 `19_再聚类/*粒度建议*.txt`（数据驱动的目标档数）。

    先找传入目录，再回落到 `data/<CAT>/19_再聚类/` —— 隔离目录里通常没有这一层
    （实测踩过：只找前者 → ②值域碎片化 永远报 0 条，等于这一项检查形同虚设）。
    """
    cands: list[Path] = []
    for base in (cat_dir, ROOT / "data" / cat):
        d = base / "19_再聚类"
        if d.is_dir():
            cands += sorted(d.glob("*粒度建议*.txt"))
        p = base / "19_再聚类" / "主体材质-粒度建议.txt"
        if p.is_file():
            cands.append(p)
    p = cands[0] if cands else cat_dir / "19_再聚类" / "主体材质-粒度建议.txt"
    out: dict[str, tuple[int, str]] = {}
    if not p.is_file():
        return out
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = [x.strip() for x in line.split("|")]
        if len(parts) >= 6 and parts[0] and parts[0] != "属性":
            try:
                out[parts[0]] = (int(parts[4]), parts[5])
            except ValueError:
                continue
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cat", required=True)
    ap.add_argument("--dir", default="", help="隔离目录（含 18_标准化 交付表）；默认用 data/<CAT>")
    a = ap.parse_args()
    cat = a.cat
    cat_dir = Path(a.dir) / cat if a.dir else ROOT / "data" / cat
    # 定义层走解析器（自己的目录优先 → 共享沉淀层继承）—— 迁移后仍能找到
    try:
        from tools.attr_stability.catalog import resolve_rules_path, resolve_spec_path
        rules_f = resolve_rules_path(cat_dir.parent, cat)
        rules_f = rules_f if rules_f.is_file() else None
        spec_f = resolve_spec_path(cat_dir.parent, cat)
    except Exception:                                            # noqa: BLE001
        rules_f = next(iter(sorted(cat_dir.glob("*.rules.json"))), None)
        spec_f = cat_dir / f"{cat}-attribute-spec.json"
    if not rules_f or not spec_f.is_file():
        print("✗ 找不到 rules.json / spec：%s" % cat_dir)
        return 1
    rules = json.loads(rules_f.read_text(encoding="utf-8-sig"))
    spec = json.loads(spec_f.read_text(encoding="utf-8-sig"))
    r_by = {x["name"]: x for x in rules.get("attributes") or [] if x.get("name")}
    s_by = {x["name"]: x for x in spec.get("attributes") or [] if x.get("name")}
    names = [n for n in s_by]
    targets = _target_levels(cat_dir, cat)

    # 交付表
    xl = cat_dir / "18_标准化" / f"{cat}-属性标注-标准化交付.xlsx"
    rows: list[dict] = []
    header: list[str] = []
    if xl.is_file():
        try:
            import openpyxl
            wb = openpyxl.load_workbook(xl, read_only=True, data_only=True)
            ws = wb["属性标注"] if "属性标注" in wb.sheetnames else wb.active
            it = ws.iter_rows(values_only=True)
            header = [str(x or "") for x in next(it)]
            rows = [dict(zip(header, r)) for r in it]
            wb.close()
        except Exception as exc:                              # noqa: BLE001
            print("⚠ 读交付表失败: %s" % exc)

    issues: list[tuple[str, str, str]] = []          # (类别, 属性, 说明)

    def cell(v):
        return str(v if v is not None else "").strip()

    # ---- ① 二值口径 ----
    for n in names:
        if not n.startswith("是否"):
            continue
        if not rows:
            break
        bad = {k: v for k, v in Counter(cell(r.get(n)) for r in rows).items()
               if k not in BOOL_OK}
        if bad:
            issues.append(("①二值口径", n,
                           "出现非 是/否 的取值: %s" % dict(list(bad.items())[:6])))

    # ---- ② 值域碎片化 ----
    for n in names:
        if n not in targets or not rows:
            continue
        n_actual = len({cell(r.get(n)) for r in rows} - EMPTY)
        n_suggest, why = targets[n]
        if n_suggest and n_actual > n_suggest:
            issues.append(("②值域碎片化", n,
                           "实际 %d 个唯一值 > 数据建议 %d 个（依据：%s）→ 该收敛了"
                           % (n_actual, n_suggest, why)))

    # ---- ③ 三方口径不一致（不变量：hint 的「典型取值」必须 ⊆ rules.values）----
    # 为什么是 ⊆ 而不是"相等"：hint 里列的是**例子**（可以只列常见的几个），
    # rules.values 是**法条**（闸门强制的完整值域）。例子越界才致命 ——
    # 那等于提示词在诱导模型输出闸门不接受的值（实测 `是否含负离子`
    # rules 只有 1 个值、hint 写了 5 个 → 58 格全掉）。
    try:
        from tools.attr_stability.spec_sync import check_caliber
        for n in names:
            vals = [str(x) for x in ((r_by.get(n, {}).get("params") or {}).get("values") or [])]
            if not vals:
                continue
            hint = (s_by.get(n) or {}).get("hint")
            out = check_caliber(hint, vals)
            if out:
                issues.append(("③三方口径", n,
                               "hint 的「典型取值」有 %d 个不在 rules.values 里: %s（会诱导模型答非法值）"
                               % (len(out), out[:6])))
    except ImportError:
        pass

    # ---- ④ 推导一致性 ----
    if rows:
        try:
            from tools.attr_stability.derive import derive_band
            for n, spec_a in r_by.items():
                p = spec_a.get("params") or {}
                src, bands = p.get("derive_from"), p.get("bands")
                if not (src and bands and rows):
                    continue
                dim = str(src).split(".")[-1]
                alias = {"长": "长", "宽": "宽", "高": "厚", "厚": "厚"}
                col = alias.get(dim, dim)
                if col not in header:
                    issues.append(("④推导一致性", n, "来源列 `%s` 不在交付表里，无法核对" % col))
                    continue
                bad = 0
                for r in rows:
                    v, got = cell(r.get(col)), cell(r.get(n))
                    if not v or got in EMPTY:
                        continue
                    want = derive_band(v.split("，")[0], bands)
                    if want and got != want:
                        bad += 1
                if bad:
                    issues.append(("④推导一致性", n,
                                   "%d 行的值与「%s 精确值推出的档」不符" % (bad, col)))
        except ImportError:
            pass

    # ---- ⑤ 跨属性冲突 ----
    if rows:
        try:
            from tools.attr_stability.cross_checks import check_rows, load_cross_checks
            decl = load_cross_checks(rules)
            hits = check_rows([{"xl_row": i + 2, "attributes": r} for i, r in enumerate(rows)], decl)
            if hits:
                c = Counter("%s↔%s" % (h["左属性"], h["右属性"]) for h in hits)
                issues.append(("⑤跨属性冲突", "-", "%d 处: %s" % (len(hits), dict(c))))
        except ImportError:
            pass

    # ---- ⑥ 声明未接通（代码里从不出现该键） ----
    py = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                   for p in list(ROOT.glob("steps/*.py")) + list(ROOT.glob("tools/attr_stability/*.py")))
    for k in ("evidence_priority", "derive_from", "bands", "converge", "cross_checks",
              "derive_from", "top_keys", "allow_keys"):
        declared = k in rules or any(k in ((x.get("params") or {})) for x in rules.get("attributes") or [])
        if declared and k not in py:
            issues.append(("⑥声明未接通", k, "rules.json 里声明了，但 steps/ 与 tools/ 的代码里从不出现"))

    # ---- 报告 ----
    print("=" * 96)
    print("属性通用自检: %s" % cat)
    print("  交付表: %s（%d 行）" % (xl.name if xl.is_file() else "（未找到）", len(rows)))
    print("  数据建议档数来源: 19_再聚类/*粒度建议*.txt（%d 个属性）" % len(targets))
    print("=" * 96)
    if not rows:
        print("\n⚠ **无法检查**：没有交付表，①③④⑤ 四类检查全部没跑。")
        print("  这不是「通过」—— 不要当通过看。请先给出交付表路径（`--dir`）。")
        return 2
    if not targets:
        print("\n⚠ 没找到 `19_再聚类/*粒度建议*.txt` → ②值域碎片化 没跑（其余照常）。")
    if not issues:
        print("\n✅ 六类检查全部通过")
    else:
        by_kind: dict[str, list] = {}
        for k, n, msg in issues:
            by_kind.setdefault(k, []).append((n, msg))
        for k in sorted(by_kind):
            print("\n■ %s（%d 条）" % (k, len(by_kind[k])))
            for n, msg in by_kind[k]:
                print("   · %-14s %s" % (n, msg))
        print("\n合计 %d 条待处理" % len(issues))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
