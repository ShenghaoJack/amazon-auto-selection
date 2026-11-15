# 单元测试: 详情页五点解析(限定 feature-bullets, 不抓页面杂项)
from core.http_client import parse_product_html


def test_bullets_only_from_feature_bullets():
    """五点只取 feature-bullets 容器内; 页面后部巨型 A+/描述 li 不得混入。"""
    html = """<html><body>
<span id="productTitle">Cutting Board Set</span>
<div id="feature-bullets">
<ul class="a-unordered-list">
<li><span class="a-list-item"><span>Bullet One</span></span></li>
<li><span class="a-list-item">Bullet Two plain</span></li>
<li><span class="a-list-item"><span>Bullet Three</span></span></li>
</ul>
</div>
<div id="productDescription">Some description</div>
<div class="aplus">
<li><span class="a-list-item"><span>""" + "A" * 30000 + """</span></span></li>
</div>
<div class="vse">
<li><span class="a-list-item"><span>video widget text</span></span></li>
</div>
</body></html>"""
    title, bullets = parse_product_html(html)
    assert title == "Cutting Board Set"
    assert bullets == ["Bullet One", "Bullet Two plain", "Bullet Three"]
    assert all(len(b) < 1000 for b in bullets)  # 无超长脏数据


def test_bullets_empty_when_no_feature_bullets():
    """页面无 feature-bullets(A+布局/结构变化): 五点留空, 不全局抓取。"""
    html = """<html><body>
<span id="productTitle">X</span>
<div class="aplus"><li><span class="a-list-item"><span>desc text</span></span></li></div>
</body></html>"""
    title, bullets = parse_product_html(html)
    assert title == "X"
    assert bullets == []


def test_title_missing_but_bullets():
    """无标题但有 feature-bullets: 五点仍正常, 标题空。"""
    html = """<html><body>
<div id="feature-bullets"><ul>
<li><span class="a-list-item">A</span></li>
<li><span class="a-list-item">B</span></li>
</ul></div>
</body></html>"""
    title, bullets = parse_product_html(html)
    assert title == ""
    assert bullets == ["A", "B"]


def test_li_with_class_attrs():
    """亚马逊真实结构: <li class='a-spacing-mini'><span class='a-list-item'>卖点</span></li>
    (li 带 class 属性也必须匹配, 否则五点抓空)。"""
    html = """<html><body>
<span id="productTitle">Cutting Board</span>
<div id="feature-bullets">
<h2>About this item</h2>
<ul class="a-unordered-list a-vertical a-spacing-mini">
<li class="a-spacing-mini"><span class="a-list-item"> VERSATILE: reversible cutting surface </span></li>
<li class="a-spacing-mini"><span class="a-list-item"> BPA-FREE: non-absorbent material </span></li>
<li class="a-spacing-mini"><span class="a-list-item"> MEASUREMENTS: includes 3 boards </span></li>
<li class="a-spacing-mini"><span class="a-list-item"> EASY TO CLEAN: dishwasher safe </span></li>
</ul>
</div>
<div id="productDetails">
<li><span class="a-list-item">#381 in Cutting Boards</span></li>
</div>
</body></html>"""
    title, bullets = parse_product_html(html)
    assert title == "Cutting Board"
    assert bullets == [
        "VERSATILE: reversible cutting surface",
        "BPA-FREE: non-absorbent material",
        "MEASUREMENTS: includes 3 boards",
        "EASY TO CLEAN: dishwasher safe",
    ]
    assert all("#" not in b for b in bullets)  # BSR 条目不混入
