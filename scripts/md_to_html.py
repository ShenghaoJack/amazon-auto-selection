# -*- coding: utf-8 -*-
"""把 18_标准化 下的 markdown 报告转成自带样式的单文件 HTML。

用法:
  python scripts/md_to_html.py --cat "Cutting Boards-US-202608"            # 目录下全部 md
  python scripts/md_to_html.py --cat "..." --only "双面异材质"            # 只转匹配的文件
"""
from __future__ import annotations

import argparse
import io
import os
import re

import markdown

STD = "18_标准化"

CSS = """
:root{--fg:#1f2328;--muted:#5b6570;--line:#e3e7eb;--head:#f6f8fa;--accent:#0b62d0;--bg:#fff;}
*{box-sizing:border-box}
body{margin:0;background:#f2f4f7;color:var(--fg);
 font:15px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB",
 "Microsoft YaHei","Noto Sans CJK SC",sans-serif;}
.wrap{display:flex;align-items:flex-start;gap:22px;max-width:1560px;margin:0 auto;padding:22px}
nav.toc{flex:0 0 288px;position:sticky;top:22px;max-height:calc(100vh - 44px);overflow:auto;
 background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:14px 16px;font-size:13.5px}
nav.toc h2{font-size:13px;letter-spacing:.06em;color:var(--muted);margin:0 0 8px;text-transform:uppercase}
nav.toc ul{list-style:none;margin:0;padding-left:0}
nav.toc ul ul{padding-left:14px}
nav.toc li{margin:3px 0}
nav.toc a{color:var(--fg);text-decoration:none;display:block;padding:2px 6px;border-radius:5px}
nav.toc a:hover{background:var(--head);color:var(--accent)}
main{flex:1 1 auto;min-width:0;background:var(--bg);border:1px solid var(--line);
 border-radius:10px;padding:30px 34px 44px}
h1{font-size:25px;line-height:1.35;margin:0 0 6px;padding-bottom:12px;border-bottom:2px solid var(--line)}
h2{font-size:20px;margin:34px 0 10px;padding-bottom:7px;border-bottom:1px solid var(--line)}
h3{font-size:16.5px;margin:24px 0 8px;color:#2b3138}
h4{font-size:15px;margin:18px 0 6px}
p{margin:9px 0}
ul,ol{margin:9px 0 9px 22px;padding:0}
li{margin:4px 0}
a{color:var(--accent)}
code{background:#f3f5f7;border:1px solid var(--line);border-radius:4px;padding:1px 5px;
 font:13px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace;white-space:nowrap}
strong{font-weight:650}
hr{border:0;border-top:1px solid var(--line);margin:26px 0}
blockquote{margin:12px 0;padding:8px 14px;border-left:3px solid var(--line);
 background:#fafbfc;color:var(--muted)}
.tw{overflow-x:auto;margin:12px 0;border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%;font-size:13.5px}
th,td{border-bottom:1px solid var(--line);padding:7px 10px;text-align:left;vertical-align:top;
 white-space:nowrap}
th{background:var(--head);font-weight:650;position:sticky;top:0;z-index:1}
tbody tr:nth-child(even){background:#fbfcfd}
tbody tr:hover{background:#eef4fd}
td:first-child,th:first-child{padding-left:14px}
td:last-child,th:last-child{padding-right:14px}
.nav{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 16px}
.nav a{font-size:12.5px;text-decoration:none;border:1px solid var(--line);border-radius:20px;
 padding:3px 12px;background:#fff;color:var(--muted)}
.nav a:hover{border-color:var(--accent);color:var(--accent)}
.nav a.on{background:var(--accent);border-color:var(--accent);color:#fff}
footer{max-width:1560px;margin:0 auto;padding:6px 22px 30px;color:var(--muted);font-size:12.5px}
@media print{body{background:#fff}nav.toc{display:none}.wrap{padding:0}main{border:0;padding:0}}
@media (max-width:1080px){.wrap{flex-direction:column;padding:12px}nav.toc{position:static;
 width:100%;flex:none;max-height:none}main{padding:18px}}
"""

TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{css}</style>
</head>
<body>
<div class="wrap">
<nav class="toc"><h2>目录</h2>{toc}</nav>
<main>
{nav}
{body}
</main>
</div>
<footer>由 scripts/md_to_html.py 生成 · 源文件 {src} · 单位 inch · 数据源 {cat}-属性标注-标准化交付.xlsx</footer>
</body>
</html>
"""


def normalize(text: str) -> str:
    """兜底: 标题/表格前若缺空行, Python-Markdown 会把它吞进上一个块(表格),
    这里统一补空行, 避免 ### 小标题在 HTML 里整段丢失。"""
    lines = text.split("\n")
    out = []
    in_fence = False
    for ln in lines:
        if ln.lstrip().startswith("```"):
            in_fence = not in_fence
        need = False
        if not in_fence and out and out[-1].strip():
            if re.match(r"^#{1,6}\s", ln):
                need = True
            elif ln.startswith("|") and not out[-1].startswith("|"):
                need = True
        if need:
            out.append("")
        out.append(ln)
    return "\n".join(out)


def convert_one(md_path: str, cat: str, siblings: list[str], only: str | None = None) -> str:
    text = normalize(io.open(md_path, encoding="utf-8").read())
    md = markdown.Markdown(extensions=["tables", "fenced_code", "sane_lists", "toc", "attr_list"],
                           extension_configs={"toc": {"toc_depth": "2-3", "permalink": False}})
    body = md.convert(text)
    body = body.replace("<table>", '<div class="tw"><table>').replace("</table>", "</table></div>")

    m = re.search(r"^#\s+(.+)$", text, re.M)
    title = m.group(1).strip() if m else os.path.basename(md_path)

    base = os.path.basename(md_path)[:-3]
    links = []
    for s in siblings:
        if only and only not in s:
            continue
        name = os.path.basename(s)[:-3]
        cls = ' class="on"' if s == md_path else ""
        links.append('<a href="%s.html"%s>%s</a>' % (name, cls, name.split("-")[-1]))
    nav = ('<div class="nav">' + "".join(links) + "</div>") if links else ""

    out = md_path[:-3] + ".html"
    io.open(out, "w", encoding="utf-8").write(TEMPLATE.format(
        title=title, css=CSS, toc=md.toc or "", nav=nav, body=body,
        src=os.path.basename(md_path), cat=cat))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cat", required=True)
    ap.add_argument("--only", default=None, help="只转文件名含该串的报告")
    args = ap.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    d = os.path.join(root, "data", args.cat, STD)
    mds = sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith(".md"))
    if args.only:
        mds = [p for p in mds if args.only in os.path.basename(p)]
    if not mds:
        print("没有匹配的 md 文件")
        return 1
    for p in mds:
        out = convert_one(p, args.cat, sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith(".md")),
                          only=args.only)
        print("[写] %s (%d KB)" % (out, os.path.getsize(out) // 1024))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
