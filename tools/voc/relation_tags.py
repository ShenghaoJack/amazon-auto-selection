"""Classify Need/Open from their traceable LLM-classified evidence, without merging IDs."""
VERSION = "voc-relation-tags-v1"


def taxonomy(aspects):
    pairs = list(dict.fromkeys((a["taxonomy_category"], a["canonical_attribute"]) for a in aspects))
    return {"category_labels": list(dict.fromkeys(c for c, _ in pairs)),
            "attribute_labels": [c + "：" + attr for c, attr in pairs]}


def classify_relations(chain):
    """Preserve candidate identities/outcomes; a category is not a Need merge decision."""
    evidence = {a["evidence_id"]: a for a in chain["aspects"]}
    needs = {n["need_id"]: n for n in chain["needs"]}
    for kind, nodes in (("need", chain["needs"]), ("open", chain["opportunities"])):
        for node in nodes:
            refs = node["evidence_ids"]
            if not refs or not set(refs) <= set(evidence):
                raise ValueError(f"{kind}分类引用未知或空证据")
            local = [evidence[ref] for ref in refs]
            if kind == "open":
                if not node["need_ids"] or not set(node["need_ids"]) <= set(needs):
                    raise ValueError("Open分类引用未知Need")
                allowed = {ref for ident in node["need_ids"] for ref in needs[ident]["evidence_ids"]}
                if not set(refs) <= allowed:
                    raise ValueError("Open证据不属于关联Need")
            node.update(taxonomy(local))
            node["classification_method"] = "关联原句的LLM功能分类，多标签；不改变Need/Open身份或结果"
    chain["relation_classification"] = {"version": VERSION, "status": "complete",
                                         "classified_needs": len(needs),
                                         "classified_opens": len(chain["opportunities"])}


def review_relations(chain, review_key):
    local = {a["evidence_id"]: a for a in chain["aspects"] if a["review_key"] == review_key}
    result = {}
    for nodes, label, ident_field, text_field in ((chain["needs"], "Need", "need_id", "outcome"),
                                                 (chain["opportunities"], "Open", "open_id", "remaining_gap")):
        related = []
        for node in nodes:
            refs = [ref for ref in node["evidence_ids"] if ref in local]
            if refs:
                related.append((node, refs, taxonomy([local[ref] for ref in refs])))
        result[label + "大类"] = "\n".join(dict.fromkeys(c for _, _, tags in related for c in tags["category_labels"]))
        result[label + "细分类"] = "\n".join(dict.fromkeys(c for _, _, tags in related for c in tags["attribute_labels"]))
        result[label + "数"] = len(related)
        entries = []
        for node, refs, tags in related:
            context = "" if label == "Open" else f"（场景：{node.get('scene') or '未知'}；对象：{node.get('user_object') or '未知'}）"
            entries.append(f"{node[ident_field]}【{'、'.join(tags['attribute_labels'])}】：{node[text_field]}{context}［本条证据：{','.join(refs)}］")
        result["候选" + label] = "\n".join(entries)
        if label == "Open":
            types = {"result": "结果缺口", "evidence": "证据缺口", "both": "结果与证据缺口"}
            result["Open缺口类型"] = "\n".join(node["open_id"] + "：" + types[node["gap_type"]] for node, _, _ in related)
            result["待验证方案"] = "\n".join(node["open_id"] + "：" + m for node, _, _ in related for m in node["mechanisms"])
    return result
