"""Open category references from buluslan/review-analyzer-skill (MIT)."""
VERSION = "github-review-analyzer-22-open-v1"
SOURCE = "https://github.com/buluslan/review-analyzer-skill/blob/main/references/tag_system.md"
SOURCE_BLOB_SHA = "1554f7417b61ee07cc08a4c5a6f8484942fa01f2"
REFERENCE_TAGS = (
    "人群_性别", "人群_年龄段", "人群_职业", "人群_购买角色", "场景_使用场景",
    "功能_满意度", "功能_具体功能", "质量_材质", "质量_做工", "质量_耐用性",
    "服务_发货速度", "服务_包装质量", "服务_客服响应", "服务_退换货", "服务_保修",
    "体验_舒适度", "体验_易用性", "体验_外观设计", "体验_价格感知",
    "市场_竞品对比", "市场_复购意愿", "情感_总体评价",
)


def policy():
    return {"version": VERSION, "source": SOURCE, "source_blob_sha": SOURCE_BLOB_SHA,
            "mode": "open_reference_categories_llm_subcategories",
            "categories": [{"name": tag.split("_", 1)[1], "dimension": tag.split("_", 1)[0], "source_tag": tag}
                           for tag in REFERENCE_TAGS]}


def metadata(name):
    match = next((tag for tag in REFERENCE_TAGS if name in (tag, tag.split("_", 1)[1])), None)
    return {"category_origin": "github_reference" if match else "llm_new",
            "reference_category_tag": match, "category_reference_version": VERSION}


def prompt():
    return ("\n大类参考来自GitHub review-analyzer-skill的22维标签：" + "、".join(REFERENCE_TAGS)
        + "。以各标签下划线后的名称作为大类参考（例如耐用性、易用性），不是封闭枚举。"
          "先核对这些参考方面是否有原文观点；无法准确容纳时允许新增大类，不强塞具体功能或其他。"
          "小类由LLM按当前评论语义自主提取、命名和同义归并，不能用优质/一般/差等评分充当小类。"
          "正负共用大类和小类，差异留在极性、具体体验和条件中。"
          "人群性别/年龄/职业只用原文明示，不根据姓名、语气或购买产品推断；"
          "仅背景信息不强造痛点/好评，泛评可零方面，未提及的标签不必填满。\n")
