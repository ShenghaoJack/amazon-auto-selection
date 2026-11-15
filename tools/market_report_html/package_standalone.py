"""Bundle the current integrated market report into one offline HTML file."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = ROOT / "Cat-Fountains-美国站市场调研报告-离线版.html"


def script_safe(text: str) -> str:
    return re.sub(r"</script", lambda _: r"<\/script", text, flags=re.IGNORECASE)


def style_safe(text: str) -> str:
    return re.sub(r"</style", lambda _: r"<\/style", text, flags=re.IGNORECASE)


def replace_one(source: str, pattern: str, replacement) -> str:
    result, count = re.subn(pattern, replacement, source, count=1)
    if count != 1:
        raise ValueError(f"Expected one match for {pattern!r}; found {count}")
    return result


def build(output: Path, data_file: Path | None = None) -> None:
    page = (ROOT / "index.html").read_text(encoding="utf-8")
    data_file = data_file or ROOT / "data" / "report-data.js"

    for name in ("styles.css", "charts.css"):
        css = style_safe((ROOT / name).read_text(encoding="utf-8"))
        pattern = rf'<link rel="stylesheet" href="{re.escape(name)}(?:\?v=\d+)?">'
        page = replace_one(page, pattern, lambda _, css=css: f"<style>\n{css}\n</style>")

    page = page.replace(
        "</head>",
        "<style>#importReport{display:none!important}</style>\n</head>",
        1,
    )
    page = page.replace('<span class="hero-tag">交互版</span>', '<span class="hero-tag">离线交互版</span>', 1)
    page = page.replace("数据直接来自所导入工作簿", "数据来自内置工作簿", 1)

    for name in ("data/report-data.js", "app.js"):
        js = script_safe((data_file if name == "data/report-data.js" else ROOT / name).read_text(encoding="utf-8"))
        pattern = rf'<script src="{re.escape(name)}(?:\?v=\d+)?"></script>'
        page = replace_one(page, pattern, lambda _, js=js: f"<script>\n{js}\n</script>")

    if re.search(r'<(?:script|link)[^>]+(?:src|href)=', page, re.IGNORECASE):
        raise ValueError("External script or stylesheet reference remains in the bundle")

    output.write_text(page, encoding="utf-8")
    print(f"Created {output} ({output.stat().st_size:,} bytes)")


if __name__ == "__main__":
    build(Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_OUTPUT,
          Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else None)
