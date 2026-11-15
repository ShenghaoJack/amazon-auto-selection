# 单元测试: step00 榜单 URL 规范校验(仅收 /gp/bestsellers/<dept>/<node> 短链接) + NR 派生(整段替换) + NR 落地校验
#
# 规范(2026-09): LIST_URLS 只填 Best Sellers 短链接, 形如
#   https://www.amazon.com/gp/bestsellers/baby-products/379024011
# New Releases 榜 = 把路径段 bestsellers 整体替换成 new-releases 得到
#   https://www.amazon.com/gp/new-releases/baby-products/379024011
# step0 启动前(validate_upstream)先校验格式, 不符合要求直接阻断并引导用户改格式。
from steps.step00_bestsellers import Step00Bestsellers, normalize_list_url

S = Step00Bestsellers

BABY_BS = "https://www.amazon.com/gp/bestsellers/baby-products/379024011"
BABY_NR = "https://www.amazon.com/gp/new-releases/baby-products/379024011"


def _pairs(url):
    return [(k, u) for u, k in S._derive_list_urls(url)]


def test_derive_canonical_bs():
    assert _pairs(BABY_BS) == [("bs", BABY_BS), ("nr", BABY_NR)]


def test_derive_keeps_ref_suffix():
    url = BABY_BS + "/ref=pd_zg_hrsr_baby-products"
    pairs = _pairs(url)
    assert pairs[0] == ("bs", url)
    assert pairs[1] == ("nr", BABY_NR + "/ref=pd_zg_hrsr_baby-products")


def test_validate_ok_cases():
    v = S._validate_bs_url
    assert v(BABY_BS) is None
    assert v(BABY_BS + "/") is None
    assert v(BABY_BS + "/ref=pd_zg_hrsr_baby-products") is None
    assert v("https://www.amazon.com/gp/bestsellers/kitchen/289863") is None
    assert v("https://www.amazon.de/gp/bestsellers/elektronik/562066") is None


def test_validate_rejects_wrong_forms():
    v = S._validate_bs_url
    # 旧版长链接(zgbs slugged) -> 提示改用规范短链接
    slug = ("https://www.amazon.com/Best-Sellers-Kitchen-Dining-Cutting-Boards/"
            "zgbs/kitchen/289863/ref=zg_bs_nav_kitchen_3_122181570011")
    assert "旧版" in v(slug)
    # 误填 New Releases 链接 -> 提示改回 Best Sellers(系统自动推导)
    assert "New Releases" in v(BABY_NR)
    # 缺节点 / 根级 / 尾随杂项 -> 格式提示
    assert "格式须为" in v("https://www.amazon.com/gp/bestsellers/kitchen")
    assert "格式须为" in v("https://www.amazon.com/gp/bestsellers/")
    assert "格式须为" in v("https://www.amazon.com/gp/bestsellers/baby-products/379024011/foo")
    # 非亚马逊主机 / 非 http 前缀 / 空
    assert "主机不是亚马逊" in v("https://example.com/gp/bestsellers/a/1")
    assert "https://" in v("www.amazon.com/gp/bestsellers/a/1")
    assert "空" in v("   ")


def test_build_tasks_dedupes_same_node():
    step = Step00Bestsellers()
    url_ref = BABY_BS + "/ref=pd_zg_hrsr_baby-products"
    # 同一节点两种字符串(带/不带 ref)-> 仍只有 bs+nr 两个任务, 不重复抓
    tasks = step._build_tasks([BABY_BS, url_ref])
    assert [(k, u) for u, k in tasks] == [("bs", BABY_BS), ("nr", BABY_NR)]


def test_validate_accepts_locale_prefix_and_normalizes():
    """**用户实测报障(2026-09-15)**: 非美国站从地址栏复制来的链接带语言前缀。

    实测 amazon.fr 给出 `https://www.amazon.fr/-/en/gp/bestsellers/kitchen/3155139031`
    (浏览器语言与站点不同时 Amazon 会把语言偏好写进路径), 原校验要求严格匹配 `/gp/bestsellers/...`
    → 直接阻断运行, 报 "LIST_URLS 有 1 条不符合规范格式, 流程未启动"。
    语言前缀只影响语言偏好、榜单内容完全相同, 应**归一后接受**(而不是让用户手改)。
    """
    v = S._validate_bs_url
    n = normalize_list_url
    fr_raw = "https://www.amazon.fr/-/en/gp/bestsellers/kitchen/3155139031"
    assert n(fr_raw) == "https://www.amazon.fr/gp/bestsellers/kitchen/3155139031"
    assert v(fr_raw) is None, "带语言前缀的规范榜单链接应通过(归一后) "
    # 其它站/其它写法
    assert n("https://www.amazon.de/-/de/gp/bestsellers/elektronik/562066") == \
        "https://www.amazon.de/gp/bestsellers/elektronik/562066"
    assert n("https://www.amazon.com/en/gp/bestsellers/kitchen/289863") == \
        "https://www.amazon.com/gp/bestsellers/kitchen/289863"
    assert v("  https://www.amazon.fr/-/en/gp/bestsellers/kitchen/3155139031  ") is None, "首尾空白也要容错"
    # 归一不得误伤: 规范链接原样, 旧版长链接/非亚马逊主机仍被拒(且理由不变)
    assert n(BABY_BS) == BABY_BS
    assert n(BABY_BS + "/ref=pd_zg_hrsr_baby-products") == BABY_BS + "/ref=pd_zg_hrsr_baby-products"
    slug = ("https://www.amazon.com/Best-Sellers-Kitchen-Dining-Cutting-Boards/"
            "zgbs/kitchen/289863/ref=zg_bs_nav_kitchen_3_122181570011")
    assert "旧版" in v(slug)
    assert "主机不是亚马逊" in v("https://example.com/-/en/gp/bestsellers/a/1")


def test_derive_and_dedupe_use_normalized_locale_url():
    """归一后的链接用于 NR 派生与去重: 带语言前缀与不带前缀的**同一个榜**只抓一次。"""
    raw = "https://www.amazon.fr/-/en/gp/bestsellers/kitchen/3155139031"
    clean = "https://www.amazon.fr/gp/bestsellers/kitchen/3155139031"
    nr = clean.replace("/bestsellers/", "/new-releases/")
    step = S()
    # 只给带语言前缀的那条: 派生出的 NR 也是规范形态(不带前缀)
    assert [(k, u) for u, k in step._build_tasks([raw])] == [("bs", clean), ("nr", nr)]
    # 两条都填(带前缀 + 不带前缀) → 归一后视为同榜, 不重复抓
    assert [(k, u) for u, k in step._build_tasks([raw, clean])] == [("bs", clean), ("nr", nr)]
    # 两个不同节点仍各自出任务
    other = "https://www.amazon.fr/gp/bestsellers/kitchen/3155140031"
    assert len(step._build_tasks([raw, other])) == 4


def test_is_no_new_releases():
    chk = S._is_no_new_releases
    # 用户实测: 无 NR 新品榜的子类目页面提示
    page = ("<html><body>Sorry, there are no hot new releases available in this category. "
            "Please check back later.</body></html>")
    assert chk(page) is True
    assert chk("<html>no new releases available yet</html>") is True
    assert chk("<html><div class='zg-item'>B0XXXX</div></html>") is False
    assert chk("") is False


def test_looks_like_nr_page():
    ok = S._looks_like_nr_page
    # 真实 NR 页
    assert ok("https://www.amazon.com/gp/new-releases/kitchen/289863",
              "Amazon.com New Releases: The best-selling new & future releases in Cutting Boards")
    # 回落到 BS 页(zgbs 规范路径 / 标题 Best Sellers) -> False
    assert not ok("https://www.amazon.com/Best-Sellers-Kitchen-Dining-Cutting-Boards/zgbs/kitchen/289863",
                  "Amazon Best Sellers: Best Cutting Boards")
    assert not ok("https://www.amazon.com/gp/bestsellers/baby-products/379024011",
                  "Amazon Best Sellers: Best Baby Formula Dispensers & Mixers")
    # 无法判断时保守放行
    assert ok("https://www.amazon.com/some/unknown/path", "")
