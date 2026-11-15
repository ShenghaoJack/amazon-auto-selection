"""Step18 admission and explicit variant/parent/brand ownership of the VOC chain."""
from collections import Counter
from copy import deepcopy
import csv
import json
from pathlib import Path

VERSION = "voc-variant-links-v1"
ADMISSION_VERSION = "voc-step18-admission-v1"


def normalized(value):
    return " ".join(str(value or "").split()).casefold()


def product_identity(product, catalog):
    return {"product_key": product["product_key"], "variant_asin": product["variant_asin"], "site": product.get("site"),
            "parent_asin": str(product.get("parent_asin") or "").strip().upper() or None,
            "brand": str(product.get("brand") or "").strip() or None,
            "step18_link": {"product_key": product["product_key"], "source": dict(catalog["source"]),
                            "sheet": product["source"]["sheet"], "row": product["source"]["row"]}}


def admit_reviews(rows, catalog, site):
    """Exclude before any LLM call; never convert requested ASIN into attribution."""
    from tools.voc.opportunity import review_key
    products = {p["variant_asin"]: p for p in catalog["products"]}
    accepted, excluded, audit = [], [], []
    for row in rows:
        source_identity = row.get("_voc_original_identity") or {
            k: row.get(k) for k in ("variant_asin", "parent_asin", "brand")}
        asin = str(row.get("variant_asin") or "").strip().upper()
        product = products.get(asin)
        reason = ""
        if not asin:
            reason = "missing_attributed_variant_asin"
        elif product is None:
            reason = "variant_not_in_step18"
        elif row.get("source") != "pangolinfo" and row.get("asin_role") != "attributed":
            reason = "variant_attribution_unconfirmed"
        elif (row.get("parent_asin") and product.get("parent_asin")
              and str(row["parent_asin"]).strip().upper() != str(product["parent_asin"]).strip().upper()):
            reason = "parent_asin_conflict"
        elif row.get("brand") and product.get("brand") and normalized(row["brand"]) != normalized(product["brand"]):
            reason = "brand_conflict"
        owner = product_identity(product, catalog) if product else {}
        record = {"review_key": review_key(row, site), "review_id": row.get("review_id"),
                  "source": row.get("source"), "queried_asin": row.get("queried_asin"),
                  "ASIN状态": row.get("asin_status"), "跨源补抓状态": row.get("asin_recovery_status"),
                  "status": "excluded" if reason else "included", "reason": reason or "exact_step18_variant_match",
                  **{"source_" + k: v for k, v in source_identity.items()},
                  **{k: owner.get(k) for k in ("variant_asin", "parent_asin", "brand", "step18_link")}}
        audit.append(record)
        if reason:
            excluded.append(dict(row, exclusion_reason=reason, extraction_status="not_extracted"))
        else:
            accepted.append(dict(row, **{k: owner[k] for k in ("variant_asin", "parent_asin", "brand")},
                                 _voc_original_identity=source_identity))
    summary = {"version": ADMISSION_VERSION, "source": dict(catalog["source"]), "input_reviews": len(rows),
               "included_reviews": len(accepted), "excluded_reviews": len(excluded),
               "reasons": dict(Counter(r["exclusion_reason"] for r in excluded))}
    return accepted, excluded, audit, summary


def write_admission(directory, excluded, audit, summary):
    from tools.voc.opportunity import save
    directory = Path(directory)
    save(directory / "评论准入.json", {"summary": summary, "records": audit})
    save(directory / "排除评论原文.json", excluded)
    with (directory / "评论准入排除记录.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        columns = list(dict.fromkeys(k for r in audit for k in r)) or ["review_key", "status", "reason"]
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for r in audit:
            values = {k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for k, v in r.items()}
            writer.writerow({k: "'" + v if isinstance(v, str) and v.lstrip().startswith(("=", "+", "-", "@")) else v
                             for k, v in values.items()})


def scope_needs(extracted, needs, catalog):
    """Retain category-level groups, create one consumer Need record per variant."""
    from tools.voc.opportunity import digest
    reviews = {r["review_key"]: r for r in extracted["reviews"]}
    evidence = {a["evidence_id"]: a for a in extracted["aspects"]}
    products = {p["variant_asin"]: p for p in catalog["products"]}
    scoped, groups = [], []
    for need in needs:
        grouped = {}
        for ref in need["evidence_ids"]:
            grouped.setdefault(reviews[evidence[ref]["review_key"]]["variant_asin"], []).append(ref)
        group = deepcopy(need)
        group.update(need_group_id=need["need_id"], scope="category_need_group", variant_need_ids=[], related_products=[])
        for asin, refs in sorted(grouped.items()):
            node = deepcopy(need)
            node.update(product_identity(products[asin], catalog), need_group_id=need["need_id"], scope="variant",
                        need_id="NV" + digest([products[asin].get("site"), need["need_id"], asin])[:18], evidence_ids=refs)
            node["review_count"] = len({evidence[ref]["review_key"] for ref in refs})
            for polarity in ("positive", "negative"):
                node[polarity + "_review_count"] = len({evidence[ref]["review_key"] for ref in refs
                                                         if evidence[ref]["polarity"] == polarity})
            node["feature_ids"] = sorted({evidence[ref]["feature_id"] for ref in refs if evidence[ref].get("feature_id")})
            for ref in refs:
                evidence[ref].update(need_id=node["need_id"], need_group_id=need["need_id"])
            scoped.append(node)
            group["variant_need_ids"].append(node["need_id"])
            group["related_products"].append(product_identity(products[asin], catalog))
        groups.append(group)
    return scoped, groups


def refresh_need_owners(needs, catalog):
    products = {p["variant_asin"]: p for p in catalog["products"]}
    for node in needs:
        node.update(product_identity(products[node["variant_asin"]], catalog))


def bind_identities(chain, catalog, admission):
    """Materialize ownership in every delivery instead of requiring an ID traversal."""
    from tools.voc.opportunity import digest
    products = {p["variant_asin"]: p for p in catalog["products"]}
    reviews = {r["review_key"]: r for r in chain["reviews"]}
    needs = {n["need_id"]: n for n in chain["needs"]}
    for r in reviews.values():
        r.update(product_identity(products[r["variant_asin"]], catalog))
    for a in chain["aspects"]:
        a.update(product_identity(products[reviews[a["review_key"]]["variant_asin"]], catalog))
    refresh_need_owners(chain["needs"], catalog)
    for group in chain.get("need_groups", []):
        group["related_products"] = [product_identity(products[asin], catalog) for asin in sorted(
            {needs[nid]["variant_asin"] for nid in group["variant_need_ids"]})]
    for s in chain["solutions"]:
        target = needs[s["need_id"]]
        s.update(product_identity(products[s["subject_id"]], catalog), target_variant_asin=target["variant_asin"],
                 target_parent_asin=target["parent_asin"], target_brand=target["brand"],
                 solution_role="target" if s["subject_id"] == target["variant_asin"] else "comparison")
    solutions = {s["solution_id"]: s for s in chain["solutions"]}
    proposals = []
    for op in chain["opportunities"]:
        if len(op["need_ids"]) != 1:
            raise ValueError("变体Open必须关联一个变体Need")
        need = needs[op["need_ids"][0]]
        owner = product_identity(products[need["variant_asin"]], catalog)
        op.update(owner, need_group_id=need["need_group_id"], scope_level="variant",
                  comparison_products=[product_identity(products[solutions[sid]["subject_id"]], catalog)
                                       for sid in op["solution_ids"] if solutions[sid]["subject_id"] != need["variant_asin"]])
        for index, mechanism in enumerate(op["mechanisms"]):
            proposals.append({"proposal_id": "P" + digest([op["open_id"], index, mechanism])[:18],
                "open_id": op["open_id"], "need_id": need["need_id"], "need_group_id": need["need_group_id"],
                **owner, "mechanism": mechanism, "status": "待验证", "validation_plan": op["validation_plan"],
                "tradeoffs": op["tradeoffs"], "hypotheses": op["hypotheses"], "evidence_ids": op["evidence_ids"]})
    chain["proposals"] = proposals
    for row in chain["coverage"]:
        row.update(product_identity(products[reviews[row["review_key"]]["variant_asin"]], catalog))
    used = {r["variant_asin"] for r in reviews.values()} | {s["subject_id"] for s in chain["solutions"]}
    chain["products"] = [dict(products[asin], **product_identity(products[asin], catalog)) for asin in sorted(used)]
    for cell in chain.get("standardized_cells", []):
        cell.update(product_identity(products[cell["product_key"]], catalog))
    links = []
    for kind, nodes, id_field in (("aspect", chain["aspects"], "evidence_id"), ("need", chain["needs"], "need_id"),
                                  ("solution", chain["solutions"], "solution_id"), ("open", chain["opportunities"], "open_id"),
                                  ("proposal", proposals, "proposal_id")):
        for node in nodes:
            links.append({"entity_type": ("praise" if node["polarity"] == "positive" else "pain" if node["polarity"] == "negative"
                                           else "uncertain") if kind == "aspect" else kind,
                          "entity_id": node[id_field], **product_identity(products[node["variant_asin"]], catalog),
                          "need_id": node.get("need_id"), "open_id": node.get("open_id"),
                          "need_group_id": node.get("need_group_id"), "review_key": node.get("review_key"),
                          "solution_role": node.get("solution_role"), "target_variant_asin": node.get("target_variant_asin"),
                          "status": node.get("status")})
    chain.update(variant_links=links, admission_summary=admission,
                 identity_association={"version": VERSION, "status": "complete", "needs_scope": "variant", "opens_scope": "variant",
                    "aspects": len(chain["aspects"]), "needs": len(needs), "solutions": len(solutions),
                    "opens": len(chain["opportunities"]), "proposals": len(proposals), "links": len(links)})
    validate_identities(chain)


def variant_need_table(chain):
    evidence = {a["evidence_id"]: a for a in chain["aspects"]}
    cells = {c["cell_id"]: c for c in chain.get("standardized_cells", [])}
    rows = []
    for need in chain["needs"]:
        own = [s for s in chain["solutions"] if s["need_id"] == need["need_id"] and s.get("solution_role") == "target"]
        comparison = [s for s in chain["solutions"] if s["need_id"] == need["need_id"] and s.get("solution_role") == "comparison"]
        opens = [o for o in chain["opportunities"] if need["need_id"] in o["need_ids"]]
        rows.append({**{k: need[k] for k in ("variant_asin", "parent_asin", "brand", "site", "step18_link", "need_id", "need_group_id")},
            "候选Need": need["outcome"], "场景": need["scene"], "对象": need["user_object"],
            "好评证据": [evidence[ref] for ref in need["evidence_ids"] if evidence[ref]["polarity"] == "positive"],
            "痛点证据": [evidence[ref] for ref in need["evidence_ids"] if evidence[ref]["polarity"] == "negative"],
            "已有解法": [s["capability_basis"] for s in own], "解决程度": "; ".join(s["status"] for s in own),
            "解决依据与边界": [{k: s[k] for k in ("solution_id", "result", "limitations", "support_ids", "counter_ids", "boundary_ids")} for s in own],
            "本变体步骤18功能属性及来源依据": [cells[ref] for ref in sorted({ref for s in own for ref in s["annotation_ids"]})],
            "比较解法（不代表本变体已解决）": [{k: s[k] for k in ("solution_id", "variant_asin", "parent_asin", "brand", "status", "capability_basis")} for s in comparison],
            "剩余Open": [{k: o[k] for k in ("open_id", "remaining_gap", "gap_type")} for o in opens],
            "待验证方案": [p for p in chain.get("proposals", []) if p["need_id"] == need["need_id"]]})
    return rows


def validate_identities(chain):
    products = {p["variant_asin"]: p for p in chain["products"]}
    reviews = {r["review_key"]: r for r in chain["reviews"]}
    evidence = {a["evidence_id"]: a for a in chain["aspects"]}
    needs = {n["need_id"]: n for n in chain["needs"]}
    opens = {o["open_id"]: o for o in chain["opportunities"]}
    for nodes in (chain["reviews"], chain["aspects"], chain["needs"], chain["solutions"], chain["opportunities"],
                  chain.get("proposals", []), chain.get("variant_links", [])):
        for node in nodes:
            p = products.get(node.get("variant_asin"))
            if p is None or any(node.get(k) != p.get(k) for k in ("parent_asin", "brand")):
                raise ValueError("实体变体/父体/品牌关联不一致")
            if node.get("step18_link") != p.get("step18_link"):
                raise ValueError("实体步骤18来源关联缺失或不一致")
    for a in evidence.values():
        if a["variant_asin"] != reviews[a["review_key"]]["variant_asin"]:
            raise ValueError("方面证据归属变体不一致")
    for n in needs.values():
        if any(evidence[ref]["variant_asin"] != n["variant_asin"] or evidence[ref].get("need_id") != n["need_id"] for ref in n["evidence_ids"]):
            raise ValueError("变体Need混入其他变体评论")
    for s in chain["solutions"]:
        if s["variant_asin"] != s["subject_id"] or s["target_variant_asin"] != needs[s["need_id"]]["variant_asin"]:
            raise ValueError("解法目标与比较变体混淆")
        if any(evidence[ref]["variant_asin"] != s["variant_asin"]
               for field in ("support_ids", "counter_ids", "boundary_ids") for ref in s[field]):
            raise ValueError("解法借用其他变体证据")
        if any(ref not in needs[s["need_id"]]["evidence_ids"] for field in ("support_ids", "counter_ids", "boundary_ids") for ref in s[field]):
            raise ValueError("解法借用其他Need证据")
    for o in opens.values():
        if len(o["need_ids"]) != 1 or o["variant_asin"] != needs[o["need_ids"][0]]["variant_asin"]:
            raise ValueError("Open目标变体不一致")
        if any(evidence[ref]["variant_asin"] != o["variant_asin"] for ref in o["evidence_ids"]):
            raise ValueError("Open混入其他变体评论")
    for proposal in chain.get("proposals", []):
        if (proposal["variant_asin"] != opens[proposal["open_id"]]["variant_asin"]
                or proposal["need_id"] not in opens[proposal["open_id"]]["need_ids"]):
            raise ValueError("方案归属与Open不一致")
