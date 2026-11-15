# -*- coding: utf-8 -*-
"""CLI: 「主体材质」两面核实(文字 + 图片视觉) → `17_属性标注/主体材质-两面核实.json`,
可选 `--apply` 写回交付表。

口径(用户 2026-09-24)
=====================
`A+B` 只表示**砧板两个切面材质不同**; 两面一样只写一个材质; 配件(垫/托盘/架/盖/包边/钢条)
不算"面"; 成套但不同件材质不同 → 只写主材质。双面可用 / 双面不同材质的行全部过图片视觉核实。

用法
====
    python scripts/rematerial.py --cat "Cutting Boards-US-202608"
    python scripts/rematerial.py --cat "Cutting Boards-US-202608" --report
    python scripts/rematerial.py --cat "Cutting Boards-US-202608" --apply
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.config import Config                                          # noqa: E402
from core.llm import LLMClient                                          # noqa: E402
from tools.attr_stability.material_faces import (                       # noqa: E402
    MaterialRefiner, DEFAULT_MATERIAL_VOCAB,
)

STEP17_DIR = "17_属性标注"
STD_DIR = "18_标准化"
ATTR_COL = "主体材质"
DUAL_COL = "是否双面可用"


class _Ctx:
    def __init__(self, config):
        self.config = config
        self._llm = None

    def llm(self):
        if self._llm is None:
            self._llm = LLMClient(self.config)
        return self._llm


def _std_path(cat_dir: str, cat: str) -> str:
    return os.path.join(cat_dir, STD_DIR, "%s-属性标注-标准化交付.xlsx" % cat)


def _asin_index(step17_dir: str) -> dict:
    rows = json.load(open(os.path.join(step17_dir, "input.json"), encoding="utf-8"))["rows"]
    idx = {}
    for i, r in enumerate(rows, start=2):
        xl = r.get("xl_row")
        try:
            xl = int(xl)
        except (TypeError, ValueError):
            xl = i
        idx[(str(r.get("父ASIN") or ""), str(r.get("变体ASIN") or ""))] = xl
    return idx


def _read_declared(cat_dir: str, cat: str, step17_dir: str) -> tuple[dict, list, str, str]:
    """读交付表 → ({xl_row: {主体材质, 是否双面可用}}, 表内容, 主sheet, 来源sheet)。"""
    import openpyxl
    path = _std_path(cat_dir, cat)
    if not os.path.exists(path):
        return {}, [], "", ""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheets = {name: [list(r) for r in wb[name].iter_rows(values_only=True)]
              for name in wb.sheetnames}
    wb.close()
    main = "属性标注" if "属性标注" in sheets else list(sheets)[0]
    src = "来源" if "来源" in sheets else ""
    hdr = sheets[main][0]
    if ATTR_COL not in hdr:
        return {}, sheets, main, src
    im = hdr.index(ATTR_COL)
    idual = hdr.index(DUAL_COL) if DUAL_COL in hdr else None
    ip = hdr.index("父ASIN") if "父ASIN" in hdr else None
    iv = hdr.index("变体ASIN") if "变体ASIN" in hdr else None
    idx = _asin_index(step17_dir)
    out = {}
    for k in range(1, len(sheets[main])):
        r = sheets[main][k]
        key = (str(r[ip] or ""), str(r[iv] or "")) if (ip is not None and iv is not None) else None
        xl = idx.get(key) if key else None
        xl = xl if xl is not None else k + 1
        out[xl] = {ATTR_COL: r[im], DUAL_COL: (r[idual] if idual is not None else None)}
    return out, sheets, main, src


def _vocab_from(cat_dir: str) -> list[str]:
    """受控材质词表: rules.json 的 主体材质 values + 交付表实际出现过的写法。"""
    vocab = list(DEFAULT_MATERIAL_VOCAB)
    try:
        path = os.path.join(cat_dir, "cutting_boards.rules.json")
        if not os.path.exists(path):
            cands = [f for f in os.listdir(cat_dir) if f.endswith(".rules.json")]
            path = os.path.join(cat_dir, cands[0]) if cands else ""
        if path and os.path.exists(path):
            rules = json.load(open(path, encoding="utf-8-sig"))
            for at in (rules.get("attributes") or []):
                if str(at.get("name") or "") == ATTR_COL:
                    for v in ((at.get("params") or {}).get("values") or []):
                        if v and v not in vocab:
                            vocab.append(str(v))
    except Exception:  # noqa: BLE001
        pass
    return vocab


def _fill_table(cat_dir: str, cat: str, payload: dict) -> dict:
    """把核实结果写回交付表「主体材质」列 + 「来源」sheet 同列。"""
    import openpyxl
    import shutil
    path = _std_path(cat_dir, cat)
    bak = path + ".bak-before-refine.xlsx"
    if not os.path.exists(bak):
        shutil.copy2(path, bak)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheets = {name: [list(r) for r in wb[name].iter_rows(values_only=True)]
              for name in wb.sheetnames}
    wb.close()
    main = "属性标注" if "属性标注" in sheets else list(sheets)[0]
    src = "来源" if "来源" in sheets else None
    hdr = sheets[main][0]
    if ATTR_COL not in hdr:
        raise SystemExit("[错误] 交付表没有「%s」列" % ATTR_COL)
    ip = hdr.index("父ASIN") if "父ASIN" in hdr else None
    iv = hdr.index("变体ASIN") if "变体ASIN" in hdr else None
    idx = _asin_index(os.path.join(cat_dir, STEP17_DIR))
    i_val = hdr.index(ATTR_COL)
    i_src = sheets[src][0].index(ATTR_COL) if src else None

    rows_map = payload.get("rows") or {}
    n_changed = n_total = n_miss = 0
    for k in range(1, len(sheets[main])):
        r = sheets[main][k]
        key = (str(r[ip] or ""), str(r[iv] or "")) if (ip is not None and iv is not None) else None
        xl = idx.get(key) if key else None
        xl = xl if xl is not None else k + 1
        rec = rows_map.get(str(xl))
        n_total += 1
        if not isinstance(rec, dict):
            n_miss += 1
            continue
        if not rec.get("改动"):
            continue
        v = str(rec.get("主体材质") or "").strip()
        if not v:
            continue
        sheets[main][k][i_val] = v
        if i_src is not None and k < len(sheets[src]):
            sheets[src][k][i_src] = str(rec.get("来源") or "").strip()
        n_changed += 1

    out = openpyxl.Workbook()
    first = out.active
    first.title = main
    for i, name in enumerate([main] + [s for s in sheets if s != main]):
        ws = first if i == 0 else out.create_sheet(name)
        for r in sheets[name]:
            ws.append(r)
    out.save(path)
    return {"覆盖行": n_total, "改动行": n_changed, "未匹配行": n_miss}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="主体材质两面核实(文字+图片视觉)")
    ap.add_argument("--cat", required=True)
    ap.add_argument("--report", action="store_true", help="只输出统计")
    ap.add_argument("--apply", action="store_true", help="写回交付表")
    ap.add_argument("--redecide", action="store_true",
                    help="用已有视觉记录重跑定案(不调用模型), 常与 --apply 连用")
    ap.add_argument("--retry-unusable", action="store_true",
                    help="只重跑已有产物里视觉记录不可用的行(不重跑全部)")
    ap.add_argument("--dry-text", action="store_true", help="只跑文字侧(不调模型, 列范围)")
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--log", default="")
    args = ap.parse_args(argv)

    cat_dir = os.path.join(_ROOT, "data", args.cat)
    cfg_path = os.path.join(cat_dir, "config.yaml")
    step17 = os.path.join(cat_dir, STEP17_DIR)
    if not os.path.exists(cfg_path):
        print("[错误] 找不到配置: %s" % cfg_path)
        return 2

    declared, _sheets, _main, _src = _read_declared(cat_dir, args.cat, step17)
    if args.dry_text:
        n_dual = sum(1 for d in declared.values() if str(d.get("是否双面可用") or "") in
                     ("双面可用", "双面不同材质"))
        n_plus = sum(1 for d in declared.values()
                     if len(re.split(r'[+\uff0b]', str(d.get(ATTR_COL) or ""))) > 1)
        print("交付表行数=%d  双面行=%d  材质含+=%d" % (len(declared), n_dual, n_plus))
        return 0

    payload = MaterialRefiner.load(step17)
    if args.redecide:
        if not payload:
            print("[提示] 尚无产物, 无法重跑定案")
            return 1
        cfg = Config.load(cfg_path)
        rows = json.load(open(os.path.join(step17, "input.json"), encoding="utf-8"))["rows"]
        ref = MaterialRefiner(_Ctx(cfg), vocab=_vocab_from(cat_dir),
                              log=lambda m: print("[主体材质] " + str(m), flush=True))
        payload = ref.redecide(rows, step17, declared=declared)
        _print_report(payload)
        if args.apply:
            print("[写回] %s" % json.dumps(_fill_table(cat_dir, args.cat, payload),
                                          ensure_ascii=False))
        return 0

    if args.report or (args.apply and payload):
        if not payload:
            print("[提示] 尚无产物")
            return 1
        if args.apply:
            print("[写回] %s" % json.dumps(_fill_table(cat_dir, args.cat, payload),
                                          ensure_ascii=False))
        if args.report:
            _print_report(payload)
        return 0

    cfg = Config.load(cfg_path)
    if args.workers:
        cfg.set("MATERIAL_WORKERS", args.workers)
    ctx = _Ctx(cfg)
    rows = json.load(open(os.path.join(step17, "input.json"), encoding="utf-8"))["rows"]

    import datetime
    log_fh = open(args.log, "a", encoding="utf-8") if args.log else None

    def log(msg):
        line = "[%s][主体材质] %s" % (datetime.datetime.now().strftime("%H:%M:%S"), msg)
        print(line, flush=True)
        if log_fh is not None:
            log_fh.write(line + "\n")
            log_fh.flush()

    vocab = _vocab_from(cat_dir)
    ref = MaterialRefiner(ctx, vocab=vocab, log=log)
    log("开始: %d 行, 并发 %d, detail=%s, 待核实 %d 行, 词表 %d 个"
        % (len(rows), ref.workers, ref.detail,
           sum(1 for r in rows if ref.needs_vision(r)) if declared else 0, len(vocab)))
    payload = ref.run(rows, step17, declared=declared,
                      retry_unusable=args.retry_unusable)
    _print_report(payload)
    if args.apply:
        print("[写回] %s" % json.dumps(_fill_table(cat_dir, args.cat, payload),
                                      ensure_ascii=False))
    if log_fh is not None:
        log_fh.close()
    return 0


def _print_report(payload) -> None:
    rows = payload.get("rows") or {}
    print("=" * 64)
    print("行数: %d  视觉调用: %s" % (len(rows), payload.get("视觉调用行数")))
    changed = [r for r in rows.values() if r.get("改动")]
    print("建议改动: %d 行" % len(changed))
    print("改动样例(前 20):")
    for k, r in list((k, v) for k, v in rows.items() if v.get("改动"))[:20]:
        print("   行%-5s %-22s → %-22s | %s" % (k, r.get("原值"), r.get("主体材质"),
                                              r.get("原因")))


if __name__ == "__main__":
    raise SystemExit(main())
