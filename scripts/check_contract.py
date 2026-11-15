"""跨步骤数据契约一致性检查。

对每个步骤模块:
1. 提取 artifacts 声明与上游读取的文件引用
2. 检查上游引用的文件是否由某个下游步骤的 artifacts 覆盖(生产者存在)
3. 检查文件命名是否遵循 <类目名>- 前缀 / filter- / screen- 等约定

## 2026-09-29 修两个"检查器自身的"假告警

① **步骤号必须从类声明取, 不能从文件名取**: `steps/step17_backup_monitor.py` 的
   `number = 20`(步骤20 备份+体检; 2026-10-09 前是 19), 文件名里却带 17 —— 原实现用文件名取号, 于是拿
   **FR-17 的契约去检查步骤19 的代码**, 必然报"契约产物 … 未在代码中出现"两条假问题。
   现改为先读 `number = N`, 取不到才回落文件名。
② **契约常量可能定义在父类模块里**: 步骤18 继承步骤17(共享 `STD_XLSX_TPL` 等常量),
   文件名字符串只出现在步骤17 的源码里 —— 原实现只看本文件, 于是报"未在代码中出现"。
   现把该文件 `from steps.X import …` 的模块源码一并纳入扫描。
"""
from __future__ import annotations

import glob
import os
import re
import sys

CONTRACT = {
    "00": ["<CAT>-bestseller-newrelease.json", "<CAT>-bestseller-newrelease-all.txt"],
    "01": ["<CAT>-dual-details.jsonl"],
    "02": ["filter-<CAT>-prompt.json"],
    "03": ["<CAT>-dual-details-filtered.jsonl", "<CAT>-filtered-dual.txt"],
    "04": ["<CAT>-keywords-candidates.txt", "<CAT>-keywords.txt",
           "<CAT>-keywords-entities.txt"],
    "05": ["<CAT>-amazon-keyword-data.json"],
    "06": ["<CAT>-amazon-monthly.json"],
    "07": ["screen-<CAT>.py", "<CAT>-screen-results.json"],
    "08": ["<CAT>-keyword-report.xlsx"],
    "09": ["<CAT>-search-asins.json", "<CAT>-search-asins-all.txt"],
    "10": ["<CAT>-results.jsonl", "<CAT>-父项变体明细.xlsx", "<CAT>-parents.txt",
           "<CAT>-variants.txt", "<CAT>-过滤审计.csv"],
    "11": ["11_查流量来源/合并-11_查流量来源.xlsx"],
    "12": ["12_查竞品/<CAT>-submitted.json"],
    "13": ["13_查竞品下载/合并-13_查竞品下载.xlsx"],
    "14": ["14_流量拆分/流量拆分数据-需人工补充.xlsx"],
    "15": ["<CAT>-初步类目分析报告.xlsx"],
    "16": ["<CAT>-attribute-spec.json"],
    # 17/18 于 2026-09-22 拆分: 17 只做标注(稳定性结果 + 单遍原始 + 唯一值统计),
    # 18 只做标准化(标准化交付); 补充归档件也登记在这里, 免得别处引用时报"无生产者"。
    # ★ 步骤17 的 run() 还会**跨目录**往 `18_标准化/` 写 3 个文件(词表聚类报告.json + 待确认清单.json/.md,
    #   经 _generate_vocab → _write_vocab_report/_write_pending_review)—— 但它们物理上落在 18_标准化/、
    #   由步骤18 的 extra_archive_files 归档, 故 DECLARED 侧登记在 18(见下), 不重复登记在 17。
    "17": ["17_属性标注/稳定性结果.json",
           "17_属性标注/<CAT>-属性标注-单遍原始.xlsx",
           "17_属性标注/唯一值统计.json",
           "17_属性标注/input.json",
           "17_属性标注/视觉补充.json"],
    # ★ 2026-10-20 补齐: 与 step18 的 artifacts + extra_archive_files(共 12 个)对齐——
    #   原只声明 4 个, 漏了 8 个(交付值校验 / 规则执行与输出校验 / 跨属性冲突 / 跨属性冲突纠正 /
    #   待确认清单.json/.md / <CAT>-数值异常校验.json / <CAT>-数值异常重跑指令.md)。
    "18": ["18_标准化/<CAT>-属性标注-标准化交付.xlsx",
           "18_标准化/新词初审裁决.json",
           "18_标准化/新词自动采纳记录.json",
           "18_标准化/词表聚类报告.json",
           "18_标准化/词表聚类历史.json",
           "18_标准化/交付值校验.json",
           "18_标准化/规则执行与输出校验.json",
           "18_标准化/跨属性冲突.json",
           "18_标准化/跨属性冲突纠正.json",
           "18_标准化/待确认清单.json",
           "18_标准化/待确认清单.md",
           "18_标准化/<CAT>-数值异常校验.json",
           "18_标准化/<CAT>-数值异常重跑指令.md"],
    "19": ["19_VOC报告/<CAT>-VOC报告.xlsx"],
    "20": ["20_备份与监控/health-report.json"],
}

FILENAME_PAT = re.compile(r'([A-Za-z0-9_\-\u4e00-\u9fff]+\.(?:jsonl|json|txt|py|csv|xlsx|md))')
_STEP_NUMBER_PAT = re.compile(r"^\s*number\s*=\s*(\d+)\s*$", re.M)
_STEPS_IMPORT_PAT = re.compile(r"from\s+steps\.(\w+)\s+import|import\s+steps\.(\w+)")

# 各步骤应读取的上游文件(按数据流, 含可选)
UPSTREAM = {
    "01": ["<CAT>-bestseller-newrelease.json"],
    "03": ["<CAT>-dual-details.jsonl", "filter-<CAT>-prompt.json"],
    # ★ 2026-10-16(融合版替换): step04 已改为融合引擎(登记根词+LLM+词库验证+语义审核),
    #   不再读 FR-05 挖词数据 / FR-00 双榜 ASIN(那是旧版覆盖率回测口径), 改为读 FR-02
    #   产品口径/排除词 filter-<CAT>-prompt.json(经 tools/kwgen_hybrid/glue.load_product_policy)。
    "04": ["<CAT>-dual-details-filtered.jsonl", "filter-<CAT>-prompt.json"],
    "05": ["<CAT>-keywords-candidates.txt"],
    "06": ["<CAT>-keywords.txt", "<CAT>-keywords-candidates.txt"],
    "07": ["<CAT>-amazon-keyword-data.json"],
    "08": ["<CAT>-amazon-keyword-data.json", "<CAT>-screen-results.json",
           "<CAT>-dual-details-filtered.jsonl", "<CAT>-keywords.txt",
           "<CAT>-amazon-monthly.json", "<CAT>-bestseller-newrelease.json",
           "<CAT>-dual-details.jsonl"],
    "09": ["<CAT>-screen-results.json", "<CAT>-keywords.txt",
           "<CAT>-keywords-entities.txt", "<CAT>-bestseller-newrelease-all.txt"],
    "10": ["<CAT>-search-asins-all.txt", "<CAT>-bestseller-newrelease-all.txt",
           "filter-<CAT>-prompt.json"],
    "11": ["<CAT>-parents.txt", "<CAT>-results.jsonl"],
    "12": ["<CAT>-variants.txt", "<CAT>-results.jsonl"],
    "13": ["12_查竞品/<CAT>-submitted.json", "<CAT>-variants.txt", "<CAT>-results.jsonl"],
    "14": ["<CAT>-父项变体明细.xlsx", "11_查流量来源/合并-11_查流量来源.xlsx",
           "13_查竞品下载/合并-13_查竞品下载.xlsx", "<CAT>-results.jsonl"],
    "15": ["14_流量拆分/流量拆分数据-需人工补充.xlsx", "<CAT>-keyword-report.xlsx"],
    "16": ["14_流量拆分/流量拆分数据-需人工补充.xlsx"],
    "17": ["<CAT>-attribute-spec.json", "14_流量拆分/流量拆分数据-需人工补充.xlsx"],
    # ★ `18_标准化/词表聚类报告.json`: 由**步骤17** 的 `_generate_vocab` 写出、并自行经
    #   `_load_prev_vocab_known` 读回"上一轮"报告算 delta(非步骤17 的输入);
    #   步骤18 经 `_review_vocab → _write_pending_review → _load_prev_vocab_known` 间接读入。
    #   故登记为步骤18 的输入(生产者 = CONTRACT["18"]), 不登记为步骤17 的输入。
    "18": ["17_属性标注/稳定性结果.json", "17_属性标注/input.json",
           "17_属性标注/视觉补充.json", "<CAT>-attribute-spec.json",
           "18_标准化/词表聚类报告.json"],
    # ★★ 修复（审核#2 发现①，2026-10-10）：原来声明 8 个文件，但 step19 实际**只读 2 个**
    #   （`14_流量拆分/…xlsx` 经 `step19_voc.py:53`→`tools/voc/pipeline.py:217,127-129`；
    #     `<CAT>-parents.txt` 经 `step19_voc.py:54`、`pipeline.py:221`）—— 8 条零命中，
    #   检查器只能证明"这些文件有生产者"，不能证明 19 真在用。改成真实输入。
    "19": ["14_流量拆分/流量拆分数据-需人工补充.xlsx", "<CAT>-parents.txt"],
}


def normalize(name: str) -> str:
    return name.replace("<CAT>", "CAT").strip()


def read_text(path: str) -> str:
    with open(path, encoding="utf-8-sig") as fh:
        return fh.read()


def step_number(path: str, src: str) -> str:
    """步骤号(两位): 优先取类声明 `number = N`, 取不到才回落文件名里的数字。

    见模块 docstring ① —— `step17_backup_monitor.py` 的文件名与类声明不一致。
    """
    m = _STEP_NUMBER_PAT.search(src)
    if m:
        return "%02d" % int(m.group(1))
    m2 = re.search(r"step(\d+)_", os.path.basename(path))
    return m2.group(1) if m2 else ""


def imported_module_texts(src: str) -> list[str]:
    """本文件引用的其它 `steps.*` 模块源码(为的是看得见继承来的契约常量)。"""
    out: list[str] = []
    for a, b in _STEPS_IMPORT_PAT.findall(src):
        name = a or b
        mp = os.path.join("steps", name + ".py")
        if name and os.path.exists(mp):
            out.append(read_text(mp))
    return out


def strip_comments(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("#"))


def extract_filenames(src: str, extra_texts: list[str] | None = None) -> set[str]:
    """从源码里提取文件名(注释行不算); f-string 的类目变量统一替换为 CAT。"""
    code = strip_comments(src)
    for t in (extra_texts or []):
        code += "\n" + strip_comments(t)
    for pat in (r"\{self\._cat\}", r"\{self\.cat\}", r"\{cat\}"):
        code = re.sub(pat, "CAT", code)
    return set(FILENAME_PAT.findall(code))


def check_keyword_reports() -> list[str]:
    """关键词报告卫生: `长尾词` sheet 里**不能有方法/口径行**(2026-10-10 用户要求做成机制)。

    背景: step8 原先把 3 行 `搜索配置`(方法标签写在 `维度` 列)追加进 `长尾词` sheet, 全仓库 35/35 份
    报告都这样, 下游会把 `搜索配置` 当成长尾词。写入端已修(`steps/step08_keyword_report.py`), 这里加一道
    **检测闸**防止回潮; 存量清理用 `python scripts/clean_keyword_report.py --apply`。
    """
    issues: list[str] = []
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from core.keyword_report_clean import default_reports, find_dirty
    except Exception as exc:                            # noqa: BLE001
        return [f"关键词报告卫生检查跳过(无法导入清理模块: {exc})"]
    for p in default_reports(include_archive=False):    # 只看在线类目; 归档是历史快照
        found = find_dirty(p)
        n = len((found or {}).get("长尾词") or [])
        if n:
            issues.append("关键词报告含方法行脏数据: %s(%d 行, 例: %s) → 跑 "
                          "`python scripts/clean_keyword_report.py --apply`"
                          % (p, n, (found["长尾词"][0] or {}).get("keyword")))
    return issues


def check() -> list[str]:
    """跑一遍检查, 返回问题列表(空 = 通过)。"""
    issues: list[str] = []
    issues += check_keyword_reports()
    all_artifacts = {normalize(n) for names in CONTRACT.values() for n in names}
    all_bases = {a.split("/")[-1] for a in all_artifacts}
    for path in sorted(glob.glob("steps/step*.py")):
        src = read_text(path)
        num = step_number(path, src)
        found = extract_filenames(src, imported_module_texts(src))
        found_base = {f.split("/")[-1] for f in found}
        for a in [normalize(n) for n in CONTRACT.get(num, [])]:
            if a not in found and a.split("/")[-1] not in found_base:
                issues.append(f"步骤{num}: 契约产物 {a} 未在代码中出现({os.path.basename(path)})")
        # 上游引用是否由某步骤产出
        for up in UPSTREAM.get(num, []):
            key = normalize(up)
            if key in all_artifacts or key.split("/")[-1] in all_bases:
                continue
            issues.append(f"步骤{num}: 上游引用 {up} 无对应生产者")
    return issues


def main() -> int:
    issues = check()
    if issues:
        print("发现问题:")
        for i in issues:
            print("  -", i)
        return 1
    print("跨步骤契约检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
