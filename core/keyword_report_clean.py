# -*- coding: utf-8 -*-
"""关键词报告脏数据清理机制(2026-10-10)。

## 背景(用户实测触发)

关键词报告 `<CAT>-keyword-report.xlsx` 的 `长尾词` sheet 末尾**混着 3 行"方法/口径行"**:

    | 长尾词   | 维度     | 说明                                    |
    | 搜索配置 | 搜索页数 | 核心大词每词搜索 5 页, 长尾词每词搜索 4 页 |
    | 搜索配置 | 广告处理 | 只取自然位, 跳过含 Sponsored 标记的广告条目 |
    | 搜索配置 | 清单合并 | 搜索结果 ASIN 并入双榜清单后全量去重        |

也就是说: `长尾词` 列被拿去当**方法名**, 真正的方法标签写进了 **`维度` 列**。全仓库审计
**35/35 份**关键词报告都是这个形态, 且 35/35 份的 `维度` 列**从未填过真实取值**。

后果: 任何把 `长尾词` sheet 当关键词清单消费的下游(整合报告 / 看板 / 数据同步 / 人工阅读)
都会把 `搜索配置` 当成长尾词列出来 —— 2026-10-10 用户在整合报告里就看到了这一行。

## 本模块提供什么

1. **单一事实来源的判定规则** `is_method_row()` —— 写入方(step8)、清理脚本、整合报告共用同一套,
   避免"各处各写一版过滤"再次漂移。
2. **工作簿级清理** `clean_workbook()` —— 给**存量**报告用: 默认 dry-run, `apply=True` 时
   把方法行从 `长尾词` 移出并**保留到 `方法说明` sheet**(信息不丢), 先备份(`.bak`, 只留最初一份)再原子替换。
3. 供 step8 生成时**防御性过滤**(即使上游又把方法行塞进关键词清单, 也会被挡在写出之前)。

判定规则(任一命中即方法行):
* `长尾词` 命中方法词表(`搜索配置/搜索口径/口径/方法/备注`);
* `维度` 命中方法标签表(`搜索页数/广告处理/清单合并/页数/搜索配置`);
* 同一张表里该标签**重复出现 ≥2 次**(真实长尾词是唯一的) —— 由调用方传 `repeated` 计数。
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

# ---------------------------------------------------------------- 规则(单一事实来源)

#: 出现在 `长尾词` 列的方法名
METHOD_KEYWORDS = ("搜索配置", "搜索口径", "口径", "方法", "备注", "说明", "搜索设置")

#: 曾经被写进 `维度` 列的方法标签
METHOD_DIM_VALUES = ("搜索页数", "广告处理", "清单合并", "页数", "搜索配置", "搜索设置")

#: `长尾词` sheet 的列名(兼容缺列/改名)
LONGTALE_HEADERS = ("长尾词", "维度", "说明")

#: 方法行搬去哪个 sheet(该 sheet 若不存在则会新建, 两列: 项目 | 说明)
METHOD_SHEET = "方法说明"


def is_method_row(keyword, dim="", note="", repeated: int = 1) -> bool:
    """这一行是方法/口径行(而不是关键词)吗?

    `repeated` = 该 `keyword` 值在本表里出现的次数(真实长尾词应为 1)。
    """
    kw = str(keyword or "").strip()
    dm = str(dim or "").strip()
    if not kw:
        return True                                    # 空的"关键词"没有意义
    if kw in METHOD_KEYWORDS or dm in METHOD_DIM_VALUES:
        return True
    if any(w and w in kw for w in METHOD_KEYWORDS if w != "说明"):
        return True
    # 重复出现**单独**不足以判定: 真实关键词也可能在清单里重复(去重前)。只有该串本身就是"标签长相"
    # (含中日韩字符, 或是没有空格的单词)时才把重复当证据 —— 真实长尾词是英文多词短语("dog dryer for pets")。
    return int(repeated or 1) > 1 and _looks_like_label(kw)


def _looks_like_label(text: str) -> bool:
    """像"标签/字段名"而不是"搜索词": 含中日韩字符, 或没有空格且较短。"""
    t = str(text or "").strip()
    if not t:
        return True
    if any("\u4e00" <= ch <= "\u9fff" for ch in t):
        return True
    return (" " not in t) and len(t) <= 16


def split_longtail_rows(rows) -> tuple[list, list]:
    """把 `[(长尾词, 维度, 说明), ...]` 拆成 `(关键词行, 方法行)`, 顺序保持。"""
    norm = []
    for r in rows:
        r = list(r or [])
        kw = str(r[0]).strip() if len(r) > 0 and r[0] is not None else ""
        dm = str(r[1]).strip() if len(r) > 1 and r[1] is not None else ""
        nt = str(r[2]).strip() if len(r) > 2 and r[2] is not None else ""
        norm.append({"keyword": kw, "dim": dm, "note": nt})
    counts: dict = {}
    for x in norm:
        counts[x["keyword"]] = counts.get(x["keyword"], 0) + 1
    words = [x for x in norm if not is_method_row(x["keyword"], x["dim"], x["note"],
                                                 counts.get(x["keyword"], 1))]
    methods = [x for x in norm if is_method_row(x["keyword"], x["dim"], x["note"],
                                                counts.get(x["keyword"], 1))]
    return words, methods


def filter_longtail_words(words) -> list:
    """给写入方用的防御性过滤: 从"关键词字符串列表"里剔除方法名。"""
    out, seen = [], {}
    for w in words or []:
        seen[str(w).strip()] = seen.get(str(w).strip(), 0) + 1
    for w in words or []:
        sw = str(w).strip()
        if not is_method_row(sw, "", "", seen.get(sw, 1)):
            out.append(w)
    return out


# ---------------------------------------------------------------- 存量报告清理

def _load_sheet_rows(ws) -> list:
    return [list(r) for r in ws.iter_rows(values_only=True)]


def find_dirty(path) -> dict:
    """扫一份报告, 返回 `{sheet: {"header_row": int, "rows": [行号...], "methods": [...]}}`。

    只检查 `长尾词` sheet(当前唯一已知的脏数据位置); 未命中返回 `{}`。
    """
    from openpyxl import load_workbook

    path = Path(path)
    if not path.is_file():
        return {}
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        if "长尾词" not in wb.sheetnames:
            return {}
        rows = _load_sheet_rows(wb["长尾词"])
    finally:
        try:
            wb.close()
        except Exception:                              # noqa: BLE001
            pass
    if not rows:
        return {}
    hdr = [str(h).strip() if h is not None else "" for h in rows[0]]
    if "长尾词" not in hdr:
        return {}
    i_kw = hdr.index("长尾词")
    i_dm = hdr.index("维度") if "维度" in hdr else None
    i_nt = hdr.index("说明") if "说明" in hdr else None
    body = rows[1:]
    counts: dict = {}
    for r in body:
        kw = str(r[i_kw]).strip() if i_kw < len(r) and r[i_kw] is not None else ""
        counts[kw] = counts.get(kw, 0) + 1
    hits = []
    for n, r in enumerate(body, start=2):              # Excel 行号(表头=1)
        kw = str(r[i_kw]).strip() if i_kw < len(r) and r[i_kw] is not None else ""
        dm = str(r[i_dm]).strip() if i_dm is not None and i_dm < len(r) and r[i_dm] is not None else ""
        nt = str(r[i_nt]).strip() if i_nt is not None and i_nt < len(r) and r[i_nt] is not None else ""
        if is_method_row(kw, dm, nt, counts.get(kw, 1)):
            hits.append({"row": n, "keyword": kw, "dim": dm, "note": nt})
    return {"长尾词": hits} if hits else {}


def clean_workbook(path, *, apply: bool = False, backup: bool = True,
                   method_sheet: str = METHOD_SHEET) -> dict:
    """清理一份关键词报告的脏数据。

    * `apply=False`(默认): **只报告不写盘**(dry-run)。
    * `apply=True`: 删除 `长尾词` 里的方法行, 并把它们追加到 `方法说明` sheet(项目=`搜索配置: 维度`, 说明=正文),
      写入前先备份为 `<name>.bak`(**只在不存在 .bak 时创建**, 保留最初那份原始文件)并原子替换。
    * 幂等: 再跑一次不会重复追加、也不会再报脏数据。

    返回 `{"path", "dirty", "removed", "moved", "backup", "applied", "sheets_created"}`。
    """
    path = Path(path)
    found = find_dirty(path)
    methods = list(found.get("长尾词") or [])
    out = {"path": str(path), "dirty": len(methods), "removed": 0, "moved": 0,
           "backup": None, "applied": False, "sheets_created": False}
    if not methods or not apply:
        return out

    from openpyxl import load_workbook

    wb = load_workbook(path)
    try:
        ws = wb["长尾词"]
        for m in sorted(methods, key=lambda x: -x["row"]):
            ws.delete_rows(m["row"], 1)
            out["removed"] += 1
        if method_sheet in wb.sheetnames:
            ws2 = wb[method_sheet]
        else:
            ws2 = wb.create_sheet(method_sheet)
            out["sheets_created"] = True
        # 表头/续写位置显式定位: 新建 sheet 时 `max_row` 就是 1(空), 用 append 会把表头写到第 2 行。
        has_header = bool(ws2.cell(row=1, column=1).value) or bool(ws2.cell(row=1, column=2).value)
        if not has_header:
            ws2.cell(row=1, column=1).value = "项目"
            ws2.cell(row=1, column=2).value = "说明"
            start = 2
        else:
            start = ws2.max_row + 1
        for i, m in enumerate(methods):
            label = ("搜索配置: %s" % m["dim"]) if m["dim"] else "搜索配置"
            ws2.cell(row=start + i, column=1).value = label
            ws2.cell(row=start + i, column=2).value = m["note"]
            out["moved"] += 1
        if backup:
            bak = path.with_suffix(path.suffix + ".bak")
            if not bak.exists():
                shutil.copyfile(path, bak)
                out["backup"] = str(bak)
        tmp = path.with_suffix(path.suffix + ".tmp")
        wb.save(tmp)
    finally:
        try:
            wb.close()
        except Exception:                              # noqa: BLE001
            pass
    os.replace(tmp, path)
    out["applied"] = True
    return out


def default_reports(include_archive: bool = False):
    """默认要处理的关键词报告: `data/*/` 下的(不含 archive), `include_archive=True` 才含归档。"""
    root = Path("data")
    if not root.is_dir():
        return []
    pats = ["*/*keyword-report.xlsx"] if not include_archive else ["**/*keyword-report.xlsx"]
    out = []
    for pat in pats:
        for p in root.glob(pat):
            s = str(p)
            if not include_archive and "archive" in p.parts:
                continue
            if "20_整合市场报告" in s:
                continue
            out.append(p)
    return sorted(set(out))
