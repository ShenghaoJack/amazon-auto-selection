# -*- coding: utf-8 -*-
"""清洗 results.jsonl 中五点的脏数据(2026-09 解析 bug 修复前的遗留)。

背景: 旧版 parse_product_html 全局扫描页面, 会把产品描述/A+/视频组件等
同构 <li> 误当五点(3-4 万字符的超长脏条目)。本脚本按启发式清理:
  - 长度 > MAX_LEN(默认 2000)的条目丢弃
  - 含页面脚本/组件特征(if(window. / mix_csa / logShoppableMetrics / vjs- 等)丢弃
保留正常短五点; 该记录五点全被过滤时置空。

用法: python scripts/clean_jsonl_bullets.py <CAT> [--max-len 2000] [--dry-run]
清洗后请重新生成明细: python scripts/regenerate_detail_sheet.py <CAT>
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MAX_LEN = 2000
_SCRIPT_MARKERS = (
    "if(window.",
    "mix_csa",
    "logShoppableMetrics",
    "ShoppableMetrics",
    "vjs-",
    "_dnNlL",
    "vse-",
    ".aplus",
    "cel_widget_id",
)


def is_dirty(text: str) -> bool:
    if len(text) > MAX_LEN:
        return True
    low = text.lower()
    return any(m in low for m in _SCRIPT_MARKERS)


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv
    if not args:
        print("用法: python scripts/clean_jsonl_bullets.py <CAT> [--dry-run]")
        return 1
    cat = args[0]
    path = os.path.join("data", cat, f"{cat}-results.jsonl")
    if not os.path.exists(path):
        print("文件不存在:", path)
        return 1
    changed_records = dropped = 0
    out_lines = []
    for line in open(path, encoding="utf-8-sig"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            out_lines.append(line)
            continue
        bullets = rec.get("bullets") or []
        if rec.get("ok") and bullets:
            kept = [b for b in bullets if not is_dirty(str(b))]
            if len(kept) != len(bullets):
                changed_records += 1
                dropped += len(bullets) - len(kept)
                rec["bullets"] = kept
        out_lines.append(json.dumps(rec, ensure_ascii=False))
    print(f"记录 {len(out_lines)} 条, 受影响 {changed_records} 条, 丢弃脏条目 {dropped} 条")
    if dry:
        print("dry-run, 未写回")
        return 0
    content = "\n".join(out_lines) + "\n"
    tmp = path + ".clean"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)
    print(f"已写回: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
