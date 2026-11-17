"""类目名 → LLM 提示词展示名(2026-09 命名约定)。

约定: 类目名用于文件/目录隔离(保持原样); 喂给 LLM 的"类目"上下文用清洗后的展示名:
- 词与词之间不再用 `-` 连接(旧命名如 salt-pepper-shaker-sets-v2 兼容);
- 版本/日期/地区只作为**末尾后缀**: `-v2` / `-v3` / `-202609` / `-US` 等;
- 这里把末尾这类后缀去掉, 中间的 `-` 转空格, 避免版本号等噪音影响提示词质量;
- 普通 2-3 字母词(如 -cat/-set)不属于后缀, 不会误删。
"""
from __future__ import annotations

import re

# 2-3 位地区/站点码(可作后缀)
# 2026-09-14: 补 "gb"(亚马逊英国的官方国家码, 用户很容易这么写; 与中国站习惯的 uk 等价)
# —— 缺它时 `Cutting Boards-GB-202609` 的 "GB" 会被当成类目名的一部分带进 LLM 提示词,
#    前端的类目中心分组也认不出站点(见 webui/index.html::REGION_TOKENS, 两处需同步)。
_REGIONS = {
    "us", "uk", "gb", "ca", "au", "eu", "cn", "jp", "de", "fr", "it", "es",
    "nl", "pl", "se", "no", "dk", "fi", "mx", "br", "in", "sg", "ae",
    "my", "th", "vn", "id", "ph", "hk", "tw", "kr", "usa",
}
_V = re.compile(r"^v\d{1,4}$", re.I)
_DIGITS = re.compile(r"^\d{2,8}$")
_2TO3 = re.compile(r"^[a-z]{2,3}$", re.I)


def _is_suffix(part: str) -> bool:
    """判断 '-' 后的末段是否为 版本/日期/地区 后缀。"""
    if not part:
        return False
    if _V.match(part) or _DIGITS.match(part):
        return True
    return bool(_2TO3.match(part) and part.lower() in _REGIONS)


def display_cat(cat: str) -> str:
    """去掉末尾版本/日期/地区后缀并把 '-' 转空格, 得到 LLM 友好的展示名。"""
    raw = re.sub(r"-RECENT30$", "", (cat or "").strip(), flags=re.I)
    if not raw:
        return raw
    parts = raw.split("-")
    guard = 0
    while len(parts) > 1 and guard < 6 and _is_suffix(parts[-1]):
        parts.pop()
        guard += 1
    base = "-".join(parts)
    display = base.replace("-", " ").strip()
    return display or raw
