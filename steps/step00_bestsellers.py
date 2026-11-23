"""FR-00 榜单收集(浏览器自动化)。

输入: 配置 LIST_URLS(分号分隔的榜单 URL, 每行一个) + 亚马逊登录态(持久 Profile) + 卖家精灵登录态
输出: <CAT>-bestseller-newrelease.json  {"bs": [...], "nr": [...], "union": [...], "processed_urls": [...]}
      <CAT>-bestseller-newrelease-all.txt (并集去重, 每行一个 ASIN)

逻辑: 前置登录检查(亚马逊 Cookie 有效性 + 卖家精灵自动登录, 任一失败明确报错阻断)
      -> 打开榜单页 -> 滚动触发懒加载 -> 正则提取 ASIN
      -> 自动推导对应 New Releases 榜单 -> 双榜合并去重 -> 断点续爬(跳过已处理 URL)
容错: 页面失败重试; 截图存档; 登录检查失败(任一站点)明确报错并阻断流程
"""
from __future__ import annotations

import os
import re
import time
from html import unescape
from urllib.parse import urljoin

from core.errors import StepFailedError
from core.io_utils import read_json, write_json, write_txt_lines
from steps.base import BaseStep, StepContext

_ASIN_PATTERN = re.compile(r"/dp/([A-Z0-9]{10})")
# 榜单卡片标记, **按可靠性排序**(2026-09-15 用真实页面校准):
#   ① zg-bdg-text      —— 卡片上的**名次编号**容器(每张榜单卡恰好一个, 最可靠; 实测 50 个 = 榜单 50 个商品)
#   ② gridItemRoot     —— 榜单卡的根 id(每卡一个)
#   ③ zg-grid-general-faceout —— 榜单卡外框(每卡一个)
#   ⚠️ `p13n-sc-uncoverable-faceout` **不能用**: 该 class 在本页面主要出现在**轮播/推荐模块**
#      (实测 62 次 vs 榜单卡 51 次), 拿它当主列表标记会把轮播切进来 —— 这正是上一版
#      "主列表卡=77 但提取 ASIN=38"自相矛盾的根因。
_CARD_MARKERS = ("zg-bdg-text", "gridItemRoot", "zg-grid-general-faceout")
# 名次徽章里的**数字**(如 `<span class="zg-bdg-text">#12</span>`): 榜单商品的唯一权威标记
_RANK_BADGE_RE = re.compile(r"zg-bdg-text[^>]*>\s*#?\s*(\d+)")
# 榜单页懒加载稳定判定: 反复"滚动到底 + 等一拍"直到名次徽章数不再增长(最多 4 轮)
_LIST_SETTLE_TRIES = 4
# 抓榜单时的浏览器视口 —— **必须够高**(2026-09-15 实测定位到"只抓到 30/38 个"的真因之一):
#   视口 1280x720  → 第 1 页只渲染 38 个名次(#1~#38, 无缺号, 滚动到底也不会再增加)
#   视口 1280x2600 → 第 1 页渲染全 50 个(#1~#50)
#   来回切换可 100% 复现(同一次会话里 720→38 / 2600→50 / 再回 720→38), 所以**不是**网络或
#   风控问题, 而是榜单卡的懒加载按"视口高度 + 预取"决定渲染多少张。抓榜单先把视口放大。
_LIST_VIEWPORT = (1280, 2600)
# 名次缺号自愈: 本页名次未到页边界(第 1 页 #50 / 后续页满 50 个) → 放大视口重载一次
_LIST_VIEWPORT_RETRY = (1280, 3600)
# Amazon 榜单每页商品数(第 2 页从 #51 起, 实测 #51~#88) —— 用于判断本页是否渲染完整
_LIST_PAGE_SIZE = 50
# 分页控件: "下一页"链接(Amazon 的 a-last / 文本 Next page); 用于逐页收集
_NEXT_PAGE_RES = (
    re.compile(r'<li[^>]*class="[^"]*a-last[^"]*"[^>]*>\s*<a[^>]*href="([^"]+)"', re.I),
    re.compile(r'<a[^>]*href="([^"]+)"[^>]*>[^<]{0,40}(?:Next page|下一页)', re.I),
)
# 下一页控件的 CSS 选择器(优先点击; 实测真实标记为 `li.a-last > a`)
_NEXT_PAGE_SELECTORS = (
    "li.a-last a",
    "nav[aria-label='pagination'] li.a-last a",
    "a:has-text('Next page')",
    "ul.a-pagination li.a-last a",
)
# 主列表区段标题(用于在多处命中卡片标记时截取"榜单主体"那一段)
_LIST_SECTION_RE = re.compile(
    r"(Best Sellers|New Releases|畅销商品|新品榜|zg-ordered-list|zg-grid)", re.I)


class ListVerificationRequired(StepFailedError):
    """A challenge page must never be treated as a list or an empty NR page."""


def _is_verification_page(html: str) -> bool:
    text = re.sub(r"<[^>]+>", " ", unescape(html or ""))
    text = re.sub(r"\s+", " ", text).lower()
    return any(marker in text for marker in (
        "check the box to continue", "i am human", "enter the characters you see below",
        "type the characters you see in this image", "automated access to amazon data",
        "verify you are human", "robot check", "请输入图片中的字符",
    ))


def _next_page_url(html: str, base_url: str = "") -> str:
    """从榜单页 HTML 里取**下一页**链接(无分页返回 "")。

    实测(2026-09-15 用真实 HTML 校准, 修正上一版结论):
    UK Chopping Boards 目录节点下 **第 1 页就是名次 #1~#50(50 个商品)**, 第 2 页是
    #51~#88(38 个) —— 即榜单共 88 个商品, 跨 2 页。上一版"一页只给 30 个、剩下 20 个在
    pg=2 上"的说法是**错的**: 那 30 个是"卡片簇截断"造成的漏抓(见 `_ranked_card_spans`),
    不是页面分页行为。翻页因此只用于第 51 名之后, 不影响"前 50 名"。

    坑(2026-09-15 实测踩到):
      ① href 可能是**相对路径**(`/Best-Sellers…?pg=2`), 直接扔给 `page.goto` 报
         `Cannot navigate to invalid URL` → 用 `base_url` 补绝对地址;
      ② 点击该控件在真机上**没能跳页**(实测第 2 页与第 1 页的 scrollY/页高/ASIN 完全相同),
         而 href 里带 `ref=zg_bs_pg_2_kitchen&_encoding=UTF8` 这种冗长参数也容易出岔子
         → `_canonical_page_url()` 把它规范成**干净的 `?pg=N`**:
         Amazon 的榜单分页只有 `pg` 参数是必要的, 这样最稳, 也便于按页码自增翻页。
    """
    raw = ""
    for rx in _NEXT_PAGE_RES:
        m = rx.search(html or "")
        if m:
            raw = m.group(1)
            break
    if not raw:
        return ""
    href = unescape(raw).strip()
    # 去掉 URL 里不允许出现的空白/换行(HTML 属性跨行时会出现)
    href = re.sub(r"[\s\u200b\u200e\u200f]+", "", href)
    if not href or href.lower().startswith(("javascript:", "#", "void(")):
        return ""
    if base_url:
        href = urljoin(base_url, href)
    if not re.match(r"^https?://[^\s/]+/.+", href, re.I):
        return ""
    return _canonical_page_url(href)


def _canonical_page_url(url: str) -> str:
    """把榜单分页链接规范成 `<scheme://host/path>?pg=<N>`(保留其它必要 query 除外)。

    为什么要规范(2026-09-15 实测): 真机链接形如
    `https://www.amazon.co.uk/Best-Sellers-…/zgbs/kitchen/3187097031/ref=zg_bs_pg_2_kitchen?_encoding=UTF8&pg=2`,
    直接 goto 过一次就报过 invalid URL, 点击控件也不跳。榜单分页真正起作用的只有 `pg`,
    因此只保留 `pg`(以及确实存在的其它参数), 得到最小可用地址。
    """
    m = re.search(r"[?&]pg=(\d+)", url or "")
    if not m:
        return url
    base = re.sub(r"[?#].*$", "", url)          # 去掉原 query/fragment
    return f"{base}?pg={m.group(1)}"


def _set_list_viewport(page, size: tuple[int, int] = _LIST_VIEWPORT) -> bool:
    """把榜单页视口放大(让整张列表进入懒加载的渲染范围), 成功返回 True。

    拿不到视口(如 CDP 直连复用用户窗口)时静默返回 False —— 调用方按"没做成"处理,
    不因为视口设置失败就放弃抓取。
    """
    try:
        page.set_viewport_size({"width": int(size[0]), "height": int(size[1])})
        return True
    except Exception:  # noqa: BLE001
        return False


def _page_incomplete(ranks: list[int], page_no: int, has_next: bool) -> bool:
    """本页是否**渲染不全**(该页的名次没到应有的页边界)。

    实测依据(2026-09-15): Amazon 榜单每页 50 个, 名次连续编号。
      · 第 1 页: 页面带"下一页"说明榜超过 50 个 → 名次必须到 #50, 只到 #38 就是渲染不全;
      · 第 2 页及以后: 上一页是满页(50 个)时, 本页也应满 50 个 —— 只有"尾页"才可能不到 50。
        实测同一类别: 视口矮时第 2 页只渲染 #51~#88(38 个, 假尾页), 放大视口后是 #51~#100。
    名次为空(没有徽章的页面)时一律返回 False —— 无从判断, 交给卡片簇逻辑并告警。
    """
    if not ranks:
        return False
    if max(ranks) >= _LIST_PAGE_SIZE * page_no:      # 已到该页应有的终点
        return False
    if page_no <= 1:
        return bool(has_next)                        # 第 1 页: 有下一页就必须到 #50
    return len(set(ranks)) < _LIST_PAGE_SIZE         # 后续页: 不到 50 个则可能是"假尾页"


def _page_content(page, attempts: int = 8) -> str:
    """Wait briefly for an in-flight navigation before re-reading the same page."""
    for attempt in range(max(1, attempts)):
        try:
            return page.content()
        except Exception as exc:
            msg = str(exc).lower()
            transient = "unable to retrieve content" in msg and "navigat" in msg
            if not transient or attempt + 1 >= attempts:
                raise
            try:
                page.wait_for_load_state("domcontentloaded", timeout=2000)
            except Exception:
                pass
            time.sleep(0.25)
    raise RuntimeError("页面内容读取失败")


def _settle_list_page(browser, page, tries: int = _LIST_SETTLE_TRIES) -> tuple[int, bool, str, str]:
    """滚动到底并**等懒加载补齐**, 返回 (滚动次数, 是否到底, 底部状态, html)。

    为什么要多轮(2026-09-15 实测): 同一榜单页有时只渲染出 38 个名次徽章(其余靠懒加载补),
    滚一次就读会少 12 个商品 —— 反复"滚动到底 + 等 1.5 秒"直到**徽章数不再增长**才算加载完,
    这也是"有时 38 个、有时 30 个"的成因之一(另一个是卡片簇截断, 见 `_ranked_card_spans`)。
    """
    scrolled = 0
    at_bottom, view, html, prev = True, "", "", -1
    for i in range(max(1, tries)):
        scrolled += browser.scroll_to_bottom(page, times=5, delay=1.0)
        at_bottom, view = _page_bottom_state(page)
        html = _page_content(page)
        cur = len(_rank_badges(html))
        if cur and cur == prev:      # 名次数量稳定 -> 加载完
            break
        prev = cur
        if i < tries - 1:
            time.sleep(1.5)
    return scrolled, at_bottom, view, html


def _page_bottom_state(page) -> tuple[bool, str]:
    """判断页面是否已滚到底, 返回 (是否到底, 供日志的可读状态)。

    到底的判据: `scrollY + 视口高 >= document.documentElement.scrollHeight - 4`(容 4px 误差)。
    拿不到尺寸时返回 (True, 说明文本), 避免因取不到而误报"没到底"造成噪音。
    """
    try:
        js = ("() => { const e = document.documentElement; "
              "return [window.scrollY, window.innerHeight, e ? e.scrollHeight : 0]; }")
        pos, view, height = (page.evaluate(js) or [0, 0, 0])[:3]
        pos, view, height = float(pos or 0), float(view or 0), float(height or 0)
        if height <= 0:
            return True, "(页面高度取不到, 按已到底处理)"
        bottom = height - (pos + view)
        return bottom <= 4, f"(scrollY={int(pos)} 视口={int(view)} 页高={int(height)} 距底={int(bottom)}px)"
    except Exception as exc:  # noqa: BLE001
        return True, f"(底部状态取不到: {type(exc).__name__})"


# 主列表区段之后紧跟的"非主列表"结构标记(用于给截取范围定上界)
_AFTER_LIST_RE = re.compile(
    r"<script|a-carousel|p13n-sc-shoveler|zg-pagination|id=\"zg-center-div\"|</ol>|</ul>",
    re.I)
_MAX_TAIL_CHARS = 4000
_MAX_LIST_CHARS = 2_000_000
# 同一"卡片簇"内相邻卡片标记的最大间隔: 超过它认为是另一段(推荐/轮播等)
_CLUSTER_GAP_CHARS = 60_000
# 单个榜单最多翻多少页(防"下一页"链接异常导致无限翻页)
_MAX_LIST_PAGES = 8


def _card_marker_positions(html: str) -> list[int]:
    """所有榜单卡片标记的字符位置(**优先用最可靠的那种标记**, 去重排序)。

    逐个标记按优先级尝试: 只要某一级别有命中就只用它 —— 混用不同级别会重复计数
    (同一张卡同时含名次容器与卡根 id, 相加会把卡片数翻倍)。
    """
    for marker in _CARD_MARKERS:
        pos: set[int] = set()
        start = 0
        while True:
            i = html.find(marker, start)
            if i < 0:
                break
            pos.add(i)
            start = i + 1
        if pos:
            return sorted(pos)
    return []


def _card_clusters(html: str, positions: list[int]) -> list[tuple[int, int, int]]:
    """把卡片标记按位置间隔切成若干"簇", 返回 [(起, 止, 卡片数), ...] 按卡片数降序。

    为什么要分簇(2026-09-15 第二次修复): 榜单页的**推荐/轮播模块复用同一套卡片 class**,
    单纯"从第一张卡到最后一张卡"会把它们一起框进主列表 —— 实测 UK 榜: 主列表只有 38 个
    商品, 但范围内卡片标记数高达 77(多出来的正是"相关类目/看了又看"等轮播), 于是
    "主列表卡=77"与"提取ASIN=38"看起来自相矛盾。主列表是**连续一大段**, 轮播之间隔着
    大段其它 HTML, 因此按"相邻标记间隔 > _CLUSTER_GAP_CHARS 就断开"分簇, 取最大簇 = 主列表。
    """
    if not positions:
        return []
    groups: list[list[int]] = [[positions[0]]]
    for p in positions[1:]:
        if p - groups[-1][-1] <= _CLUSTER_GAP_CHARS:
            groups[-1].append(p)
        else:
            groups.append([p])
    out = []
    for c in groups:
        # 簇上界: 最后一张卡之后最近的"非主列表"结构边界(否则会切掉最后一张卡的 /dp/ 链接)
        last = c[-1]
        end = min(len(html), last + _MAX_TAIL_CHARS)
        m = _AFTER_LIST_RE.search(html, last)
        if m and m.start() > last:
            end = m.start()
        out.append((c[0], end, len(c)))
    out.sort(key=lambda x: (-x[2], x[0]))
    return out


# 主列表片段的判定: 片段内卡片数 ≥ 该值才算"像主列表"的候选
_MIN_LIST_CARDS = 10


def _rank_badges(html: str) -> list[int]:
    """页面上的名次编号列表(按出现顺序, 未去重)。"""
    return [int(x) for x in _RANK_BADGE_RE.findall(html or "")]


def _ranked_card_spans(html: str) -> list[tuple[int, int, int]]:
    """**带名次徽章**的榜单卡范围 [(起, 止, 名次), ...], 按位置升序。

    为什么按名次徽章取范围, 而不是"卡片簇"(2026-09-15 第三次修复, 也是"只抓到 30/32/38 个"
    的**真因**):

      实测 UK Chopping Board Sets 第 1 页真实 HTML: 名次徽章 #1~#50 **共 50 个**, 但按
      "相邻卡片标记间隔 > 60k 字符就断簇"分出来是 **两簇**: #1~#30(31 张) + #31~#50(20 张),
      两簇之间隔着 86024 字符(懒加载分块/占位), 于是 `_pick_main_cluster` 只取到第一簇 ——
      **50 个商品只解析出 30 个**。所以"翻页"不是主因: 前 50 名本来就在第 1 页上。

      推荐/轮播卡**没有名次徽章**, 所以"带名次的卡"恰好就是榜单商品本身: 既不漏第 31~50 名,
      也不会混进轮播 —— 比"卡片簇 + 数量阈值"更准。无徽章的页面(部分新品榜)退回簇逻辑。
    """
    html = html or ""
    positions = _card_marker_positions(html)
    if not positions:
        return []
    spans: list[tuple[int, int, int]] = []
    seen: set[int] = set()
    for m in _RANK_BADGE_RE.finditer(html):
        before = [p for p in positions if p <= m.start()]
        if not before:
            continue
        start = before[-1]
        if start in seen:            # 同一张卡上的重复徽章只算一次
            continue
        seen.add(start)
        after = [p for p in positions if p > start]
        if after:
            end = after[0]                      # 卡片上界 = 下一张卡的开头
        else:
            # 最后一张卡: 用固定尾巴长度兜住它的 /dp/ 链接, 但不得越过轮播/分页等结构边界
            end = min(len(html), start + _MAX_TAIL_CHARS)
            boundary = _AFTER_LIST_RE.search(html, start)
            if boundary and boundary.start() > start:
                end = min(end, boundary.start())
        spans.append((start, end, int(m.group(1))))
    spans.sort(key=lambda x: x[0])
    return spans


def _rank_range(html: str) -> str:
    """名次范围的可读文本(如 "#1~#50"), 没有徽章返回 "-"。"""
    ranks = _rank_badges(html)
    if not ranks:
        return "-"
    return f"#{min(ranks)}~#{max(ranks)}(共{len(set(ranks))}个名次)"


def _pick_main_cluster(clusters: list[tuple[int, int, int]]) -> tuple[int, int, int]:
    """从多个卡片簇里挑出**主列表**那一簇。

    规则: 在卡片数 ≥ `_MIN_LIST_CARDS` 的候选里取**位置最靠前**的那一簇; 没有候选时退回
    "卡片数最多"的簇。**不能只按"卡片数最多"挑** —— 实测 UK 榜主列表 38 张, 而后面的
    "相关类目"轮播反而有 39 张(轮播会复用同一套卡片 class), 按数量挑就会挑到轮播去;
    而 Best Sellers 主列表永远排在页面靠前位置, 所以"第一个成规模的簇"才是它。
    """
    if not clusters:
        return 0, 0, 0
    big = [c for c in clusters if c[2] >= _MIN_LIST_CARDS]
    if big:
        return min(big, key=lambda c: c[0])
    return max(clusters, key=lambda c: c[2])


def _main_list_scope(html: str) -> tuple[str, int, bool, int]:
    """从榜单页 HTML 中截出**主列表**区段。

    返回 (scope_html, 主列表卡片数, 是否严格模式, 页面卡片标记总数)。
    严格模式 = 识别到卡片容器; 此时 `scope` 只覆盖**主列表那一簇卡片**,
    推荐/轮播簇被排除。认不出容器 → 回退全文, strict=False, 调用方需自行告警。

    取值优先级(2026-09-15 第三次修复后):
      ① **带名次徽章的卡**(`_ranked_card_spans`): 最准 —— 前 50 名全在第 1 页, 但用"卡片簇"
         只能框到其中 30 个(实测两簇 31+20 张, 中间隔 86024 字符), 按徽章取就不会漏;
      ② 无名次徽章(部分新品榜) → 退回"卡片簇", 取第一个成规模的簇;
      ③ 连卡片标记都没有 → 回退全文并置 strict=False。

    上界的取法(两个坑都踩过, 由单测钉死):
      ① 不能"从第一张卡往后截 N 字符" —— 会把紧随其后的推荐/轮播一起框进来;
      ② 也不能只截到最后一个卡片标记处 —— 标记在卡的开头, 那样会切掉最后一张卡的
         `/dp/` 链接(50 张卡只解析出 49 个 ASIN)。
    """
    html = html or ""
    positions = _card_marker_positions(html)
    if not positions:
        return html, 0, False, 0
    spans = _ranked_card_spans(html)
    if spans:
        # 按卡拼接(而非取首尾之间的连续片段): 两簇之间夹着的懒加载/推荐区段不会被框进来
        scope = "".join(html[s:e] for s, e, _ in spans)
        return scope, len(spans), True, len(positions)
    start, end, cards = _pick_main_cluster(_card_clusters(html, positions))
    # 区段标题(如 "Best Sellers in Kitchen")通常紧挨第一张卡之前: 用于把起点上移到标题
    for m in _LIST_SECTION_RE.finditer(html[:start]):
        start = m.start()
    if end - start > _MAX_LIST_CHARS:
        end = start + _MAX_LIST_CHARS
    return html[start:end], cards, True, len(positions)


def normalize_list_url(url: str) -> str:
    """把浏览器地址栏复制来的榜单 URL 归一为规范短链接(2026-09-15)。

    只做**无损**归一: 去掉紧跟主机名的**语言/地区前缀**(`/-/en/`、`/en/`、`/en-gb/`、`/fr/` …)。
    非美国站(实测 amazon.fr)会把语言偏好写进路径, 例如
      https://www.amazon.fr/-/en/gp/bestsellers/kitchen/3155139031
    它只是语言偏好、榜单内容完全相同, 但原校验要求严格匹配 `/gp/bestsellers/...` → 直接阻断运行
    (用户实测报障: "LIST_URLS 有 1 条不符合规范格式")。

    安全性: 仅当该前缀后面紧跟 `gp/`、`Best-Sellers-` 或 `zgbs/` 时才剥离 —— 正常类目路径不会长这样,
    所以不会误伤(如 `/gp/bestsellers/...` 与旧版长链接 `/Best-Sellers-.../zgbs/...` 都原样保留)。
    """
    u = (url or "").strip()
    if not u:
        return u
    return re.sub(
        r"^(https?://[^/?#]+)/(?:-\/)?(?:[a-z]{2}(?:[-_][a-z]{2})?)/(?=gp/|Best-Sellers-|zgbs/)",
        r"\1/", u, count=1, flags=re.I)


class Step00Bestsellers(BaseStep):
    number = 0
    title = "榜单收集"
    description = "FR-00: 浏览器自动化抓取双榜 ASIN 并合并去重"

    def validate_upstream(self, ctx: StepContext) -> None:
        self._cat = ctx.config.cat
        if not self._cat:
            raise ValueError("配置缺少 CAT")
        # 启动前预检: LIST_URLS 必须为规范 Best Sellers 短链接(2026-09)。
        # 不符合要求 -> 阻断运行, 引导用户改格式(不允许带病启动)。
        bad = [(url, why) for url in ctx.config.list_urls
               if (why := self._validate_bs_url(url)) is not None]
        if bad:
            for url, why in bad:
                self.log(ctx, f"[LIST_URLS 校验未通过] {url} -> {why}")
            self.log(ctx, "要求: 在浏览器打开目标类目 Best Sellers 页, 从地址栏复制规范短链接, 形如 "
                          "https://www.amazon.com/gp/bestsellers/baby-products/379024011")
            self.log(ctx, "注意: 只填 Best Sellers 一条即可, New Releases 榜由系统自动推导; "
                          "不要粘贴 /Best-Sellers-.../zgbs/... 旧版长链接, 也不要手工把 bestsellers 改成 new-releases 后填入")
            raise StepFailedError(
                f"LIST_URLS 有 {len(bad)} 条不符合规范格式(详见上方日志), 流程未启动, "
                "请修改 config.yaml 的 LIST_URLS 后重新运行"
            )

    @property
    def artifacts(self) -> list[str]:
        return [
            f"{self._cat}-bestseller-newrelease.json",
            f"{self._cat}-bestseller-newrelease-all.txt",
        ]

    def run(self, ctx: StepContext) -> None:
        urls = ctx.config.list_urls
        if not urls:
            raise StepFailedError("配置缺少 LIST_URLS(榜单 URL 列表)")
        # 归一(2026-09-15): 语言前缀(/-/en/ 等)只影响语言偏好, 抓取/派生 NR/去重都用归一后的链接
        clean = [normalize_list_url(u) for u in urls]
        for raw, got in zip(urls, clean):
            if raw.strip() != got:
                self.log(ctx, f"[{self._cat}] LIST_URLS 已归一(去掉语言前缀): {raw.strip()} -> {got}")
        urls = clean
        self.log(ctx, f"开始榜单收集: CAT={self._cat} 输入URL数={len(urls)}")

        from core.amz_crawl_lock import (
            AmzCrawlSlot,
            DEFAULT_WAIT_SECONDS as _AMZ_WAIT_DEFAULT,
            PRIORITY_NORMAL,
            PRIORITY_SHORT,
            resolve_max_concurrent,
            resolve_short_batch,
        )
        short_thresh = resolve_short_batch(ctx.config)
        est_batch = max(1, len(urls) * 2)
        crawl_slot = AmzCrawlSlot(
            max_concurrent=resolve_max_concurrent(ctx.config),
            wait_seconds=float(ctx.config.get("AMZ_CRAWL_LOCK_WAIT", _AMZ_WAIT_DEFAULT) or _AMZ_WAIT_DEFAULT),
            label=f"{self._cat or 'pipeline'}·步骤0榜单",
            log=lambda m: self.log(ctx, m),
            warn=lambda m: ctx.log.warning(f"[Step00Bestsellers] {m}"),
            enabled=not ctx.dry_run,
            priority=PRIORITY_SHORT if (short_thresh > 0 and est_batch <= short_thresh) else PRIORITY_NORMAL,
            remaining=est_batch,
        )
        with crawl_slot:
            self.ensure_amazon_login(ctx)
            self.ensure_sellersprite_login(ctx)

            json_path = ctx.resolve_artifact(self.artifacts[0])
            txt_path = ctx.resolve_artifact(self.artifacts[1])
            bs_set, nr_set, processed = self._load_previous(json_path)

            tasks = self._build_tasks(urls)
            todo = [t for t in tasks if t[0] not in processed]
            self.log(ctx, f"榜单任务: 共{len(tasks)}个 已处理{len(tasks) - len(todo)}个 待抓{len(todo)}个")

            if todo:
                remaining = list(todo)
                while remaining:
                    blocked = None
                    with ctx.browser("amazon") as browser:
                        while remaining:
                            url, kind = remaining[0]
                            try:
                                asins = self._scrape_list(ctx, browser, url, kind=kind)
                            except ListVerificationRequired:
                                blocked = (url, kind)
                                break
                            self._record_list_result(ctx, json_path, txt_path, bs_set, nr_set, processed, url, kind, asins)
                            remaining.pop(0)
                    if blocked:
                        url, kind = blocked
                        reuse_ss = bool(str(ctx.config.get("SS_CDP_URL", "") or "").strip())
                        if reuse_ss:
                            self.log(ctx, f"[{kind}] 遇到亚马逊人机验证，正在卖家精灵浏览器新建标签，请手动完成验证：{url}")
                        else:
                            self.log(ctx, f"[{kind}] 遇到亚马逊人机验证，正在打开有头浏览器，请手动完成验证：{url}")
                        with ctx.browser("sellersprite" if reuse_ss else "amazon", headed=True) as browser:
                            asins = self._scrape_list(ctx, browser, url, kind=kind, manual_challenge=True)
                        self._record_list_result(ctx, json_path, txt_path, bs_set, nr_set, processed, url, kind, asins)
                        remaining.pop(0)
            else:
                self.log(ctx, "所有榜单 URL 均已处理, 跳过抓取")

        union = sorted(bs_set | nr_set)
        if not union:
            self.log(ctx, "警告: 双榜并集为空, 请检查榜单 URL 与登录态")
        self._save(json_path, txt_path, bs_set, nr_set, processed)
        overlap = len(bs_set & nr_set)
        self.log(
            ctx,
            f"榜单收集完成: BS={len(bs_set)} NR={len(nr_set)} 并集={len(union)} "
            f"两榜重叠={overlap}(重叠应明显小于并集, 若 NR==BS 说明 NR URL 回落, 见日志警告)",
        )

    def _record_list_result(self, ctx, json_path, txt_path, bs_set, nr_set, processed, url, kind, asins):
        if asins is None:
            self.log(ctx, f"[{kind}] 页面校验未通过(NR 派生 URL 未打开真实新品榜), 已标记跳过: {url}")
        elif kind == "nr":
            nr_set.update(asins)
            self.log(ctx, f"榜单抓取完成[nr]: {url} -> {len(asins)}个ASIN")
        else:
            bs_set.update(asins)
            self.log(ctx, f"榜单抓取完成[bs]: {url} -> {len(asins)}个ASIN")
        processed.add(url)
        self._save(json_path, txt_path, bs_set, nr_set, processed)

    def _load_previous(self, json_path: str) -> tuple[set, set, set]:
        prev = read_json(json_path, default=None)
        if isinstance(prev, dict):
            bs_set = set(prev.get("bs", []) or [])
            nr_set = set(prev.get("nr", []) or [])
            processed = set(prev.get("processed_urls", []) or [])
            return bs_set, nr_set, processed
        return set(), set(), set()

    def _build_tasks(self, urls: list[str]) -> list[tuple[str, str]]:
        tasks = []
        seen = set()
        for url in urls:
            url = normalize_list_url(url)      # 语言前缀(/-/en/ 等)归一后再派生/去重(2026-09-15)
            for u, kind in self._derive_list_urls(url):
                # 按 (kind, host, dept, node) 去重: 同一节点的 slugged BS 与规范 BS 算同榜,
                # 避免同节点重复抓取(URL 字符串可能不同, 如 slugged vs /gp/ 形态)
                host, dept, node = self._parse_list_url(u)
                key = (kind, host, dept, node)
                if key not in seen:
                    seen.add(key)
                    tasks.append((u, kind))
        return tasks

    @staticmethod
    def _parse_list_url(url: str) -> tuple[str, str, str]:
        """解析榜单 URL -> (host, dept, node); 无法解析节点结构时返回 (host, "", "")。

        兼容两种亚马逊 BS 形态与规范 NR 形态:
          slugged:  /Best-Sellers-<slug>/zgbs/<dept>/<node>/ref=...
          简短:     /gp/bestsellers/<dept>/<node>/...
          规范NR:   /gp/new-releases/<dept>/<node>/...
        """
        mh = re.match(r"https?://([^/?#]+)", url)
        host = mh.group(1) if mh else "www.amazon.com"
        m = re.search(r"/(?:zgbs|gp/(?:bestsellers|new-releases))/([^/?#]+?)/(\d+)", url)
        if m:
            return host, m.group(1), m.group(2)
        m = re.search(r"/(?:zgbs|gp/(?:bestsellers|new-releases))/([^/?#]+?)(?:/|$)", url)
        if m:
            return host, m.group(1), ""
        return host, "", ""

    @staticmethod
    def _derive_list_urls(url: str) -> list[tuple[str, str]]:
        """规范 BS URL -> [(原 BS, bs), (替换得到的 NR, nr)]。

        /gp/bestsellers/<dept>/<node> 与 /gp/new-releases/<dept>/<node> 是亚马逊
        同构的规范路径, NR 榜 = 把路径段 bestsellers 整体替换成 new-releases
        (bestsellers/new-releases 词长不同, 必须整段替换不能逐字平移)。
        输入须先通过 _validate_bs_url(validate_upstream 启动前预检)。
        """
        return [(url, "bs"), (re.sub(r"/gp/bestsellers/", "/gp/new-releases/", url, count=1), "nr")]

    @staticmethod
    def _validate_bs_url(url: str) -> str | None:
        """校验 Best Sellers 榜单 URL 是否为规范短链接, 返回不通过原因或 None。

        规范形态: https://<host>/gp/bestsellers/<类目>/<节点ID>(可带尾随 /ref=...)
        例: https://www.amazon.com/gp/bestsellers/baby-products/379024011

        2026-09-15: 先做 `normalize_list_url()` 归一 —— 非美国站(实测 amazon.fr)从浏览器地址栏
        复制出来常带**语言前缀**, 如 `https://www.amazon.fr/-/en/gp/bestsellers/kitchen/3155139031`,
        它只是语言偏好、不影响榜单内容, 归一后就是规范链接, 不该因此阻断运行(用户实测报障:
        \"LIST_URLS 有 1 条不符合规范格式\" 正是被这个前缀卡住)。
        """
        u = normalize_list_url(url)
        if not u:
            return "URL 为空"
        if not (u.startswith("http://") or u.startswith("https://")):
            return "必须以 https:// 开头"
        m = re.match(r"^https?://([^/?#]+)", u)
        host = (m.group(1) if m else "").lower()
        if "amazon" not in host:
            return f"主机不是亚马逊站点(host={host or '未识别'})"
        if re.search(r"/zgbs/", u) or re.search(r"/Best-Sellers-", u):
            return "旧版长链接(/Best-Sellers-.../zgbs/...); 请在浏览器打开该 Best Sellers 页, 复制地址栏 /gp/bestsellers/<类目>/<节点> 短链接"
        if re.search(r"/gp/new-releases/", u):
            return "这是 New Releases 链接; 请改为 Best Sellers 链接(New Releases 由系统自动推导, 勿手填)"
        if not re.match(r"^https?://[^/?#]+/gp/bestsellers/[^/?#]+/\d+(?:/(?:ref=[^?#]*)?)?$", u):
            return "格式须为 https://www.amazon.com/gp/bestsellers/<类目>/<节点ID>(类目=英文短名, 节点ID=纯数字)"
        return None

    @staticmethod
    def _looks_like_nr_page(final_url: str, title: str) -> bool:
        """校验落地页确为 New Releases 榜单(防 NR 派生 URL 回落到 Best Sellers 页)。

        决定性反证优先: 落地 URL 含 zgbs(BS 规范路径)或标题含 "Best Sellers" -> 回落;
        其次决定性正证: URL/标题含 new-releases; 无法判断时保守放行(宁可重叠也不丢数据)。
        """
        f = (final_url or "").lower()
        t = (title or "").lower()
        if "zgbs" in f or "best sellers" in t:
            return False
        if "new-releases" in f or "new releases" in t:
            return True
        return True

    def ensure_amazon_login(self, ctx: StepContext) -> None:
        """确保亚马逊登录态: Profile 有效 -> COOKIE 导入 -> 弹窗手动登录兜底。

        公共方法: 步骤0 与编排器断点续跑预检查共用。
        """
        if ctx.dry_run:
            self.log(ctx, "dry-run 跳过亚马逊登录检查")
            return
        try:
            from core.amz_crawl_lock import remaining_ip_cooldown
            cd_rem, cd_info = remaining_ip_cooldown()
            if cd_rem > 0:
                cd_reason = str(cd_info.get("reason") or "跨类目连续风控冷却")
                self.log(ctx, f"亚马逊登录预检等待本机 IP 冷却剩余 {int(round(cd_rem))}s ({cd_reason})")
                while True:
                    rem, _ = remaining_ip_cooldown()
                    if rem <= 0:
                        break
                    time.sleep(min(2.0, rem))
        except Exception:
            pass
        if self._check_amazon(ctx):
            self.log(ctx, "亚马逊登录态有效")
            return
        if not self._may_manage_amazon_login():
            from core.errors import WaitingForHuman
            raise WaitingForHuman("亚马逊共享登录态无效或已过期，请联系管理员重新登录该站点后续跑")
        cookie = str(ctx.config.get("COOKIE", "") or "").strip()
        if cookie:
            self.log(ctx, "Profile 无有效会话, 尝试用配置 COOKIE 导入浏览器")
            if self._inject_amazon_cookie(ctx, cookie) and self._check_amazon(ctx, publish=True):
                self.log(ctx, "COOKIE 导入成功, 登录态有效")
                return
            self.log(ctx, "COOKIE 导入后仍无有效登录态(Cookie 可能已过期)")
        self.log(ctx, "亚马逊 Cookie 无效或过期, 打开有头窗口等待手动登录")
        timeout = int(ctx.config.get("LOGIN_TIMEOUT", 300))
        ok = self._manual_login_wait(ctx, "amazon", timeout)
        if not ok:
            raise StepFailedError("亚马逊 Cookie 无效或过期")

    def _inject_amazon_cookie(self, ctx: StepContext, cookie: str) -> bool:
        with ctx.browser("amazon") as browser:
            page = browser.new_page()
            try:
                return bool(browser.inject_cookies(page, cookie))
            except Exception as exc:
                self.log(ctx, f"COOKIE 导入异常: {exc}")
                return False
            finally:
                self._close_page(page)

    @staticmethod
    def _may_manage_amazon_login() -> bool:
        # Workbench jobs receive their role from the authenticated server.
        # A missing role on a workbench job fails closed; local CLI is separate.
        return not os.environ.get('PIPELINE_WORKBENCH_USER') or os.environ.get('PIPELINE_WORKBENCH_ROLE', '').strip().lower() == 'admin'

    def _check_amazon(self, ctx: StepContext, publish: bool = False) -> bool:
        allow_cap = bool(getattr(self, "_allow_captcha_passthrough", False))
        with ctx.browser("amazon") as browser:
            page = browser.new_page()
            try:
                from core import amazon_login_share
                if browser.check_amazon_login(page):
                    if self._may_manage_amazon_login():
                        try:
                            amazon_login_share.save(page.context, ctx.paths.profile_root, ctx.config.market, replace=publish)
                        except (OSError, ValueError, TypeError) as exc:
                            self.log(ctx, f"亚马逊共享登录态保存失败: {type(exc).__name__}")
                    return True
                is_cap = getattr(browser, "is_amazon_captcha_page", None)
                has_auth = getattr(browser, "has_valid_amazon_auth_cookie", None)
                if allow_cap and callable(is_cap) and callable(has_auth) and is_cap(page) and has_auth(page):
                    self.log(ctx, "亚马逊首页预检命中无头验证码(非掉登录；Profile 持有有效认证 Cookie)，放行交由步骤兜底链处理")
                    return True
                try:
                    restored = amazon_login_share.restore(page.context, ctx.paths.profile_root, ctx.config.market)
                except (OSError, ValueError, TypeError) as exc:
                    self.log(ctx, f"亚马逊共享登录态读取失败: {type(exc).__name__}")
                    restored = False
                if restored and browser.check_amazon_login(page):
                    self.log(ctx, "已验证并复用本亚马逊站点的共享登录态")
                    return True
                if allow_cap and restored and callable(is_cap) and callable(has_auth) and is_cap(page) and has_auth(page):
                    self.log(ctx, "已恢复共享认证 Cookie，首页预检命中无头验证码(非掉登录)，放行交由步骤兜底链处理")
                    return True
                return False
            finally:
                self._close_page(page)

    def ensure_sellersprite_login(self, ctx: StepContext) -> None:
        """确保卖家精灵登录态: Profile 会话 -> 账号自动登录 -> 弹窗手动登录兜底。

        公共方法: 步骤0 与编排器断点续跑预检查共用。
        """
        if ctx.dry_run:
            self.log(ctx, "dry-run 跳过卖家精灵登录检查")
            return
        if self._check_sellersprite(ctx):
            self.log(ctx, "卖家精灵登录态有效")
            return
        account = str(ctx.config.get("SS_ACCOUNT", "") or "").strip()
        password = str(ctx.config.get("SS_PASSWORD", "") or "").strip()
        if account and password:
            self.log(ctx, "卖家精灵未登录, 尝试自动登录")
            if self._auto_login_sellersprite(ctx, account, password):
                return
            self.log(ctx, "卖家精灵自动登录失败, 打开有头窗口等待手动登录")
        else:
            self.log(ctx, "未配置 SS_ACCOUNT/SS_PASSWORD, 打开有头窗口等待手动登录")
        timeout = int(ctx.config.get("LOGIN_TIMEOUT", 300))
        ok = self._manual_login_wait(ctx, "sellersprite", timeout)
        if not ok:
            raise StepFailedError("卖家精灵登录失败")

    def _check_sellersprite(self, ctx: StepContext) -> bool:
        from core.browser import BrowserAutomation
        if BrowserAutomation.login_verified(ctx.paths.profile_ss()):
            self.log(ctx, "卖家精灵登录态有效(本轮已确认)")
            return True
        with ctx.browser("sellersprite") as browser:
            page = browser.new_page()
            try:
                return bool(browser.check_sellersprite_login(page))
            finally:
                self._close_page(page)

    def _auto_login_sellersprite(self, ctx: StepContext, account: str, password: str) -> bool:
        with ctx.browser("sellersprite") as browser:
            page = browser.new_page()
            try:
                return bool(browser.login_sellersprite(page, account, password))
            except Exception as exc:
                self.log(ctx, f"卖家精灵自动登录异常: {exc}")
                return False
            finally:
                self._close_page(page)

    def _manual_login_wait(self, ctx: StepContext, site: str, timeout: int) -> bool:
        """打开有头浏览器并**主动导航到登录页**等待用户手动登录。

        检查使用 navigate=False 非侵入方式(不跳走页面, 避免打断用户输入)。
        """
        deadline = time.monotonic() + timeout
        with ctx.browser(site, headed=True) as browser:
            page = browser.new_page()
            try:
                if site == "amazon":
                    browser.goto(page, f"https://{ctx.config.market}/ap/signin")
                else:
                    browser.goto(page, "https://www.sellersprite.com/w/user/login")
                self.log(ctx, f"请在浏览器窗口完成 {site} 登录, 等待最多 {timeout} 秒")
                while time.monotonic() < deadline:
                    time.sleep(3)
                    if site == "amazon":
                        if browser.check_amazon_login(page, navigate=False):
                            try:
                                from core import amazon_login_share
                                amazon_login_share.save(page.context, ctx.paths.profile_root, ctx.config.market)
                            except (OSError, ValueError, TypeError) as exc:
                                self.log(ctx, f"亚马逊共享登录态保存失败: {type(exc).__name__}")
                            return True
                    else:
                        if browser.check_sellersprite_login(page, navigate=False):
                            return True
            finally:
                self._close_page(page)
        return False

    @staticmethod
    def _close_page(page) -> None:
        if hasattr(page, "close"):
            try:
                page.close()
            except Exception:
                pass

    @staticmethod
    def _current_url(page, fallback: str) -> str:
        """当前页真实 URL(取不到就用调用方给的 fallback), 用于把相对 href 补成绝对地址。"""
        try:
            cur = str(getattr(page, "url", "") or "")
        except Exception:
            cur = ""
        return cur if cur.startswith("http") else fallback

    def _click_next_page(self, ctx: StepContext, browser, page, kind: str) -> str:
        """**点击**"下一页"控件, 返回**点击后的真实 URL**(未点击/点击失败返回 "")。

        为什么要返回 URL 做校验(2026-09-15 实测踩到): 只"点一下"并不代表真的翻页了 ——
        实测点击 `li.a-last a` 后页面**纹丝不动**(第 2 页与第 1 页的 `scrollY/页高/ASIN`
        完全相同, 只是被去重掩盖成"60 个"), 于是"翻了两页"其实还是同一页。
        调用方据返回值判断是否真跳转, 没跳就回退到直接导航。
        """
        before = self._current_url(page, "")
        for sel in _NEXT_PAGE_SELECTORS:
            try:
                browser.click(page, sel, timeout=8000)
            except Exception:
                continue
            self.log(ctx, f"[{kind}] 已点击下一页控件: {sel}")
            time.sleep(1.6)                       # 等分页 JS/导航
            after = self._current_url(page, "")
            if after and after != before:
                return after
            self.log(ctx, f"[{kind}] 点击后 URL 未变化(仍在 {after or before}), 判定未真正翻页")
            return ""
        return ""

    def _goto_next_page(self, ctx: StepContext, browser, page, nxt: str, kind: str) -> bool:
        """翻到下一页: 先点控件(真实用户行为), **未真正跳转则回退直接导航**。返回是否成功。"""
        clicked_url = self._click_next_page(ctx, browser, page, kind)
        if clicked_url:
            return True
        try:
            browser.goto(page, nxt, timeout=60000)
            return True
        except Exception as exc:  # noqa: BLE001
            self.log(ctx, f"直接导航也失败({nxt[:110]}): {exc}")
            return False

    def _verify_list_page(self, ctx, browser, page, url, kind, page_no, manual_challenge):
        if not _is_verification_page(_page_content(page)):
            return
        shot = os.path.join(ctx.paths.screenshot_dir(),
                            f"bestseller-verification-p{page_no}-{self._safe_name(url)}.png")
        try:
            browser.screenshot(page, shot)
        except Exception:
            pass
        if not manual_challenge:
            raise ListVerificationRequired(f"[{kind}] 第 {page_no} 页遇到亚马逊人机验证: {url}")
        timeout = max(1, int(ctx.config.get("LOGIN_TIMEOUT", 300) or 300))
        self.log(ctx, f"[{kind}] 请在有头浏览器完成人机验证，等待最多 {timeout} 秒；验证通过后自动继续。")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(2)
            try:
                html = _page_content(page)
            except Exception as exc:
                if "unable to retrieve content" in str(exc).lower() and "navigat" in str(exc).lower():
                    continue
                raise
            if not _is_verification_page(html):
                _, _, strict, _ = _main_list_scope(html)
                if strict or (kind == "nr" and self._is_no_new_releases(html)):
                    self.log(ctx, f"[{kind}] 验证通过，已恢复榜单页面。")
                    return
        raise ListVerificationRequired(f"[{kind}] 人机验证未完成或榜单未恢复，已保留断点: {url}")

    def _read_list_page(self, ctx: StepContext, browser, page, url: str, kind: str,
                        page_no: int = 1, manual_challenge: bool = False) -> tuple:
        """读**一页**榜单: 滚动到底(等懒加载稳定) → 名次缺号自愈 → 截图 → 只取榜单卡 ASIN
        → 打印自查行。

        返回 (滚动次数, 是否到底, 状态文本, scope_html, 主列表卡数, 严格模式, 卡片标记合计, ASIN 列表)。
        """
        where = f"第 {page_no} 页 " if page_no > 1 else ""
        self._verify_list_page(ctx, browser, page, url, kind, page_no, manual_challenge)
        scrolled, at_bottom, view, html = _settle_list_page(browser, page)
        self._verify_list_page(ctx, browser, page, url, kind, page_no, manual_challenge)
        html = _page_content(page)
        scope, cards, strict, cards_all = _main_list_scope(html)
        # **只取榜单卡范围**内的 ASIN(2026-09-15): 原先全文扫 /dp/ 会把推荐/轮播的 ASIN
        # 混进榜单清单(实测全文 64 个 vs 榜单 50 个)。
        asins = list(dict.fromkeys(_ASIN_PATTERN.findall(scope)))
        # 名次缺号自愈(2026-09-15 实测): "有下一页"或"上一页是满页"时, 本页名次必须到页边界
        # (第 1 页 #50 / 后续页满 50 个) —— 只到 #38 就是**渲染不全**(视口矮时只渲染 38 个),
        # 放大视口重载一次再读。这是"只抓到 38 个""缺 #39~#50""第 2 页假尾页(#51~#88)"的直接补丁。
        ranks = _rank_badges(html)
        if _page_incomplete(ranks, page_no, bool(_next_page_url(html, url))):
            self.log(ctx, f"[{kind}] {where}名次={_rank_range(html)} 未达第 {page_no} 页应有的 "
                          f"#{page_no * _LIST_PAGE_SIZE}, 判定渲染不全 → 放大视口重载补齐")
            if _set_list_viewport(page, _LIST_VIEWPORT_RETRY):
                try:
                    # **重载当前页**(不能重载函数入参 url: 第 2 页往后它是首页地址, 会把首页
                    # 内容当成第 2 页结果 → 表现为"第 2 页没有新增 ASIN"而提前停止翻页)
                    browser.goto(page, self._current_url(page, url), timeout=60000)
                    self._verify_list_page(ctx, browser, page, url, kind, page_no, manual_challenge)
                    scrolled, at_bottom, view, html = _settle_list_page(browser, page)
                    self._verify_list_page(ctx, browser, page, url, kind, page_no, manual_challenge)
                    html = _page_content(page)
                    scope, cards, strict, cards_all = _main_list_scope(html)
                    asins = list(dict.fromkeys(_ASIN_PATTERN.findall(scope)))
                    ranks = _rank_badges(html)
                    self.log(ctx, f"[{kind}] {where}重载后名次={_rank_range(html)} "
                                  f"提取ASIN={len(asins)}")
                except ListVerificationRequired:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self.log(ctx, f"[{kind}] {where}放大视口重载失败(保留首次结果): {exc}")
        suffix = "" if page_no == 1 else f"-p{page_no}"
        shot = os.path.join(ctx.paths.screenshot_dir(),
                            f"bestseller{suffix}-{self._safe_name(url)}.png")
        try:
            browser.screenshot(page, shot)
        except Exception:
            pass
        self.log(
            ctx,
            f"[{kind}] {where}页面解析: 主列表卡={cards if strict else '未识别'}"
            f"(页面卡片标记合计 {cards_all}, 差额多为推荐/轮播)"
            f" 严格模式={'是' if strict else '否(回退全文扫描, 可能混入推荐/轮播)'} "
            f"名次={_rank_range(html)} "
            f"提取ASIN={len(asins)} 滚动={scrolled}次 到底={'是' if at_bottom else '否'} {view}",
        )
        return scrolled, at_bottom, view, scope, cards, strict, cards_all, asins

    def _scrape_list(self, ctx: StepContext, browser, url: str, kind: str = "bs", attempts: int = 3,
                     manual_challenge: bool = False) -> list[str] | None:
        """抓取单个榜单页 ASIN。

        返回值语义:
          list  -> 抓到的 ASIN(可能为空列表)
          None  -> 页面校验未通过(仅 nr: 派生 URL 回落到 Best Sellers 页), 调用方跳过且不计入 nr
        空列表的特殊情形(nr 榜): NR 榜为**可选项**——页面出现 "Sorry, there are no hot new releases
        available in this category" 文案, **或**滚动后抓不到任何 ASIN, 两条件任一命中都直接跳过,
        记为空继续, 不重试不失败(bs 榜空/失败才按抓取失败重试与报错)。
        """
        last_err = ""
        for attempt in range(1, attempts + 1):
            page = browser.new_page()
            try:
                # 视口先放大再导航(2026-09-15 实测): 720 高的视口下第 1 页只渲染 38 个名次,
                # 放大到 2600 才渲染全 50 个 —— 懒加载按视口高度决定渲染多少张卡。
                _set_list_viewport(page)
                browser.goto(page, url, timeout=60000)
                self._verify_list_page(ctx, browser, page, url, kind, 1, manual_challenge)
                if kind == "nr":
                    final_url, title = "", ""
                    try:
                        final_url = page.url
                    except Exception:
                        pass
                    try:
                        title = page.title()
                    except Exception:
                        pass
                    if not self._looks_like_nr_page(final_url, title):
                        shot = os.path.join(
                            ctx.paths.screenshot_dir(),
                            f"bestseller-nr-mismatch-{self._safe_name(url)}.png",
                        )
                        try:
                            browser.screenshot(page, shot)
                        except Exception:
                            shot = "(截图失败)"
                        self.log(
                            ctx,
                            f"警告: NR 派生 URL 未打开真实新品榜(final={final_url[:90]!r} "
                            f"title={title[:60]!r}), 截图 {os.path.basename(str(shot))}, 不计入 NR: {url}",
                        )
                        return None
                scrolled, at_bottom, view, scope, cards, strict, cards_all, page_asins = \
                    self._read_list_page(ctx, browser, page, url, kind, manual_challenge=manual_challenge)
                if not at_bottom:
                    self.log(ctx, f"警告: 榜单页滚动后**未到达页面底部**({view}) —— "
                                  f"懒加载可能还有未渲染的商品, 该榜 ASIN 可能不全: {url}")
                if not strict:
                    self.log(ctx, f"警告: 未识别到主列表卡片容器, 已回退全文 /dp/ 扫描, "
                                  f"结果可能混入推荐/轮播模块: {url}")
                if strict and cards and len(page_asins) < cards:
                    self.log(ctx, f"提示: 主列表 {cards} 张卡但只解析出 {len(page_asins)} 个 ASIN "
                                  f"(部分卡片可能未含 /dp/ 链接或为占位卡): {url}")

                # ---- 翻页收集(2026-09-15) ----
                # 实测 UK Chopping Boards 目录节点: 榜单共 **88 个**商品, 第 1 页 #1~#50、
                # 第 2 页 #51~#88(页面含 <li class="a-last"><a …>Next page)。翻页只为拿第 51
                # 名之后; 前 50 名本来就在第 1 页(拿不全的原因见 _ranked_card_spans / _LIST_VIEWPORT)。
                html = _page_content(page)
                pages_ok = 1
                seen_pages = {url}
                while pages_ok < _MAX_LIST_PAGES:
                    nxt = _next_page_url(html, base_url=self._current_url(page, url))
                    if not nxt or nxt in seen_pages:
                        if not nxt:
                            self.log(ctx, f"[{kind}] 未发现下一页链接(单页榜单或控件结构变化), "
                                          f"本榜共 {pages_ok} 页")
                        break
                    seen_pages.add(nxt)
                    self.log(ctx, f"[{kind}] 下一页链接: {nxt[:130]}")
                    page_no = pages_ok + 1
                    if not self._goto_next_page(ctx, browser, page, nxt, kind):
                        self.log(ctx, f"翻页失败(第 {page_no} 页), 保留已抓 "
                                      f"{len(page_asins)} 个 ASIN")
                        break
                    try:
                        _, at_b2, view2, _, cards2, strict2, _, page2 = \
                            self._read_list_page(ctx, browser, page, url, kind, page_no=page_no,
                                                 manual_challenge=manual_challenge)
                        self.log(ctx, f"[{kind}] 第 {page_no} 页: "
                                      f"主列表卡={cards2 if strict2 else '未识别'} "
                                      f"提取ASIN={len(page2)} 到底={'是' if at_b2 else '否'} {view2}")
                    except ListVerificationRequired:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        self.log(ctx, f"第 {page_no} 页解析失败, 保留已抓内容: {exc}")
                        break
                    new_ones = [a for a in page2 if a not in page_asins]
                    if not new_ones:
                        # 翻页后没有任何**新** ASIN: 大概率还停在同一页(点击没生效/分页是懒加载),
                        # 必须停手 —— 否则会把同一页重复计入, 造成"共 2 页 60 个"的假象(实测踩过)。
                        self.log(ctx, f"第 {page_no} 页没有新增 ASIN(疑似未真正翻页或已到末页), "
                                      f"停止翻页; 本榜保留 {len(page_asins)} 个")
                        break
                    pages_ok += 1
                    page_asins = list(dict.fromkeys(page_asins + new_ones))
                    html = _page_content(page)
                if pages_ok > 1:
                    self.log(ctx, f"[{kind}] 翻页收集完成: 共 {pages_ok} 页, "
                                  f"去重后 {len(page_asins)} 个 ASIN")
                asins = page_asins
                if kind == "nr":
                    # NR 榜为可选项: 两条件**任一命中**都直接跳过, 不重试不失败——
                    #   ① 页面出现无新品文案("Sorry, there are no hot new releases ...")
                    #   ② 滚动后抓不到任何 ASIN(部分子类目确实没有 New Releases 新品榜)
                    no_nr_text = self._is_no_new_releases(html)
                    if asins and not no_nr_text:
                        return asins
                    reason = "页面提示无新品" if no_nr_text else "未提取到任何 ASIN"
                    self.log(ctx, f"该类目无 New Releases 新品榜({reason}), NR 记为空, 直接跳过: {url}")
                    return []
                if asins:
                    return asins
                last_err = "页面未提取到 ASIN"
                self.log(ctx, f"第{attempt}次未提取到 ASIN: {url}")
            except ListVerificationRequired:
                raise
            except Exception as exc:
                last_err = str(exc)
                self.log(ctx, f"榜单页抓取失败(第{attempt}/{attempts}次): {url} -> {last_err}")
            finally:
                self._close_page(page)
            if attempt < attempts:
                time.sleep(2 * attempt)
        if kind == "nr":
            # NR 页多次尝试失败/异常(网络等)后仍无数据: 记空继续, 不阻断整条流水线
            self.log(ctx, f"警告: New Releases 页多次尝试未取到数据(NR 记为空继续, 不阻断): {url}")
            return []
        raise StepFailedError(f"榜单页抓取失败: {url} ({last_err})")

    @staticmethod
    def _is_no_new_releases(html: str) -> bool:
        """识别"该类目暂无 New Releases 新品"页面文案(亚马逊英文站点)。

        例: "Sorry, there are no hot new releases available in this category.
             Please check back later."
        """
        low = (html or "").lower()
        return any(m in low for m in ("no hot new releases", "no new releases available"))

    def _extract_asins(self, html: str) -> list[str]:
        """提取榜单 ASIN: **只取主列表卡片**, 并额外回报页面里的卡片数(便于发现"没抓全")。

        为什么不能全文扫 `/dp/`(2026-09-15 修复): 榜单页除了主列表, 还有轮播/推荐
        ("相关类目"/"看了又看"/广告)等模块也含 `/dp/` 链接, 全文扫描会把它们一起算进来,
        且无法判断"主列表到底渲染了几条" —— 实测 UK 榜单每页本应 50 个商品, 却只抓到
        31~32 个且**没有任何告警**。这里改为:
          ① 优先在主列表容器(桌面版卡片 `p13n-sc-uncoverable-faceout` / `zg-grid-general-faceout`)
             范围内取 ASIN(与步骤9 只认结果卡同一思路);
          ② 容器识别不到时回退全文扫描(老结构/结构漂移), 但调用方会打告警。
        返回去重后的 ASIN 列表(保持页面顺序)。
        """
        scope, grid_n, strict = _main_list_scope(html)
        found = _ASIN_PATTERN.findall(scope if strict else html)
        return list(dict.fromkeys(found))

    @staticmethod
    def _safe_name(url: str) -> str:
        name = re.sub(r"[^A-Za-z0-9_-]+", "-", url)
        return name.strip("-")[:120] or "list"

    def _save(self, json_path: str, txt_path: str, bs_set: set, nr_set: set, processed: set) -> None:
        union = sorted(bs_set | nr_set)
        write_json(
            json_path,
            {
                "bs": sorted(bs_set),
                "nr": sorted(nr_set),
                "union": union,
                "processed_urls": sorted(processed),
            },
        )
        write_txt_lines(txt_path, union)
