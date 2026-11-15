# 单元测试: 变体详情页六步解析(Twister/图片块/优先级链/文本/变体图/全图集)
from steps.step10_variants import (
    parse_variants,
    _restore_full_image,
    _balanced_scan,
)


def _html():
    return """<html><body>
<span id="productTitle">Salt &amp; Pepper Grinder Set Stainless</span>
<img id="landingImage" data-old-hires="https://m.media-amazon.com/images/I/main_full.jpg" src="https://m.media-amazon.com/images/I/main_thumb.jpg">
<div id="feature-bullets"><ul><li><span class="a-list-item"><span>Adjustable grind</span></span></li>
<li><span class="a-list-item"><span>Ceramic burr</span></span></li></ul></div>
<script>
P.register('twister-js-init-dpx-data', function(){ var dataToReturn = {"parentAsin":"B0PARENT111","currentAsin":"B0PAGE00001","dimensionToAsinMap":{"Black":"B03C537AFE","Silver":"B0SILVER111","Set":"B06FDBCB61"}}; });
</script>
<script>
jQuery.parseJSON('{"colorToAsin":{"Black":"B03C537AFE","Silver":"B0SILVER111"},"colorImages":{"Black":[{"hiRes":"https://m.media-amazon.com/images/I/black_hi.jpg","large":"https://m.media-amazon.com/images/I/black_lg.jpg"}],"Silver":[{"hiRes":"https://m.media-amazon.com/images/I/silver_hi.jpg"}]}}');
</script>
<div id="altImages">
<img class="imageThumbnail" src="https://m.media-amazon.com/images/I/g1._AC_US40_.jpg">
<img class="imageThumbnail" src="https://m.media-amazon.com/images/I/g2._AC_US40_.jpg">
<img class="videoThumbnail" src="https://m.media-amazon.com/images/I/video.jpg">
</div>
<div id="twister"><div data-asin="B0199F4920"></div></div>
</body></html>"""


def test_twister_parent_and_dimension_map():
    parent, variants, labels, images = parse_variants(_html(), "B0PAGE00001")
    assert parent == "B0PARENT111"
    assert "B03C537AFE" in variants
    assert "B0SILVER111" in variants
    assert "B06FDBCB61" in variants
    assert "B0PAGE00001" in variants
    # data-asin 兜底仅在变体<=1时触发; 已有4个变体时 B0199F4920 不加入(符合优先级链)
    assert "B0199F4920" not in variants


def test_labels_from_color_map():
    _, _, labels, _ = parse_variants(_html(), "B0PAGE00001")
    assert labels.get("B03C537AFE") == "Black"
    assert labels.get("B0SILVER111") == "Silver"


def test_gallery_restored_and_main_first():
    _, _, _, images = parse_variants(_html(), "B0PAGE00001")
    assert images[0] == "https://m.media-amazon.com/images/I/main_full.jpg"
    assert "https://m.media-amazon.com/images/I/g1.jpg" in images
    assert "https://m.media-amazon.com/images/I/g2.jpg" in images
    # 视频缩略图排除
    assert not any("video" in i for i in images)


def test_restore_full_image():
    assert _restore_full_image("https://m.media-amazon.com/images/I/a._AC_US40_.jpg") == (
        "https://m.media-amazon.com/images/I/a.jpg"
    )


def test_restore_full_image_strips_avif_wrapper():
    """2026-09-20: 抓取时图片地址会被套 `AVIF_MEASUREMENT` 包装 → 必须归一成规范路径。

    实测包装路径与规范路径下载到的是同一张原图(1492×1476 一致), 但包装 token
    是会话级/实验级的, 存进交付表后可能失效。例:
      /images/W/BW_MEDIAX_AVIF_MEASUREMENT_1306696-T2/images/I/71x.jpg
      → /images/I/71x.jpg
    """
    assert _restore_full_image(
        "https://m.media-amazon.com/images/W/BW_MEDIAX_AVIF_MEASUREMENT_1306696-T2"
        "/images/I/71zbaZwcfqL._AC_SL1492_.jpg"
    ) == "https://m.media-amazon.com/images/I/71zbaZwcfqL.jpg"
    # 已是规范路径的不受影响
    assert _restore_full_image("https://m.media-amazon.com/images/I/a.jpg") == (
        "https://m.media-amazon.com/images/I/a.jpg")


def _html_container_style():
    """方式 A 结构: imageThumbnail 是外层容器(li)的 class, <img> 在内部。"""
    return """<html><body>
<span id="productTitle">Salt Shaker</span>
<img id="landingImage" data-old-hires="https://m.media-amazon.com/images/I/main_full.jpg" src="https://m.media-amazon.com/images/I/main_thumb.jpg">
<div id="feature-bullets"><ul><li><span>Ceramic</span></li></ul></div>
<div id="altImages">
<li class="imageThumbnail"><img src="https://m.media-amazon.com/images/I/a1._AC_US40_.jpg"></li>
<li class="imageThumbnail"><img src="https://m.media-amazon.com/images/I/a2._AC_US40_.jpg"></li>
<li class="videoThumbnail"><img src="https://m.media-amazon.com/images/I/video_frame.jpg"></li>
</div>
</body></html>"""


def test_gallery_container_style_excludes_video():
    """方式 A(容器)结构: 图片收集正常且 video 缩略图排除、无重复。"""
    _, _, _, images = parse_variants(_html_container_style(), "B0PAGE00001")
    assert images[0] == "https://m.media-amazon.com/images/I/main_full.jpg"
    assert "https://m.media-amazon.com/images/I/a1.jpg" in images
    assert "https://m.media-amazon.com/images/I/a2.jpg" in images
    # 视频缩略图(videoThumbnail 容器)不得混入
    assert not any("video" in i for i in images)
    # 无重复(尺寸还原后)
    assert len(images) == len(set(images))


def test_balanced_scan():
    html = 'xx {"a": {"b": 1}, "c": [1, 2]} yy'
    idx = html.find("{")
    text = _balanced_scan(html, idx)
    assert text == '{"a": {"b": 1}, "c": [1, 2]}'


def test_single_variant_fallback():
    html = "<html><span id='productTitle'>X</span></html>"
    parent, variants, _, _ = parse_variants(html, "B06D2E1A16")
    assert variants == ["B06D2E1A16"]
    assert parent == "B06D2E1A16"


# ---------------------------------------------------------------------------
# 2026-09-14 真站结构漂移修复(amazon.fr B017CBD1C5 实测):
#   ① colorToAsin 的值是嵌套对象 {"标签": {"asin": "B0…"}} → 旧实现 isinstance(asin, str)
#      永不成立, 标签与每配色主图恒为空(历史 6 类目 10,456 条记录 variant_labels 非空 0 条);
#   ② 只有尺寸变体的页面没有配色映射(colorToAsin=0), 标签只能取自 dimensionValuesDisplayData;
#   ③ 图集混入浏览器扩展注入的 chrome-extension:// / 占位 defaultPic / 保存页相对路径,
#      还会占掉 10 张上限; 主图未做尺寸还原会与缩略图重复(线上 0.8% 记录)。
# ---------------------------------------------------------------------------
def _html_real_nested_structure():
    """照 amazon.fr 真实变体页结构写的夹具(6 个配色 × 规格, 未做尺寸还原的主图)。"""
    colors = {
        "Light Bamboo Lot de 3": ("B017CBD1C5", "81oW6jgpWrL"),
        "Dark Bamboo Lot de 3": ("B0513CF8EF", "81jyPnaSx9L"),
        "Dark Bamboo XL": ("B0F2485B33", "91hTfwYfTWL"),
        "Light Bamboo Lot XL + XXL": ("B066727CBE", "91q4CQChiYL"),
        "Light Bamboo XL": ("B0A3D4DD8C", "81KPvWj8NHL"),
        "Light Bamboo XXL": ("B0B8E0F9FF", "81Fr0zb76KL"),
    }
    return """<html><body>
<span id="productTitle">Deer &amp; Oak Set of 3 Bamboo Chopping Boards</span>
<img id="landingImage" data-old-hires="https://m.media-amazon.com/images/I/81oW6jgpWrL._AC_SL1500_.jpg">
<div id="feature-bullets"><ul><li><span class="a-list-item"><span>PRE-OILED</span></span></li></ul></div>
<script>P.register('twister-js-init-dpx-data', function(){ var dataToReturn = {"parentAsin":"B0460D7BA8","currentAsin":"B017CBD1C5","dimensionToAsinMap":{"0_0":"B017CBD1C5","0_1":"B066727CBE","1_0":"B0513CF8EF","0_2":"B0A3D4DD8C","0_3":"B0B8E0F9FF","1_2":"B0F2485B33"}}; });</script>
<script>jQuery.parseJSON('{"colorToAsin":{"Light Bamboo Lot de 3":{"asin":"B017CBD1C5"},
"Dark Bamboo Lot de 3":{"asin":"B0513CF8EF"},"Dark Bamboo XL":{"asin":"B0F2485B33"},
"Light Bamboo Lot XL + XXL":{"asin":"B066727CBE"},"Light Bamboo XL":{"asin":"B0A3D4DD8C"},
"Light Bamboo XXL":{"asin":"B0B8E0F9FF"}},
"colorImages":{"Light Bamboo Lot de 3":[{"hiRes":"https://m.media-amazon.com/images/I/81oW6jgpWrL._AC_SL1500_.jpg","large":"https://m.media-amazon.com/images/I/51rb0irdekL._AC_.jpg"}],
"Dark Bamboo Lot de 3":[{"hiRes":"https://m.media-amazon.com/images/I/81jyPnaSx9L._AC_SL1500_.jpg"}],
"Dark Bamboo XL":[{"hiRes":"https://m.media-amazon.com/images/I/91hTfwYfTWL._AC_SL1500_.jpg"}],
"Light Bamboo Lot XL + XXL":[{"hiRes":"https://m.media-amazon.com/images/I/91q4CQChiYL._AC_SL1500_.jpg"}],
"Light Bamboo XL":[{"hiRes":"https://m.media-amazon.com/images/I/81KPvWj8NHL._AC_SL1500_.jpg"}],
"Light Bamboo XXL":[{"hiRes":"https://m.media-amazon.com/images/I/81Fr0zb76KL._AC_SL1500_.jpg"}]}}');</script>
<div id="altImages">
<img class="imageThumbnail" src="https://m.media-amazon.com/images/I/51rb0irdekL._AC_US100_.jpg">
<img class="imageThumbnail" src="./page_files/419nt0IeoOL.jpg">
<img class="imageThumbnail" src="chrome-extension://lnbmbgocenenhhhdojdielgnmeflbnfb/assets/images/minicon/yunduo.png">
<img class="imageThumbnail" src="defaultPic">
<img class="videoThumbnail" src="https://m.media-amazon.com/images/I/video.jpg">
</div>
</body></html>"""


def test_labels_from_nested_color_to_asin():
    """真站嵌套结构 {"标签": {"asin": …}} 必须能取到标签与每配色主图(原实现全空)。"""
    _, variants, labels, _ = parse_variants(_html_real_nested_structure(), "B017CBD1C5")
    assert len(variants) == 6
    assert labels["B017CBD1C5"] == "Light Bamboo Lot de 3"
    assert labels["B0F2485B33"] == "Dark Bamboo XL"
    assert len(labels) == 6
    from steps.step10_variants import _extract_image_block, _extract_variant_images
    _, imgs = _extract_variant_images(variants, _extract_image_block(_html_real_nested_structure()))
    assert imgs["B017CBD1C5"] == "https://m.media-amazon.com/images/I/81oW6jgpWrL._AC_SL1500_.jpg"
    assert len(imgs) == 6


def test_labels_still_work_for_legacy_string_structure():
    """旧字符串结构 {"标签": "B0…"} 保持可用(向后兼容, 不因新结构而回退)。"""
    _, _, labels, _ = parse_variants(_html(), "B0PAGE00001")
    assert labels.get("B03C537AFE") == "Black"
    assert labels.get("B0SILVER111") == "Silver"


def test_labels_fallback_to_dimension_values_display():
    """只有尺寸变体(无配色映射)的页面: 标签取自 dimensionValuesDisplayData。"""
    html = """<html><body>
<span id="productTitle">Tray</span>
<script>P.register('twister-js-init-dpx-data', function(){ var dataToReturn =
{"parentAsin":"B0PARENT111","currentAsin":"B0A19071A3","dimensionToAsinMap":{"0_0":"B0A19071A3","0_1":"B0954A7F03"}}; });</script>
<script>var dimensionValuesDisplayData = {"B0A19071A3": ["Small", "Red"], "B0954A7F03": ["Large"]};</script>
</body></html>"""
    _, variants, labels, _ = parse_variants(html, "B0A19071A3")
    assert set(variants) == {"B0A19071A3", "B0954A7F03"}
    assert labels["B0A19071A3"] == "Small / Red"
    assert labels["B0954A7F03"] == "Large"


def test_gallery_drops_extension_and_placeholder_srcs():
    """图集只收 http(s): 扩展注入/占位/相对路径全部剔除, 且不占 10 张上限名额。"""
    _, _, _, images = parse_variants(_html_real_nested_structure(), "B017CBD1C5")
    assert images[0] == "https://m.media-amazon.com/images/I/81oW6jgpWrL.jpg"   # 主图已还原
    assert all(u.startswith("https://") for u in images)
    assert not any(("chrome-extension" in u or "defaultPic" in u or "_files/" in u) for u in images)
    assert not any("video" in u for u in images)


def test_gallery_main_image_deduped_after_size_restore():
    """主图与缩略图是同一张(仅尺寸参数不同) → 只保留一条(原实现留两条)。"""
    html = """<html><body>
<span id="productTitle">X</span>
<img id="landingImage" data-old-hires="https://m.media-amazon.com/images/I/aaa._AC_SL1500_.jpg">
<div id="altImages">
<img class="imageThumbnail" src="https://m.media-amazon.com/images/I/aaa._AC_US40_.jpg">
<img class="imageThumbnail" src="https://m.media-amazon.com/images/I/bbb._AC_US40_.jpg">
</div></body></html>"""
    _, _, _, images = parse_variants(html, "B0PAGE00001")
    assert images == ["https://m.media-amazon.com/images/I/aaa.jpg",
                      "https://m.media-amazon.com/images/I/bbb.jpg"]


# ---------------------------------------------------------------------------
# 2026-09-20 图集改「大图优先」(用户要求"都抓取大图"):
#   真实商品页 altImages 的 imageThumbnail 是**缩略图专用资源**(ID 41…/51…, 最大500×500),
#   大图在 colorImages 的 hiRes(ID 71…, 1500px级)、以及 #ivLargeImage 查看器里。
#   原实现只收 altImages → 图集 = 1 张主图大图 + N 张 500px 缩略图,
#   图画上的小字(过滤层/材料标注)OCR 读不清。实测 B04DB5E72F 的
#   71zbaZwcfqL(1492×1476, 6-Stage Vertical Filtration) 完全没进图集。
# ---------------------------------------------------------------------------

def test_gallery_prefers_hires_from_colorimages():
    """colorImages 的 hiRes 大图必须进图集, 且排在缩略图之前。"""
    html = """<html><body>
<span id="productTitle">X</span>
<img id="landingImage" data-old-hires="https://m.media-amazon.com/images/I/main_hi._AC_SL1500_.jpg">
<script>
jQuery.parseJSON('{"colorToAsin":{"White":{"asin":"B0PAGE00001"}},"colorImages":{"White":[{"hiRes":"https://m.media-amazon.com/images/I/71main._AC_SL1500_.jpg"},{"hiRes":"https://m.media-amazon.com/images/I/71big2._AC_SL1492_.jpg"}]}}');
</script>
<div id="ivLargeImage" role="button"><img class="fullscreen" src="https://m.media-amazon.com/images/I/71big2._AC_SL1492_.jpg"></div>
<div id="altImages">
<img class="imageThumbnail" src="https://m.media-amazon.com/images/I/41thumb1._AC_US40_.jpg">
</div></body></html>"""
    _, _, _, images = parse_variants(html, "B0PAGE00001")
    assert images[0] == "https://m.media-amazon.com/images/I/main_hi.jpg"      # 主图最前
    assert "https://m.media-amazon.com/images/I/71main.jpg" in images          # 大图进图集
    assert "https://m.media-amazon.com/images/I/71big2.jpg" in images
    assert images.index("https://m.media-amazon.com/images/I/71big2.jpg") < \
        images.index("https://m.media-amazon.com/images/I/41thumb1.jpg")       # 大图排在缩略图前
    assert len(images) == len(set(images))


def test_gallery_hires_from_ivlargeimage_when_no_image_block():
    """图片块缺失时, #ivLargeImage 查看器里的大图仍要收进图集。"""
    html = """<html><body>
<span id="productTitle">X</span>
<img id="landingImage" data-old-hires="https://m.media-amazon.com/images/I/main_hi._AC_SL1500_.jpg">
<div id="ivLargeImage" role="button"><img class="fullscreen" src="https://m.media-amazon.com/images/I/71view._AC_SL1492_.jpg"></div>
</body></html>"""
    _, _, _, images = parse_variants(html, "B0PAGE00001")
    assert "https://m.media-amazon.com/images/I/71view.jpg" in images


def test_gallery_hires_from_imageblockatf_parsejson():
    """完整图廊在 ImageBlockATF 的 `A.$.parseJSON('[…]')` 里。

    旧实现只匹配 `jQuery.parseJSON` → **漏掉这个块**, 于是只能靠"整页扫 hiRes"硬凑,
    把 A+/推荐位/广告的图也扫了进来。实测 B0F1902E67: 完整图廊 9 张在该块内。
    """
    html = """<html><body>
<span id="productTitle">X</span>
<img id="landingImage" data-old-hires="https://m.media-amazon.com/images/I/main_hi._AC_SL1500_.jpg">
<script>
ImageBlockATF", function(A){ var data = { 'asin':'B0PAGE00001',
 'colorImages': { 'initial': A.$.parseJSON('[{"hiRes":"https://m.media-amazon.com/images/I/71a._AC_SL1500_.jpg","thumb":"https://m.media-amazon.com/images/I/41a._AC_US40_.jpg"},{"hiRes":"https://m.media-amazon.com/images/I/71b._AC_SL1492_.jpg","thumb":"https://m.media-amazon.com/images/I/41b._AC_US40_.jpg"}]') } }; });
</script>
</body></html>"""
    _, _, _, images = parse_variants(html, "B0PAGE00001")
    assert "https://m.media-amazon.com/images/I/71a.jpg" in images
    assert "https://m.media-amazon.com/images/I/71b.jpg" in images


def test_gallery_ignores_hires_outside_image_block():
    """页面别处的 hiRes(A+/推荐位/广告) 不得进图集。

    实测教训: 上一版用"整页扫 hiRes"硬凑, 结果 B0F1902E67 多扫进 4 张非本商品图
    (页面 675K 与 1.07M 处的模块), 用户一眼看出"本商品只有 9 张大图"。
    """
    html = """<html><body>
<span id="productTitle">X</span>
<img id="landingImage" data-old-hires="https://m.media-amazon.com/images/I/main_hi._AC_SL1500_.jpg">
<script>
ImageBlockATF", function(A){ var data = { 'colorImages': { 'initial': A.$.parseJSON('[{"hiRes":"https://m.media-amazon.com/images/I/71g1._AC_SL1500_.jpg"},{"hiRes":"https://m.media-amazon.com/images/I/71g2._AC_SL1500_.jpg"}]') } }; });
</script>
<script>var aplus = A.$.parseJSON('[{"hiRes":"https://m.media-amazon.com/images/I/71aplus._AC_SL1500_.jpg"}]');</script>
<div id="similarities"><img src="https://m.media-amazon.com/images/I/41rec._AC_US40_.jpg"></div>
</body></html>"""
    _, _, _, images = parse_variants(html, "B0PAGE00001")
    assert "https://m.media-amazon.com/images/I/71g1.jpg" in images
    assert "https://m.media-amazon.com/images/I/71g2.jpg" in images
    # 图廊之外的 hiRes 一律不得混入(上一版的真实缺陷)
    assert "https://m.media-amazon.com/images/I/71aplus.jpg" not in images


def test_build_ok_record_carries_real_variant_labels():
    """抓取记录里 variant_labels 必须是页面真实映射(原实现写死 {} → 历史数据全空)。"""
    from types import SimpleNamespace

    from steps.step10_variants import Step10Variants
    step = Step10Variants()
    # 夹具本身是 amazon.fr 的真实结构 → ctx 必须给出本站域名,
    # 否则会被"是否商品页"判定当成异站页面(2026-09-15: 该判定改为按配置 MARKET 走)
    ctx = SimpleNamespace(config=SimpleNamespace(market="amazon.fr"))
    # _build_ok_record 有"HTML 长度 >= 50000"的拒收门槛(与 skill 一致), 夹具需补足长度
    html = _html_real_nested_structure() + "<!--" + "x" * 60000 + "-->"
    rec = step._build_ok_record(ctx, "B017CBD1C5", html,
                                "2026-09-14 18:00:00",
                                "https://www.amazon.fr/dp/B017CBD1C5", "", "direct_http")
    assert rec["status"] == "ok"
    assert rec["parent_asin"] == "B0460D7BA8"
    assert len(rec["variant_asins"]) == 6
    assert rec["variant_labels"]["B017CBD1C5"] == "Light Bamboo Lot de 3"
    assert len(rec["variant_labels"]) == 6
    assert rec["images"] and all(u.startswith("https://") for u in rec["images"])
    assert rec["title"].startswith("Deer & Oak")
    assert rec["bullets"] == ["PRE-OILED"]


def test_aplus_images_extracted_separately():
    """A+ 内容图单独抽取(2026-09-20 用户要求): 作为 `A+图片链接` 新列, 不与图廊 `图片链接` 混。

    A+ 图常承载规格明细 —— 实测 B04DB5E72F 的「6-Stage Vertical Filtration + 每层材料」
    只在 A+ 图里, 图廊 9 张没有。
    """
    from steps.step10_variants import _extract_aplus_images
    html = """<html><body>
<img src="https://m.media-amazon.com/images/I/71gallery._AC_SL1500_.jpg">
<div id="aplus">
<img src="https://m.media-amazon.com/images/S/aplus-media-library-service-media/abc-123.__CR0,0,970,600_PT0_SX970_V1___.png">
<img src="https://m.media-amazon.com/images/S/aplus-media-library-service-media/def-456.__CR0,0,300,300_PT0_SX300_V1___.jpg">
<img src="https://m.media-amazon.com/images/I/71inline-not-aplus.jpg">
</div></body></html>"""
    aplus = _extract_aplus_images(html)
    assert aplus == [
        "https://m.media-amazon.com/images/S/aplus-media-library-service-media/abc-123.png",
        "https://m.media-amazon.com/images/S/aplus-media-library-service-media/def-456.jpg",
    ]
    # 只收 S/aplus* 前缀: 普通图廊图 / 内联图 不得混进 A+ 列
    assert not any("/images/I/" in u for u in aplus)


def test_aplus_images_not_mixed_into_gallery():
    """A+ 图不得进图廊(A+ 是长图/拼图, 混进去会挤占图廊名额)。"""
    html = """<html><body>
<span id="productTitle">X</span>
<img id="landingImage" data-old-hires="https://m.media-amazon.com/images/I/main_hi._AC_SL1500_.jpg">
<div id="aplus">
<img src="https://m.media-amazon.com/images/S/aplus-media-library-service-media/abc-123.__CR0,0,970,600_PT0_SX970_V1___.png">
</div></body></html>"""
    _, _, _, images = parse_variants(html, "B0PAGE00001")
    assert not any("/S/aplus" in u for u in images)
