"""FR-10 变体抓取 + 第二遍过滤(2026-09-08 定稿: 全变体逐 ASIN 抓取 + 变体级过滤, 无继承)。

输入: 全量ASIN清单(<CAT>-search-asins-all.txt, FR-09 输出, 缺失回退双榜并集)
      + 类目过滤提示词(filter-<CAT>-prompt.json, FR-02 输出, 缺失仅导出不过滤)
输出: <CAT>-results.jsonl / <CAT>-父项变体明细.xlsx(3 sheet: 变体明细/父体汇总/统计)
      / <CAT>-parents.txt(过滤后仍有效的**多变体父体**: 原 >=2 子 且**至少一个子变体被保留**)
      / <CAT>-variants.txt(过滤后的变体: **保留的子体** ∪ **保留的单变体产品自身**)
      / <CAT>-过滤审计.csv
      + <CAT>-variant-texts.jsonl(逐变体自身页补抓, 断点续爬)

逻辑(2026-09-08 用户定稿):
  ① HTTP 直连逐个抓清单详情页, 解析每个父体页里的变体 ASIN 清单;
  ② **全量变体补抓**: 对发现出的每个子变体 ASIN 单独抓其自身页面
     (标题/五点/全部图片链接), 断点续爬; 多次重试仍失败 -> 剔除该变体;
  ③ **过滤对象 = 每个有自身文本的 ASIN(请求 ∪ 成功补抓变体, ~数千行)**, 用其
     **自己的标题+五点** 分批 LLM 判定(FR-02.1, 阶梯批), 不是只判请求记录;
  ④ 明细 = 保留的每个 ASIN 一行(自身标题/五点/全图), **无"继承父体"**;
  ⑤ 步骤11/12 清单(parents/variants) = **过滤后口径(2026-09-09 用户定稿)**:
     parents=有保留子体的多变体父体(省流量配额), variants=保留子体∪保留单变体。
容错: 断点续爬(已抓记录跳过); 提示词缺失仅导出; 过滤/复核 LLM 失败走 judge_batch
      阶梯重试(集中重发 -> 拆 2 条/批 -> 1 条/批逐条重试, 见 steps/_filtering.py)
"""
from __future__ import annotations

import csv
import json
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

from core.amazon_variation import (
    attempts_for,
    auto_resolve_cross_parent,
    build_groups_skill,
    normalize_asin,
    parse_variations as skill_parse_variations,
    url_candidates,
    worker_count as skill_worker_count,
)
from core.category_config import (
    NON_US_QUALIFIED_TOP,
    is_us_site as _is_us_site,
    pick_top_volume_candidates,
)
from core.errors import StepDataError
from core.http_client import HttpFetcher, parse_product_html
from core.io_utils import (
    append_jsonl,
    atomic_write_text,
    df_to_excel,
    dedup_keep_best,
    read_json,
    read_jsonl,
    read_txt_lines,
    write_txt_lines,
)
from core.keyword_lists import resolve as resolve_keywords
from core.mock import make_asins
from core.validation import require_file
from steps._filtering import (
    apply_review_policy,
    enrich_prompt_with_keywords,
    judge_batch,
    review_filter_results,
)
from steps.base import BaseStep, StepContext

# ---------------------------------------------------------------- 终态 / 拒收状态
# **终态**(永久跳过): 断点续爬、后续重试轮、浏览器补抓都不再重抓这些 ASIN。
#   · skipped_page_not_found   —— 404 / 标题判定 Page Not Found(站上确实没有该商品)
#   · not_found                —— 旧版本写入的历史值(兼容读)
#   · skipped_too_many_variants —— 变体数超过门限(1..100): 页面真实存在且解析成功,
#     只是被本程序的上限拒收 → 属终态, 反复重抓只会白耗请求(2026-09-16 新增)
_TERMINAL_STATUSES = (
    "skipped_page_not_found",
    "not_found",
    "skipped_too_many_variants",
)
# **拒收**(页面已成功取回, 内容被本程序门限拒绝): 与"网络失败"是两回事, 记录必须
# 保留解析器写下的取证字段(variant_count/html_length/map_counts/extended_variant_sources)。
_REJECT_STATUSES = ("parse_failed", "skipped_too_many_variants")


def _is_terminal(status) -> bool:
    """该状态是否"终态永久跳过"(断点续爬/后续轮次/浏览器补抓都不再重抓)。"""
    return str(status or "") in _TERMINAL_STATUSES



def parse_variants(html: str, request_asin: str) -> tuple[str, list[str], dict, list[str]]:
    """从详情页 HTML 提取 父ASIN/变体ASIN列表/变体标签/全图集(六步解析)。

    第一步 Twister 数据(P.register('twister-js-init-dpx-data') dataToReturn,
            括号配对扫描; 正则 parentAsin/currentAsin/dimensionToAsinMap 兜底)
    第二步 图片块(jQuery.parseJSON 单引号 JSON -> colorToAsin/colorImages)
    第三步 组装变体列表(优先级链: dimensionToAsinMap 值 -> 页面ASIN -> colorToAsin
            -> data-asin 色块 -> 父ASIN 兜底)
    第四步 文本字段(标题 productTitle / 五点 feature-bullets / 主图 landingImage)
    第五步 变体标签/图片(colorToAsin 配色 + colorImages[color][0] 主图)
    第六步 全图集(**大图优先**: colorImages 的 hiRes/large → ivLargeImage → 缩略图兜底)
    返回 (parent, variant_asins, labels, images)。
    """
    twister = _extract_twister(html)
    img_block = _extract_image_block(html)
    parent, variants = _assemble_variants(html, request_asin, twister, img_block)
    title, bullets, main_img = _extract_text_fields(html)
    labels = _extract_variant_labels(html, img_block, variants)
    gallery = _extract_gallery(html, main_img, img_block)
    return parent, variants, labels, gallery


def _extract_twister(html: str) -> dict:
    """第一步: Twister 数据。优先 dataToReturn 括号配对扫描, 正则兜底。"""
    marker = "twister-js-init-dpx-data"
    idx = html.find(marker)
    dt = html.find("dataToReturn", idx if idx != -1 else 0)
    if dt != -1:
        brace = html.find("{", dt)
        if brace != -1:
            text = _balanced_scan(html, brace)
            if text:
                try:
                    data = json.loads(text)
                    if isinstance(data, dict) and data:
                        return data
                except (json.JSONDecodeError, ValueError):
                    pass
    return _twister_regex_fallback(html)


def _balanced_scan(html: str, brace: int) -> str:
    """从 { 位置括号配对扫描, 返回闭合的 JSON 片段。"""
    depth = 0
    in_str = False
    escape = False
    for i in range(brace, min(brace + 200000, len(html))):
        ch = html[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return html[brace : i + 1]
    return ""


def _twister_regex_fallback(html: str) -> dict:
    """正则逐个兜底: parentAsin / currentAsin / dimensionToAsinMap。"""
    data: dict = {}
    m = re.search(r'"parentAsin"\s*:\s*"(B[0-9A-Z]{9})"', html)
    if m:
        data["parentAsin"] = m.group(1)
    m = re.search(r'"currentAsin"\s*:\s*"(B[0-9A-Z]{9})"', html)
    if m:
        data["currentAsin"] = m.group(1)
    m = re.search(r'"dimensionToAsinMap"\s*:\s*(\{.{0,3000}?\})', html, re.S)
    if m:
        try:
            obj = json.loads(m.group(1))
            if isinstance(obj, dict):
                data["dimensionToAsinMap"] = obj
        except (json.JSONDecodeError, ValueError):
            pass
    return data


def _extract_image_block(html: str) -> dict:
    """第二步: 图片块 jQuery.parseJSON('...') 单引号 JSON -> colorToAsin/colorImages。"""
    for m in re.finditer(r"jQuery\.parseJSON\('(.+?)'\)", html, re.S):
        raw = _unescape_js_json(m.group(1))
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict) and ("colorToAsin" in obj or "colorImages" in obj):
            return obj
    return {}


def _unescape_js_json(raw: str) -> str:
    """手动转义解析单引号包裹的 JS JSON(双反斜杠/引号转义)。"""
    return raw.replace("\\'", "'").replace('\\"', '"').replace("\\\\", "\\")


def _assemble_variants(html: str, page_asin: str, twister: dict, img_block: dict) -> tuple[str, list[str]]:
    """第三步: 变体列表优先级链。"""
    parent = twister.get("parentAsin") or page_asin
    variants: list[str] = []
    dim_map = twister.get("dimensionToAsinMap") or {}
    for value in dim_map.values():
        items = value if isinstance(value, list) else [value]
        for a in items:
            if isinstance(a, str) and a and a not in variants:
                variants.append(a)
    if page_asin and page_asin not in variants:
        variants.insert(0, page_asin)
    color_map = img_block.get("colorToAsin") or {}
    for a in color_map.values():
        if isinstance(a, str) and a and a not in variants:
            variants.append(a)
    if len(variants) <= 1:
        # 兜底仅在"变体控件区域"内收 data-asin(2026-09-08: 原**整页**扫描会把
        # 配件/推荐/广告等无关 ASIN 收成"假变体", 造成整组继承父体文本的假数据)
        low = html.lower()
        region_start = -1
        for marker in ('id="twister_feature_div"', 'id="variation', "twister-plus"):
            j = low.find(marker)
            if j != -1:
                region_start = j
                break
        if region_start != -1:
            seg = html[region_start:region_start + 150_000]
            for m in re.finditer(r'data-asin="(B[0-9A-Z]{9})"', seg):
                a = m.group(1)
                if a != page_asin and a not in variants:
                    variants.append(a)
    if len(variants) <= 1:
        variants = [page_asin]
    return parent, variants


def _extract_text_fields(html: str) -> tuple[str, list[str], str]:
    """第四步: 标题 / 五点 / 主图。"""
    title = ""
    m = re.search(r'<span id="productTitle"[^>]*>\s*(.*?)\s*</span>', html, re.S)
    if m:
        title = _clean_html(m.group(1))
    bullets: list[str] = []
    block = html[html.find('id="feature-bullets"') : html.find('id="feature-bullets"') + 30000]
    for m in re.finditer(r'<span class="a-list-item">\s*(.*?)\s*</span>', block, re.S):
        text = _clean_html(m.group(1))
        if text and len(bullets) < 5:
            bullets.append(text)
    main_img = ""
    m = re.search(r'<img[^>]+id="landingImage"[^>]*>', html)
    if m:
        tag = m.group(0)
        m2 = re.search(r'data-old-hires="([^"]+)"', tag)
        if m2:
            main_img = m2.group(1)
        else:
            m2 = re.search(r'src="([^"]+)"', tag)
            if m2:
                main_img = m2.group(1)
    return title, bullets, main_img


def _extract_variant_images(variants: list[str], img_block: dict) -> tuple[dict, dict]:
    """第五步: 变体标签/图片(colorToAsin 配色 + colorImages[color][0] 主图)。

    2026-09-14 修(真站结构漂移): 真页面 `colorToAsin` 的值是**嵌套对象**
        {"Light Bamboo Lot de 3": {"asin": "B017CBD1C5"}, …}
    原实现只接受字符串形态 {"Black": "B03C537AFE"}(见 tests/test_parse_variants.py 的旧夹具),
    `isinstance(asin, str)` 永不成立 → **标签与每配色主图恒为空**, 且是静默失效
    (实测: 历史 6 个类目 10,456 条 ok 记录里 variant_labels 非空 0 条)。现两种形态都支持。
    """
    labels: dict = {}
    images: dict = {}
    color_to_asin = img_block.get("colorToAsin") or {}
    color_images = img_block.get("colorImages") or {}
    asin_to_color = {}
    for color, asin in color_to_asin.items():
        code = _variant_asin_of(asin)
        if code:
            asin_to_color[code] = color
    for a in variants:
        color = asin_to_color.get(a)
        if not color:
            continue
        labels[a] = color
        imgs = color_images.get(color) or []
        if imgs and isinstance(imgs[0], dict):
            images[a] = imgs[0].get("hiRes") or imgs[0].get("large")
    return labels, images


def _variant_asin_of(value: object) -> str:
    """取 colorToAsin 值里的 ASIN: 兼容 "B095AC5A88" 与 {"asin": "B095AC5A88"} 两种形态。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("asin", "ASIN"):
            v = value.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


_ASIN_FULL_RE = re.compile(r"^[A-Z0-9]{10}$")


def _labels_from_dimension_display(html: str) -> dict:
    """`dimensionValuesDisplayData` -> {ASIN: "Light Bamboo / Lot de 3"}。

    仅尺寸/规格类变体(没有配色映射)的页面只有这个来源 —— 实测美国站有的类目
    `map_counts.colorToAsin = 0` 而变体多达 23 个, 标签只能从这里取。
    """
    out: dict = {}
    # 页面形态两种都见过: JSON 字段 "dimensionValuesDisplayData":{…} 与 JS 赋值
    # `var dimensionValuesDisplayData = {…}`(真站 amazon.fr 页是后者)
    m = re.search(r'dimensionValuesDisplayData"?\s*[:=]\s*(\{)', html or "")
    if not m:
        return out
    raw = _balanced_scan(html, m.start(1))
    if not raw:
        return out
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return out
    if not isinstance(data, dict):
        return out
    for asin, vals in data.items():
        code = str(asin or "").strip().upper()
        if not _ASIN_FULL_RE.match(code):
            continue
        if isinstance(vals, list):
            text = " / ".join(str(v).strip() for v in vals if str(v).strip())
        else:
            text = str(vals or "").strip()
        if text:
            out[code] = text
    return out


def _extract_variant_labels(html: str, img_block: dict, variants: list[str]) -> dict:
    """变体标签(ASIN -> 人类可读名): 配色名优先, 缺失时回落维度取值组合。"""
    color_labels, _ = _extract_variant_images(variants, img_block)
    dim_labels = _labels_from_dimension_display(html)
    out: dict = {}
    for a in variants:
        name = color_labels.get(a) or dim_labels.get(a) or ""
        if name:
            out[a] = name
    return out


GALLERY_MAX = 24
"""图集上限(2026-09-20 由 10 提到 24): 大图源并入后 10 张会把图廊截断。"""


_PARSEJSON_RE = re.compile(r"parseJSON\('(.+?)'\)", re.S)


def _gallery_from_image_block_json(html: str) -> list[dict] | None:
    """取**完整图廊**数组(ImageBlockATF 里 `colorImages.initial` 的 `parseJSON('[...]')`)。

    真实结构(2026-09-20 实测 B0F1902E67):
        ImageBlockATF", function(A){
          var data = { 'asin': '…',
            'colorImages': { 'initial': A.$.parseJSON('[{"hiRes":…,"thumb":…,
                            "large":…,"main":{…},"variant":"MAIN",
                            "physicalIdForMedia":"71…"}, …]') }, … }

    **为什么必须用它**: 页面另一处按配色给的 `colorImages`(`{"White":[…],"Blue":[…]}`)
    每个配色**只有 1 张**(仅该变体主图, 本页共 3 张); 真正 9 张的完整图廊只在这个
    `initial` 数组里。旧实现只匹配 `jQuery.parseJSON`, **漏掉了 `A.$.parseJSON`**,
    于是完整图廊整段拿不到 —— 这才导致上一版只能靠"整页扫 hiRes"硬凑, 把页面后部
    (A+/推荐位/广告)的图也扫了进来(实测多出 4 张非本商品图)。

    只取「条目是 dict 且带 hiRes/large」的数组, 并取**最长的一个**(完整图廊)。
    """
    best: list[dict] | None = None
    for m in _PARSEJSON_RE.finditer(html):
        raw = _unescape_js_json(m.group(1))
        if "hiRes" not in raw:
            continue
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, list):
            continue
        items = [x for x in obj
                 if isinstance(x, dict) and (x.get("hiRes") or x.get("large"))]
        if items and (best is None or len(items) > len(best)):
            best = items
    return best


_APLUS_IMG_RE = re.compile(
    r'https://m\.media-amazon\.com/images/(?:W/[^/"\'\s]+/images/)?S/'
    r'(?:aplus-media-library-service-media|aplus-media/vc|aplus-media/sc)/[^"\'\s\\]+')


def _extract_aplus_images(html: str) -> list[str]:
    """A+ 内容图（商品描述模块）—— **单独一列 `A+图片链接`，不与图廊 `图片链接` 混**。

    为什么单独成列（2026-09-20 用户要求）:
      · A+ 图常承载**规格明细** —— 实测 B04DB5E72F 的「6-Stage Vertical Filtration +
        每层材料」只在 A+ 图里, 图廊那 9 张没有; 对属性标注/复核价值很高;
      · 但它与图廊图性质不同(多为长图/拼图, 单张可达数十 MB 级像素),
        混进 `图片链接` 会挤占图廊名额、也让下游分不清"哪张是主图"。

    形态: `https://m.media-amazon.com/images/S/aplus-media-library-service-media/<uuid>.png`
    去掉 `.__CR0,0,970,600_PT0_SX970_V1___` 之类的裁剪/缩放参数取原图。
    只收 `S/aplus*` 前缀, 避免误抓页面里的 js/css/头像等资源。
    """
    out: list[str] = []
    for m in _APLUS_IMG_RE.finditer(html or ""):
        # 先归一 AVIF 包装路径(实测 2026-09-20: B0AF13FD56 的 A+ 图带
        # `/W/BW_MEDIAX_AVIF_MEASUREMENT_<id>-T2/images/` 前缀, 不归一会被
        # 正则漏掉 → A+ 列静默抓成 0 张), 再去掉 `.__CR..__` 裁剪参数
        u = _restore_full_image(m.group(0))
        base = re.sub(r"\.__[^/]*__\.(jpg|jpeg|png|gif|webp)$", r".\1", u, flags=re.I)
        base = re.sub(r"\._[A-Z]{2}[^/]*_\.(jpg|jpeg|png|gif|webp)$", r".\1", base, flags=re.I)
        if _usable_image_url(base) and base not in out:
            out.append(base)
    return out


def _extract_gallery(html: str, main_img: str, img_block: dict | None = None) -> list[str]:
    """第六步: 全图集 —— **大图优先**(尺寸参数还原, 主图在前, 去重)。

    2026-09-20 改(用户要求"都抓取大图")。采集顺序:
      ① 主图 `landingImage`(还原原图)
      ② **完整图廊** `ImageBlockATF` 的 `colorImages.initial`(`parseJSON` 数组)的
         `hiRes`/`large` —— 大图的**唯一正确来源**(实测 9 张)
      ③ 按配色的 `colorImages`(每配色仅 1 张, 作补充; 图片块缺失时也算兜底)
      ④ `#ivLargeImage` 查看器里的大图
      ⑤ 以上都没有 → 回退 `altImages` 的 `imageThumbnail` 缩略图

    ⚠ 不要整页扫 `"hiRes"`: 页面后部还有 A+/推荐位/赞助广告的图(实测会多扫进 4 张
    非本商品图), 必须限定在商品图廊数据块内。

    **为什么必须改**: 实测 `altImages` 的 `imageThumbnail` 用的是**缩略图专用资源**
    (ID 形如 `41…`/`51…`, 最大 500×500; 请求 `_AC_SL1500_` 也只有 466×464),
    而同一张商品图的大图在完整图廊的 `hiRes`(ID 形如 `71…`, 1500px 级)。
    原实现只取 `altImages`, 于是图集 = 1 张主图大图 + N 张 500px 缩略图 ——
    **图画上的小字(过滤层/材料标注等)OCR 根本读不清**。

    既有两个加固仍然保留:
    A/B 两种 `imageThumbnail` 结构都兼容, `videoThumbnail` 自然排除;
    2026-09-14 ① 只收 http(s)(扩展注入/占位/`data:`/相对路径全丢, 这类垃圾曾占掉
    上限名额); ② 主图先做尺寸还原再并入去重(否则同图留两条 URL)。
    """
    gallery: list[str] = []

    def _add(src: str) -> None:
        if not _usable_image_url(src):
            return
        restored = _restore_full_image(src)
        if restored and restored not in gallery and len(gallery) < GALLERY_MAX:
            gallery.append(restored)

    # ① 主图
    _add(main_img or "")

    # ② 完整图廊(ImageBlockATF 的 initial 数组) —— **权威来源, 有它就到此为止**
    #    实测该数组就是商品页图廊的全部大图(例: B0F1902E67 共 9 条, variant=MAIN/PT01…PT08)。
    #    **每条只取 `hiRes`**: 数组里同一张图的 `large`/`thumb` 是**缩略图资源**
    #    (ID 形如 41…, 与 hiRes 的 71… 不是同一张), 两个都取会让图集翻倍(实测 9→18)。
    #    拿到完整图廊后**不要再掺** altImages 缩略图 / ivLargeImage / colorImages:
    #    缩略图与大图是同一批图(仅分辨率低), 混进来只会让图集变长变糊;
    #    altImages 里还可能夹带视频帧(实测出现 ID 形如 `3141-…` 的视频缩略图)。
    items = _gallery_from_image_block_json(html)
    if items:
        for it in items:
            u = it.get("hiRes") or it.get("large")
            if isinstance(u, str) and u:
                _add(u)
        return gallery[:GALLERY_MAX]

    # ③ 按配色的 colorImages(每配色 1 张; 兼容 {"initial":[…]} 与 {color:[…]} 两种结构)
    ci = (img_block or {}).get("colorImages") or {}
    for arr in (ci.values() if isinstance(ci, dict) else [ci]):
        if not isinstance(arr, list):
            continue
        for item in arr:
            if not isinstance(item, dict):
                continue
            u = item.get("hiRes") or item.get("large")
            if isinstance(u, str) and u:
                _add(u)

    # ④ 图片查看器里的大图
    for m in re.finditer(r'id="ivLargeImage"[^>]*>(.{0,3000}?)</div>', html, re.S):
        im = re.search(r'<img[^>]+src="([^"]+)"', m.group(1))
        if im:
            _add(im.group(1))

    # ⑤ 缩略图兜底（仅在前四者都拿不到时）
    i = html.find('id="altImages"')
    if i == -1:
        return gallery[:GALLERY_MAX]
    window = html[i : i + 200000]

    # 方式 A: 容器(imageThumbnail)内找 <img src>(亚马逊现行: li/div 容器包裹 img)
    for m in re.finditer(r'imageThumbnail[^>]*>', window):
        # 跳过 img 标签自身的匹配(如 <img class="imageThumbnail" ...> 属方式 B 结构;
        # 若把 img 当容器向后扫描, 会把相邻的 videoThumbnail 等后续 <img> 一并扫入)
        lt = window.rfind("<", 0, m.start())
        if lt != -1 and window[lt + 1 : lt + 5].lower().startswith("img"):
            continue
        seg = window[m.end() : m.end() + 3000]
        im = re.search(r'<img[^>]+src="([^"]+)"', seg)
        if im:
            _add(im.group(1))
        if len(gallery) >= GALLERY_MAX:
            break
    # 方式 B: <img> 自身含 imageThumbnail class
    for m in re.finditer(r'<img[^>]*imageThumbnail[^>]*>', window):
        im = re.search(r'src="([^"]+)"', m.group(0))
        if im:
            _add(im.group(1))
        if len(gallery) >= GALLERY_MAX:
            break
    return gallery[:GALLERY_MAX]


_AVIF_WRAP_RE = re.compile(
    r"^https://m\.media-amazon\.com/images/W/[^/]+/images/")


def _restore_full_image(url: str) -> str:
    """缩略图尺寸参数还原 + AVIF 包装路径归一。

    ① `..._AC_US40_.jpg` -> 原图 `.jpg`（尺寸参数还原）
    ② `https://m.media-amazon.com/images/W/<token>/images/I/x.jpg`
       -> `https://m.media-amazon.com/images/I/x.jpg`

    为什么做 ②（2026-09-20）：抓取时页面上的图片地址被亚马逊套了一层
    `AVIF_MEASUREMENT_<数字>-T2` 包装（用于格式协商）。实测包装路径与规范路径
    都能下到同一张原图（1492×1476 完全一致），但**包装 token 是会话级/实验级的**，
    存进交付表后可能失效或换名；统一归一成规范路径更稳、也更便于跨轮次去重。
    """
    u = re.sub(r"\._AC_.*?\.jpg$", ".jpg", str(url or ""))
    return _AVIF_WRAP_RE.sub("https://m.media-amazon.com/images/", u)


# 非商品图片的 src 前缀(浏览器扩展注入/占位/内联数据): 一律不进图集
_JUNK_IMG_PREFIX_RE = re.compile(
    r"^(?:chrome-extension|moz-extension|safari-extension|ms-browser-extension|"
    r"data|blob|about|javascript):", re.I)


def _usable_image_url(src: str) -> bool:
    """是否是可用的商品图片地址(2026-09-14): 只收 http(s), 排除扩展注入/占位/相对路径。"""
    u = str(src or "").strip()
    if not u or u.lower() == "defaultpic":
        return False
    if _JUNK_IMG_PREFIX_RE.match(u):
        return False
    return u.startswith(("http://", "https://"))


def _clean_html(raw: str) -> str:
    """去标签 + 完整 HTML 反转义 + 合并空白。

    用 html.unescape 处理全部实体(命名如方向标记, 数字如双引号/对勾),
    避免五点/标题残留未解析实体。
    """
    import html
    text = re.sub(r"<[^>]+>", "", raw)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _aggregate_families(records: list[dict],
                        variant_texts: list[dict] | None = None) -> dict[str, dict]:
    """全量父体家族聚合(2026-09-08): 全部 ok 请求 + 成功补抓变体记录 -> 父ASIN 家族。

    家族 children 含: 父页声明的子变体 + 已知父子关系里"自己是子体"的记录自身
    (父页/兄弟页没抓到时也不丢)。用途: ①xlsx 父体汇总 sheet(全量);
    ②步骤11/12 过滤后清单的父体/子体集合。
    """
    fam_groups: dict[str, dict] = {}
    for rec in records:
        if not rec.get("ok"):
            continue
        fam = str(rec.get("parent_asin") or rec.get("request_asin") or "")
        if not fam:
            continue
        self_asin = str(rec.get("request_asin") or "")
        kids = [
            str(v) for v in (rec.get("variant_asins") or [])
            if str(v or "").strip() and str(v) != self_asin
        ]
        g = fam_groups.setdefault(fam, {"titles": [], "children": set()})
        g["titles"].append(rec.get("title", ""))
        g["children"].update(kids)
        # 记录自己是某家族的子变体(parent_asin 存在且非自身)时把自己并入 children
        if self_asin and fam != self_asin:
            g["children"].add(self_asin)
    for vr in variant_texts or []:      # 补: 补抓记录里的父子关系(个别父页未列全)
        pa = str(vr.get("parent_asin") or "").strip()
        va = str(vr.get("request_asin") or "").strip()
        if pa and va and pa != va:
            g = fam_groups.setdefault(pa, {"titles": [], "children": set()})
            g["children"].add(va)
    return fam_groups


def _step11_12_inputs(fam_groups: dict[str, dict], records: list[dict],
                      kept: list[dict]) -> tuple[list[str], list[str]]:
    """步骤11/12 输入清单(2026-09-09 用户定稿: 由"全量不过滤"改为**过滤后**口径):

    - parents(步骤11 查流量来源) = 多变体父体(原 >=2 个子变体)且**至少一个子变体
      被保留**——父体页标题杂易被判剔, 但家族仍有保留成员就值得查流量(省配额);
    - variants(步骤12 查竞品) = **保留的子体** ∪ **保留的单变体产品自身**
      (自身页无子变体的 ok 记录); 纯多变体父体即使自身 keep 也不进步骤12。
    返回 (parents, variants), 均已排序、字符串 ASIN。
    """
    kept_asins = {str(r.get("request_asin") or "").strip() for r in kept}
    kept_asins = {a for a in kept_asins if a}
    parents = sorted(
        p for p, g in fam_groups.items()
        if len(g["children"]) >= 2 and bool(g["children"] & kept_asins)
    )
    child_union = {v for g in fam_groups.values() for v in g["children"]}
    singles: set[str] = set()
    for rec in records:
        if not rec.get("ok"):
            continue
        self_asin = str(rec.get("request_asin") or "").strip()
        kids = [
            str(v) for v in (rec.get("variant_asins") or [])
            if str(v or "").strip() and str(v) != self_asin
        ]
        if self_asin and not kids and self_asin in kept_asins:
            singles.add(self_asin)
    variants = sorted((child_union & kept_asins) | singles)
    return parents, variants


class Step10Variants(BaseStep):
    number = 10
    title = "变体抓取与第二遍过滤"
    description = "FR-10: HTTP 抓取变体详情 + 第二遍过滤(FR-02.1) + 明细/清单/审计输出"

    def validate_upstream(self, ctx: StepContext) -> None:
        self._cat = ctx.config.cat
        if not self._cat:
            raise ValueError("配置缺少 CAT")
        primary = ctx.resolve_artifact(f"{self._cat}-search-asins-all.txt")
        fallback = ctx.resolve_artifact(f"{self._cat}-bestseller-newrelease-all.txt")
        if os.path.exists(primary):
            self._input_path = primary
        elif os.path.exists(fallback):
            self._input_path = fallback
        else:
            require_file(primary, f"{self._cat}-search-asins-all.txt(回退亦缺失)")

    @property
    def artifacts(self) -> list[str]:
        return [
            f"{self._cat}-results.jsonl",
            f"{self._cat}-父项变体明细.xlsx",
            f"{self._cat}-parents.txt",
            f"{self._cat}-variants.txt",
            f"{self._cat}-过滤审计.csv",
        ]

    @property
    def extra_archive_files(self) -> list[str]:
        """本轮实际生效的判定参考词(随步骤10 归档进 10_父项变体/, 不参与 DONE 校验;
        无参考词时文件不存在, 归档自动跳过)。**只写不读** —— 供事后核对过滤口径。"""
        return [f"{self._cat}-injected-keywords.txt"]

    def run(self, ctx: StepContext) -> None:
        cat = self._cat
        self.log(ctx, f"开始 FR-10 变体抓取+第二遍过滤: CAT={cat}")
        self.log(ctx, f"输入清单: {self._input_path}")
        asins = read_txt_lines(self._input_path)
        if not asins:
            self.log(ctx, "输入清单为空, 产出空结果")
            self._write_outputs(ctx, [], [], [], None)
            return
        results_path = ctx.resolve_artifact(self.artifacts[0])
        records = self._fetch_all(ctx, asins, results_path)
        filter_prompt = read_json(ctx.resolve_artifact(f"filter-{cat}-prompt.json"))
        # 步骤10 注入判定参考词后再过滤/复核(FR-02 生成时关键词体系未产出):
        # 2026-10-06 用户口径: 词源 = webui「关键词清单」面板里最终生效的**核心大词**
        # (原「步骤10 注入词」面板的功能已并入该面板; 长尾词不参与注入)。
        final_words = self._final_keywords(ctx, cat)
        before_q = len((filter_prompt or {}).get("达标关键词") or (filter_prompt or {}).get("related_title") or [])
        filter_prompt = enrich_prompt_with_keywords(filter_prompt, final_words)
        after_q = len((filter_prompt or {}).get("达标关键词") or [])
        qualify = (filter_prompt or {}).get("达标关键词") or []
        if qualify:
            # 日志完整列出实际注入词 + 落盘清单(供人工核对过滤口径)
            self.log(ctx, f"步骤10 注入判定参考词: 新增 +{after_q - before_q} 个, "
                          f"共 {len(qualify)} 个(来源=关键词清单 核心大词[{len(final_words)}])")
            self.log(ctx, f"  实际注入词: {' | '.join(qualify)}")
            inj_used = ctx.resolve_artifact(f"{cat}-injected-keywords.txt")
            write_txt_lines(inj_used, qualify)
            self.log(ctx, f"  判定参考词清单已保存: {inj_used}")
        else:
            self.log(ctx, "步骤10 无判定参考词可注入(关键词清单尚未产出), "
                          "过滤/复核使用 related_title 底稿")
        # ---- 2026-09-08 定稿(用户): 每个变体 ASIN 单独抓 + 按变体过滤, 无继承 ----
        # 1) 全量变体补抓: 对"全部 ok 请求"发现出的每个子变体 ASIN 单独抓自身页
        #    (标题/五点/全部图片), 断点续爬; 多次重试仍失败 -> 该变体剔除(不继承父体)
        variant_records = self._fetch_variant_texts(ctx, records, results_path)
        failed_variants = [r for r in variant_records if not r.get("ok")]
        # 2) 过滤对象 = 每个有自身文本的 ASIN(ok 请求记录 ∪ ok 补抓子变体, asin 去重)
        candidates = self._merge_own_records(records, variant_records)
        kept, dropped = self._filter_records(ctx, candidates, filter_prompt)
        for fr in failed_variants:
            dropped.append(dict(fr, keep=False, reason="变体页多次重试抓取失败, 剔除(无继承)"))
        # 2026-09-09 修复"复核一致率仍差(22%)": 复核的"原判定"必须取**本次首轮过滤**
        # 的判定, 不能是候选对象从磁盘带来的旧 keep(旧 run/旧提示词版本)——
        # 离线 A/B(60 条真实候选): 两轮10条/批一致100%、两轮逐条100%、
        # 逐条 vs 10条/批 98%, 判定本身稳定, 差在复核对比了过期基准。
        fresh_keep = {
            str(r.get("request_asin")): bool(r.get("keep", True))
            for r in kept + dropped
        }
        fresh_reason = {
            str(r.get("request_asin")): str(r.get("reason") or "")
            for r in kept + dropped
        }
        for c in candidates:
            a = str(c.get("request_asin") or "")
            if a in fresh_keep:
                c["keep"] = fresh_keep[a]
                if fresh_reason.get(a):
                    c["reason"] = fresh_reason[a]
        self._write_outputs(ctx, records, kept, dropped, filter_prompt, variant_records)
        self.log(
            ctx,
            f"FR-10 完成: 候选(请求+成功补抓变体){len(candidates)} "
            f"补抓失败剔除{len(failed_variants)} 保留{len(kept)} 剔除{len(dropped)}",
        )
        # 变体抓取质量审计(2026-09-08 对齐 skill build_outputs: 父汇总/变体明细/
        # 输入映射/质量检查; 请求记录 + 补抓变体记录一并审计)
        _audit_records: list[dict] = []
        _seen_asin: set[str] = set()
        for _r in list(records) + list(variant_records):
            _a = str(_r.get("request_asin") or "").strip()
            if _a and _a not in _seen_asin:
                _seen_asin.add(_a)
                _audit_records.append(_r)
        self._write_variation_audit(ctx, _audit_records)
        # 可选: 步骤完成后自动附带抽查(REVIEW_AUTO 开关, 条数 REVIEW_LIMIT)
        if str(ctx.config.get("REVIEW_AUTO", "true") or "").strip().lower() in (
            "1", "true", "yes", "on",
        ):
            limit = max(0, int(ctx.config.get("REVIEW_LIMIT", 0) or 0))
            min_rate = max(0, int(ctx.config.get("REVIEW_MIN_RATE", 90) or 0))
            policy = str(ctx.config.get("REVIEW_POLICY", "keep_original") or "keep_original")
            review_workers = max(1, int(ctx.config.get("FILTER_JUDGE_WORKERS", 3) or 3))
            # 复核对象 = 全部候选(请求 + 成功补抓变体, 每个 ASIN 自身文本)
            ok_candidates = [r for r in candidates if r.get("ok")]
            stats = review_filter_results(
                ctx.llm(),
                ctx.config.market,
                ok_candidates,
                filter_prompt,
                limit=limit,
                out_path=ctx.resolve_artifact(f"{cat}-复核-过滤审计.csv"),
                disagree_path=ctx.resolve_artifact(f"{cat}-复核-不一致项.csv"),
                log=lambda text: self.log(ctx, text),
                workers=review_workers,
            )
            # 改判策略: 不一致项按配置处理(就地改 ok_candidates 的 keep)
            changed = apply_review_policy(ok_candidates, stats["disagree_rows"], policy)
            if changed:
                kept2 = [r for r in ok_candidates if r.get("keep", True)]
                dropped2 = [r for r in ok_candidates if not r.get("keep", True)]
                for fr in failed_variants:
                    dropped2.append(dict(fr, keep=False,
                                         reason="变体页多次重试抓取失败, 剔除(无继承)"))
                self._write_outputs(ctx, records, kept2, dropped2,
                                    filter_prompt, variant_records)
            # 有效一致率: keep_original(不改判, 需人工处理不一致) = 复核原始一致率;
            # follow_review/drop_on_disagree(自动改判) = 不一致项已被策略消解/剔除, 门槛视为通过
            gate_rate = stats["rate"] if policy == "keep_original" else 1.0
            self.log(
                ctx,
                f"自动抽查完成: 复核{stats['sample']}条 一致{stats['agree']} "
                f"不一致{stats['disagree']} 一致率{stats['rate']:.1%}"
                f"{'' if policy == 'keep_original' else ' → 自动改判策略, 门槛通过'} "
                f"策略={policy}(改判{changed}条) 门槛={min_rate}% "
                f"(REVIEW_AUTO=true REVIEW_LIMIT={limit})",
            )
            # 一致率门槛: 仅 keep_original(不一致留待人工)低于门槛时阻断;
            # 自动改判策略(follow_review/drop_on_disagree)已处理不一致, 不阻断
            # (仅真实模式; dry-run 为 Mock 伪随机判定, 不阻断)
            if (
                not ctx.dry_run
                and policy == "keep_original"
                and stats["sample"] > 0
                and gate_rate * 100 < min_rate
            ):
                raise StepDataError(
                    f"过滤复核一致率 {stats['rate']:.1%} 低于门槛 {min_rate}%, "
                    f"不一致 {stats['disagree']} 条见 {cat}-复核-不一致项.csv, "
                    f"人工复核后重跑步骤 10"
                )

    def _auto_core(self, ctx: StepContext, cat: str) -> list[str]:
        """**自动态**核心大词(与步骤9 的"自动核心词"完全同一批口径)。

        - **US 站**: FR-07 对**全部候选词**判定为"达标"的词全集
          (`<cat>-screen-results.json` 中 verdict=达标; step04 核心分区=全部候选,
          达标不限个数)——与 step08 报告核心分析 / step09 搜索收集口径一致,
          不再用 search-asins.json 键(那只是 达标核心∩搜索过的子集, 会漏 grinder 等);
        - **非 US 站(2026-09-15 用户口径)**: 不套 SCREEN_REQUIREMENT, 直接取
          **站内月搜索量前 NON_US_QUALIFIED_TOP(5) 个**候选词。
        """
        # 自动: US 站用 FR-07 达标全集 / 非 US 站用站内月搜索量前 5
        if not _is_us_site(
            site=str(ctx.config.get("SITE", "") or ""),
            market=str(ctx.config.market or ""),
            cat=cat,
        ):
            data = read_json(ctx.resolve_artifact(f"{cat}-amazon-keyword-data.json"), default=[])
            picked = [w.lower() for w in pick_top_volume_candidates(data, NON_US_QUALIFIED_TOP)]
            if picked:
                self.log(ctx, f"非美国站: 自动核心词 = 站内月搜索量前 {len(picked)} 个候选词"
                              f"(与步骤9 达标核心同口径): " + "、".join(picked))
                return picked
            self.log(ctx, "非美国站: 候选词缺站内月搜索量数据, 自动核心词回退到 FR-07 达标结果")
        screen = read_json(ctx.resolve_artifact(f"{cat}-screen-results.json"), default=[])
        if isinstance(screen, list):
            out = []
            for rec in screen:
                if not isinstance(rec, dict):
                    continue
                if str(rec.get("verdict") or "").strip() == "达标":
                    w = str(rec.get("keyword") or "").strip().lower()
                    if w and w not in out:
                        out.append(w)
            if out:
                return out
        return []

    def _screen_verdict(self, ctx: StepContext, cat: str) -> dict[str, str]:
        """`{关键词: FR-07 判定}`(供三态门槛口径复用)。"""
        out: dict[str, str] = {}
        screen = read_json(ctx.resolve_artifact(f"{cat}-screen-results.json"), default=[])
        if isinstance(screen, list):
            for rec in screen:
                if not isinstance(rec, dict):
                    continue
                w = str(rec.get("keyword") or "").strip().lower()
                if w:
                    out[w] = str(rec.get("verdict") or "").strip()
        return out

    def _final_keywords(self, ctx: StepContext, cat: str) -> list[str]:
        """步骤10 注入的判定参考词 = 「关键词清单」里最终生效的**核心大词** + `[仅注入]` 词。

        2026-10-06 用户口径: 原「步骤10 注入词」面板已下线, 它的功能**并入
        「关键词清单」面板的核心大词列** —— 原理与原注入词完全一致:
          ① 存在人工核心清单 `filter-<CAT>-core-words.txt` 的 `[核心大词]` 分区
             (在面板里勾选/手填后保存) → 就用这份清单(可删掉不想要的达标词,
               也可加不达标词或手填新词), **不再看 FR-07 判定**;
          ② 没有人工核心清单 → 自动: **US 站** = FR-07 判"达标"的词全集;
             **非 US 站** = 站内月搜索量前 NON_US_QUALIFIED_TOP(5) 个候选词;
          ③ 人工停止标记只影响步骤9(不抓取), 此处核心词仍回落自动口径, 不会变空。

        **额外可加"仅注入词"**(同一文件的 `[仅注入]` 分区, 面板上独立一行):
        `plan.inject_words` = 核心大词 + 仅注入词 —— 仅注入词**只参与步骤10 判定,
        不被步骤9 抓取**、也不进门槛判定。
        **长尾词不参与注入**(2026-10-06 用户明确): 长尾词只用于步骤9 抓取与报告。
        `resolve()` 读不到文件一律退化为自动态, 不抛异常。
        """
        us_site = _is_us_site(
            site=str(ctx.config.get("SITE", "") or ""),
            market=str(ctx.config.market or ""),
            cat=cat,
        )
        try:
            gate = max(1, int(ctx.config.get("GATE_MIN_QUALIFIED", 2) or 2))
        except (TypeError, ValueError):
            gate = 1
        plan = resolve_keywords(
            ctx.paths.cat_root, cat,
            auto_core=self._auto_core(ctx, cat),
            auto_longtail=[],                 # 长尾词不参与注入(只用于步骤9 抓取/报告)
            screen_verdict=self._screen_verdict(ctx, cat),
            gate=gate,
            gate_applies=us_site,
        )
        self.log(ctx, "步骤10 判定参考词源(取「关键词清单」的核心大词): " + plan.source_text())
        self.log(ctx, "  " + plan.inject_text())
        out: list[str] = []
        for w in plan.inject_words:
            wl = str(w).strip().lower()
            if wl and wl not in out:
                out.append(wl)
        return out

    # ---- 2026-09-09 用户定稿: 变体抓取重试流程固定(不再无限重试/不再递增退避) ----
    #  * 每轮之间固定冷却 60 秒(轮轮相同, 不逐轮递增);
    #  * 直接请求阶段 = 首轮 + FETCH_STALL_ROUNDS(默认3)轮重试; 用尽后残余失败
    #    **不论多少全部**转本机浏览器补抓(原 BROWSER_FALLBACK_THRESHOLD 阈值取消);
    #  * 浏览器每 ASIN 最多试 _BROWSER_MAX_TRIES=1 次(用户 2026-09-09 指定写死 1), 仍失败 → 舍弃该变体
    #    (ok=False 留审计记录, 不进候选), 随后照常进入过滤阶段;
    #  * **2026-09-16 用户追加**: "如果本机浏览器补抓后还有失败的可以再进行一轮本机浏览器补抓"
    #    → 浏览器补抓改为**多轮循环**(BROWSER_FALLBACK_ROUNDS 默认 2, 轮间冷却
    #    BROWSER_ROUND_PAUSE 默认 30s): 每轮只补"仍未解决"的 ASIN, 全解决或轮数用尽即停,
    #    仍失败者才按舍弃处理。**每轮结果都必须写回台账**(见 `_fetch_all` 里 prune+append 的注释:
    #    漏掉剪除会让整轮补抓成果被 dedup 静默丢弃, 2026-09-16 实际发生过)。
    #  * Page Not Found(404/标题判定) = 终态 skipped_page_not_found/not_found 永久跳过。
    #  * **2026-09-16 用户定稿**: 变体数超过上限(1..100)也 = 终态
    #    `skipped_too_many_variants`(页面真实存在且解析成功, 只是超本程序门限) ——
    #    不反复重抓、不计入"网络失败/未解决", 但**保留解析器取证字段**并在日志列出 ASIN,
    #    便于人工抽页面确认"是真实大变体家族, 还是解析器把相似商品算进来了"。
    #  * 单轮内每 ASIN 尝试次数(与轮数独立): 默认按剩余量动态(>30→2 / ≤30→4), 可由
    #    FETCH_ASIN_TRIES 显式固定(如 1)压低单轮请求量(2026-09-09 新增可选配置)。
    _ROUND_PAUSE_DEFAULT = 60.0      # 轮间固定冷却秒(每轮一样)
    _BROWSER_MAX_TRIES = 1           # 浏览器补抓单 ASIN 最多尝试次数(用户 2026-09-09 写死=1)
    # 浏览器补抓轮间冷却(2026-09-16 用户要求"补抓后还有失败的可以再进行一轮补抓" → 补抓改多轮)
    _BROWSER_ROUND_PAUSE_DEFAULT = 30.0
    # ---- 2026-09-16 内化(用户定调: "太多参数了, 我只需要个兜底的就行") ----
    # 直连重试/补抓的细节**不再要求用户配置**, 内部取值 = 现有 10 个真实类目的一致口径(UK 实测最优):
    #   直连阶段 = 首轮 + 1 轮重试(轮间 60s) → 残余全部转本机浏览器补抓(2 轮, 轮间 30s)
    # 历史配置键(FETCH_STALL_ROUNDS / FETCH_ASIN_TRIES / BROWSER_FALLBACK_ROUNDS / BROWSER_ROUND_PAUSE)
    # **仍兼容** —— 配置里写了就以配置为准(避免"删了键, 行为悄悄变")。
    _STALL_ROUNDS_DEFAULT = 1        # 直连阶段固定重试轮数(首轮之外的轮数; 原默认 3)
    _ASIN_TRIES_DEFAULT = 1          # 单轮内每 ASIN 尝试次数(原为动态: >30→2 / ≤30→4)
    _BROWSER_ROUNDS_DEFAULT = 2      # 浏览器补抓轮数(无头)
    # 有头兜底轮数(2026-09-29 用户要求): 无头补抓轮用尽后仍有未解决 → 开可见窗口再补 N 轮。
    # 依据(用户 2026-09-29 实测/要求): "如果无头的无效再使用有头的"; 无头 Chrome 比桌面 Chrome
    # 更容易被亚马逊识别成机器人(同一 IP/Profile 下, 桌面 Chrome 能正常打开商品页, 无头吃验证码页),
    # 所以最后一招是**可见窗口**。窗口由本程序自己开关(这一轮跑完就关), 期间请勿手动关闭。
    _HEADED_ROUNDS_DEFAULT = 1

    @staticmethod
    def _fallback_mode(ctx: StepContext) -> str:
        """兜底开关(全局唯一需要关心的键): `FALLBACK` = auto(默认) / off。

        auto: 直连阶段(首轮 + 1 轮重试)用尽后, 残余失败**全部转本机浏览器补抓**(补抓 2 轮);
        off : **完全不用浏览器** —— 只直连, 抓不到就如实记为失败(适合不想弹浏览器窗口/纯离线核对)。
        步骤01 用的是同一个键(`STEP01_BROWSER_FALLBACK` 作为历史专用键优先级更高)。
        """
        raw = str(ctx.config.get("FALLBACK", "auto") or "auto").strip().lower()
        if raw in ("off", "false", "0", "no", "none", "disable", "disabled"):
            return "off"
        return "auto"

    def _log_round_summary(self, ctx: StepContext, label: str, results_path: str) -> None:
        """单轮/阶段汇总日志: 成功与失败个数 + 全部失败变体 ASIN(用户要求可视)。

        - label: 如 "第 2 轮完成" / "浏览器补抓后" / "抓取完成";
        - 失败 = ok=False 且非终态(终态 Page Not Found / 变体数越界分别单独计数并列 ASIN)。
        """
        ok_count = fail_count = pnf_count = too_many_count = 0
        failed_asins: list[str] = []
        pnf_asins: list[str] = []
        too_many_asins: list[str] = []
        for r in read_jsonl(results_path):
            status = str(r.get("status") or "")
            if r.get("ok"):
                ok_count += 1
            elif status in ("skipped_page_not_found", "not_found"):
                pnf_count += 1
                pnf_asins.append(str(r.get("request_asin") or ""))
            elif status == "skipped_too_many_variants":
                too_many_count += 1
                too_many_asins.append(str(r.get("request_asin") or ""))
            else:
                fail_count += 1
                failed_asins.append(str(r.get("request_asin") or ""))
        self.log(
            ctx,
            f"汇总[{label}] 成功 {ok_count} / 失败 {fail_count}"
            f"{f' / PageNotFound终态 {pnf_count}' if pnf_count else ''}"
            f"{f' / 变体数越界终态 {too_many_count}' if too_many_count else ''}",
        )
        if failed_asins:
            # 分块展示, 避免单行过长截断(每块 50 个)
            for i in range(0, len(failed_asins), 50):
                chunk = failed_asins[i:i + 50]
                self.log(
                    ctx,
                    f"  失败变体ASIN({i + 1}-{i + len(chunk)}/{len(failed_asins)}): "
                    + ", ".join(a for a in chunk if a),
                )
        if pnf_asins:
            self.log(ctx, f"  终态PageNotFound: {', '.join(a for a in pnf_asins if a)}")
        if too_many_asins:
            # 2026-09-16 用户要求: 这类被本程序门限拒收的父体必须能在日志里直接看到 ASIN,
            # 便于人工抽一个页面确认"是真实大变体家族还是解析器把相似商品算进来了"。
            self.log(
                ctx,
                f"  终态变体数越界(>100, 永久跳过): {', '.join(a for a in too_many_asins if a)}",
            )

    def _fetch_all(self, ctx: StepContext, asins: list[str], results_path: str) -> list[dict]:
        """多轮抓取 + 残余全转浏览器(2026-09-09 用户定稿固定流程)。

        首轮抓全部 → 失败子集固定重试 `FETCH_STALL_ROUNDS`(默认 3)轮, 每轮之间
        固定冷却 `FETCH_ROUND_PAUSE`(默认 60s, 不再递增) → 重试轮用尽后仍失败
        (非终态)的**全部**转本机浏览器补抓, 浏览器每 ASIN 最多试 1 次(用户 2026-09-09 写死), 仍失败
        舍弃(ok=False 留审计); 终态(`_TERMINAL_STATUSES`: Page Not Found / 变体数越界)永久跳过。
        **2026-09-16 用户追加**: 补抓改**多轮**(`BROWSER_FALLBACK_ROUNDS` 默认 2, 轮间冷却
        `BROWSER_ROUND_PAUSE` 默认 30s) —— 补抓后仍有失败的会自动再补一轮, 每轮结果都写回台账。
        """
        stall_rounds = max(1, int(ctx.config.get("FETCH_STALL_ROUNDS",
                                                 self._STALL_ROUNDS_DEFAULT)
                                  or self._STALL_ROUNDS_DEFAULT))
        max_rnd = 1 + stall_rounds   # 首轮 + 固定重试轮数(写死上限, 防死循环)
        raw_pause = ctx.config.get("FETCH_ROUND_PAUSE")
        pause = self._ROUND_PAUSE_DEFAULT if raw_pause is None else max(0.0, float(raw_pause))
        terminal = _TERMINAL_STATUSES
        pending = list(asins)
        rnd = 0
        while True:
            rnd += 1
            if rnd > 1:
                self.log(
                    ctx,
                    f"第 {rnd} 轮重试: 待重抓 {len(pending)} 个失败 ASIN, "
                    f"间隔 {pause:.0f} 秒(固定冷却)",
                )
                time.sleep(pause)
            records = self._fetch_round(ctx, pending, results_path)
            skipped = {
                r.get("request_asin")
                for r in records
                if not r.get("ok") and str(r.get("status")) in terminal
            }
            failed = {
                str(r.get("request_asin")) for r in records
                if not r.get("ok") and str(r.get("status")) not in terminal
            }
            failed = {a for a in failed if a}
            self._log_round_summary(ctx, f"第 {rnd} 轮完成", results_path)
            if not failed:
                if skipped:
                    self.log(ctx, f"{len(skipped)} 个终态跳过(Page Not Found/变体数越界, 永久不重试)")
                self.log(ctx, f"全部 {len(records)} 个 ASIN 抓取完成")
                break
            if rnd >= max_rnd:
                self.log(
                    ctx,
                    f"直接请求固定重试 {stall_rounds} 轮后仍失败 {len(failed)} 个, "
                    f"全部转本机浏览器补抓(每 ASIN 最多 {self._BROWSER_MAX_TRIES} 次)",
                )
                break
            pending = sorted(failed)
        # 残余(非终态)无论多少 → 全部转本机浏览器补抓(用户定稿: 不再限 ≤8);
        # 2026-09-16 用户追加要求: "如果本机浏览器补抓后还有失败的可以再进行一轮本机浏览器补抓"
        # → 补抓改为**多轮**(BROWSER_FALLBACK_ROUNDS, 默认 2), 每轮只补仍未解决的 ASIN,
        # 轮间冷却 BROWSER_ROUND_PAUSE(默认 30s), 直到没有未解决项或轮数用尽。
        if not ctx.dry_run:
            fallback_on = self._fallback_mode(ctx) != "off"
            rounds = max(1, int(ctx.config.get("BROWSER_FALLBACK_ROUNDS",
                                               self._BROWSER_ROUNDS_DEFAULT)
                                or self._BROWSER_ROUNDS_DEFAULT))
            raw_gap = ctx.config.get("BROWSER_ROUND_PAUSE")
            gap = (self._BROWSER_ROUND_PAUSE_DEFAULT if raw_gap is None
                   else max(0.0, float(raw_gap)))
            for br in range(1, rounds + 1):
                leftover = sorted(
                    str(r.get("request_asin")) for r in read_jsonl(results_path)
                    if not r.get("ok") and str(r.get("status")) not in terminal
                )
                if not leftover:
                    if br > 1:
                        self.log(ctx, f"浏览器补抓第 {br} 轮无需进行: 已无未解决 ASIN")
                    break
                if not fallback_on:
                    self.log(
                        ctx,
                        f"FALLBACK=off: 不启用浏览器兜底, {len(leftover)} 个未解决 ASIN "
                        f"如实记为失败(要兜底请把配置改回 FALLBACK: auto)",
                    )
                    break
                if br == 1:
                    self.log(ctx, f"残余 {len(leftover)} 个失败 ASIN 全部转本机浏览器补抓"
                                  f"(最多 {rounds} 轮, 每 ASIN 每轮试 {self._BROWSER_MAX_TRIES} 次)…")
                else:
                    self.log(ctx, f"浏览器补抓第 {br}/{rounds} 轮: 仍有 {len(leftover)} 个未解决, "
                                  f"冷却 {gap:.0f} 秒后再补抓一轮")
                    if gap:
                        time.sleep(gap)
                label = f"浏览器补抓第 {br} 轮"
                try:
                    browser_recs = self._browser_fallback(ctx, leftover)
                except Exception as exc:
                    self.log(ctx, f"{label}不可用/失败: {exc}(该轮未解决项按舍弃处理)")
                    if br == 1:
                        break          # 连会话都起不来, 再补一轮也没意义
                    continue
                if not self._apply_browser_round(ctx, results_path, browser_recs, label):
                    break
                self._log_round_summary(ctx, f"{label}后", results_path)

            # ---- 有头兜底(2026-09-29 用户要求: "如果无头的无效再使用有头的") ----
            # 无头 Chrome 比桌面 Chrome 更容易被识别成机器人 → 无头轮用尽后, 再用**可见窗口**
            # 补 N 轮(BROWSER_HEADED_ROUNDS 默认 1, 0=关闭)。每轮只补"仍未解决"的 ASIN,
            # 全解决 / 轮数用尽即停; 窗口由程序开关, 期间请勿手动关闭。
            raw_headed = ctx.config.get("BROWSER_HEADED_ROUNDS", self._HEADED_ROUNDS_DEFAULT)
            try:
                headed_rounds = max(0, int(raw_headed))
            except (TypeError, ValueError):
                # 键留空(None/空串)按默认算; 显式写 0 才是"关闭有头兜底"
                headed_rounds = self._HEADED_ROUNDS_DEFAULT
            for hb in range(1, headed_rounds + 1):
                leftover = sorted(
                    str(r.get("request_asin")) for r in read_jsonl(results_path)
                    if not r.get("ok") and str(r.get("status")) not in terminal
                )
                if not leftover:
                    self.log(ctx, "浏览器补抓(有头兜底)无需进行: 已无未解决 ASIN")
                    break
                if not fallback_on:
                    break          # FALLBACK=off: 连无头都不用, 有头更不该弹窗
                hlabel = f"浏览器补抓(有头)第 {hb}/{headed_rounds} 轮"
                self.log(ctx, f"浏览器补抓(有头兜底)第 {hb}/{headed_rounds} 轮: 仍有 "
                              f"{len(leftover)} 个未解决 → 打开**可见**浏览器窗口再补一轮"
                              f"(窗口由程序自动开关, 请勿手动关闭)")
                if hb > 1 and gap:
                    time.sleep(gap)
                try:
                    browser_recs = self._browser_fallback(ctx, leftover, headed=True)
                except Exception as exc:
                    self.log(ctx, f"{hlabel}不可用/失败: {exc}(该轮未解决项按舍弃处理)")
                    break
                if not self._apply_browser_round(ctx, results_path, browser_recs, hlabel):
                    break
                self._log_round_summary(ctx, f"{hlabel}后", results_path)
            # 收尾口径: 多轮结束后还剩多少未解决(用户要能一眼看出补抓到底救回多少)
            unresolved = [
                str(r.get("request_asin")) for r in read_jsonl(results_path)
                if not r.get("ok") and str(r.get("status")) not in terminal
            ]
            if unresolved:
                stage = f"无头 {rounds} 轮" + (f" + 有头 {headed_rounds} 轮" if headed_rounds else "")
                self.log(ctx, f"浏览器补抓({stage})后仍有 {len(unresolved)} 个未解决"
                              f"(已按舍弃处理, 见上方失败清单)")
        self._log_round_summary(ctx, "抓取完成", results_path)
        return read_jsonl(results_path)

    def _apply_browser_round(self, ctx: StepContext, results_path: str,
                             browser_recs: list[dict], label: str) -> bool:
        """把一轮浏览器补抓的结果写回台账; 返回 False = 该轮没返回任何记录(调用方应停止后续轮)。

        **为什么必须"先剪除旧失败行再 append"(2026-09-16 实测 bug)**: 这些 ASIN 在台账里
        **本来就有失败记录**, 而 `append_jsonl(dedup_key=…)` 的语义是"已存在的同一主体**跳过**"
        (keep-first, **从不覆盖**) —— 直接 append 会让补抓的成功记录**一条都写不进去**。
        实测 `Chopping Boards-UK-202608`: 补抓 904 次成功 903 次(日志逐条 `浏览器补抓 X -> ok`),
        台账却仍停在 304/579, 白跑 1.5 小时且步骤10 质量检查报"894 未解决/需复核"。
        处理与 `_fetch_round` 完全一致: **先剪除这些 ASIN 的旧失败行, 再写入新记录**。
        """
        keys = {str(r.get("request_asin") or "") for r in browser_recs}
        keys.discard("")
        if not keys:
            self.log(ctx, f"⚠ {label}未返回任何记录, 台账保持原样(不剪除旧失败行)")
            return False
        self._prune_failed(results_path, keys)
        added, skipped = append_jsonl(results_path, browser_recs, dedup_key="request_asin")
        ok_n = sum(1 for r in browser_recs if r.get("ok"))
        self.log(ctx, f"{label}结果已写回台账: 追加 {added} 条(其中 ok {ok_n} 条)"
                      f", 剪除旧失败行 {len(keys)} 个 ASIN"
                      f"{f', 跳过 {skipped} 条' if skipped else ''}")
        if skipped:
            self.log(ctx, f"⚠ 有 {skipped} 条补抓记录因同 ASIN 已存在被跳过"
                          f"(正常应为 0, 说明剪除未生效, 请检查 dedup 键)")
        return True

    def _browser_fallback(self, ctx: StepContext, asins: list[str],
                          *, headed: bool = False) -> list[dict]:
        """残余失败 ASIN 用本机浏览器打开商品页补抓(2026-09-09 用户定稿)。

        每 ASIN 最多尝试 `_BROWSER_MAX_TRIES`(1)次(依次换 URL 形态), 成功或**终态**
        (Page Not Found / 变体数越界, 见 `_TERMINAL_STATUSES`)即停; 1 次仍失败 → ok=False
        记录(舍弃该变体, 留审计, 不进候选)。返回记录列表(source='local_chrome')。

        `headed=True`(2026-09-29): 用**可见窗口**跑这一轮 —— 无头 Chrome 更容易被亚马逊识别,
        所以无头轮用尽后由调用方再补一轮有头(见 `_fetch_all` 的有头兜底段)。
        """
        max_tries = self._BROWSER_MAX_TRIES
        now = self.now()
        out: list[dict] = []
        try:
            with ctx.browser("amazon", headed=headed) as browser:
                page = browser.new_page()
                for asin in asins:
                    rec = None
                    first_url = url_candidates(ctx.config.market, asin)[0]
                    tried = 0
                    evidence: dict = self._page_evidence("", first_url, page)
                    for url in url_candidates(ctx.config.market, asin):
                        if tried >= max_tries:
                            break
                        tried += 1
                        try:
                            browser.goto(page, url)
                            time.sleep(2.0)          # 页面渲染缓冲(原生等待接口不通用)
                            text = page.content() or ""
                            evidence = self._page_evidence(text, url, page)
                            rec = self._build_ok_record(
                                ctx, asin, text, now, first_url, "", "local_chrome",
                            )
                            if rec.get("ok") or _is_terminal(rec.get("status")):
                                break
                            rec = None              # 页面可打开但内容无效 -> 算一次失败
                            self.log(ctx, f"浏览器补抓 {asin} 第 {tried}/{max_tries} 次内容无效"
                                          f"({evidence.get('blocked_kind')}, {evidence.get('html_length')} 字节), 换形态")
                        except Exception as exc:
                            rec = None
                            evidence = {"final_url": url, "html_length": 0, "page_title": "",
                                        "blocked_kind": "error",
                                        "error_detail": f"{type(exc).__name__}: {exc}"[:200]}
                            self.log(ctx, f"浏览器补抓 {asin} 第 {tried}/{max_tries} 次失败({exc}), 换形态")
                    if rec is None:
                        rec = self._failed_record(asin, "failed", now)
                        rec["error"] = f"浏览器补抓 {max_tries} 次失败, 舍弃该变体"
                        rec["source"] = "local_chrome"
                        rec.update(evidence)        # 失败取证(2026-09-29): 让"为什么失败"可事后归因
                    out.append(rec)
                    self.log(ctx, f"浏览器补抓 {asin} -> {rec['status']}"
                                  + (f"[{rec.get('blocked_kind')}]" if rec.get("blocked_kind") else ""))
        except Exception as exc:
            self.log(ctx, f"浏览器补抓会话失败: {exc}")
            for asin in asins:
                rec = self._failed_record(asin, "failed", now)
                rec["error"] = f"浏览器会话失败: {exc}"
                rec["source"] = "local_chrome"
                rec["blocked_kind"] = "session_error"
                rec["error_detail"] = f"{type(exc).__name__}: {exc}"[:200]
                out.append(rec)
        return out

    @staticmethod
    def _page_evidence(text: str, url: str, page=None) -> dict:
        """浏览器补抓的**页面取证**(2026-09-29): 失败行也留下"看到了什么", 让失败可归因。

        事故背景: `Dog Dryer-US-202608` 一轮里 311 个补抓失败, 台账只有一句
        `浏览器补抓 1 次失败, 舍弃该变体` —— `html_length`/`final_url`/`title` 全是 None,
        连"是验证码页、登录页还是超时"都查不出来, 只能重跑一遍去猜。

        取证字段(写进失败记录, 与成功记录的字段同名同义):
          `final_url`(最终 URL, 优先浏览器自报) / `html_length`(HTML 字节数) /
          `page_title`(页面 `<title>`) / `blocked_kind`
          (`captcha` 验证码页 / `signin` 登录页 / `empty` 空白页 / `content` 有内容但不符合商品页门槛 /
           `error` 打开就抛异常)。
        ⚠️ 页面标题**只写 `page_title` 这个取证字段**, 绝不写进 `title`(那是"商品标题"业务字段) ——
        免得验证码页的 "Amazon.com" 被下游当成商品名。
        """
        text = text or ""
        low = text.lower()
        if any(m in low for m in ("captcha", "robot check", "automated access", "verify you are human")):
            kind = "captcha"
        elif "ap/signin" in low or "signin" in low:
            kind = "signin"
        elif not text.strip():
            kind = "empty"
        else:
            kind = "content"
        m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
        title = re.sub(r"\s+", " ", m.group(1)).strip()[:200] if m else ""
        final_url = str(url or "")
        try:
            if page is not None and getattr(page, "url", None):
                final_url = str(page.url)
        except Exception:                              # noqa: BLE001 — 取证失败不影响主流程
            pass
        return {"final_url": final_url, "html_length": len(text),
                "page_title": title, "blocked_kind": kind}

    def _fetch_round(self, ctx: StepContext, asins: list[str], results_path: str) -> list[dict]:
        """单轮并发抓取(2026-09-08 对齐 skill: 按剩余量动态并发 + 动态尝试次数)。

        终态跳过(ok / `_TERMINAL_STATUSES`: Page Not Found / 变体数越界)断点续爬不重抓;
        其余失败剪除旧行后重抓(允许成功覆盖); 写入用锁保护。
        """
        existing = read_jsonl(results_path)
        done = {
            r.get("request_asin") for r in existing
            if r.get("ok") or _is_terminal(r.get("status"))
        }
        pending_fail = {
            r.get("request_asin") for r in existing
            if not r.get("ok") and not _is_terminal(r.get("status"))
        }
        retry_keys = {a for a in asins if a in pending_fail}
        if retry_keys:
            self._prune_failed(results_path, retry_keys)
        pending = [a for a in asins if a not in done]
        if not pending:
            return read_jsonl(results_path)
        cap = max(1, int(ctx.config.get("FETCH_WORKERS", 8) or 8))
        workers = skill_worker_count(len(pending), cap)
        # 单轮内每 ASIN 尝试次数: 显式配置 FETCH_ASIN_TRIES(如 1)则固定, 否则按剩余量动态
        # (>30 → 2 次, ≤30 → 4 次, 见 core/amazon_variation.attempts_for)。注意与
        # FETCH_STALL_ROUNDS(重试轮数)是两个独立维度。
        attempts = attempts_for(len(pending), ctx.config.get("FETCH_ASIN_TRIES",
                                                            self._ASIN_TRIES_DEFAULT))
        lo = ctx.config.get("REQUEST_MIN_INTERVAL", 1.5)
        hi = ctx.config.get("REQUEST_MAX_INTERVAL", 3.0)
        self.log(
            ctx,
            f"待抓 {len(pending)} 个 ASIN, 并发 {workers} 线程(动态), "
            f"每 ASIN 尝试 {attempts} 次, 每线程独立 {lo}-{hi} 秒间隔",
        )
        lock = threading.Lock()
        long_every = max(0, int(ctx.config.get("FETCH_LONG_EVERY", 25) or 0))
        long_lo = float(ctx.config.get("FETCH_LONG_MIN", 8) or 8)
        long_hi = float(ctx.config.get("FETCH_LONG_MAX", 15) or 15)
        local = threading.local()

        def work(asin: str) -> None:
            # 每线程计数, 每 long_every 条加一次长间隔(对齐实测稳定方案: 每25条 +8-15秒)
            count = getattr(local, "n", 0)
            if long_every > 0 and count > 0 and count % long_every == 0:
                extra = random.uniform(long_lo, long_hi)
                self.log(ctx, f"长间隔冷却 {extra:.1f} 秒(已抓 {count} 条)")
                time.sleep(extra)
            local.n = count + 1
            http = HttpFetcher(
                ctx.config,
                cookie=str(ctx.config.get("COOKIE", "") or "") or None,
            )
            try:
                rec = self._fetch_one(ctx, http, asin, attempts=attempts)
            except Exception as exc:
                rec = self._failed_record(asin, f"exception:{type(exc).__name__}", self.now())
            with lock:
                append_jsonl(results_path, [rec], dedup_key="request_asin")
            self.log(ctx, f"已抓取 {asin} ok={rec['ok']} status={rec['status']}")

        if workers <= 1 or len(pending) <= 1:
            for asin in pending:
                work(asin)
        else:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                list(executor.map(work, pending))
        return read_jsonl(results_path)

    def _prune_failed(self, path: str, keys: set) -> None:
        """剪除指定失败记录行, 使重试成功记录可写入(同 FR-01 处理)。"""
        records = read_jsonl(path)
        kept = [r for r in records if r.get("request_asin") not in keys]
        if len(kept) == len(records):
            return
        content = "".join(
            json.dumps(r, ensure_ascii=False) + "\n" for r in kept
        )
        atomic_write_text(path, content)

    def _fetch_one(self, ctx: StepContext, http, asin: str, attempts: int = 4) -> dict:
        """抓取单个 ASIN 详情并解析变体关系(2026-09-08 对齐 skill 全套)。

        - 依次尝试 4 种商品页形态(dp / gp/product?th=1&psc=1 / dp?th=1&psc=1 /
          dp?language=en_US), 单个 ASIN 最多 attempts 次;
        - Page Not Found(含标题判定) → 终态 skipped_page_not_found, 永久跳过;
        - blocked(Robot/CAPTCHA, 即使 HTTP200) / http 失败 → 失败可重试;
        - 父 ASIN 格式 / HTML>=50000 不满足 → parse_failed 拒收(保留富记录, 可重试);
        - 变体数不在 1..100 → 终态 skipped_too_many_variants(保留富记录, 不再重抓);
        - 记录附带 skill 审计字段(父体识别方式/来源映射/扩展变体/声明数/HTML长度)。
        """
        now = self.now()
        market = ctx.config.market
        urls = url_candidates(market, asin)
        if ctx.dry_run:
            resp = http.get(urls[0], retries=1)
            title, bullets = parse_product_html(resp.text or "")
            parent, variants, labels, images = self._mock_variants(asin)
            return {
                "request_asin": asin, "ok": True, "status": "ok", "source": "dry_run",
                "source_url": urls[0], "final_url": resp.url or urls[0],
                "parent_asin": parent, "parent_method": "mock",
                "parent_candidates": [], "variant_asins": variants,
                "variant_count": len(variants), "variant_labels": labels,
                "expected_variation_count": None, "map_counts": {},
                "map_asins": {}, "fallback_variant_asins": [],
                "extended_variant_asins": [], "extended_variant_sources": {},
                "html_length": len(resp.text or ""), "images": images,
                "title": title, "bullets": bullets, "fetched_at": now,
            }
        last_err = ""
        last_reject: dict | None = None
        for attempt in range(1, max(1, attempts) + 1):
            url = urls[(attempt - 1) % len(urls)]
            resp = http.get(url, retries=1)
            if resp.status_class == "not_found":
                rec = self._failed_record(asin, "skipped_page_not_found", now)
                rec["error"] = "Amazon 404 / Page Not Found, 永久跳过"
                rec["source_url"] = urls[0]
                rec["final_url"] = resp.url
                return rec
            if resp.status_class != "ok":
                last_err = f"HTTP 状态 {resp.status_class}"
                continue
            rec = self._build_ok_record(ctx, asin, resp.text, now, urls[0], resp.url, "")
            if rec.get("status") == "ok":
                return rec
            if rec.get("status") == "skipped_page_not_found":
                return rec
            if str(rec.get("status")) in _REJECT_STATUSES:
                # 2026-09-16 修(用户实测发现): 页面**已成功取回**, 是内容被本程序门限拒绝
                # (变体数越界 / 父 ASIN 格式 / HTML 太短) —— 原实现末尾无条件用瘦
                # `_failed_record("failed")` 覆盖, 于是 `_reject_record` 写下的
                # variant_count/html_length/map_counts/extended_variant_sources 全丢,
                # 且终态 skipped_too_many_variants 被降级成 failed(下次续跑白重抓)。
                # 现记住富记录, 若最后一次尝试就是它 → 直接返回(保留状态与取证字段)。
                last_reject = rec
            last_err = str(rec.get("error") or rec.get("status"))
            if rec.get("status") == "blocked":
                # blocked 不消耗尝试次数意义不大: 直接换形态重试
                continue
        if last_reject is not None and str(last_reject.get("error") or "") == last_err:
            # 最后一次尝试的结论就是拒收 → 用富记录(而不是被覆盖成"多次尝试失败: <原因>")
            return last_reject
        rec = self._failed_record(asin, "failed", now)
        rec["error"] = f"多次尝试失败: {last_err}"
        rec["source_url"] = urls[0]
        return rec

    def _build_ok_record(self, ctx: StepContext, asin: str, text: str, now: str,
                         source_url: str, final_url: str, source: str) -> dict:
        """用 skill 解析器解析文本并生成记录(ok / blocked / skipped / parse_failed)。

        传 `market` 让解析器按**本站域名**判断"是否商品页"(2026-09-15 修复: 原先写死
        amazon.com, 非 US 站详情页标题是 Amazon.co.uk/Amazon.de, 会被误判 unexpected_page)。
        """
        pv = skill_parse_variations(text, asin, market=str(ctx.config.market or ""))
        status = pv["status"]
        if status == "page_not_found":
            rec = self._failed_record(asin, "skipped_page_not_found", now)
            rec.update({
                "error": "Amazon page title is Page Not Found",
                "title": pv["title"],
                "source_url": source_url, "final_url": final_url,
                "source": source or "direct_http", "html_length": pv["html_length"],
            })
            return rec
        if status in ("blocked", "unexpected_page"):
            rec = self._failed_record(asin, "blocked", now)
            rec.update({
                "error": f"页面判定 {status}(Robot/CAPTCHA/非商品页), 可重试",
                "title": pv["title"],
                "source_url": source_url, "final_url": final_url,
                "source": source or "direct_http", "html_length": pv["html_length"],
            })
            return rec
        # 门槛: 父 ASIN 格式 / 变体数 1..100 / HTML>=50000(与 skill 拒收一致)
        parent = str(pv["parent_asin"] or "").upper()
        variants = pv["variant_asins"] or []
        if normalize_asin(parent) is None:
            return self._reject_record(asin, text, now, source_url, final_url, source,
                                       "父 ASIN 格式无效", parent, variants, pv)
        if not (1 <= len(variants) <= 100):
            # 2026-09-16 用户定稿: 变体数越界 = **终态**(页面真实存在且解析成功, 只是超本程序
            # 上限) → 标 skipped_too_many_variants: 不反复重抓、不计入"网络失败"、日志列 ASIN,
            # 便于人工抽页面确认是真实大变体家族还是解析器把"相似商品"算进来了。
            return self._reject_record(asin, text, now, source_url, final_url, source,
                                       f"变体数 {len(variants)} 不在 1..100(超上限, 终态跳过)",
                                       parent, variants, pv,
                                       status="skipped_too_many_variants")
        if pv["html_length"] < 50_000:
            return self._reject_record(asin, text, now, source_url, final_url, source,
                                       f"HTML 长度 {pv['html_length']}<50000", parent, variants, pv)
        title, bullets, main_img = _extract_text_fields(text)
        images = _extract_gallery(text, main_img)
        aplus_images = _extract_aplus_images(text)   # A+ 内容图: 单独一列, 不混进图廊
        # 变体标签(2026-09-14): 原实现此处写死 `{}` → 即使页面有配色/规格名也永远为空
        # (历史 10k+ 条记录 variant_labels 非空 0 条); 现按页面真实映射落库。
        labels = _extract_variant_labels(text, _extract_image_block(text), variants)
        return {
            "request_asin": asin,
            "ok": True,
            "status": "ok",
            "source": source or "direct_http",
            "source_url": source_url,
            "final_url": final_url,
            "parent_asin": parent,
            "parent_method": pv["parent_method"],
            "parent_candidates": pv["parent_candidates"],
            "variant_asins": variants,
            "variant_count": len(variants),
            "variant_labels": labels,
            "expected_variation_count": pv["expected_variation_count"],
            "map_counts": pv["map_counts"],
            "map_asins": pv["map_asins"],
            "fallback_variant_asins": pv["fallback_variant_asins"],
            "extended_variant_asins": pv["extended_variant_asins"],
            "extended_variant_sources": pv["extended_variant_sources"],
            "html_length": pv["html_length"],
            "images": images,
            "aplus_images": aplus_images,
            "title": title,
            "bullets": bullets,
            "fetched_at": now,
        }

    def _reject_record(self, asin: str, text: str, now: str, source_url: str, final_url: str,
                       source: str, reason: str, parent: str, variants: list,
                       pv: dict, status: str = "parse_failed") -> dict:
        """页面已取回但被本程序门限拒收的记录(保留解析器取证字段)。

        `status` 默认 parse_failed(可重试的拒收: 父 ASIN 格式 / HTML 太短);
        变体数越界传 `skipped_too_many_variants`(终态, 见 `_TERMINAL_STATUSES`)。
        """
        rec = self._failed_record(asin, status, now)
        rec.update({
            "error": f"页面拒收: {reason}",
            "title": pv.get("title", ""),
            "parent_asin": parent or asin,
            "parent_method": pv.get("parent_method", "failed_fallback_input_asin"),
            "variant_asins": variants or [asin],
            "variant_count": len(variants) or 1,
            "expected_variation_count": pv.get("expected_variation_count"),
            "map_counts": pv.get("map_counts", {}),
            "map_asins": pv.get("map_asins", {}),
            "fallback_variant_asins": pv.get("fallback_variant_asins", []),
            "extended_variant_asins": pv.get("extended_variant_asins", []),
            "extended_variant_sources": pv.get("extended_variant_sources", {}),
            "html_length": pv.get("html_length", 0),
            "source_url": source_url, "final_url": final_url,
            "source": source or "direct_http",
        })
        return rec

    def _failed_record(self, asin: str, status: str, now: str) -> dict:
        return {
            "request_asin": asin,
            "ok": False,
            "status": status,
            "parent_asin": "",
            "variant_asins": [],
            "variant_labels": {},
            "images": [],
            "title": "",
            "bullets": [],
            "fetched_at": now,
        }

    def _mock_variants(self, request_asin: str) -> tuple[str, list[str], dict, list[str]]:
        """dry-run: 用 core.mock 生成模拟父/变体关系(部分单变体, 其余 2-3 个变体)。"""
        seed = sum(ord(c) for c in request_asin)
        if seed % 4 == 0:
            count = 1
        elif seed % 2 == 0:
            count = 3
        else:
            count = 2
        if count == 1:
            variants = [request_asin]
            labels = {request_asin: "默认"}
        else:
            offset = getattr(self, "_mock_offset", 0)
            pool = make_asins(offset + count)
            variants = pool[offset : offset + count]
            self._mock_offset = offset + count
            labels = {v: f"款式{idx}" for idx, v in enumerate(variants, 1)}
        images = [f"https://m.media-amazon.com/images/I/61{request_asin}.jpg"]
        return request_asin, variants, labels, images

    def _filter_records(
        self,
        ctx: StepContext,
        records: list[dict],
        filter_prompt: dict | None,
    ) -> tuple[list[dict], list[dict]]:
        """第二遍过滤(FR-02.1): 仅成功记录参与, 提示词缺失仅导出不过滤。"""
        success = [r for r in records if r.get("ok")]
        if not success:
            return [], []
        if not filter_prompt:
            self.log(ctx, "过滤提示词缺失(filter-<CAT>-prompt.json), 仅导出不过滤")
            kept = [dict(r, keep=True, reason="提示词缺失未过滤") for r in success]
            return kept, []
        if not ctx.dry_run and not ctx.config.get("LLM_API_KEY"):
            self.log(ctx, "告警: LLM_API_KEY 未配置, 第二遍过滤将全保留")
        batch = max(1, int(ctx.config.get("BATCH_FILTER", 10) or 10))
        tries = max(1, int(ctx.config.get("FILTER_JUDGE_TRIES", 3) or 3))
        fallback_keep = str(ctx.config.get("FILTER_JUDGE_FALLBACK", "keep")
                            or "keep").strip().lower() != "drop"
        workers = max(1, int(ctx.config.get("FILTER_JUDGE_WORKERS", 3) or 3))
        self.log(ctx, f"第二遍过滤: BATCH_FILTER={batch} "
                      f"判定模式={'逐条' if batch <= 1 else '阶梯批(失败降档至1条/批)'} "
                      f"并发{workers} 单条重试{tries}次 失败兜底={'保留待复核' if fallback_keep else '剔除'}")
        kept, dropped = judge_batch(ctx.llm(), filter_prompt, success, batch_size=batch,
                                    log=lambda text: self.log(ctx, text),
                                    max_tries=tries, fallback_keep=fallback_keep,
                                    workers=workers)
        if not kept:
            self.log(ctx, "告警: 第二遍过滤无保留记录(LLM 判定或全保留逻辑异常)")
        return kept, dropped

    def _fetch_variant_texts(self, ctx: StepContext, records: list[dict], results_path: str) -> list[dict]:
        """全量变体逐 ASIN 抓取(2026-09-08 用户定稿: **每个变体 ASIN 都要爬, 无继承**)。

        对**全部 ok 请求记录**发现出的每个子变体 ASIN(去重、排除已有自身 ok 记录的)
        逐个抓详情页, 取该变体**自己页面**的标题/五点/全部图片链接(合并 listing/不同规格的
        变体页面文本图集本就不同, 必须逐变体抓取)。
        结果写 `<cat>-variant-texts.jsonl`(断点续爬, ok/失败都留痕);
        **多次重试仍失败(ok=false)的变体由调用方剔除**(明细不再用父体/家族文本回退)。

        `VARIANT_TEXT_CAP`>0 时只补前 N 个(仅调试用, 默认 0=全部)。
        """
        # 断点续爬台账(2026-09-14 修): 子变体记录写在 `<cat>-variant-texts.jsonl`,
        # 而首轮"请求 ASIN"记录写在 results_path —— **两份都要读**。原实现只读 results_path,
        # 于是"已有自身记录/本轮待抓"两个数字长期严重失真: 实战(2026-09-14 Grilling)子变体台账
        # 已 3410 条 ok, 日志仍报"已有 238 / 本轮待抓 3510", 真正待抓只有 98 —— 用户据此
        # 以为"3000 多个子变体被重跑"。去重本身不受影响(`_fetch_round` 会按同一份台账再过滤),
        # 但数字误导, 且 `VARIANT_TEXT_CAP` 会按虚报的待抓数截断(少抓)。
        vpath = ctx.resolve_artifact(f"{self._cat}-variant-texts.jsonl")
        existing_ok: set[str] = set()
        for _p in dict.fromkeys([results_path, vpath]):
            for _r in read_jsonl(_p):
                _a = str(_r.get("request_asin") or "").strip()
                if _a and _r.get("ok"):
                    existing_ok.add(_a)
        discovered: set[str] = set()
        for rec in records:
            if not rec.get("ok"):
                continue
            fam = str(rec.get("request_asin") or "")
            for v in rec.get("variant_asins") or []:
                v = str(v or "").strip()
                if not v or v == fam:
                    continue
                discovered.add(v)
        # 已有补抓 ok 的不重抓(断点续爬); 曾失败的仍留在待抓里会被 _fetch_all 再试
        want = sorted(discovered - existing_ok)
        cap = max(0, int(ctx.config.get("VARIANT_TEXT_CAP", "0") or 0))
        if cap > 0 and len(want) > cap:
            self.log(ctx, f"VARIANT_TEXT_CAP={cap}, 只补前 {cap}/{len(want)} 个变体文本")
            want = want[:cap]
        self.log(ctx, f"全量变体抓取: 全部 ok 请求发现子变体 {len(discovered)} 个(去重), "
                      f"其中已有自身记录 {len(discovered) - len(want)} 个, 本轮待抓 {len(want)} 个")
        if want:
            try:
                self._fetch_all(ctx, want, vpath)
            except Exception as exc:
                self.log(ctx, f"变体级抓取中断(剩余将由下次运行续抓): {exc}")
        return read_jsonl(vpath) if discovered else []

    def _merge_own_records(self, records: list[dict], variant_records: list[dict]) -> list[dict]:
        """候选集 = 每个有自身成功文本的 ASIN(2026-09-08 定稿, 过滤对象)。

        规则: 同一 ASIN 同时有请求记录与补抓记录时取**请求记录**(先出现); 只成功(ok)
        记录入候选; 补抓失败(ok=false)不入(由调用方作为"抓取失败"剔除)。
        """
        merged: dict[str, dict] = {}
        order: list[str] = []
        for r in list(records) + list(variant_records or []):
            if not r.get("ok"):
                continue
            asin = str(r.get("request_asin") or "").strip()
            if not asin:
                continue
            if asin not in merged:
                merged[asin] = r
                order.append(asin)
        return [merged[a] for a in order]

    def _write_variation_audit(self, ctx: StepContext, records: list[dict]) -> None:
        """变体抓取质量审计(2026-09-08 对齐 skill build_outputs, 附档非正式产物)。

        输出(类目根目录, 随运行生成):
          <CAT>-变体抓取-父ASIN汇总.csv / -变体ASIN明细.csv / -输入ASIN映射.csv
          / -质量检查.json(需复核原因/未解决/PNF/跨父重复/扩展变体清单)
        """
        if ctx.dry_run:
            return
        market = ctx.config.market
        asins = [str(r.get("request_asin") or "") for r in records]
        ok_records = [r for r in records if r.get("ok") and r.get("status") == "ok"]
        groups = build_groups_skill(asins, ok_records)
        # 跨父重复自动归属(2026-09-08): 确定性规则消解, 记录日志
        for note in auto_resolve_cross_parent(groups, ok_records):
            self.log(ctx, f"变体审计自动处理: {note}")
        # 剩余跨父重复 + 核对状态
        variant_parents: dict[str, list[str]] = {}
        for g in groups:
            for v in g["variant_asins"]:
                variant_parents.setdefault(v, []).append(g["parent_asin"])
        cross = {v: sorted(set(ps)) for v, ps in variant_parents.items() if len(set(ps)) > 1}

        def ext_label(variant: str, sources: list[str]) -> str:
            src = "+".join(sources) if sources else "补充允许字段"
            return f"{variant}(扩展变体: 仅{src}; 超出页面声明/主变体映射)"

        for g in groups:
            issues: list[str] = []
            # 2026-09-08(用户): "提取<页面声明数"暂不复核/不阻断(信息缺失无法自动补,
            # 保留已抓最大并集, 继续逐变体抓取流程) —— 仅剩跨父重复判"需复核"
            rep = [v for v in g["variant_asins"] if v in cross]
            if rep:
                issues.append("跨父组重复变体:" + ",".join(rep))
            g["_issues"] = issues
            g["check_status"] = (
                "需复核" if issues else "已核对(含扩展变体)" if g["extended_variant_asins"] else "已核对"
            )

        skipped = sorted(
            str(r.get("request_asin")) for r in records
            if _is_terminal(r.get("status"))
        )
        too_many = sorted(
            str(r.get("request_asin")) for r in records
            if str(r.get("status")) == "skipped_too_many_variants"
        )
        unresolved = sorted(
            str(r.get("request_asin")) for r in records
            if not r.get("ok") and not _is_terminal(r.get("status"))
        )
        prefix = ctx.resolve_artifact(f"{ctx.config.cat}-变体抓取")
        parent_map = {g["parent_asin"]: g for g in groups}

        # 1) 父ASIN汇总
        rows = [["序号", "父 ASIN", "全部变体 ASIN(逗号拼接)", "变体数量", "扩展变体标注",
                 "输入命中 ASIN", "输入数量", "核对状态", "代表来源链接"]]
        for idx, g in enumerate(groups, 1):
            rows.append([
                idx, g["parent_asin"], ",".join(g["variant_asins"]), g["variant_count"],
                ";".join(ext_label(v, g["extended_variant_sources"].get(v, []))
                         for v in g["extended_variant_asins"]),
                ",".join(g["input_asins"]), g["input_count"], g["check_status"],
                (g["source_urls"] or [""])[0],
            ])
        with open(prefix + "-父ASIN汇总.csv", "w", newline="", encoding="utf-8-sig") as fh:
            csv.writer(fh).writerows(rows)

        # 2) 变体ASIN明细(2026-09-08: 增加该变体**自己页面**的 标题/五点/全部图片 内容列)
        content_by_asin = {
            str(r.get("request_asin") or ""): r for r in ok_records
        }
        detail = [["父 ASIN", "变体 ASIN", "变体数量", "变体来源标注",
                   "标题", "五点描述", "图片链接"]]
        for g in groups:
            for v in g["variant_asins"]:
                rec = content_by_asin.get(v) or {}
                detail.append([
                    g["parent_asin"], v, g["variant_count"],
                    ext_label(v, g["extended_variant_sources"].get(v, []))
                    if v in g["extended_variant_asins"] else "",
                    rec.get("title", ""),
                    " | ".join(str(b) for b in (rec.get("bullets") or [])),
                    ";".join(str(u) for u in (rec.get("images") or [])),
                ])
        with open(prefix + "-变体ASIN明细.csv", "w", newline="", encoding="utf-8-sig") as fh:
            csv.writer(fh).writerows(detail)

        # 3) 输入ASIN映射
        mapping = [["输入 ASIN", "父 ASIN", "父体全部变体 ASIN(逗号拼接)", "父体变体数",
                    "抓取状态", "父体识别方式", "商品页面", "页面标题", "HTML 长度",
                    "页面声明变体数", "父体扩展变体标注"]]
        for r in records:
            asin = str(r.get("request_asin") or "").upper()
            ok = bool(r.get("ok"))
            status = str(r.get("status") or "")
            parent = str(r.get("parent_asin") or "").upper() if ok else ""
            g = parent_map.get(parent)
            scrape = ("成功" if ok else
                      "已跳过(Page Not Found)" if status in ("skipped_page_not_found", "not_found") else
                      "已跳过(变体数超上限)" if status == "skipped_too_many_variants" else
                      "已拒收(页面内容不符合门限)" if status == "parse_failed" else "失败")
            mapping.append([
                asin, parent, ",".join(g["variant_asins"]) if g else "", g["variant_count"] if g else 0,
                scrape, r.get("parent_method", ""),
                r.get("source_url") or r.get("final_url") or "",
                r.get("title", ""), int(r.get("html_length", 0) or 0),
                r.get("expected_variation_count") or "",
                ";".join(ext_label(v, g["extended_variant_sources"].get(v, []))
                         for g2 in [g] if g for v in g2["extended_variant_asins"]),
            ])
        with open(prefix + "-输入ASIN映射.csv", "w", newline="", encoding="utf-8-sig") as fh:
            csv.writer(fh).writerows(mapping)

        # 4) 质量检查.json
        review_groups = [g for g in groups if g["check_status"] == "需复核"]
        summary = {
            "input_count": len(records),
            "success_count": len(ok_records),
            "skipped_count": len(skipped),
            # 2026-09-16: 终态细分(变体数越界单独计数, 便于看板/人工快速识别; 与未解决互斥)
            "skipped_too_many_variants_count": len(too_many),
            "residual_count": len(unresolved),
            "parent_count": len(groups),
            "variant_count_sum": sum(g["variant_count"] for g in groups),
            "review_group_count": len(review_groups),
            "cross_parent_variant_count": len(cross),
            "extended_parent_count": sum(1 for g in groups if g["extended_variant_asins"]),
            "extended_variant_count": sum(len(g["extended_variant_asins"]) for g in groups),
        }
        summary["overall_status"] = (
            "通过" if summary["residual_count"] == 0 and not review_groups else "需复核"
        )
        quality = {
            "summary": summary,
            "unresolved_asins": unresolved,
            "skipped_page_not_found_asins": skipped,
            "skipped_too_many_variants_asins": too_many,
            "review_groups": [
                {"parent_asin": g["parent_asin"], "issues": g["_issues"]} for g in review_groups
            ],
            "cross_parent_variants": cross,
            "extended_variants": [
                {"parent_asin": g["parent_asin"], "variant_asins": g["extended_variant_asins"],
                 "labels": [ext_label(v, g["extended_variant_sources"].get(v, []))
                            for v in g["extended_variant_asins"]]}
                for g in groups if g["extended_variant_asins"]
            ],
        }
        with open(prefix + "-质量检查.json", "w", encoding="utf-8") as fh:
            json.dump(quality, fh, ensure_ascii=False, indent=2)
        self.log(
            ctx,
            f"变体抓取审计: 输入{summary['input_count']} 成功{summary['success_count']} "
            f"终态跳过{summary['skipped_count']}(其中变体数越界"
            f"{summary['skipped_too_many_variants_count']}) 未解决{summary['residual_count']} "
            f"父组{summary['parent_count']} 变体合计{summary['variant_count_sum']} "
            f"需复核{summary['review_group_count']} 扩展变体{summary['extended_variant_count']}"
            f" 跨父重复{summary['cross_parent_variant_count']} "
            f"总体={summary['overall_status']} -> {prefix}-*"
        )
        if too_many:
            self.log(ctx, f"终态变体数越界(>100, 已永久跳过): {', '.join(too_many)}")

    def _write_outputs(
        self,
        ctx: StepContext,
        records: list[dict],
        kept: list[dict],
        dropped: list[dict],
        filter_prompt: dict | None,
        variant_texts: list[dict] | None = None,
    ) -> None:
        """写出 results.jsonl / 3 sheet 明细表 / parents / variants / 审计 CSV。"""
        judgment = {r.get("request_asin"): r for r in kept + dropped}
        out_records = []
        for rec in records:
            merged = dict(rec)
            if rec.get("ok"):
                j = judgment.get(rec.get("request_asin"))
                if j:
                    merged["keep"] = j.get("keep", True)
                    merged["reason"] = j.get("reason", "保留")
            out_records.append(merged)
        atomic_write_text(
            ctx.resolve_artifact(self.artifacts[0]),
            "\n".join(json.dumps(r, ensure_ascii=False) for r in out_records) + "\n",
        )
        # ---- 2026-09-08 定稿: 明细=保留变体各自的自身行; 2026-09-09 定稿:
        #      步骤11/12 清单(parents/variants)由"全量不过滤"改为**过滤后**口径 ----
        fam_groups = _aggregate_families(records, variant_texts)
        detail_df = self._build_detail_df(kept)
        parent_df = self._build_parent_df(fam_groups)
        stat_df = self._build_stat_df(records, kept, dropped, filter_prompt, fam_groups)
        xlsx_path = ctx.resolve_artifact(self.artifacts[1])
        df_to_excel(xlsx_path, {"变体明细": detail_df, "父体汇总": parent_df, "统计": stat_df})
        # 步骤11/12 输入(2026-09-09 用户定稿, 过滤后口径):
        #   parents = 有保留子体的多变体父体(>=2 子且至少一个子体保留) -> 步骤11 查流量
        #   variants = 保留的子体 ∪ 保留的单变体产品自身(纯多变体父体不进) -> 步骤12 查竞品
        parents_all, variants_all = _step11_12_inputs(fam_groups, records, kept)
        write_txt_lines(ctx.resolve_artifact(self.artifacts[2]), parents_all)
        write_txt_lines(ctx.resolve_artifact(self.artifacts[3]), variants_all)
        audit_rows = []
        market = ctx.config.market
        for rec in kept + dropped:
            asin = str(rec.get("request_asin") or "")
            if not asin:
                continue
            j = judgment.get(asin, {})
            audit_rows.append(
                {
                    "来源标识": asin,
                    "标题": rec.get("title", ""),
                    "保留或剔除": "保留" if j.get("keep", True) else "剔除",
                    "原因": j.get("reason", "保留"),
                    "链接": f"https://{market}/dp/{asin}" if market else "",
                }
            )
        audit_path = ctx.resolve_artifact(self.artifacts[4])
        with open(audit_path, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(
                fh, fieldnames=["来源标识", "标题", "保留或剔除", "原因", "链接"]
            )
            writer.writeheader()
            writer.writerows(audit_rows)

    def _build_detail_df(self, kept: list[dict]) -> pd.DataFrame:
        """变体明细 sheet(2026-09-08 定稿): 每个保留的 ASIN 一行, 内容=**它自己页面**的
        标题/五点/全部图片链接, **不再有父体/家族文本回退与"继承"**。

        候选集已按 ASIN 去重且只含抓取成功记录(请求 ok ∪ 补抓 ok);
        补抓失败的变体已剔除, 不会出现在明细里。
        """
        rows = []
        for rec in kept:
            asin = str(rec.get("request_asin") or "")
            if not asin:
                continue
            imgs = rec.get("images") or []      # 只用自身页图集(无图即空, 不回退家族)
            rows.append(
                {
                    # 来源ASIN/文本来源 列名与列数与原来一致(2026-09-08 用户要求);
                    # 内容仍是该 ASIN 自己页面的: 来源ASIN=变体自身, 文本来源恒为"自有"
                    "来源ASIN": asin,
                    # 父ASIN: 该记录自己的父子归属(自身为父体时=自身), 仅用于分组
                    "父ASIN": str(rec.get("parent_asin") or "") or asin,
                    "变体ASIN": asin,
                    "文本来源": "自有",
                    # 全部图片 URL ";" 拼接(用户要求, 非单主图)
                    "图片链接": ";".join(str(u) for u in imgs),
                    # A+ 内容图(商品描述模块) — 单独一列, 不与图廊混(2026-09-20 用户要求)
                    "A+图片链接": ";".join(str(u) for u in (rec.get("aplus_images") or [])),
                    "标题": rec.get("title", ""),
                    # 五点: 逐条去 HTML 标签/反转义后 " | " 拼接(用户要求)
                    "五点描述": " | ".join(
                        _clean_html(str(b)) for b in (rec.get("bullets") or [])
                    ),
                    "是否成功": "是" if rec.get("ok") else "否",
                    "状态": rec.get("status", ""),
                    "抓取时间": rec.get("fetched_at", ""),
                }
            )
        cols = ["来源ASIN", "父ASIN", "变体ASIN", "文本来源", "图片链接", "A+图片链接",
                "标题", "五点描述", "是否成功", "状态", "抓取时间"]
        df = pd.DataFrame(rows, columns=cols)
        return dedup_keep_best(df, keys=["变体ASIN"], weight_cols=["标题", "五点描述"])

    def _build_parent_df(self, parent_groups: dict) -> pd.DataFrame:
        """父体汇总 sheet(2026-09-08): 按父ASIN 汇总其发现的子变体数与清单(全量, 不过滤)。"""
        rows = []
        for p in sorted(parent_groups):
            g = parent_groups[p]
            children = sorted(g["children"])
            if not children:
                continue
            rows.append(
                {
                    "父ASIN": p,
                    "标题": g["titles"][0] if g["titles"] else "",
                    "变体数": len(children),
                    "变体ASIN列表": " | ".join(children),
                }
            )
        cols = ["父ASIN", "标题", "变体数", "变体ASIN列表"]
        return pd.DataFrame(rows, columns=cols)

    def _build_stat_df(
        self,
        records: list[dict],
        kept: list[dict],
        dropped: list[dict],
        filter_prompt: dict | None,
        parent_groups: dict,
    ) -> pd.DataFrame:
        """统计 sheet: 单行统计(2026-09-08: 过滤口径=每个 ASIN 候选)。"""
        total = len(records)
        success = sum(1 for r in records if r.get("ok"))
        unique_parents = len([p for p, g in parent_groups.items() if g["children"]])
        variant_total = len({v for g in parent_groups.values() for v in g["children"]})
        single = sum(1 for g in parent_groups.values() if len(g["children"]) == 1)
        multi = sum(1 for g in parent_groups.values() if len(g["children"]) > 1)
        if filter_prompt:
            rules = " | ".join(filter_prompt.get("rules", []))
        else:
            rules = "提示词缺失未过滤"
        row = {
            "总记录": total,
            "成功": success,
            "候选ASIN数": len(kept) + len(dropped),
            "相关保留": len(kept),
            "剔除": len(dropped),
            "唯一父体数": unique_parents,
            "变体总数": variant_total,
            "单变体数": single,
            "多变体数": multi,
            "生成时间": self.now(),
            "过滤规则": rules,
        }
        return pd.DataFrame([row])
