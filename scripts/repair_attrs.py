# -*- coding: utf-8 -*-
"""CLI: 交付表列级修复 —— 「主体材质」归一 + 「产品类型」对齐 + 空白格补全。

用法
====
    python scripts/repair_attrs.py --cat "Cutting Boards-US-202608"          # 跑(带断点)
    python scripts/repair_attrs.py --cat "..." --report                      # 看规划统计
    python scripts/repair_attrs.py --cat "..." --apply                       # 写回交付表

口径(用户 2026-09-24)
=====================
1. 材质归一(全量, 含 `+` 单元格): 木种→**木制**; PP/塑料/小麦秸秆PP/亚克力/树脂/TPU→**塑料**;
   304不锈钢→**不锈钢**; 竹木→**竹制**; 钛金属→**钛**; 钢化玻璃→**玻璃**; 大理石/花岗岩→**石材**。
2. **逐行核实材质真伪**(文本优先 + 原文核验, 文字判不出才走图片视觉), 再按材质对齐「产品类型」。
3. **只补真空格**(不含「未明确」): 主体材质 / 产品类型 / 套装数量 / 尺寸规格。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.config import Config                                       # noqa: E402
from core.llm import LLMClient                                       # noqa: E402
from tools.attr_stability.attr_repair import (                       # noqa: E402
    AttrRepair, REPAIR_NAME, normalize_material,
)

STEP17_DIR = "17_属性标注"
STD_DIR = "18_标准化"
COLS = ("主体材质", "产品类型", "套装数量", "尺寸规格")


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


def _load(cat_dir: str, cat: str, step17_dir: str):
    """读交付表 → (sheets, main, src, {xl_row: {列: 值}})。"""
    import openpyxl
    path = _std_path(cat_dir, cat)
    if not os.path.exists(path):
        raise SystemExit("[错误] 找不到交付表: %s" % path)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheets = {n: [list(r) for r in wb[n].iter_rows(values_only=True)] for n in wb.sheetnames}
    wb.close()
    main = "属性标注" if "属性标注" in sheets else list(sheets)[0]
    src = "来源" if "来源" in sheets else ""
    hdr = sheets[main][0]
    idx = _asin_index(step17_dir)
    i = {c: hdr.index(c) for c in COLS if c in hdr}
    ip = hdr.index("父ASIN") if "父ASIN" in hdr else None
    iv = hdr.index("变体ASIN") if "变体ASIN" in hdr else None
    decl: dict = {}
    xls: list = []
    for k in range(1, len(sheets[main])):
        r = sheets[main][k]
        key = (str(r[ip] or ""), str(r[iv] or "")) if (ip is not None and iv is not None) else None
        xl = idx.get(key) if key else None
        xl = xl if xl is not None else k + 1
        xls.append(xl)
        decl[xl] = {c: r[j] for c, j in i.items()}
    return sheets, main, src, decl, xls


def _fill(cat_dir: str, cat: str, step17_dir: str, payload: dict) -> dict:
    import openpyxl
    import shutil
    sheets, main, src, decl, xls = _load(cat_dir, cat, step17_dir)
    path = _std_path(cat_dir, cat)
    bak = path + ".bak-before-repair.xlsx"
    if not os.path.exists(bak):
        shutil.copy2(path, bak)
    hdr = sheets[main][0]
    i = {c: hdr.index(c) for c in COLS if c in hdr}
    i_src = {c: sheets[src][0].index(c) for c in COLS if src and c in sheets[src][0]}
    rows_map = payload.get("rows") or {}
    n = 0
    for k, xl in enumerate(xls):
        rec = rows_map.get(str(xl))
        if not isinstance(rec, dict):
            continue
        changed = False
        for c in COLS:
            if c not in i:
                continue
            v = str(rec.get(c) or "").strip()
            if not v:
                continue
            if str(sheets[main][k + 1][i[c]] or "").strip() != v:
                sheets[main][k + 1][i[c]] = v
                changed = True
            srctxt = str(rec.get(c + "_来源") or "").strip()
            if srctxt and c in i_src and k + 1 < len(sheets[src]):
                sheets[src][k + 1][i_src[c]] = srctxt
        if changed:
            n += 1
    out = openpyxl.Workbook()
    first = out.active
    first.title = main
    for j, name in enumerate([main] + [s for s in sheets if s != main]):
        ws = first if j == 0 else out.create_sheet(name)
        for r in sheets[name]:
            ws.append(r)
    out.save(path)
    return {"覆盖行": len(xls), "改动行": n}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="交付表列级修复(材质归一/类型对齐/空白补全)")
    ap.add_argument("--cat", required=True)
    ap.add_argument("--report", action="store_true", help="只输出规划统计, 不调模型")
    ap.add_argument("--apply", action="store_true", help="写回交付表")
    ap.add_argument("--reload-text", action="store_true", help="复用已有文本审核结果, 不重调文本模型")
    ap.add_argument("--redecide", action="store_true",
                    help="用产物里留档的原始结论重跑定案(不调模型), 常与 --apply 连用")
    ap.add_argument("--size-detail", action="store_true",
                    help="重定案时额外对「多件套但只有一个尺寸」的行做逐件细化(会调模型)")
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--log", default="", help="同时把进度写到该文件(便于后台跑时监控)")
    args = ap.parse_args(argv)

    cat_dir = os.path.join(_ROOT, "data", args.cat)
    cfg_path = os.path.join(cat_dir, "config.yaml")
    step17 = os.path.join(cat_dir, STEP17_DIR)
    if not os.path.exists(cfg_path):
        print("[错误] 找不到配置: %s" % cfg_path)
        return 2

    _sheets, _main, _src, decl, _xls = _load(cat_dir, args.cat, step17)
    plan = AttrRepair.plan(decl)
    if args.report:
        import collections
        c = collections.Counter(v["原因"] for v in plan.values())
        print("需要处理 %d 行 / 共 %d 行" % (len(plan), len(decl)))
        for k, v in c.most_common():
            print("   %5d  %s" % (v, k))
        print("材质核实=%d 类型=%d 套装=%d 尺寸=%d"
              % (sum(1 for p in plan.values() if p["材质核实"]),
                 sum(1 for p in plan.values() if p["产品类型"]),
                 sum(1 for p in plan.values() if p["套装数量"]),
                 sum(1 for p in plan.values() if p["尺寸规格"])))
        return 0

    payload_path = os.path.join(step17, REPAIR_NAME)
    payload = {}
    # `--apply` 默认**复用已有产物**(不重跑模型); 要重跑就不带 --apply 先跑一遍
    if (args.redecide or args.apply) and os.path.exists(payload_path):
        try:
            payload = json.load(open(payload_path, encoding="utf-8"))
        except (OSError, ValueError):
            payload = {}
    if args.redecide:
        if not payload:
            print("[提示] 尚无产物, 无法重跑定案")
            return 1
        cfg = Config.load(cfg_path)
        rows = []
        if args.size_detail:
            rows = json.load(open(os.path.join(step17, "input.json"), encoding="utf-8"))["rows"]
        ref = AttrRepair(_Ctx(cfg), log=lambda m: print("[属性修复] " + str(m), flush=True))
        payload = ref.redecide(decl, payload, rows=rows, out_dir=step17,
                               with_size_detail=args.size_detail)
        ref._save(step17, payload)
        print("[重定案] 完成")
    if not payload:
        cfg = Config.load(cfg_path)
        if args.workers:
            cfg.set("REPAIR_WORKERS", args.workers)
        rows = json.load(open(os.path.join(step17, "input.json"), encoding="utf-8"))["rows"]
        import datetime
        log_fh = open(args.log, "a", encoding="utf-8") if args.log else None

        def _log(msg):
            line = "[%s][属性修复] %s" % (datetime.datetime.now().strftime("%H:%M:%S"), msg)
            print(line, flush=True)
            if log_fh is not None:
                log_fh.write(line + "\n")
                log_fh.flush()

        ref = AttrRepair(_Ctx(cfg), log=_log)
        payload = ref.run(rows, step17, decl, reload_text=args.reload_text)
        if log_fh is not None:
            log_fh.close()
    if args.apply:
        print("[写回] %s" % json.dumps(_fill(cat_dir, args.cat, step17, payload),
                                      ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
