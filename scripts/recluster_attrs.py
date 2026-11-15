# -*- coding: utf-8 -*-
"""词表再聚类 —— 在「标准化词表」基础上再收敛到自己的业务值域, 无需找人改代码。

三级值域
========
    原始值(模型逐行判定)  ──18步聚类报告/rules.json──▶  规范值(项目词表)  ──本工具的规则表──▶  业务值域(你的口径)

规则表是一张 **Excel 长表**(你自己在 Excel 里编辑):
    | 属性     | 源值        | 目标值 | 备注 |
    | 主体材质 | 相思木      | 木制   | 木种合并 |
    | 主体材质 | 木材*       | 木制   | 前缀通配 |
    | 主体材质 | *纸*        | 木纤维 | 包含通配 |
    | 主体材质 | 硅胶        | 保持   | 不动 |
    · 目标值 `保持` → 保持不变
    · 目标值 留空   → 视为未定, 报告里列为"待补规则"
    · 源值支持 `*` 通配(前缀/后缀/包含)

用法(全部可自己跑)
==================
    # 1) 生成/刷新规则模板(预填当前全部值 + 现有映射, 目标值列直接改)
    python scripts/recluster_attrs.py --cat "Cutting Boards-US-202608" --init

    # 2) 三级值域逐值对照表(原始/规范/业务)
    python scripts/recluster_attrs.py --cat "..." --compare

    # 3) 干跑: 报告变更行数 / 未映射值 / 规则里多余的行(不写文件)
    python scripts/recluster_attrs.py --cat "..." --rules "...-再聚类规则.xlsx"

    # 4) 写回交付表(自动备份 + 来源列留痕)
    python scripts/recluster_attrs.py --cat "..." --rules "..." --apply

输入值域用 `--source 17|18` 选择: 17=原始打标, 18=交付表(默认)。
"""
from __future__ import annotations

import argparse
import collections
import io
import json
import os
import shutil
import sys

import openpyxl
from openpyxl.styles import Font, PatternFill

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

OUT_DIR = "19_再聚类"
KEEP = "保持"


# ---------------------------------------------------------------- 载入
def find_sheet(wb, attr):
    for n in wb.sheetnames:
        ws = wb[n]
        for r in ws.iter_rows(min_row=1, max_row=3, values_only=True):
            if r and attr in [str(x) for x in r if x is not None]:
                return n
    return wb.sheetnames[0]


def load_table(path, attr):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[find_sheet(wb, attr)]
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    wb.close()
    for i, r in enumerate(rows):
        hdr = [str(x) if x is not None else "" for x in r]
        if attr in hdr:
            return hdr, rows[i + 1:], i + 1
    raise ValueError("找不到含「%s」的表头: %s" % (attr, path))


def col_values(rows, hdr, attr):
    j = hdr.index(attr)
    return [(str(r[j]).strip() if j < len(r) and r[j] is not None else "") for r in rows]


def load_vocab(cat_dir):
    p = os.path.join(cat_dir, "18_标准化", "词表聚类报告.json")
    if not os.path.exists(p):
        return {}
    o = json.load(io.open(p, encoding="utf-8"))
    out = {}
    for x in o.get("attributes") or []:
        out[x.get("属性")] = {
            "规范值": [str(v).strip() for v in (x.get("规范值") or [])],
            "归并": {str(g["原值"]).strip(): str(g["归入"]).strip()
                   for g in (x.get("归并") or []) if isinstance(g, dict)},
        }
    return out


def current_business_map():
    """现有业务值域映射(来自 tools/attr_stability/attr_repair.MATERIAL_MAP), 仅作模板预填参考。"""
    try:
        from tools.attr_stability.attr_repair import MATERIAL_MAP  # type: ignore
        return dict(MATERIAL_MAP)
    except Exception:
        return {}


# ---------------------------------------------------------------- 规则
def load_rules(path):
    """规则表 → {属性: [(源值原始写法, 目标值)]}"""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    wb.close()
    hdr = [str(x) if x is not None else "" for x in rows[0]]
    need = ["属性", "源值", "目标值"]
    for c in need:
        if c not in hdr:
            raise ValueError("规则表缺少列「%s」(实际表头: %s)" % (c, hdr))
    i_a, i_s, i_t = hdr.index("属性"), hdr.index("源值"), hdr.index("目标值")
    i_n = hdr.index("备注") if "备注" in hdr else None
    rules = collections.defaultdict(list)
    for r in rows[1:]:
        if not r or not r[i_a] or not r[i_s]:
            continue
        note = str(r[i_n]).strip() if i_n is not None and i_n < len(r) and r[i_n] else ""
        rules[str(r[i_a]).strip()].append(
            (str(r[i_s]).strip(), "" if r[i_t] is None else str(r[i_t]).strip(), note))
    return rules


def apply_rules(values, attr_rules):
    """返回 (新值列表, 命中统计, 未映射值计数, 未命中的规则)"""
    hit = collections.Counter()
    leftover = collections.Counter()
    used = set()
    out = []
    for v in values:
        if not v:
            out.append(v)
            continue
        tgt = None
        for i, (src, to, _note) in enumerate(attr_rules):
            if src == v:
                tgt, key = to, i
                break
        if tgt is None:
            for i, (src, to, _note) in enumerate(attr_rules):
                if "*" in src:
                    pat = src.strip("*")
                    if pat and (src.startswith("*") and src.endswith("*") and pat in v
                                or src.endswith("*") and v.startswith(pat)
                                or src.startswith("*") and v.endswith(pat)):
                        tgt, key = to, i
                        break
        if tgt is None:
            leftover[v] += 1
            out.append(v)
            continue
        used.add(key)
        if tgt == KEEP:
            out.append(v)
        elif tgt == "":
            leftover[v] += 1
            out.append(v)
        else:
            out.append(tgt)
            hit[(v, tgt)] += 1
    unused = [attr_rules[i] for i in range(len(attr_rules)) if i not in used]
    return out, hit, leftover, unused


# ---------------------------------------------------------------- 输出
def write_ledger(path, sheets):
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    head = Font(bold=True, color="FFFFFF")
    fill = PatternFill("solid", fgColor="1F4E78")
    for name, (hdr, rows) in sheets.items():
        ws = wb.create_sheet(name[:31])
        ws.append(hdr)
        for c in ws[1]:
            c.font, c.fill = head, fill
        for r in rows:
            ws.append(r)
        for i, h in enumerate(hdr, 1):
            ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = max(
                12, min(46, max([len(str(h))] + [len(str(r[i - 1])) for r in rows[:200]] or [10]) + 4))
        ws.freeze_panes = "A2"
    wb.save(path)


# ---------------------------------------------------------------- 命令
def cmd_compare(cat, cat_dir, out_dir, attrs):
    vocab = load_vocab(cat_dir)
    biz = current_business_map()
    std_path = os.path.join(cat_dir, "18_标准化", "%s-属性标注-标准化交付.xlsx" % cat)
    raw_path = os.path.join(cat_dir, "17_属性标注", "%s-属性标注-单遍原始.xlsx" % cat)
    report_lines = []
    for attr in attrs:
        raw = std = None
        if os.path.exists(raw_path):
            hdr, rows, _ = load_table(raw_path, attr)
            raw = col_values(rows, hdr, attr)
        if os.path.exists(std_path):
            hdr, rows, _ = load_table(std_path, attr)
            std = col_values(rows, hdr, attr)
        v = vocab.get(attr, {})
        join = v.get("归并", {})
        rows_out = []
        cnt_raw = collections.Counter(x for x in (raw or []) if x)
        cnt_std = collections.Counter(x for x in (std or []) if x)
        keys = set(cnt_raw) | set(cnt_std) | set(join) | set(v.get("规范值") or []) | set(biz)
        for k in sorted(keys, key=lambda x: -(cnt_raw.get(x, 0) + cnt_std.get(x, 0))):
            rows_out.append([k, cnt_raw.get(k, ""), cnt_std.get(k, ""),
                             join.get(k, ""), biz.get(k, ""),
                             "是" if k in (v.get("规范值") or []) else "",
                             "是" if k in join or k in (v.get("规范值") or []) else ""])
        hdr = ["值", "①原始打标(17步)行数", "②交付表(18步)行数", "③聚类报告归入(规范值)",
               "④现有业务映射(MATERIAL_MAP)", "在26规范值内", "在聚类映射表内"]
        p = os.path.join(out_dir, "%s-%s-三级值域对照.xlsx" % (cat, attr))
        write_ledger(p, {"三级对照": (hdr, rows_out)})
        report_lines.append("== %s: 原始 %d 值 / 交付表 %d 值 / 规范值 %d / 聚类映射 %d 条 → %s" % (
            attr, len(cnt_raw), len(cnt_std), len(v.get("规范值") or []), len(join), p))
    return report_lines


def cmd_init(cat, cat_dir, out_dir, attrs):
    vocab = load_vocab(cat_dir)
    biz = current_business_map()
    std_path = os.path.join(cat_dir, "18_标准化", "%s-属性标注-标准化交付.xlsx" % cat)
    raw_path = os.path.join(cat_dir, "17_属性标注", "%s-属性标注-单遍原始.xlsx" % cat)
    rows_out = []
    lines = []
    for attr in attrs:
        cur = collections.Counter()
        raw = collections.Counter()
        if os.path.exists(std_path):
            hdr, rows, _ = load_table(std_path, attr)
            cur.update(x for x in col_values(rows, hdr, attr) if x)
        if os.path.exists(raw_path):
            hdr, rows, _ = load_table(raw_path, attr)
            raw.update(x for x in col_values(rows, hdr, attr) if x)
        v = vocab.get(attr, {})
        norm = list(v.get("规范值") or [])
        # 第一块: 26 个规范值(标准化的落点) —— 目标值预填现有业务映射, 可改
        for val in sorted(set(norm) | set(cur), key=lambda x: -(cur.get(x, 0) + raw.get(x, 0))):
            if norm and val not in norm:
                continue
            pre = biz.get(val, "")
            rows_out.append([attr, val, pre, cur.get(val, 0), raw.get(val, 0),
                             "词表规范值; 目标值可改/可写「保持」"])
        # 第二块: 原始打标里的长尾值(不在词表内) —— 只有跑 --source 17 时才会遇到
        extras = [k for k in raw if k and (not norm or k not in norm) and k not in cur]
        for val in sorted(extras, key=lambda x: -raw[x]):
            if raw[val] <= 0:
                continue
            pre = biz.get(val, "")
            rows_out.append([attr, val, pre, 0, raw[val],
                             "原始长尾值(不在词表); 不填则原样保留"])
    hdr = ["属性", "源值", "目标值", "交付表行数", "原始打标行数", "备注(改完保存即可)"]
    p = os.path.join(out_dir, "%s-再聚类规则.xlsx" % cat)
    if os.path.exists(p):
        p = os.path.join(out_dir, "%s-再聚类规则-新.xlsx" % cat)
        lines.append("规则表已存在, 新模板写到: %s" % p)
    write_ledger(p, {"规则": (hdr, rows_out)})
    lines.append("规则模板: %s  (%d 行; 目标值填空=待定, 写「保持」=不动, 支持 * 通配)" % (p, len(rows_out)))
    return lines


def cmd_run(cat, cat_dir, out_dir, attrs, rules_path, source, apply_):
    rules = load_rules(rules_path)
    std_path = os.path.join(cat_dir, "18_标准化", "%s-属性标注-标准化交付.xlsx" % cat)
    raw_path = os.path.join(cat_dir, "17_属性标注", "%s-属性标注-单遍原始.xlsx" % cat)
    path = raw_path if source == "17" else std_path
    lines = ["输入值域: %s (%s)" % ("17步原始打标" if source == "17" else "18步交付表", os.path.basename(path))]
    ledger = {}
    plan = {}
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheets = {n: [list(r) for r in wb[n].iter_rows(values_only=True)] for n in wb.sheetnames}
    wb.close()
    main = "属性标注" if "属性标注" in sheets else list(sheets)[0]
    hdr = [str(x) if x is not None else "" for x in sheets[main][0]]
    src_sheet = "来源" if "来源" in sheets else None

    for attr in attrs:
        if attr not in rules:
            continue
        if attr not in hdr:
            lines.append("  ! 交付表无列「%s」, 跳过" % attr)
            continue
        j = hdr.index(attr)
        vals = [(str(r[j]).strip() if j < len(r) and r[j] is not None else "")
                for r in sheets[main][1:]]
        new, hit, leftover, unused = apply_rules(vals, rules[attr])
        changed = sum(1 for a, b in zip(vals, new) if a != b)
        lines.append("== %s: %d 行, 变更 %d 行" % (attr, len(vals), changed))
        dist = collections.Counter(x for x in new if x)
        lines.append("   目标值分布: " + "、".join("%s(%d)" % (k, v) for k, v in dist.most_common()))
        if leftover:
            lines.append("   [!] 未映射值 %d 个(%d 行) —— 需在规则表补行: %s" % (
                len(leftover), sum(leftover.values()),
                "、".join("%s(%d)" % (k[:26], v) for k, v in leftover.most_common(12))))
        if unused:
            lines.append("   规则表里未命中的行 %d 条(可清理): %s" % (
                len(unused), "、".join("%s→%s" % (s[:18], t or "(空)") for s, t, _ in unused[:10])))
        plan[attr] = (j, new, changed)
        ledger["映射命中"] = (["属性", "源值", "目标值", "命中行数"],
                          ledger.get("映射命中", (None, []))[1] +
                          [[attr, s, t, c] for (s, t), c in hit.most_common()])
        ledger["目标值分布"] = (["属性", "目标值", "行数"],
                          ledger.get("目标值分布", (None, []))[1] +
                          [[attr, k, v] for k, v in dist.most_common()])
        ledger["未映射值"] = (["属性", "值", "行数", "处理"], ledger.get("未映射值", (None, []))[1] +
                          [[attr, k, v, "保持原值(规则缺失)"] for k, v in leftover.most_common()])

    if not plan:
        lines.append("没有可执行的规则(检查规则表的「属性」列是否与交付表列名一致)")
        return lines
    p = os.path.join(out_dir, "%s-再聚类台账.xlsx" % cat)
    write_ledger(p, ledger)
    lines.append("台账: %s" % p)
    if not apply_:
        lines.append("(干跑, 未写回; 加 --apply 写回交付表)")
        return lines

    bak = path + ".bak-before-recluster.xlsx"
    if not os.path.exists(bak):
        shutil.copy2(path, bak)
    note = "再聚类(规则:%s)" % os.path.basename(rules_path)
    i_src = None
    if src_sheet:
        sh = [str(x) if x is not None else "" for x in sheets[src_sheet][0]]
        if attr in sh:
            i_src = sh.index(attr)
    for attr, (j, new, changed) in plan.items():
        for k in range(1, len(sheets[main])):
            old = str(sheets[main][k][j]).strip() if sheets[main][k][j] is not None else ""
            nv = new[k - 1]
            if old != nv:
                sheets[main][k][j] = nv
                if i_src is not None:
                    cur = str(sheets[src_sheet][k][i_src] or "")
                    sheets[src_sheet][k][i_src] = (cur + "; " + note)[:1000] if note not in cur else cur
    out = openpyxl.Workbook()
    first = out.active
    first.title = main
    for i, name in enumerate([main] + [s for s in sheets if s != main]):
        ws = first if i == 0 else out.create_sheet(name)
        for r in sheets[name]:
            ws.append(r)
    out.save(path)
    lines.append("已写回: %s (备份 %s)" % (path, os.path.basename(bak)))
    return lines


def cmd_suggest(cat, cat_dir, out_dir, attrs, source="17", batch=45, target=None):
    """让 LLM 出「建议归并」→ 落成建议规则表(仍需你扫一眼)。

    三重校验(本项目铁律: 模型只建议, 脚本才执行):
      ① 源值防编造    —— LLM 返回的源值必须逐字存在于输入集合, 否则丢弃并记录
      ② 目标值防越界  —— 目标值必须在允许值域内; 越界的标红待人工
      ③ 漏项必补齐    —— LLM 没覆盖的源值自动填「保持」并列出
    """
    import collections as _c
    vocab = load_vocab(cat_dir)
    std_path = os.path.join(cat_dir, "18_标准化", "%s-属性标注-标准化交付.xlsx" % cat)
    raw_path = os.path.join(cat_dir, "17_属性标注", "%s-属性标注-单遍原始.xlsx" % cat)
    path = raw_path if source == "17" else std_path
    lines = ["LLM 建议归并: 输入 %s" % os.path.basename(path)]
    if not os.path.exists(path):
        return lines + ["! 输入不存在"]

    from core.config import Config
    from core.llm import LLMClient
    cfg = Config.load(os.path.join(cat_dir, "config.yaml"))
    llm = LLMClient(cfg)

    all_rows = []
    for attr in attrs:
        hdr, rows, _ = load_table(path, attr)
        vals = _c.Counter(x for x in col_values(rows, hdr, attr) if x)
        if not vals:
            continue
        allowed = list(vocab.get(attr, {}).get("规范值") or [])
        if not allowed:
            allowed = sorted(vals)
        items = vals.most_common()
        got = {}
        for i in range(0, len(items), batch):
            part = items[i:i + batch]
            listing = "\n".join("%d. %s （%d 行）" % (n + 1, k, v)
                                for n, (k, v) in enumerate(part))
            prompt = (
                "你是电商类目词表治理助手。下面是类目「%s」这一列的全部**原始取值**(按出现次数降序),"
                "请把它们归并到给定的规范值域。\n\n"
                "【规范值域】(目标值**只能**从这里选, 一字不改地照抄):\n%s\n\n"
                "【补充口径】\n"
                "· `A+B` 表示一块板两个切面材质不同; 若能确定主材质则归主材质, 不确定就保留原写法\n"
                "· `未明确`/空白 → 目标值也写「未明确」\n"
                "· 不确定就给低置信度, **不要猜**; 允许新增值, 但目标值写成 `__新值__:建议名`\n\n"
                "【原始取值】(共 %d 个)\n%s\n\n"
                "输出 JSON, 必须**覆盖全部 %d 个源值、一个不漏**, 源值逐字照抄:\n"
                '{"映射":[{"源值":"...","目标值":"...","置信度":0-100,"理由":"不超过20字"}]}'
                % (attr, "、".join(allowed), len(part), listing, len(part)))
            try:
                res = llm.chat_json(prompt, temperature=0.0, max_tokens=8000)
            except Exception as e:      # noqa: BLE001
                lines.append("  ! %s 第 %d 批 LLM 失败: %s" % (attr, i // batch + 1, e))
                continue
            maps = (res or {}).get("映射") or (res or {}).get("mapping") or []
            valid_src = {k for k, _ in part}
            for m in maps:
                s = str(m.get("源值") or m.get("source") or "").strip()
                t = str(m.get("目标值") or m.get("target") or "").strip()
                if s not in valid_src:
                    lines.append("  ! 丢弃编造源值: %r" % s[:30])
                    continue
                got[s] = {"目标值": t, "置信度": m.get("置信度", m.get("confidence")),
                          "理由": str(m.get("理由") or m.get("reason") or "")[:40]}
        # 校验 + 补漏
        out_rows = []
        for k, v in items:
            g = got.get(k)
            if g is None:
                out_rows.append([attr, k, KEEP, v, "", "LLM 未判断 → 自动保持", "需人工"])
                continue
            t = g["目标值"]
            try:
                conf = float(g["置信度"])
            except (TypeError, ValueError):
                conf = None
            if t in allowed or t in ("未明确", KEEP, ""):
                flag = "OK(值域内)"
            elif t.startswith("__新值__"):
                flag = "LLM 提议新增值 → 需人工"
            else:
                flag = "疑似新增值(不在值域) → 需人工"
            if flag == "OK(值域内)" and (conf or 0) >= 80:
                flag = "OK(高置信)"
            out_rows.append([attr, k, t, v, conf if conf is not None else "",
                             g["理由"], flag])
        all_rows += out_rows
        n_ok = sum(1 for r in out_rows if str(r[-1]).startswith("OK"))
        lines.append("  %s: %d 个源值, LLM 直接可用 %d, 需人工 %d" % (
            attr, len(out_rows), n_ok, len(out_rows) - n_ok))
    if not all_rows:
        return lines + ["无可建议内容"]
    p = os.path.join(out_dir, "%s-再聚类建议.xlsx" % cat)
    write_ledger(p, {"建议归并": (
        ["属性", "源值", "建议目标值", "原始行数", "置信度", "理由", "校验"], all_rows)})
    lines.append("建议表: %s" % p)
    lines.append("→ 审核后把「源值/建议目标值」两列覆盖进规则表, 再 --rules ... --apply")
    return lines


def num(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


METRIC_COL = __import__("re").compile(
    r"(销[量额]|销售额|价格|BSR|评论|评分|上架|毛利|FBA|运费|流量|变体数|Coupon|LQS|卖家数)")
IDENT_COL = __import__("re").compile(
    r"(ASIN|SKU|链接|标题|主图|图片|描述|五点|参数|标签|卖家|品牌|关键词|Buyer|Q&A|来源|_来源$)")
BINARY_ATTR = __import__("re").compile(r"^(是否|有无|可否)")
YESNO_SET = {"是", "否", "有", "无", "支持", "不支持", "Y", "N", "true", "false",
             "防滑", "带手柄", "带汁槽", "带挂孔", "双面可用", "双面不同材质",
             "可洗碗机清洗", "仅手洗", "不可洗碗机", "食品级", "BPA-Free", "无毒",
             "耐高温", "不耐高温", "一般耐热", "含收纳架", "不含收纳架"}
OPS_COL = __import__("re").compile(
    r"(配送|物流|FBA|FBM|BuyBox|新老品|类目|大类|小类|标识|页面|视频|广告|秒杀|品牌故事|故事)")
UNTRUSTED_COL = __import__("re").compile(
    r"(商品尺寸|包装尺寸|包装重量|商品重量|单位换算|尺寸分段)")
NUM_METRIC_COL = __import__("re").compile(
    r"(率|重量|BSR|评分数|评分|天数|LQS|卖家数|流量|毛利|个数$)")


def _analyse_gran(path, sheet, attrs=None, rev_col="变体月销额", par_col="父ASIN", brand_col="品牌"):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[sheet] if sheet in wb.sheetnames else wb.worksheets[0]
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    wb.close()
    hdr = [str(h) if h is not None else "" for h in rows[0]]
    body = rows[1:]
    res = []
    for name in hdr:
        if attrs and name not in attrs:
            continue
        if (METRIC_COL.search(name) or IDENT_COL.search(name) or OPS_COL.search(name)
                or UNTRUSTED_COL.search(name) or NUM_METRIC_COL.search(name)
                or name in (rev_col, par_col, brand_col)):
            continue
        j = hdr.index(name)
        jr = hdr.index(rev_col) if rev_col in hdr else None
        jp = hdr.index(par_col) if par_col in hdr else None
        jb = hdr.index(brand_col) if brand_col in hdr else None
        cnt, rev = collections.Counter(), collections.defaultdict(float)
        par, br = collections.defaultdict(set), collections.defaultdict(set)
        n = 0
        for r in body:
            v = str(r[j]).strip() if j < len(r) and r[j] is not None else ""
            if v in ("", "None", "nan"):
                continue
            n += 1
            cnt[v] += 1
            rev[v] += num(r[jr]) if jr is not None and jr < len(r) else 0
            if jp is not None and jp < len(r) and r[jp]:
                par[v].add(str(r[jp]))
            if jb is not None and jb < len(r) and r[jb]:
                br[v].add(str(r[jb]))
        if not cnt or len(cnt) <= 1:      # 常量列不是属性, 跳过
            continue
        # 父体内唯一值 → 判「同款多变体」
        bypar = collections.defaultdict(set)
        for r in body:
            v = str(r[j]).strip() if j < len(r) and r[j] is not None else ""
            if v in ("", "None", "nan") or jp is None or jp >= len(r) or not r[jp]:
                continue
            bypar[str(r[jp])].add(v)
        sizes = sorted(len(s) for s in bypar.values()) or [1]
        within_med = sizes[len(sizes) // 2]
        multi = (sum(1 for s in bypar.values() if len(s) > 1) / max(1, len(bypar))) * 100
        # 值形态(按**行数**占比, 不是按唯一值个数; 未知≠否!)
        UNK = {"未明确", "缺", "不适用", "无法判断"}
        n_rows = sum(cnt.values()) or 1
        known_rows = sum(c for k, c in cnt.items() if k not in UNK) or 1
        r_yes = sum(c for k, c in cnt.items() if k in YESNO_SET) / known_rows
        r_num = sum(c for k, c in cnt.items() if num(k)) / known_rows
        tot = sum(rev.values()) or 1
        acc, pareto = 0.0, 0
        for k in sorted(rev, key=lambda x: -rev[x]):
            acc += rev[k]
            pareto += 1
            if acc / tot >= 0.8:
                break
        gate = sum(1 for k in cnt
                   if len(par.get(k, ())) >= 5 and len(br.get(k, ())) >= 3 and cnt[k] >= 0.03 * n)
        res.append({"属性": name, "唯一值": len(cnt), "行数": n, "帕累托80%需": pareto,
                    "过闸门水平数": gate, "覆盖率": n / max(1, len(body)) * 100,
                    "多值父体%": multi, "父体内中位": within_med,
                    "是/否比例": r_yes, "数值比例": r_num,
                    "布尔型": _binary_like(cnt, UNK)})
    return res


def _binary_like(cnt, UNK):
    """是不是"布尔型"属性(含 有手柄/无手柄 这类同骨架+有无前缀写法)。

    说明: 只用显式是/否词表会漏掉"无手柄/无挂孔"等否定写法; 这里再加一条
    「同一骨架词 + 有没有/带不带/含不含/可不可 前缀」的结构判据。
    """
    import re as _re
    keys = [k for k in cnt if k not in UNK]
    if not keys or len(keys) > 6:
        return False
    tot = sum(cnt[k] for k in keys) or 1
    if sum(cnt[k] for k in keys if k in YESNO_SET) / tot >= 0.7:
        return True
    skel = collections.Counter()
    for k in keys:
        m = _re.match(r"^(有|无|带|不带|含|不含|可|不可|支持|不支持|仅)?(.+)$", k)
        skel[m.group(2)] += cnt[k]
    return skel.most_common(1)[0][1] / tot >= 0.7


def _advise_gran(res):
    """级联: 6 个可算信号 → 角色 → 粒度策略。

    策略跨类目固定; 角色只看数据形状, 不看属性名(所以换类目不用重写规则表)。
    """
    out = []
    for r in res:
        if (r["布尔型"] or r["是/否比例"] >= 0.7) and r["唯一值"] <= 6:
            role, tgt = "D 二值层", 2
            why = "布尔型(值域%.0f%%落在 是/否 或同骨架+有无前缀) 且水平≤6 → 强制二值(未知单列)" % (
                r["是/否比例"] * 100)
        elif r["数值比例"] >= 0.6:
            role, tgt = "N 数值规格", r["唯一值"]
            why = "数值占比%.0f%% → 保留原值(报告层再分档)" % (r["数值比例"] * 100)
        elif r["多值父体%"] >= 30 or r["父体内中位"] >= 2:
            role, tgt = "P 设计参数", max(2, r["帕累托80%需"])
            why = "同款多变体(多值父体%.0f%%/父体内中位%d) → 帕累托截断" % (
                r["多值父体%"], r["父体内中位"])
        elif r["过闸门水平数"] >= 3 and r["唯一值"] <= 30:
            role, tgt = "A 细分轴", r["过闸门水平数"]
            why = "全部过闸门(父体≥5/品牌≥3/行数≥3%%)的水平 %d 个 → 用它切格子" % r["过闸门水平数"]
        elif r["唯一值"] > 30:
            role, tgt = "E 长尾枚举", max(2, r["帕累托80%需"])
            why = "长尾(唯一值%d) → 先归并再帕累托" % r["唯一值"]
        else:
            role, tgt = "? 需人工", 0
            why = "信号不足(过闸门%d/唯一值%d) → 看一眼" % (r["过闸门水平数"], r["唯一值"])
        out.append([r["属性"], role, tgt or "—", r["唯一值"], r["过闸门水平数"],
                    r["帕累托80%需"], "%.0f%%" % r["多值父体%"], why])
    order = {"A": 0, "P": 1, "D": 2, "N": 3, "E": 4, "?": 9}
    out.sort(key=lambda x: (order.get(x[1][0], 8), -x[3]))
    return out


def cmd_granularity(cat, cat_dir, out_dir, attrs):
    """按「属性角色」算每个属性的粒度建议(目标水平数), 替代 granularity 感觉参数。"""
    std = os.path.join(cat_dir, "18_标准化", "%s-属性标注-标准化交付.xlsx" % cat)
    if not os.path.exists(std):
        return ["! 找不到交付表: %s" % std]
    res = _analyse_gran(std, "属性标注", attrs=None)   # 粒度建议: 扫描全部属性列(排除指标/身份列)
    rows = _advise_gran(res)
    p = os.path.join(out_dir, "%s-粒度建议.xlsx" % cat)
    write_ledger(p, {"粒度建议": (
        ["属性", "角色", "建议目标水平数", "唯一值", "过闸门水平数", "帕累托80%需",
         "多值父体%", "依据"], rows)})
    auto = sum(1 for r in rows if not r[1].startswith("?"))
    return ["粒度建议(%d 个属性): 自动可判 %d(%.0f%%), 需人工 %d → %s" % (
        len(rows), auto, auto / max(1, len(rows)) * 100, len(rows) - auto, p),
            "  " + "；".join("%s[%s]→%s" % (r[0], r[1].split()[0], r[2]) for r in rows[:12])]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cat", default="Cutting Boards-US-202608")
    ap.add_argument("--attrs", default="主体材质,产品类型,套装数量,尺寸规格,形状,厚度,适用场景")
    ap.add_argument("--init", action="store_true", help="生成规则模板")
    ap.add_argument("--compare", action="store_true", help="三级值域逐值对照表")
    ap.add_argument("--suggest", action="store_true", help="LLM 出建议归并(落成建议表, 需人工审核)")
    ap.add_argument("--granularity", action="store_true", help="按属性角色算粒度建议(目标水平数)")
    ap.add_argument("--batch", type=int, default=45, help="建议归并每批源值数")
    ap.add_argument("--rules", default=None)
    ap.add_argument("--source", choices=["17", "18"], default="18")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    cat_dir = os.path.join(_ROOT, "data", args.cat)
    out_dir = os.path.join(cat_dir, OUT_DIR)
    os.makedirs(out_dir, exist_ok=True)
    attrs = [a.strip() for a in args.attrs.split(",") if a.strip()]
    lines = []
    if args.compare:
        lines += cmd_compare(args.cat, cat_dir, out_dir, attrs)
    if args.init:
        lines += cmd_init(args.cat, cat_dir, out_dir, attrs)
    if args.suggest:
        lines += cmd_suggest(args.cat, cat_dir, out_dir, attrs,
                             source=("17" if args.source == "17" else "18"), batch=args.batch)
    if args.granularity:
        lines += cmd_granularity(args.cat, cat_dir, out_dir, attrs)
    if args.rules:
        lines += cmd_run(args.cat, cat_dir, out_dir, attrs, args.rules, args.source, args.apply)
    if not (args.compare or args.init or args.rules or args.suggest or args.granularity):
        ap.print_help()
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
