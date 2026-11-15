# -*- coding: utf-8 -*-
"""CLI: 重跑「厚度」精确值(文字 + 图片视觉 + OCR) → `17_属性标注/厚度-精确值.json`,
可选 `--apply` 直接把精确厚度写回交付表。

用法
====
    # 跑/续跑(行级断点, 中断后重跑只补剩余)
    python scripts/rethickness.py --cat "Cutting Boards-US-202608"

    # 只统计已有产物, 不调用模型
    python scripts/rethickness.py --cat "Cutting Boards-US-202608" --report

    # 把产物写回 标准化交付 xlsx 的「厚度」列 + 「来源」列
    python scripts/rethickness.py --cat "Cutting Boards-US-202608" --apply

产物 / 改动
===========
`data/<CAT>/17_属性标注/厚度-精确值.json`
    {"cat":…, "unit":"cm", "rows": {"<xl_row>": {厚度, cm, 层级, 套装件数, 来源, 需复核, 证据, 视觉}}}
`data/<CAT>/18_标准化/<CAT>-属性标注-标准化交付.xlsx`(仅 --apply)
    「属性标注」sheet 的「厚度」列 = 精确 cm 值; 「来源」sheet 的同列 = 取证方式。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.config import Config                                    # noqa: E402
from core.llm import LLMClient                                    # noqa: E402
from tools.attr_stability.thickness import (                      # noqa: E402
    ThicknessRefiner, text_verdict as thickness_text_verdict,
)

STEP17_DIR = "17_属性标注"
STD_DIR = "18_标准化"
ATTR_COL = "厚度"


class _Ctx:
    """最小上下文: 本工具只用 `config` 与 `llm()`。"""

    def __init__(self, config):
        self.config = config
        self._llm = None

    def llm(self):
        if self._llm is None:
            self._llm = LLMClient(self.config)
        return self._llm


def _asin_index(step17_dir: str) -> dict:
    """{ (父ASIN, 变体ASIN): xl_row } —— 交付表里没有行号列, 靠 ASIN 对回行号。"""
    path = os.path.join(step17_dir, "input.json")
    rows = json.load(open(path, encoding="utf-8"))["rows"]
    idx = {}
    for i, r in enumerate(rows, start=2):
        xl = r.get("xl_row")
        try:
            xl = int(xl)
        except (TypeError, ValueError):
            xl = i
        idx[(str(r.get("父ASIN") or ""), str(r.get("变体ASIN") or ""))] = xl
    return idx


def _sheet_rows_with_xl(cat_dir: str, cat: str, step17_dir: str):
    """读交付表两个 sheet → (sheets, index_main, index_src, [每数据行的 xl_row])。"""
    import openpyxl
    path = os.path.join(cat_dir, STD_DIR, "%s-属性标注-标准化交付.xlsx" % cat)
    if not os.path.exists(path):
        return None, None, None, None, path
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheets = {name: [list(r) for r in wb[name].iter_rows(values_only=True)]
              for name in wb.sheetnames}
    wb.close()
    main = "属性标注" if "属性标注" in sheets else list(sheets)[0]
    hdr = sheets[main][0]
    ip = hdr.index("父ASIN") if "父ASIN" in hdr else None
    iv = hdr.index("变体ASIN") if "变体ASIN" in hdr else None
    idx = _asin_index(step17_dir)
    xls = []
    for k in range(1, len(sheets[main])):
        r = sheets[main][k]
        key = (str(r[ip] or ""), str(r[iv] or "")) if (ip is not None and iv is not None) else None
        xl = idx.get(key) if key else None
        xls.append(xl if xl is not None else k + 1)   # 兜底: 按写表顺序(与 xl_row 同序)
    return sheets, main, ("来源" if "来源" in sheets else None), xls, path


def _load_set_labels(cat_dir: str, cat: str, step17_dir: str) -> dict:
    """从交付表读「套装数量」列 → {xl_row: "3件套"}。

    厚度最容易错的地方就是套装: 结构化尺寸那一维可能是**整叠之和**。
    件数以交付表已标注的「套装数量」为准(用户 2026-09-24 要求)。
    """
    try:
        sheets, main, _src, xls, _p = _sheet_rows_with_xl(cat_dir, cat, step17_dir)
        if not sheets:
            return {}
        hdr = sheets[main][0]
        if "套装数量" not in hdr:
            return {}
        i_set = hdr.index("套装数量")
        return {xl: sheets[main][k + 1][i_set]
                for k, xl in enumerate(xls) if k + 1 < len(sheets[main])}
    except Exception as exc:  # noqa: BLE001
        print("[提示] 读交付表「套装数量」失败(改按标题推定件数): %s" % exc)
        return {}


def _backup_once(path: str) -> str:
    """写回前留一份备份(**只留第一次**), 便于对照/回滚。"""
    import shutil
    bak = path + ".bak-before-refine.xlsx"
    if not os.path.exists(bak):
        shutil.copy2(path, bak)
    return bak


def _fill_table(cat_dir: str, cat: str, step17_dir: str, payload: dict) -> dict:
    """把精确厚度写回交付表: 「属性标注」sheet 的厚度列 + 「来源」sheet 的同列。

    整表重写(与步骤18 `_write_deliverables` 同法), 但只动这两列, 其余原样保留。
    """
    import openpyxl
    rows_map = payload.get("rows") or {}
    sheets, main, src, xls, path = _sheet_rows_with_xl(cat_dir, cat, step17_dir)
    if not sheets:
        raise SystemExit("[错误] 找不到交付表: %s" % path)
    _backup_once(path)
    hdr = sheets[main][0]
    if ATTR_COL not in hdr:
        raise SystemExit("[错误] 交付表没有「%s」列" % ATTR_COL)
    i_val = hdr.index(ATTR_COL)
    i_src = sheets[src][0].index(ATTR_COL) if src else None

    n_set = n_val = n_unknown = n_miss = 0
    for k, xl in enumerate(xls):
        rec = rows_map.get(str(xl))
        if not isinstance(rec, dict):
            n_miss += 1
            continue
        v = str(rec.get("厚度") or "").strip() or "未明确"
        sheets[main][k + 1][i_val] = v
        if i_src is not None and k + 1 < len(sheets[src]):
            sheets[src][k + 1][i_src] = str(rec.get("来源") or "").strip()
        try:
            if int(rec.get("套装件数") or 0) > 1:
                n_set += 1
        except (TypeError, ValueError):
            pass
        if v == "未明确":
            n_unknown += 1
        else:
            n_val += 1

    out = openpyxl.Workbook()
    first = out.active
    first.title = main
    for idx, name in enumerate([main] + [s for s in sheets if s != main]):
        ws = first if idx == 0 else out.create_sheet(name)
        for r in sheets[name]:
            ws.append(r)
    out.save(path)
    return {"覆盖行": n_val + n_unknown, "精确值": n_val, "未明确": n_unknown,
            "套装行": n_set, "未匹配行": n_miss}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="厚度精确值重提取(文字+图片视觉+OCR)")
    ap.add_argument("--cat", required=True, help="类目名(即 data/ 下的目录名)")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 行(调试用)")
    ap.add_argument("--report", action="store_true", help="只输出统计, 不调用模型")
    ap.add_argument("--apply", action="store_true", help="把结果写回 标准化交付 xlsx")
    ap.add_argument("--redecide", action="store_true",
                    help="用已有视觉记录重跑定案(不调用模型), 常与 --apply 连用")
    ap.add_argument("--retry-unusable", action="store_true",
                    help="只重跑已有产物里视觉记录不可用的行(不重跑全部)")
    ap.add_argument("--text-pass", action="store_true",
                    help="先跑一遍**文本 LLM 复核**(纯文字找单块厚度, 带原文核验), 再重定案")
    ap.add_argument("--workers", type=int, default=0, help="覆盖并发数")
    ap.add_argument("--log", default="", help="同时把进度写到该文件(便于后台跑时监控)")
    args = ap.parse_args(argv)

    cat_dir = os.path.join(_ROOT, "data", args.cat)
    cfg_path = os.path.join(cat_dir, "config.yaml")
    if not os.path.exists(cfg_path):
        print("[错误] 找不到配置: %s" % cfg_path)
        return 2
    step17 = os.path.join(cat_dir, STEP17_DIR)
    input_path = os.path.join(step17, "input.json")
    if not os.path.exists(input_path):
        print("[错误] 找不到步骤17 输入快照: %s" % input_path)
        return 2

    payload = ThicknessRefiner.load(step17)

    if args.text_pass:
        cfg = Config.load(cfg_path)
        if args.workers:
            cfg.set("THICKNESS_WORKERS", args.workers)
        rows = json.load(open(input_path, encoding="utf-8"))["rows"]
        labels = _load_set_labels(cat_dir, args.cat, step17)
        ref = ThicknessRefiner(_Ctx(cfg), log=lambda m: print("[厚度] " + str(m), flush=True))
        only = set()
        for r in rows:
            if thickness_text_verdict(r)["status"] != "decided":
                try:
                    only.add(int(r.get("xl_row")))
                except (TypeError, ValueError):
                    pass
        print("[厚度] 文本复核范围: %d 行(正则没抓到显式厚度的行)" % len(only))
        ref.text_pass(rows, step17, only_xls=only)
        payload = ref.redecide(rows, step17, set_labels=labels)
        _print_report(payload)
        if args.apply:
            stat = _fill_table(cat_dir, args.cat, step17, payload)
            print("[写回] %s" % json.dumps(stat, ensure_ascii=False))
        return 0

    if args.redecide:
        if not payload:
            print("[提示] 尚无产物, 无法重跑定案")
            return 1
        cfg = Config.load(cfg_path)
        rows = json.load(open(input_path, encoding="utf-8"))["rows"]
        ref = ThicknessRefiner(_Ctx(cfg), log=lambda m: print("[厚度] " + str(m), flush=True))
        payload = ref.redecide(rows, step17, set_labels=_load_set_labels(cat_dir, args.cat, step17))
        _print_report(payload)
        if args.apply:
            stat = _fill_table(cat_dir, args.cat, step17, payload)
            print("[写回] %s" % json.dumps(stat, ensure_ascii=False))
        return 0

    if args.report or (args.apply and payload):
        if not payload:
            print("[提示] 尚无产物 %s" % os.path.join(step17, ThicknessRefiner.OUT_NAME))
            return 1
        if args.apply:
            stat = _fill_table(cat_dir, args.cat, step17, payload)
            print("[写回] %s" % json.dumps(stat, ensure_ascii=False))
        if args.report:
            _print_report(payload)
        return 0

    cfg = Config.load(cfg_path)
    if args.workers:
        cfg.set("THICKNESS_WORKERS", args.workers)
    ctx = _Ctx(cfg)
    rows = json.load(open(input_path, encoding="utf-8"))["rows"]
    if args.limit:
        rows = rows[:args.limit]
    set_labels = _load_set_labels(cat_dir, args.cat, step17)

    import datetime
    log_fh = open(args.log, "a", encoding="utf-8") if args.log else None

    def log(msg):
        line = "[%s][厚度] %s" % (datetime.datetime.now().strftime("%H:%M:%S"), msg)
        print(line, flush=True)
        if log_fh is not None:
            log_fh.write(line + "\n")
            log_fh.flush()

    ref = ThicknessRefiner(ctx, log=log)
    n_set = sum(1 for x in set_labels.values() if str(x) not in ("", "None", "未明确")
                and not str(x).startswith("1件"))
    log("开始: %d 行, 并发 %d, detail=%s, 图片上限 %d+%d, 套装(>1件) %d 行, 需视觉 %d 行"
        % (len(rows), ref.workers, ref.detail, ref.max_images, ref.aplus_images,
           n_set, sum(1 for r in rows if ref.needs_vision(r))))
    payload = ref.run(rows, step17, set_labels=set_labels,
                      retry_unusable=args.retry_unusable)
    _print_report(payload)
    if args.apply:
        stat = _fill_table(cat_dir, args.cat, step17, payload)
        print("[写回] %s" % json.dumps(stat, ensure_ascii=False))
    if log_fh is not None:
        log_fh.close()
    return 0


def _print_report(payload) -> None:
    rows = payload.get("rows") or {}
    print("=" * 64)
    print("类目: %s  行数: %d  单位: %s" % (payload.get("cat"), len(rows), payload.get("unit")))
    print("视觉调用行数: %s" % payload.get("视觉调用行数"))
    print("层级分布:")
    for k, v in sorted((payload.get("统计") or {}).items(), key=lambda x: -x[1]):
        print("   %-14s %d" % (k, v))
    known = sum(1 for r in rows.values() if r.get("厚度") not in (None, "", "未明确"))
    review = sum(1 for r in rows.values() if r.get("需复核"))
    print("有精确值: %d / %d (%.1f%%)" % (known, len(rows), 100.0 * known / max(len(rows), 1)))
    print("标待复核: %d" % review)


if __name__ == "__main__":
    raise SystemExit(main())
