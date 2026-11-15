# 单元测试: 整合市场报告(HTML) —— 三份输入缺一不可(2026-10-09 新增)
#
# 用户口径: "通过步骤8 关键词报告 + 步骤18 属性标注-标准化交付 + 步骤19 VOC报告 一键生成整合报告,
#            这三个报告缺一个不可, 缺其中一个都无法生成整合报告; 报告形式按 html, 点击进去看"。
# 本文件钉住:
#   ① **门槛**: 缺任何一份 → 拒绝生成(退出码 2)且**不写任何产物**;
#   ② **契约**: 输出必须是 kit 的四模块顺序(关键词分析/市场大盘分析/VOC差评分析/选品建议),
#      columns 与 headers 等长、表 id 唯一、sourceRow≥1、无 NaN —— 否则 renderer 会拒绝/页面崩;
#   ③ **产物**: 单文件离线 HTML(无任何外部 script/link/http 资源引用)+ report.json;
#   ④ 两个曾经踩到的实现坑: 价格段必须**互斥**(每段各自的变体数)、功能三态"未明确"**不计入分母**。
import importlib.util
import json
from pathlib import Path

import pytest
from openpyxl import Workbook

ROOT = Path(__file__).resolve().parents[1]
CAT = "Demo Cat-US-202608"
SCRIPT = ROOT / "scripts" / "build_integrated_report_html.py"


def _load():
    spec = importlib.util.spec_from_file_location("_birh", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


birh = _load()


# --------------------------------------------------------------------------- 夹具

def _mk_keyword_xlsx(path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "结论总览"
    ws.append(["项目", "内容"])
    ws.append(["生成日期", "2026-10-09"])
    ws = wb.create_sheet("核心词分析")
    ws.append(["关键词", "站内月搜索量", "站内同比%", "Google指数", "Google同比%", "CPC",
               "CPC下限", "CPC上限", "达标判定", "Google季节性", "站内季节性"])
    ws.append(["demo word", 50000, 12.5, 40, 3.0, 1.2, 0.9, 1.8, "达标", "旺季:12月", "旺季:7月"])
    ws.append(["second word", 20000, -5.0, 30, 1.0, 0.8, 0.5, 1.1, "达标", "旺季:11月", "旺季:8月"])
    ws.append(["weak word", 0, 0, None, None, None, None, None, "数据不足", "数据不足", "数据不足"])
    ws = wb.create_sheet("季节性分析")
    ws.append(["关键词"] + ["%d月" % i for i in range(1, 13)] + ["3高峰"])
    ws.append(["demo word"] + [1000 + i * 10 for i in range(12)] + ["7月"])
    ws.append(["second word"] + [500 + i * 5 for i in range(12)] + ["8月"])
    ws = wb.create_sheet("长尾词")
    ws.append(["长尾词", "维度", "说明"])
    ws.append(["demo word for kitchen", "—", "来自 FR-04 关键词文件"])
    ws.append(["second word for home", "—", "来自 FR-04 关键词文件"])
    # 方法/口径行: `长尾词` 列是 `搜索配置`, 把方法标签写进了 `维度` 列(真实源表就是这样)
    ws.append(["搜索配置", "搜索页数", "核心大词每词搜索 5 页, 长尾词每词搜索 3 页"])
    ws.append(["搜索配置", "广告处理", "只取自然位, 跳过 Sponsored 条目"])
    wb.save(path)


def _mk_product_xlsx(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    hdr = ["父ASIN", "变体ASIN", "标题", "品牌", "商品详情页链接", "价格($)", "变体月销量",
           "变体月销额", "评分", "评分数", "新老品", "配送方式", "产品类型", "主体材质",
           "是否双面可用", "是否带手柄"]
    wb = Workbook()
    ws = wb.active
    ws.title = "属性标注"
    ws.append(hdr)
    rows = [
        # 6 个新品 + 6 个老品(功能宣称表要求两个分组各有 ≥5 条"明确"取值);
        # 含 1 条"未明确"(不计入分母)与 1 条评分 0(排除出评分均值)
        ("B095CCB4B5", "B095CCB4B5", "New Double Sided Board", "BrandA", "https://x/1", 12.0, 100, 1200.0, 4.5, 50, "新品", "FBA", "竹制砧板", "竹制", "双面可用", "带手柄"),
        ("B0B6A1E434", "B0B6A1E434", "New Plain Board", "BrandA", "https://x/2", 25.0, 80, 2000.0, 4.0, 30, "新品", "FBA", "实木砧板", "木制", "单面使用", "未明确"),
        ("B0398EBA94", "B0398EBA94", "New Board 3", "BrandB", "https://x/3", 30.0, 60, 1800.0, 4.2, 20, "新品", "FBA", "竹制砧板", "竹制", "双面可用", "带手柄"),
        ("B0C42356AE", "B0C42356AE", "New Board 4", "BrandB", "https://x/4", 45.0, 40, 1800.0, 4.1, 15, "新品", "AMZ", "塑料砧板", "塑料", "双面可用", "带手柄"),
        ("B0622D5AF9", "B0622D5AF9", "New Board 5", "BrandC", "https://x/5", 60.0, 30, 1800.0, 3.8, 12, "新品", "FBA", "实木砧板", "木制", "双面不同材质", "无手柄"),
        ("B08287FD5B", "B08287FD5B", "New Board 6", "BrandC", "https://x/6", 75.0, 20, 1500.0, 4.4, 8, "新品", "AMZ", "竹制砧板", "竹制", "未明确", "带手柄"),
        ("B0D6345C65", "B0D6345C65", "Old Board 7", "BrandD", "https://x/7", 18.0, 260, 4680.0, 4.0, 80, "老品", "AMZ", "竹制砧板", "竹制", "单面使用", "无手柄"),
        ("B09C1DA496", "B09C1DA496", "Old Board 8", "BrandD", "https://x/8", 33.0, 210, 6930.0, 4.3, 55, "老品", "FBA", "实木砧板", "木制", "双面可用", "带手柄"),
        ("B037B75E57", "B037B75E57", "Old Board 9", "BrandE", "https://x/9", 52.0, 130, 6760.0, 4.1, 40, "老品", "FBA", "塑料砧板", "塑料", "单面使用", "带手柄"),
        ("B082EF233E", "B082EF233E", "Old Board 10", "BrandE", "https://x/10", 90.0, 90, 8100.0, 0.0, 10, "老品", "FBA", "塑料砧板", "塑料", "单面使用", "无手柄"),
        ("B0B27CC53A", "B0B27CC53A", "Old Board 11", "BrandF", "https://x/11", 120.0, 40, 4800.0, 4.8, 25, "老品", "FBA", "实木砧板", "木制", "双面不同材质", "带手柄"),
        ("B01262E82E", "B01262E82E", "Old Board 12", "BrandF", "https://x/12", 140.0, 20, 2800.0, 4.6, 15, "老品", "AMZ", "实木砧板", "木制", "未明确", "无手柄"),
    ]
    for r in rows:
        ws.append(list(r))
    wb.create_sheet("来源").append(hdr)      # 引擎会读「来源」sheet(可空)
    wb.save(path)


def _voc_payload() -> dict:
    """VOC 读取器的输出结构(真实键名取自 tools/cat_report/voc_reader.py 的返回)。"""
    return {
        "available": True,
        "overview": {"去重后唯一评论": 300, "父ASIN 数": 8, "1–3★(负向)评论": 180,
                     "4–5★(正向)评论": 120, "采集来源": "pangolinfo:300",
                     "页数上限": "每父体 5 页"},
        "issues": [{"问题": "手柄开裂", "所属属性": "做工/结构", "负向评论数": 40,
                    "问题占比": 0.2222, "负向优先级分": 71.0, "优先级": "P1", "安全等级": "C"},
                   {"问题": "尺寸偏小", "所属属性": "尺寸", "负向评论数": 25,
                    "问题占比": 0.1389, "负向优先级分": 55.0, "优先级": "P2", "安全等级": "C"}],
        "drivers": [{"属性": "双面可用", "正向评论数": 60, "正向驱动分": 78.0, "解读": "保留并放大"}],
        "matrix": [{"属性": "双面可用", "象限": "稳定优势—必须保留", "正向提及率": 0.5,
                    "负向问题率": 0.02, "证据合计": 62}],
        "claims": [{"宣称方向": "耐用", "对应属性": "做工/结构", "负向证据条数": 18, "风险等级": "高"}],
        "audit": {"requests": 12, "pages": 30, "points": 150, "sources": {"pangolinfo": 12}},
        "evidence_ids": {"R1", "R2", "R3"},
        "limitations": ["样本为页数上限内评论, 不得当作故障率"],
    }


@pytest.fixture()
def ws_env(tmp_path, monkeypatch):
    """造一个最小的 data/<CAT>/ 三份输入 + 把 VOC 读取器换成罐头数据(不依赖真实 VOC 工作簿)。"""
    data_root = tmp_path / "data"
    cat_dir = data_root / CAT
    cat_dir.mkdir(parents=True)
    _mk_keyword_xlsx(cat_dir / f"{CAT}-keyword-report.xlsx")
    _mk_product_xlsx(cat_dir / "18_标准化" / f"{CAT}-属性标注-标准化交付.xlsx")
    voc = cat_dir / "19_VOC报告" / f"{CAT}-VOC报告.xlsx"
    voc.parent.mkdir(parents=True)
    voc.write_bytes(b"placeholder")                     # 门槛只认"文件存在"
    monkeypatch.setattr(birh, "read_voc_workbook", lambda path: _voc_payload())
    return {"root": tmp_path, "data_root": data_root, "cat_dir": cat_dir, "voc": voc}


# --------------------------------------------------------------------------- ① 门槛

def test_gate_passes_when_all_three_present(ws_env):
    inputs = birh.locate_inputs(CAT, str(ws_env["data_root"]))
    assert birh.check_gate(inputs) == []
    assert all(v["exists"] for v in inputs.values())


@pytest.mark.parametrize("missing_step,rel", [
    (8, "{cat}-keyword-report.xlsx"),
    (18, "18_标准化/{cat}-属性标注-标准化交付.xlsx"),
    (19, "19_VOC报告/{cat}-VOC报告.xlsx"),
])
def test_gate_blocks_when_any_one_missing(ws_env, missing_step, rel):
    p = ws_env["cat_dir"] / rel.format(cat=CAT)
    p.unlink()
    inputs = birh.locate_inputs(CAT, str(ws_env["data_root"]))
    missing = birh.check_gate(inputs)
    assert [m["step"] for m in missing] == [missing_step]
    assert missing[0]["name"]                          # 报错必须点名是哪一份


def test_cli_returns_2_and_writes_nothing_when_incomplete(ws_env, capsys):
    (ws_env["cat_dir"] / "19_VOC报告" / f"{CAT}-VOC报告.xlsx").unlink()
    out = ws_env["root"] / "out"
    rc = birh.main(["--cat", CAT, "--data-root", str(ws_env["data_root"]), "--out-dir", str(out)])
    txt = capsys.readouterr().out
    assert rc == 2
    assert "缺 1 份输入" in txt and "拒绝生成" in txt
    assert not out.exists(), "门槛不过时不能产出任何文件"


# --------------------------------------------------------------------------- ② 契约 + 产物

def test_build_report_has_five_sheets_in_kit_order(ws_env):
    """sheet 顺序: 决策摘要 → 关键词分析 → 市场大盘分析 → VOC差评分析 → 选品建议
    (2026-10-10 按 .codex skill 的 html-report-contract §2 增加首屏「决策摘要」)。"""
    report, _ = birh.build(CAT, str(ws_env["data_root"]),
                           voc=str(ws_env["cat_dir"] / "19_VOC报告" / f"{CAT}-VOC报告.xlsx"))
    assert [s["name"] for s in report["sheets"]] == birh.SHEET_ORDER
    assert birh.SHEET_ORDER[0] == "决策摘要"
    birh.validate_report(report)


def test_cli_generates_single_file_offline_html(ws_env, capsys):
    out = ws_env["root"] / "out"
    rc = birh.main(["--cat", CAT, "--data-root", str(ws_env["data_root"]), "--out-dir", str(out)])
    assert rc == 0
    html = (out / "index.html").read_text(encoding="utf-8")
    assert (out / "report.json").is_file() and (out / "构建日志.txt").is_file()
    # 单文件离线: 不得有任何外部 script/link 或 http(s) 资源引用
    import re
    assert not re.search(r"<(?:script|link)[^>]+(?:src|href)=", html)
    assert not re.search(r'(?:src|href)="https?://', html)
    assert "window.REPORTS" in html
    for name in birh.SHEET_ORDER:
        assert name in html
    assert "Cat Fountains" not in html                 # kit 侧栏写死的示例类目名要被替换
    data = json.loads((out / "report.json").read_text(encoding="utf-8"))
    birh.validate_report(data)


def test_report_json_is_reproducible_and_provenance_present(ws_env):
    out = ws_env["root"] / "out"
    birh.main(["--cat", CAT, "--data-root", str(ws_env["data_root"]), "--out-dir", str(out)])
    data = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert data["id"].startswith(f"{CAT}-integrated-")
    ids = [t["id"] for s in data["sheets"] for sec in s["sections"] for t in sec["tables"]]
    assert len(ids) == len(set(ids))                   # 表 id 全局唯一
    assert all(t["sourceRef"] or t["sourceLabel"] for s in data["sheets"]
               for sec in s["sections"] for t in sec["tables"]), "每张表都要有来源"


# --------------------------------------------------------------------------- ③ 两个历史坑

def test_price_bands_are_mutually_exclusive(ws_env):
    """价格段必须互斥: 每段的变体数各自统计(曾因兜底分支把全部变体算进每一段)。"""
    report, _ = birh.build(CAT, str(ws_env["data_root"]),
                           voc=str(ws_env["cat_dir"] / "19_VOC报告" / f"{CAT}-VOC报告.xlsx"))
    t = next(t for s in report["sheets"] for sec in s["sections"] for t in sec["tables"]
             if t["id"] == "m-price")
    counts = [r["values"][1] for r in t["rows"]]
    assert len(counts) >= 2
    assert sum(counts) == 12, f"各段变体数之和应等于有效变体总数: {counts}"
    assert max(counts) < 12, "任何一段都不该等于全量(说明分段没生效)"


def _row_by(tabs, tid, col_idx, key):
    """按某列的值取整行 → dict(表头→值); 用**表头名**断言, 表头顺序调整不会误报。"""
    tab = tabs[tid]
    for r in tab["rows"]:
        if r["values"][col_idx] == key:
            return dict(zip(tab["headers"], r["values"]))
    raise AssertionError("表 %s 里找不到 %r" % (tid, key))


def test_feature_coverage_excludes_unknown(ws_env):
    """功能三态(skill §4 覆盖率口径): 『未明确』不计入分母、也不当作『否』。"""
    report, _ = birh.build(CAT, str(ws_env["data_root"]),
                           voc=str(ws_env["cat_dir"] / "19_VOC报告" / f"{CAT}-VOC报告.xlsx"))
    tabs = {t["id"]: t for s in report["sheets"] for sec in s["sections"] for t in sec["tables"]}
    row = _row_by(tabs, "m-life-func", 0, "是否双面可用")
    # 新品 6 条: 1 条"未明确" → 明确分母 5; 3 条"双面可用" + 1 条"双面不同材质" → 具备数 4
    assert row["新品已明确数"] == 5 and row["新品具备数"] == 4
    assert round(row["新品覆盖率"], 4) == 0.8
    # 老品 6 条: 1 条"未明确" → 分母 5; 2 条具备 → 0.4
    assert row["老品已明确数"] == 5 and row["老品具备数"] == 2
    assert round(row["老品覆盖率"], 4) == 0.4
    assert round(row["百分点差"], 2) == 40.0
    assert 0 < row["新品证据完整度"] <= 1 and 0 < row["老品证据完整度"] <= 1
    assert row["样本判定"] in ("样本充分", "明确数少", "证据覆盖低")


# --------------------------------------------------------------------------- ④ 契约校验器

def _sheet_text(sheet: dict) -> str:
    """sheet 内**全部**文字: 各节旁注 + 洞察 + 表格(含末尾「方法与口径」节)。

    2026-10-12 起例行口径集中到 `*-method` 节(contract §2 L95 / QA L17), 因此口径类断言
    必须读全 sheet 文本, 而不是只看某节旁注。
    """
    parts = []
    for s in sheet["sections"]:
        parts += [str(n) for n in s.get("notes") or []]
        parts += [str(i.get("text")) for i in s.get("insights") or []]
        for tb in s.get("tables") or []:
            parts += [str(tb.get("sourceLabel") or ""), str(tb.get("sourceRef") or "")]
            parts += [str(v) for r in tb["rows"] for v in r["values"]]
    return " ".join(parts)


def _core_sections(sheet: dict) -> list:
    """排除集中口径节后的模块标题/ID —— 模块顺序断言只关心分析模块。"""
    return [s for s in sheet["sections"] if not str(s["id"]).endswith("-method")]


def _sheet(report: dict, name: str) -> dict:
    """按名字取 sheet —— 报告从 4 张表扩成 5 张(新增首屏「决策摘要」)后, 位置索引不再可靠。"""
    return next(s for s in report["sheets"] if s["name"] == name)


def _payload_sheet(payload_report: dict, name: str) -> dict:
    return next(s for s in payload_report["sheets"] if s["name"] == name)


def _minimal_report() -> dict:
    t = birh.table("t1", "T", ["A", "B"], [["x", 1]])
    sec = birh.section("s1", "S", [t])
    sheets = []
    for i, n in enumerate(birh.SHEET_ORDER):
        # 第一张表带 table(校验器的破坏性用例都作用在它上面), 其余给空 section
        sheets.append(birh.sheet(n, "T", "S", [birh.kpi("K", 1)], [sec])
                      if i == 0 else birh.sheet(n, "T", "S", [], [birh.section("s-%s" % n, "S", [])]))
    return {"id": "x", "title": "t", "filename": "f.html", "generatedAt": "2026-10-09T00:00:00",
            "sheets": sheets}


def test_validate_accepts_minimal_report():
    birh.validate_report(_minimal_report())


@pytest.mark.parametrize("mutate,err", [
    (lambda r: r.pop("title"), "顶层字段"),
    (lambda r: r["sheets"].reverse(), "sheets 必须按"),
    (lambda r: r["sheets"][0]["sections"][0]["tables"].append(
        dict(r["sheets"][0]["sections"][0]["tables"][0])), "表 id 重复"),
    (lambda r: r["sheets"][0]["sections"][0]["tables"][0]["columns"].append(99), "不等长"),
    (lambda r: r["sheets"][0]["sections"][0]["tables"][0]["rows"][0]["values"].append("extra"), "长度"),
    (lambda r: r["sheets"][0]["sections"][0]["tables"][0]["rows"][0]["values"].__setitem__(1, float("nan")), "NaN"),
    (lambda r: r["sheets"][0]["sections"][0]["tables"][0].__setitem__("sourceHeaderRow", 0), "≥1"),
])
def test_validate_rejects_broken_reports(mutate, err):
    r = _minimal_report()
    mutate(r)
    with pytest.raises(ValueError) as ei:
        birh.validate_report(r)
    assert err in str(ei.value)


# --------------------------------------------------------------------------- ⑤ 进度上报(按钮显示用)

def test_progress_file_location_and_atomic_write(tmp_path):
    out = tmp_path / CAT / birh.OUT_DIR / birh.HTML_SUBDIR
    p = birh.progress_path(out)
    assert p == tmp_path / CAT / birh.OUT_DIR / birh.PROGRESS_NAME
    birh._progress(p, "读取步骤8 关键词报告", 50)
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["phase"] == "读取步骤8 关键词报告" and data["pct"] == 50
    assert data["done"] is False and data["ts"] and data["pid"]
    assert not list(p.parent.glob("*.tmp")), "进度必须原子替换, 不留 .tmp"


def test_progress_is_best_effort(tmp_path):
    """进度写失败绝不该影响报告生成(盘满/权限等) → 静默忽略。"""
    birh._progress(tmp_path / "file.txt", "x", 1)                    # 父路径是文件 → OSError
    birh._progress(tmp_path / "nope" / "\0bad" / "x.json", "x", 1)   # 非法路径 → ValueError
    birh._progress(None, "x", 1)                                     # None = 不上报


def test_build_reports_monotonic_progress(ws_env):
    seen = []
    report, _ = birh.build(CAT, str(ws_env["data_root"]),
                           voc=str(ws_env["cat_dir"] / "19_VOC报告" / f"{CAT}-VOC报告.xlsx"),
                           progress=lambda phase, pct: seen.append((phase, pct)))
    assert birh.SHEET_ORDER == [s["name"] for s in report["sheets"]]
    assert len(seen) >= 5
    assert [p for _, p in seen] == sorted(p for _, p in seen), "百分比必须单调不回退"
    phases = " ".join(ph for ph, _ in seen)
    for need in ("步骤18", "步骤8", "VOC", "选品建议"):
        assert need in phases, phases


def test_cli_writes_done_progress(ws_env):
    out = ws_env["root"] / "out"
    rc = birh.main(["--cat", CAT, "--data-root", str(ws_env["data_root"]), "--out-dir", str(out)])
    assert rc == 0
    data = json.loads(birh.progress_path(out).read_text(encoding="utf-8"))
    assert data["done"] is True and data["pct"] == 100 and not data["error"]


def test_cli_writes_failed_progress_on_error(ws_env, monkeypatch):
    """生成失败也要落一条 done+error 的进度, 否则看板按钮会一直卡在上一帧。"""
    def boom(*a, **k):
        raise ValueError("模拟渲染失败")
    monkeypatch.setattr(birh, "_render_html", boom)
    out = ws_env["root"] / "out"
    rc = birh.main(["--cat", CAT, "--data-root", str(ws_env["data_root"]), "--out-dir", str(out)])
    assert rc == 1
    data = json.loads(birh.progress_path(out).read_text(encoding="utf-8"))
    assert data["done"] is True and "模拟渲染失败" in data["error"]


# --------------------------------------------------------------------------- ⑦ 2026-10-10 三个真事故的回归

def _mk_product_unregistered_xlsx(path: Path) -> None:
    """只带**未登记到引擎词表**的 `是否*` 列(模拟 Dog Dryer: 是否便携/是否免手持),
    且新老品各 6 条(满足 skill §4 的"每队列已明确数 ≥5"信号门槛)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    hdr = ["父ASIN", "变体ASIN", "标题", "品牌", "商品详情页链接", "价格($)", "变体月销量",
           "变体月销额", "评分", "评分数", "新老品", "配送方式", "产品类型", "是否便携", "是否免手持"]
    wb = Workbook()
    ws = wb.active
    ws.title = "属性标注"
    ws.append(hdr)
    for r in [
        # 新品 6 条: 便携迷你 ×3, 常规台式 ×2, 专业大功率 ×1; 免手持: 手持式 ×4, 未明确 ×2
        ("B095CCB4B5", "B095CCB4B5", "A", "BrandA", "https://x/1", 20.0, 100, 2000.0, 4.5, 50, "新品", "FBA", "宠物吹水机", "便携迷你", "手持式"),
        ("B0B6A1E434", "B0B6A1E434", "B", "BrandA", "https://x/2", 30.0, 80, 2400.0, 4.0, 30, "新品", "FBA", "宠物吹水机", "便携迷你", "手持式"),
        ("B0398EBA94", "B0398EBA94", "C", "BrandB", "https://x/3", 45.0, 60, 2700.0, 4.2, 20, "新品", "FBA", "宠物吹水机", "便携迷你", "未明确"),
        ("B0C42356AE", "B0C42356AE", "D", "BrandB", "https://x/4", 60.0, 40, 2400.0, 4.1, 15, "新品", "AMZ", "宠物吹水机", "常规台式", "手持式"),
        ("B0622D5AF9", "B0622D5AF9", "E", "BrandC", "https://x/5", 80.0, 30, 2400.0, 3.8, 12, "新品", "FBA", "宠物吹水机", "常规台式", "手持式"),
        ("B08287FD5B", "B08287FD5B", "F", "BrandC", "https://x/6", 95.0, 20, 1900.0, 4.4, 8, "新品", "AMZ", "宠物吹水机", "专业大功率", "未明确"),
        # 老品 6 条: 便携迷你 ×3, 常规台式 ×2, 专业大功率 ×1; 免手持: 手持式 ×3, 支架 ×2, 未明确 ×1
        ("B0D6345C65", "B0D6345C65", "G", "BrandA", "https://x/7", 22.0, 90, 1980.0, 4.3, 40, "老品", "FBA", "宠物吹水机", "便携迷你", "手持式"),
        ("B09C1DA496", "B09C1DA496", "H", "BrandA", "https://x/8", 32.0, 70, 2240.0, 4.1, 25, "老品", "FBA", "宠物吹水机", "便携迷你", "支架"),
        ("B037B75E57", "B037B75E57", "I", "BrandB", "https://x/9", 42.0, 55, 2310.0, 4.0, 18, "老品", "FBA", "宠物吹水机", "便携迷你", "手持式"),
        ("B082EF233E", "B082EF233E", "J", "BrandB", "https://x/10", 58.0, 35, 2030.0, 3.9, 14, "老品", "AMZ", "宠物吹水机", "常规台式", "支架"),
        ("B0B27CC53A", "B0B27CC53A", "K", "BrandC", "https://x/11", 78.0, 25, 1950.0, 3.7, 10, "老品", "FBA", "宠物吹水机", "常规台式", "手持式"),
        ("B01262E82E", "B01262E82E", "L", "BrandC", "https://x/12", 92.0, 18, 1656.0, 4.2, 6, "老品", "AMZ", "宠物吹水机", "专业大功率", "未明确"),
    ]:
        ws.append(list(r))
    wb.create_sheet("来源").append(hdr)
    wb.save(path)


def test_build_auto_locates_voc_without_explicit_path(ws_env):
    """**看板只传 `--cat`** —— 脚本必须自己定位三份输入(否则报 `VOC 工作簿路径不存在: None`)。"""
    report, _ = birh.build(CAT, str(ws_env["data_root"]))          # 不传 voc / product
    assert [s["name"] for s in report["sheets"]] == birh.SHEET_ORDER
    birh.validate_report(report)


def test_cli_without_explicit_voc_succeeds(ws_env):
    out = ws_env["root"] / "out"
    rc = birh.main(["--cat", CAT, "--data-root", str(ws_env["data_root"]), "--out-dir", str(out)])
    assert rc == 0 and (out / "index.html").is_file()


def test_feature_distribution_and_dominant_when_wordlist_unregistered(ws_env):
    """类目 `是否*` 取值未登记『是/否』词表时: 出**取值分布** + **主配置口径**配对表,
    **不**出覆盖率口径(无法判定是/否), 也不臆断。"""
    unreg = ws_env["cat_dir"] / "18_标准化" / "unregistered.xlsx"
    _mk_product_unregistered_xlsx(unreg)
    report, _ = birh.build(CAT, str(ws_env["data_root"]), product=str(unreg),
                           voc=str(ws_env["cat_dir"] / "19_VOC报告" / f"{CAT}-VOC报告.xlsx"))
    tabs = {t["id"]: t for s in report["sheets"] for sec in s["sections"] for t in sec["tables"]}
    assert "m-feature-dist" in tabs and "m-life-func" not in tabs and "m-life-claim" in tabs
    t = tabs["m-feature-dist"]
    assert t["headers"] == ["属性", "取值", "变体数", "占该列明确取值"]
    vals = {(r["values"][0], r["values"][1]) for r in t["rows"]}
    assert ("是否便携", "便携迷你") in vals and ("是否免手持", "手持式") in vals
    assert all(r["values"][1] != "未明确" for r in t["rows"]), "『未明确』不该进取值分布"
    row = _row_by(tabs, "m-life-claim", 1, "是否便携")           # values[1] = 所属列
    assert row["属性/功能"] == "便携迷你"                        # 主配置 = 该列取值最多的那一个
    assert row["新品主配置占比"] is not None and row["老品主配置占比"] is not None
    assert row["样本判定"] in ("样本充分", "明确数少", "证据覆盖低")
    sec = next(s for s in _sheet(report, "市场大盘分析")["sections"] if s["id"] == "m-3A")
    assert "未登记" in _sheet_text(_sheet(report, "市场大盘分析")), "词表未登记说明必须仍可核对(已集中到方法与口径)"


def test_voc_ratio_zero_with_count_is_blank_not_zero(ws_env, monkeypatch):
    """源表『问题占比』整列为空时会被读成 0.0 —— 渲染必须**留空**, 不是「0.0%」。"""
    payload = _voc_payload()
    payload["issues"][0]["问题占比"] = 0.0          # 计数 40, 但占比列没填
    payload["issues"][1]["负向评论数"] = 0
    payload["issues"][1]["问题占比"] = 0.0         # 计数也是 0 → 保留 0.0, 不误伤
    monkeypatch.setattr(birh, "read_voc_workbook", lambda path: payload)
    report, _ = birh.build(CAT, str(ws_env["data_root"]), voc="x.xlsx")
    tab = {tb["id"]: tb for s in report["sheets"] for sec in s["sections"] for tb in sec["tables"]}["v-core"]
    r0 = dict(zip(tab["headers"], tab["rows"][0]["values"]))
    r1 = dict(zip(tab["headers"], tab["rows"][1]["values"]))
    assert r0["问题占比"] is None, "计数>0 而占比为 0 → 留空(不把没填当 0)"
    assert r0["负向评论数"] == 40 and r0["评论数"] == 40   # 源表缺 评论数 时用负向数兜底
    assert r1["问题占比"] == 0.0, "计数=0 的 0.0 要保留"
    sec = next(s for s in _sheet(report, "VOC差评分析")["sections"] if s["id"] == "v-1")
    assert sec["tables"], "总览节必须有表"


# --------------------------------------------------------------- ⑧ skill §4 契约(模块顺序/图表签名)

SPEC_MARKET_ORDER = ["01 报告摘要", "02 市场大盘与价格段", "03 新品老品分析",
                     "02A 新品老品功能与属性迭代", "05 品牌与履约集中度",
                     "06 产品类型与属性结构", "07 功能溢价分析",
                     "08 价格段 × 属性/功能差异分析(含功能热力图)",
                     "09 Top20 产品分析", "10 Top5品牌组合对标", "11 市场总结与洞察"]


VOC_SECTION_ORDER = ["01 差评分析_总览", "02 核心功能性差评", "03 体验感差评",
                      "04 用户场景分析", "05 安全与合规风险", "06 VOC总结与洞察",
                      "07 支撑模块(好评驱动 / 属性象限 / Listing 风险)",
                      # 2026-10-12(contract §2 第 5 块 + §3 检索): 可检索的评论证据库(评论ID + 全文, 收录上限已披露)
                      "08 评论证据库(可检索)",
                      # 2026-10-12(QA L11 全 V3 等价视图): 品牌/配件服务/机会矩阵/路线图/字典
                      "09 V3 等价视图(品牌 / 配件服务 / 机会矩阵 / 路线图 / 字典)"]


RECO_SECTION_ORDER = ["选品结论摘要", "机会白地矩阵", "候选产品方案评分", "产品定义矩阵",
                      # 2026-10-10(参照 .codex skill `new-product-hero-concept.md` + contract §2 第 6-8 块):
                      # 新品→Hero → Concept 画像(7 维) → 详细定价建议(5 点), 插在"产品定义"与"风险验证"之间
                      "新品 → Hero(首轮入围, 未确认)", "跨源证据矩阵", "Concept 画像(7 维)",
                      "详细定价建议(5 点)", "不建议方向与停止条件"]


def test_reco_sections_follow_spec_module_order(ws_env):
    """spec §6 模块顺序固定。"""
    report, _ = birh.build(CAT, str(ws_env["data_root"]),
                           voc=str(ws_env["cat_dir"] / "19_VOC报告" / f"{CAT}-VOC报告.xlsx"))
    reco = _sheet(report, "选品建议")
    assert [s["title"] for s in _core_sections(reco)] == RECO_SECTION_ORDER


def test_reco_score_is_hundred_point_with_weights(ws_env):
    """100 分制: 六个分项 0–100 × 权重, 加权=分项×权重/100, 总分=Σ加权; 权重合计 100%。"""
    report, _ = birh.build(CAT, str(ws_env["data_root"]),
                           voc=str(ws_env["cat_dir"] / "19_VOC报告" / f"{CAT}-VOC报告.xlsx"))
    reco = _sheet(report, "选品建议")
    tabs = {tb["id"]: tb for s in reco["sections"] for tb in s["tables"]}
    tab = tabs["r-score"]
    assert "方案" in tab["headers"] and "总分" in tab["headers"]
    import re as _re
    weights = [int(m) for m in _re.findall(r"加权\((\d+)%\)", " ".join(tab["headers"]))]
    assert weights == [20, 20, 15, 20, 15, 10] and sum(weights) == 100
    for r in tab["rows"]:
        row = dict(zip(tab["headers"], r["values"]))
        total = row["总分"]
        acc = 0.0
        avail_w = 0
        for k, w in zip(("关键词需求", "价段吸引", "竞争余量", "配置白地", "VOC问题价值", "证据置信"), weights):
            v = row["%s(0-100)" % k]
            wv = row["%s加权(%d%%)" % (k, w)]
            if isinstance(v, (int, float)):
                assert 0 <= v <= 100
                assert isinstance(wv, (int, float)), (k, v, wv)
                assert abs(wv - v * w / 100.0) < 0.02, (k, v, wv)
                acc += wv
                avail_w += w
            else:
                assert v == "未提供", v
                assert wv == "未提供(权重不计入分母)", (k, wv)
        if isinstance(total, (int, float)):
            # 缺失分项**权重不计入分母** → 总分 = Σ加权 / 可用权重 × 100(旁注里写明的口径)
            expect = acc * 100.0 / avail_w if avail_w else 0.0
            assert abs(total - expect) < 0.3, (row["方案"], total, expect, acc, avail_w)
            assert 0 <= total <= 100


def test_reco_labels_and_gate_follow_spec_rules(ws_env):
    report, _ = birh.build(CAT, str(ws_env["data_root"]),
                           voc=str(ws_env["cat_dir"] / "19_VOC报告" / f"{CAT}-VOC报告.xlsx"))
    reco = _sheet(report, "选品建议")
    tabs = {tb["id"]: tb for s in reco["sections"] for tb in s["tables"]}
    lab = tabs["r-label"]
    allowed = {"优先开发", "小批验证", "持续观察", "不建议进入"}
    for r in lab["rows"]:
        row = dict(zip(lab["headers"], r["values"]))
        assert row["标签"] in allowed
        conf = row["证据置信"] or 0
        if conf < 50:
            assert row["标签"] in ("持续观察", "不建议进入"), row
        if row["硬否决原因"] not in ("—", None, ""):
            assert row["标签"] == "不建议进入", row
    # 门槛说明与标签判定顺序必须写在旁注里(spec §6 要求可复现)
    notes = _sheet_text(reco)          # 含末尾「方法与口径」节(例行口径集中处)
    assert "门槛" in notes and "封顶" in notes and ("硬否决" in notes or "否决" in notes)
    # 成本/工程类输入缺失必须显式写 未提供, 且不得据此生成财务否决
    summary = " ".join(str(v) for r in tabs["r-summary"]["rows"] for v in r["values"])
    assert "未提供" in summary and "成本" in summary


def test_reco_competition_headroom_matches_band_data(ws_env):
    """竞争余量 = 100×(1 − 段内 Top3 品牌销额占比): 必须与 08 节同源数据一致。"""
    report, _ = birh.build(CAT, str(ws_env["data_root"]),
                           voc=str(ws_env["cat_dir"] / "19_VOC报告" / f"{CAT}-VOC报告.xlsx"))
    tabs = {tb["id"]: tb for s in report["sheets"] for sec in s["sections"] for tb in sec["tables"]}
    mix = {str(dict(zip(tabs["m-band-mix"]["headers"], r["values"]))["价格段"]):
           dict(zip(tabs["m-band-mix"]["headers"], r["values"])) for r in tabs["m-band-mix"]["rows"]}
    sc = {str(r["values"][0]).split("段+")[0]: dict(zip(tabs["r-score"]["headers"], r["values"]))
          for r in tabs["r-score"]["rows"]}
    for band, row in sc.items():
        top3 = mix.get(band, {}).get("Top3品牌销额占比(段内)")
        if top3 is None:
            continue
        assert abs(row["竞争余量(0-100)"] - round(100 * (1 - top3), 1)) < 0.2, (band, row)


def test_reco_definition_matrix_only_executable(ws_env):
    """`产品定义矩阵` 只列通过门槛的候选; 没有候选时不编造产品画像(spec §6)。"""
    report, _ = birh.build(CAT, str(ws_env["data_root"]),
                           voc=str(ws_env["cat_dir"] / "19_VOC报告" / f"{CAT}-VOC报告.xlsx"))
    reco = _sheet(report, "选品建议")
    tabs = {tb["id"]: tb for s in reco["sections"] for tb in s["tables"]}
    passed = [dict(zip(tabs["r-label"]["headers"], r["values"]))
              for r in tabs["r-label"]["rows"]
              if dict(zip(tabs["r-label"]["headers"], r["values"]))["是否过门槛"] == "通过"]
    def_rows = [dict(zip(tabs["r-def"]["headers"], r["values"])) for r in tabs["r-def"]["rows"]]
    if passed:
        assert len(def_rows) == len(passed)
    else:
        assert "未提供" in str(def_rows[0].get("目标价格段"))


def test_longtail_excludes_meta_rows_and_drops_empty_dim_column(ws_env):
    """源表 `长尾词` 表混着方法行(`长尾词`=`搜索配置`, 方法标签写在 `维度` 列) —— 不能当关键词列出;

    维度列整列无值时不出空列, 改为在旁注里披露"未标注维度 + 需上游补"。
    """
    report, _ = birh.build(CAT, str(ws_env["data_root"]),
                           voc=str(ws_env["cat_dir"] / "19_VOC报告" / f"{CAT}-VOC报告.xlsx"))
    kw_sheet = _sheet(report, "关键词分析")
    tabs = {tb["id"]: tb for s in kw_sheet["sections"] for tb in s["tables"]}
    long_tab = tabs["kw-long"]
    names = [r["values"][1] for r in long_tab["rows"]]
    assert "搜索配置" not in names, names
    assert "demo word for kitchen" in names and "second word for home" in names
    # 用户口径: 长尾词不要"维度"这一维度 —— 表头固定 `# | 长尾词 | 说明`, 且旁注不再提它
    assert long_tab["headers"] == ["#", "长尾词", "说明"], long_tab["headers"]
    # 只看 kw-4 节(其他节的"覆盖率维度"是另一件事, 不该误伤)
    sec4 = next(s for s in kw_sheet["sections"] if s["id"] == "kw-4")
    assert "维度" not in " ".join(n["text"] for n in sec4["notes"])
    assert all(len(r["values"]) == 3 for r in long_tab["rows"]), long_tab["rows"][:1]
    # 方法行**整行丢弃**: 连旁注里也不能出现(用户 2026-10-10 明确要求)
    whole = " ".join(str(v) for s in kw_sheet["sections"] for tb in s["tables"]
                     for r in tb["rows"] for v in r["values"]) + \
        " ".join(n["text"] for n in sec4["notes"])
    for junk in ("搜索配置", "搜索页数", "广告处理", "清单合并"):
        assert junk not in whole, junk
    # Top1 长尾也不能选到方法行
    top1 = tabs["kw-long-top1"]["rows"][0]["values"][0]
    assert top1 != "搜索配置"


def test_longtail_meta_row_detector():
    """方法行识别: 命中方法词 / 维度是方法标签 / 同表内 lavel 重复 → 判为方法行(35/35 报告都这样)。"""
    rep = {"搜索配置": 3, "dog dryer for pets": 1}
    assert birh._is_longtail_meta({"keyword": "搜索配置", "dim": "搜索页数"}, rep) is True
    assert birh._is_longtail_meta({"keyword": "随便一个词", "dim": "清单合并"}, rep) is True
    assert birh._is_longtail_meta({"keyword": "重复标签", "dim": "—"},
                                  {"重复标签": 2}) is True
    assert birh._is_longtail_meta({"keyword": "dog dryer for pets", "dim": "—"}, rep) is False
    assert birh._is_longtail_meta({"keyword": "", "dim": ""}, rep) is True


def test_voc_group_maps_taxonomy_attributes():
    """归口规则按 VOC skill taxonomy §2 的 17 类属性: 功能/体验/未分类(不硬塞)。"""
    cases = {
        "可靠性/耐用性": "功能", "核心效果/净化能力": "功能", "速度/风量/覆盖": "功能",
        "安全/信任/保护": "功能", "兼容/安装/集成": "功能", "精准度/传感一致性": "功能",
        "噪音/舒适/环境影响": "体验", "安装/操控/易用": "体验", "清洁/维护/收纳": "体验",
        "外观/尺寸/贴合": "体验", "包装/物流/开箱": "体验", "客服/退货/保修": "体验",
        "价格/价值/预期": "体验", "耗材/配件/持有成本": "体验", "与描述不符": "体验",
        "其他/未分类": "未分类", "": "未分类", "未知属性": "未分类",
    }
    for attr, want in cases.items():
        assert birh._voc_group(attr) == want, (attr, birh._voc_group(attr), want)


def test_voc_sheet_follows_spec_composition(ws_env):
    """spec §5: KPI + 1–3★ 构成 + 差评总览 Top10 + 核心功能性 + 体验感 + 用户场景 + 洞察≥6。"""
    report, _ = birh.build(CAT, str(ws_env["data_root"]),
                           voc=str(ws_env["cat_dir"] / "19_VOC报告" / f"{CAT}-VOC报告.xlsx"))
    voc = _sheet(report, "VOC差评分析")
    assert [s["title"] for s in _core_sections(voc)] == VOC_SECTION_ORDER
    assert len(voc["kpis"]) >= 6
    tabs = {tb["id"]: tb for s in voc["sections"] for tb in s["tables"]}
    n_issues = next(k["value"] for k in voc["kpis"] if k["label"] == "问题条目")
    assert len(tabs["v-core"]["rows"]) == min(10, n_issues)       # Top10(spec)
    stars = [r["values"][0] for r in tabs["v-star"]["rows"]]
    assert stars[:5] == ["1★", "2★", "3★", "4★", "5★"]           # 1–3★ mix(spec)
    assert len(stars) in (5, 6)                                   # 有星级分布时多一行 1–3★ 合计
    assert len(voc["sections"][5]["insights"]) >= 4               # fixture 规模小; ≥6 在真数据冒烟里钉


def test_voc_functional_vs_experience_grouping(ws_env):
    """功能 vs 体验感必须归口且**互斥**: 每条问题只出现在一张表里; 未归口的单列披露。"""
    report, _ = birh.build(CAT, str(ws_env["data_root"]),
                           voc=str(ws_env["cat_dir"] / "19_VOC报告" / f"{CAT}-VOC报告.xlsx"))
    voc = _sheet(report, "VOC差评分析")
    tabs = {tb["id"]: tb for s in voc["sections"] for tb in s["tables"]}
    assert "v-func" in tabs or "v-exp" in tabs, "至少要有一张归口表"
    f_names = {r["values"][0] for r in (tabs.get("v-func") or {"rows": []})["rows"]}
    e_names = {r["values"][0] for r in (tabs.get("v-exp") or {"rows": []})["rows"]}
    assert not (f_names & e_names), f_names & e_names
    for r in (tabs.get("v-func") or {"rows": []})["rows"]:
        assert birh._voc_group(str(r["values"][1])) == "功能", r["values"][:2]
    for r in (tabs.get("v-exp") or {"rows": []})["rows"]:
        assert birh._voc_group(str(r["values"][1])) == "体验", r["values"][:2]
    # 未分类的披露在有未归口条目时才出现 → 由真数据冒烟覆盖(Dog Dryer 有 2 条未分类)


def test_voc_excludes_quality_module_and_review_excerpts(ws_env):
    """spec §5 源处理: 不展示 分析口径/数据质量与限制; 不贴代表评论摘录。"""
    report, _ = birh.build(CAT, str(ws_env["data_root"]),
                           voc=str(ws_env["cat_dir"] / "19_VOC报告" / f"{CAT}-VOC报告.xlsx"))
    voc = _sheet(report, "VOC差评分析")
    joined = " ".join(s["title"] for s in voc["sections"])
    assert "数据质量" not in joined and "分析口径" not in joined
    for s in voc["sections"]:
        for tb in s["tables"]:
            assert not any("摘录" in h for h in tb["headers"]), tb["id"]
            assert not any("评论ID" in h for h in tb["headers"]), tb["id"]


def test_voc_insights_cover_minimum_topics(ws_env):
    report, _ = birh.build(CAT, str(ws_env["data_root"]),
                           voc=str(ws_env["cat_dir"] / "19_VOC报告" / f"{CAT}-VOC报告.xlsx"))
    voc = _sheet(report, "VOC差评分析")
    ins = " ".join(i["text"] for i in voc["sections"][5]["insights"])
    assert "最高频" in ins
    assert len(voc["sections"][5]["insights"]) >= 4


def test_market_sections_follow_spec_module_order(ws_env):
    report, _ = birh.build(CAT, str(ws_env["data_root"]),
                           voc=str(ws_env["cat_dir"] / "19_VOC报告" / f"{CAT}-VOC报告.xlsx"))
    mkt = _sheet(report, "市场大盘分析")
    titles = [s["title"] for s in _core_sections(mkt)]
    assert titles == SPEC_MARKET_ORDER, titles


def test_band_matrix_uses_in_band_revenue_and_heatmap_signature(ws_env):
    """spec §4 ⑧: 属性/功能占比的**分母是段内变体月销额**; 功能热力图必须命中查看器签名。"""
    report, _ = birh.build(CAT, str(ws_env["data_root"]),
                           voc=str(ws_env["cat_dir"] / "19_VOC报告" / f"{CAT}-VOC报告.xlsx"))
    mkt = _sheet(report, "市场大盘分析")
    tabs = {t["id"]: t for s in mkt["sections"] for t in s["tables"]}
    assert "m-band-mix" in tabs and "m-band-type" in tabs
    # ① 段内结构表: 主导类型/材质都带**段内销额占比**口径
    assert any("段内销额占比" in h for h in tabs["m-band-mix"]["headers"])
    # ② 价格段 × 产品类型: 每行各段占比之和 = 该类型全类目销额占比(同一分母口径的自洽性)
    tb = tabs["m-band-type"]
    band_rev = [r["values"][4] for r in tabs["m-price"]["rows"]]        # 各段月销额(分母)
    total_rev = next(k["value"] for k in mkt["kpis"] if k["label"] == "变体月销额合计(USD)")
    checked = 0
    for r in tb["rows"][:5]:
        shares = r["values"][1:-1]
        overall = r["values"][-1]
        assert all(v is None or 0 <= v <= 1.0001 for v in shares), r["values"]
        if overall is None:
            continue
        # 自洽性: Σ(段内占比 × 段销额) 应还原该类型的全类目销额 = 全类目占比 × 总销额
        recon = sum((v or 0) * (rv or 0) for v, rv in zip(shares, band_rev))
        expect = overall * total_rev
        assert abs(recon - expect) <= max(0.02 * expect, 1.0), (r["values"][0], recon, expect)
        checked += 1
    assert checked >= 1
    # ③ 功能热力图签名: 首列恰好 `功能` + 所在 section 标题含"热力图"
    heat = next((x for x in tabs.values() if x["id"] == "m-band-func"), None)
    assert heat is not None, "缺少功能×价格段热力图"
    assert heat["headers"][0] == "功能"
    sec = next(s for s in mkt["sections"] if any(tb0["id"] == "m-band-func" for tb0 in s["tables"]))
    assert "热力图" in sec["title"]
    assert 2 <= len(heat["rows"]) <= 20          # 查看器图表规则: 行数 2–20


def test_stair_matrix_has_adjacent_delta_tags(ws_env):
    """spec §4: 价格配置阶梯要给出"相较前一档"的新增/强化/回落/稳定, 并按段样本量标证据强度。"""
    report, _ = birh.build(CAT, str(ws_env["data_root"]),
                           voc=str(ws_env["cat_dir"] / "19_VOC报告" / f"{CAT}-VOC报告.xlsx"))
    tabs = {tb["id"]: tb for s in report["sheets"] for sec in s["sections"] for tb in sec["tables"]}
    st = tabs["m-stair"]
    assert st["headers"][1] == "相较前一档"
    assert st["rows"][0]["values"][1] == "起点档"
    for r in st["rows"][1:]:
        assert str(r["values"][1]).startswith("相较 "), r["values"][1]
        _st = str(r["values"][7])
        assert _st.startswith(("证据充分", "稀疏", "证据不足")), _st   # base<5 → 证据不足(合同默认规则)
        assert any(str(r["values"][i]) != "—" for i in (2, 3, 4, 5)), r["values"]
    # 小 fixture 的档间配置几乎不变 → 只钉结构; "必须给出档间变化标签"在真数据冒烟里钉
    for r in st["rows"]:
        assert r["values"][6] not in (None, "")            # 每档都要有代表变体或明确的未提供


def test_top20_product_level_stair_and_vs_market(ws_env):
    report, _ = birh.build(CAT, str(ws_env["data_root"]),
                           voc=str(ws_env["cat_dir"] / "19_VOC报告" / f"{CAT}-VOC报告.xlsx"))
    tabs = {tb["id"]: tb for s in report["sheets"] for sec in s["sections"] for tb in sec["tables"]}
    st = tabs["m-top20-stair"]
    assert st["headers"][0] == "价格段" and "Top20 数量" in st["headers"]
    assert "本段首现配置(段内销额占比≥30%)" in st["headers"]
    vs = tabs["m-top20-vs"]
    assert vs["headers"] == ["维度", "取值", "Top20 占比(%)", "全市场占比(%)", "差异(pp)"]
    for r in vs["rows"]:
        a, b, d = r["values"][2], r["values"][3], r["values"][4]
        if None not in (a, b, d):
            assert abs((a - b) - d) < 0.02, r["values"]      # 差异 = Top20 − 全市场(pp)


def test_top5_brand_block_and_voc_matching(ws_env):
    """spec §4 ⑩: Top5 品牌组合对标(价格梯队/功能覆盖/代表变体/目标段/直配 VOC)。

    直配 VOC 只有在**能直接匹配品牌证据**时才写; 匹配不上必须标注 `类目级VOC，非品牌归因`。
    """
    report, _ = birh.build(CAT, str(ws_env["data_root"]),
                           voc=str(ws_env["cat_dir"] / "19_VOC报告" / f"{CAT}-VOC报告.xlsx"))
    mkt = _sheet(report, "市场大盘分析")
    tabs = {t["id"]: t for s in mkt["sections"] for t in s["tables"]}
    t5 = tabs["m-top5"]
    assert len(t5["rows"]) == 5
    for need in ("品牌", "销额占比", "价格梯队(段内销额占比)", "功能覆盖(主配置)",
                 "代表变体(月销额最高)", "目标段证据", "直配 VOC 证据"):
        assert need in t5["headers"], need
    last = t5["headers"].index("直配 VOC 证据")
    for r in t5["rows"]:
        txt = str(r["values"][last])
        assert txt.startswith("直配:") or txt.startswith("类目级VOC，非品牌归因"), txt
    assert "非品牌归因" in _sheet_text(mkt), "品牌/类目级归因边界必须仍可核对"


def test_market_tables_match_viewer_chart_signatures(ws_env):
    """表名/表头必须命中 vendored 查看器 `chartSpec()` 的签名, 否则 spec 要求的图表不出来。"""
    report, _ = birh.build(CAT, str(ws_env["data_root"]),
                           voc=str(ws_env["cat_dir"] / "19_VOC报告" / f"{CAT}-VOC报告.xlsx"))
    tabs = {t["id"]: t for s in report["sheets"] for sec in s["sections"] for t in sec["tables"]}
    assert tabs["m-price"]["title"] == "价格段" and "月销额" in tabs["m-price"]["headers"]
    assert tabs["m-life"]["title"] == "生命周期" and "数量占比" in tabs["m-life"]["headers"]
    # 小 fixture 只有 6 个品牌(分散图需要 ≥8 行), 这里只钉签名; 真实数据的行数在冒烟测试里钉
    assert tabs["m-brand"]["title"] == "品牌" and len(tabs["m-brand"]["rows"]) >= 1
    for h in ("中位价", "销额占比", "变体数"):
        assert h in tabs["m-brand"]["headers"]
    assert tabs["m-fulfil"]["title"] == "履约方式" and "销额占比" in tabs["m-fulfil"]["headers"]
    assert tabs["m-type"]["title"] == "产品类型" and "销额占比" in tabs["m-type"]["headers"]
    assert tabs["m-material"]["title"] == "主体材质"
    assert tabs["m-premium"]["title"] == "功能"                     # 正负价差图签名
    assert "均价差(USD)" in tabs["m-premium"]["headers"]
    for tid in ("m-life-func", "m-life-claim", "m-life-attr"):
        if tid in tabs:
            assert tabs[tid]["headers"][0] == "属性/功能", tid
            assert "百分点差" in tabs[tid]["headers"] and "新品证据完整度" in tabs[tid]["headers"]
    assert tabs["m-life-claim"]["headers"][:2] == ["属性/功能", "所属列"]


def test_hhi_uses_ten_thousand_scale(ws_env):
    """skill §1: HHI = Σ(品牌销额占比²) × 10,000 —— 不是 0–1 的小数。"""
    report, _ = birh.build(CAT, str(ws_env["data_root"]),
                           voc=str(ws_env["cat_dir"] / "19_VOC报告" / f"{CAT}-VOC报告.xlsx"))
    tabs = {t["id"]: t for s in report["sheets"] for sec in s["sections"] for t in sec["tables"]}
    shares = [r["values"][3] for r in tabs["m-brand"]["rows"] if r["values"][3] is not None]
    expect = round(sum(s * s for s in shares) * 10000, 1)
    got = next(r["values"][1] for r in tabs["m-concentration"]["rows"] if r["values"][0] == "HHI")
    assert got == expect, (got, expect)
    assert got > 1, "HHI 必须是万分制"


def test_top_brand_table_has_spec_fields(ws_env):
    report, _ = birh.build(CAT, str(ws_env["data_root"]),
                           voc=str(ws_env["cat_dir"] / "19_VOC报告" / f"{CAT}-VOC报告.xlsx"))
    tabs = {t["id"]: t for s in report["sheets"] for sec in s["sections"] for t in sec["tables"]}
    hdr = tabs["m-topbrand"]["headers"]
    for need in ("变体数", "新品数", "老品数", "月销量", "月销额", "销额占比", "价格区间(USD)",
                 "均价(USD)", "中位价(USD)", "有效评分均值", "履约构成"):
        assert need in hdr, need


def test_summary_and_insights_present(ws_env):
    """模块 ① 报告摘要 + 模块 11 市场总结与洞察(spec §4 要求洞察成文)。"""
    report, _ = birh.build(CAT, str(ws_env["data_root"]),
                           voc=str(ws_env["cat_dir"] / "19_VOC报告" / f"{CAT}-VOC报告.xlsx"))
    mkt = _sheet(report, "市场大盘分析")
    summary = next(s for s in mkt["sections"] if s["id"] == "m-1")
    assert summary["tables"] and summary["tables"][0]["id"] == "m-summary"
    ins = next(s for s in mkt["sections"] if s["id"] == "m-8")
    assert len(ins["insights"]) >= 10  # spec §4 的最低洞察集(C 批还会再加阶梯/演进类)
    assert all(i.get("number") and i.get("sourceRef") for i in ins["insights"])


# --------------------------------------------- ⑩ 决策摘要/模块状态/来源索引(contract §2)

def test_exec_sheet_is_first_and_complete(ws_env):
    """首屏「决策摘要」: 源时间窗与样本 / 价格锚与集中度 / 主要机会与主要反证 / 模块状态与来源索引。"""
    report, _ = birh.build(CAT, str(ws_env["data_root"]),
                           voc=str(ws_env["cat_dir"] / "19_VOC报告" / f"{CAT}-VOC报告.xlsx"))
    ex = report["sheets"][0]
    assert ex["name"] == "决策摘要"
    assert [s["id"] for s in _core_sections(ex)] == ["e-1", "e-2", "e-3"]
    tabs = {t["id"]: t for s in ex["sections"] for t in s["tables"]}
    assert {"e-scope", "e-opp", "e-src"} <= set(tabs)
    assert len(ex["kpis"]) >= 8
    # 范围表必须显式声明"源覆盖样本 ≠ 全市场"
    scope_txt = " ".join(str(v) for r in tabs["e-scope"]["rows"] for v in r["values"])
    assert "覆盖范围声明" in scope_txt and ("不是全市场" in scope_txt or "不等于全市场" in scope_txt)
    # 机会与反证必须并列出现
    kinds = {r["values"][0] for r in tabs["e-opp"]["rows"]}
    assert {"机会", "反证"} <= kinds, kinds
    # 来源索引: 四个模块都有状态 + 三份输入都要出现在来源里
    idx = tabs["e-src"]
    mods = [r["values"][0] for r in idx["rows"]]
    for need in ("关键词分析", "市场大盘分析", "VOC差评分析", "选品建议"):
        assert need in mods, mods
    idx_txt = " ".join(str(v) for r in idx["rows"] for v in r["values"])
    assert "keyword-report" in idx_txt and "VOC报告" in idx_txt
    assert "无法独立复核" in idx_txt and "数据不足" in idx_txt


def test_every_module_carries_a_status_label(ws_env):
    """contract §2: 每个模块必须标 recomputed/imported/unverified/insufficient 之一。"""
    report, _ = birh.build(CAT, str(ws_env["data_root"]),
                           voc=str(ws_env["cat_dir"] / "19_VOC报告" / f"{CAT}-VOC报告.xlsx"))
    for s in report["sheets"][1:]:                    # 首屏自身是汇总页, 不重复标
        first = s["sections"][0]["notes"][0]["text"]
        assert first.startswith("模块状态:"), (s["name"], first[:40])
        assert any(w in first for w in ("重新计算", "沿用源报告", "无法独立复核", "数据不足")), first


def test_viewer_lands_on_first_sheet_and_nav_follows_report():
    """查看器: 默认落在第 1 张表(决策摘要), 且导航按**报告实际 sheet**生成(不写死 4 个名字)。"""
    js = (ROOT / "tools" / "market_report_html" / "app.js").read_text(encoding="utf-8")
    assert "report: 0, sheet: 0" in js
    assert "report()?.sheets||[]" in js and "sheetNames.length?sheetNames:names" in js


# ------------------------------- ⑭ 与 skill 数值/内容契约同步(2026-10-12 版)

def test_vendored_rules_module_matches_skill_source():
    """数值规则只有一处: vendored 模块必须与 skill 源文件 sha256 一致(源不存在时跳过)。

    skill 源在用户 profile 下会不断更新 → 这里做**漂移检测**: 一旦源变了, 测试失败提醒重新同步。
    """
    import hashlib
    vend = ROOT / "tools" / "report_rules" / "analysis_rules.py"
    assert vend.is_file()
    src = Path(r"C:\Users\Jack_New\.codex\skills\build-amazon-integrated-html-report\scripts\analysis_rules.py")
    if not src.is_file():
        pytest.skip("skill 源文件不在本机")
    def _body(p):
        return p.read_text(encoding="utf-8").split('"""', 2)[2]
    assert hashlib.sha256(_body(vend).encode("utf-8")).hexdigest() == \
           hashlib.sha256(_body(src).encode("utf-8")).hexdigest(), "skill 数值模块已更新 → 需重新同步 tools/report_rules"


def test_numeric_rules_are_reused_from_skill_module():
    """准入/双榜/季节门槛必须走 vendored 规则(边界与 skill 自检一致), 不再各处自写一版。"""
    assert birh._rules.keyword_admission(30000, -0.20, -0.20, 2.99)["status"] == "达标"
    assert birh._rules.keyword_admission(30000, -0.20, -0.20, 3)["status"] == "不达标"
    assert birh._rules.keyword_admission(29999, None, -0.21, 3)["status"] == "不达标"
    assert birh._rules.dual_rank_status(8) == "新品增强"
    assert birh._rules.dual_rank_status(-8) == "成熟榜更强"
    assert birh._rules.dual_rank_status(None) == "数据不足"


def test_seasonality_gate_uses_latest_three_complete_years():
    """季节门槛取**最近三个完整年**(skill 口径): 更早的年份即使很强也不得进入月序指数。"""
    # 只有两个平坦年 → 不算季节(强度 1.0), 但**不是**"数据不足"(那是 <2 可用年时才给)
    flat = birh._rules.seasonality_gate({y: [1.0] * 12 for y in (2019, 2020)},
                                        completed_through_year=2020)
    assert flat["status"] == "季节性不成立" and flat["peak_off_ratio"] == 1.0
    # 老年份在 1 月有 9 倍尖峰; 若被算进来月序指数会抬高 → 用"最近三年"时比值仍应为 1.0
    old = {2019: [9.0] + [1.0] * 11, 2020: [9.0] + [1.0] * 11}
    recent = {2023: [1.0] * 12, 2024: [1.0] * 12, 2025: [1.0] * 12}
    res = birh._rules.seasonality_gate({**old, **recent}, completed_through_year=2025)
    assert res["years"] == [2023, 2024, 2025]
    assert res["peak_off_ratio"] == pytest.approx(1.0), "更早年份泄漏进了月序指数"
    assert res["status"] == "季节性不成立"


def test_reco_omits_standalone_risk_and_listing_modules(ws_env):
    """新版 SKILL.md §14: 默认**不设**独立的 `风险与验证计划`/`Listing与上市验证` 模块;
    材料风险贴在受影响方向旁(r-crisk-*)。"""
    reco = _reco(ws_env)
    titles = " ".join(s["title"] for s in reco["sections"])
    assert "风险与验证计划" not in titles and "Listing与上市验证" not in titles
    ids = {t["id"] for s in reco["sections"] for t in s["tables"]}
    assert "r-test" not in ids and "r-listing" not in ids
    if "r-concept-1" in ids:                       # 有候选时必须有方向旁风险
        assert "r-crisk-1" in ids


def test_cross_source_matrix_labels_evidence_match_level(ws_env):
    """跨源证据矩阵: 每行必须标明证据匹配级别(品牌级 vs 仅类目级), 不得把类目结论当变体事实。"""
    _, tabs = _reco_tabs(ws_env)
    assert "r-xsrc" in tabs
    m = tabs["r-xsrc"]
    assert m["headers"][0] == "变体ASIN" and any("匹配" in h for h in m["headers"])
    assert len(m["rows"]) <= 12
    lvl = [str(r["values"][-1]) for r in m["rows"]]
    for x in lvl:
        assert "匹配(品牌级)" in x or "仅类目级" in x, x


def test_voc_score_audit_reconciles_with_source(ws_env):
    """VOC 分项重算: 用 skill 公式从 F/S/H/T/C 重算, 与源分对账必须能列出逐条结果。"""
    _, tabs = _voc_sheet_of(ws_env)
    if "v-score-audit" not in tabs:                # 合成 fixture 无 issues 行
        pytest.skip("该 fixture 没有 VOC 问题行")
    a = tabs["v-score-audit"]
    assert a["headers"][:7] == ["问题", "F", "S", "H", "T", "C", "安全等级"]
    for r in a["rows"]:
        assert str(r["values"][11]).strip()        # 必须有对账结论


def test_purchase_drivers_kept_separate_from_experience(ws_env):
    """购买理由与体验收益必须分开(新版 SKILL.md §14): 源未建模的一方如实标数据不足。"""
    _, tabs = _voc_sheet_of(ws_env)
    assert "v-buy" in tabs, "缺少购买驱动/体验收益分开的模块"
    txt = " ".join(str(x) for r in tabs["v-buy"]["rows"] for x in r["values"])
    assert "体验收益" in txt and "购买理由" in txt
    assert "数据不足" in txt or "未建模" in txt


# ------------------------------- ⑬ VOC 评论证据库与正文检索(contract §2 第 5 块 / §3 检索)

def _voc_sheet_of(ws_env):
    report, _ = birh.build(CAT, str(ws_env["data_root"]),
                           voc=str(ws_env["cat_dir"] / "19_VOC报告" / f"{CAT}-VOC报告.xlsx"))
    sheet = _sheet(report, "VOC差评分析")
    return sheet, {t["id"]: t for s in sheet["sections"] for t in s["tables"]}


def test_voc_evidence_section_exists_with_disclosure(ws_env):
    """证据视图必须给出 评论ID + 全文, 且**收录上限与检索范围**必须披露(不得当成全量)。"""
    sheet, tabs = _voc_sheet_of(ws_env)
    assert "v-8" in [s["id"] for s in _core_sections(sheet)], "评论证据库必须是独立一节"
    if "v-evi" not in tabs:                                  # 合成 fixture 无证据库 → 数据不足分支
        assert "v-evi-na" in tabs
        return
    evi = tabs["v-evi"]
    assert "评论ID" in evi["headers"] and any("正文" in h for h in evi["headers"])
    assert len(evi["rows"]) <= 400, "证据库必须有收录上限"
    body_i = next(i for i, h in enumerate(evi["headers"]) if "正文" in h)
    for r in evi["rows"]:
        assert str(r["values"][0]).strip(), "必须有评论ID"
        assert str(r["values"][body_i]).strip(), "必须带正文"
    note = " ".join(str(x) for r in tabs["v-evi-note"]["rows"] for x in r["values"])
    for need in ("证据库总量", "本报告收录", "上限", "全文", "检索范围", "归因"):
        assert need in note, need
    # 旧口径"不贴摘录"的说明必须已被替换(否则自相矛盾)
    all_notes = " ".join(str(n) for s in sheet["sections"] for n in s["notes"])
    assert "不贴" not in all_notes, all_notes


def test_viewer_counts_review_body_hits(ws_env, tmp_path):
    """查看器必须把**评论正文命中**单独计数, 并让『零表行但有正文命中』不被显示成没有结果。"""
    js = (ROOT / "tools" / "market_report_html" / "app.js").read_text(encoding="utf-8")
    assert "评论正文命中" in js and "bodyHits" in js
    assert "证据库" in js and "正文" in js
    assert "_lastTextOnly=Math.max(textHits,bodyHits)" in js.replace(" ", "")
    report, _ = birh.build(CAT, str(ws_env["data_root"]),
                           voc=str(ws_env["cat_dir"] / "19_VOC报告" / f"{CAT}-VOC报告.xlsx"))
    html = _render_to(report, tmp_path)
    assert "评论正文命中" in html


@pytest.mark.skipif(not (ROOT / "data" / "Dog Dryer-US-202608" / "19_VOC报告"
                          / "Dog Dryer-US-202608-VOC报告.xlsx").is_file(),
                    reason="缺少 Dog Dryer VOC 工作簿")
def test_real_data_evidence_library(tmp_path):
    """真数据: 证据库收录到上限、负向优先、正文可检索, 且与源工作簿 reviewId 对得上。"""
    out = tmp_path / "out"
    assert birh.main(["--cat", _KW_CAT, "--out-dir", str(out)]) == 0
    data = json.loads((out / "report.json").read_text(encoding="utf-8"))
    voc = next(s for s in data["sheets"] if s["name"] == "VOC差评分析")
    tabs = {x["id"]: x for s in voc["sections"] for x in s["tables"]}
    evi = tabs["v-evi"]
    assert len(evi["rows"]) == 400
    star_i = evi["headers"].index("星级")
    stars = [birh._num_of(r["values"][star_i]) for r in evi["rows"]]
    neg = sum(1 for s in stars if s is not None and s <= 3)
    pos = sum(1 for s in stars if s is not None and s >= 4)
    assert neg > pos, (neg, pos)                              # 负向优先策略
    body_i = next(i for i, h in enumerate(evi["headers"]) if "正文" in h)
    bodies = " ".join(str(r["values"][body_i]).lower() for r in evi["rows"])
    assert "noise" in bodies or "noisy" in bodies, "正文应可检索到具体词"
    src = ROOT / "data" / _KW_CAT / "19_VOC报告" / f"{_KW_CAT}-VOC报告.xlsx"
    if src.is_file():
        wb = birh.load_workbook(src, read_only=True, data_only=True)
        try:
            ws = wb["评论证据库"]
            ids = set()
            for row in ws.iter_rows(values_only=True):
                if row and str(row[0]).strip().startswith("R") and len(str(row[0])) > 8:
                    ids.add(str(row[0]).strip())
        finally:
            wb.close()
        shown = {str(r["values"][0]) for r in evi["rows"]}
        assert shown <= ids, "收录的 reviewId 必须都能在源表检索到"


# ------------------------------- ⑫ 关键词契约: 四条准入 / 双榜 / Google 季节门槛(contract §1/§2)

def _kw(ws_env):
    report, _ = birh.build(CAT, str(ws_env["data_root"]),
                           voc=str(ws_env["cat_dir"] / "19_VOC报告" / f"{CAT}-VOC报告.xlsx"))
    sheet = _sheet(report, "关键词分析")
    return sheet, {t["id"]: t for s in sheet["sections"] for t in s["tables"]}


def test_keyword_sections_cover_contract_views(ws_env):
    """关键词模块必须含: 核心词选中 · 四条准入 · 双榜支持 · Google 季节门槛 · CPC · 月度源数据 · 长尾 · 洞察。"""
    sheet, _ = _kw(ws_env)
    assert [s["id"] for s in _core_sections(sheet)] == ["kw-1", "kw-adm", "kw-dual", "kw-sea",
                                                    "kw-2", "kw-3", "kw-4", "kw-5"]


def test_admission_recomputes_each_criterion_and_reconciles(ws_env):
    """四条准入: 逐条给值与判定, 最终判定严格按合同规则(不达标优先于数据不足), 且与源判定对账。"""
    _, tabs = _kw(ws_env)
    adm = tabs["kw-admission"]
    assert adm["headers"][:2] == ["关键词", "①月搜索量"]
    for r in adm["rows"]:
        v = r["values"]
        marks = [v[2], v[4], v[6], v[8]]
        assert set(marks) <= {"通过", "不通过", "缺失"}, marks
        final = v[9]
        if "不通过" in marks:
            assert final == "不达标", v
        elif "缺失" in marks:
            assert final == "数据不足", v
        else:
            assert final == "达标", v
        # 每一处不通过/缺失都要在原因里逐条出现
        for label, mark in zip(("①月搜索量", "②站内同比", "③Google同比", "④CPC"), marks):
            if mark != "通过":
                assert label in v[10], (label, v[10])
        assert "一致" in v[11] or "不一致" in v[11]
    # 计数表: 完整四条件定义 + 三类计数 + 达标词 + 逐词原因 + 目标月
    txt = " ".join(str(x) for r in tabs["kw-admission-summary"]["rows"] for x in r["values"])
    for need in ("30,000", "-20%", "$3.00", "重算计数", "源判定计数(对账)", "达标词", "未通过词与原因", "目标月"):
        assert need in txt, need
    assert "等号通过" in txt, "必须写明等号边界"
    notes = _sheet_text(_kw(ws_env)[0])
    assert "加权评分" in notes and "数据不足" in notes, "必须声明不做加权评分且数据不足≠不达标"


def test_admission_treats_missing_as_insufficient_not_fail(ws_env):
    """缺失 ≠ 0: 源表标注缺字段的词(如 dryer for pet)必须是 `数据不足`, 不能因 0 被算成不达标。"""
    _, tabs = _kw(ws_env)
    rows = {str(r["values"][0]): r["values"] for r in tabs["kw-admission"]["rows"]}
    miss = [v for v in rows.values() if "缺失" in (v[2], v[4], v[6], v[8]) and "不通过" not in (v[2], v[4], v[6], v[8])]
    assert miss, "真实数据里应有缺字段的词"
    for v in miss:
        assert v[9] == "数据不足", v
        assert "缺失" in v[10]


def test_dual_rank_separates_occurrence_from_unique(ws_env):
    """双榜: 原始出现数 ≥ 唯一 ASIN 数; 分母是唯一 ASIN; 覆盖率与 NR−BS 差自洽; 零支持不作信号。"""
    _, tabs = _kw(ws_env)
    if "kw-dual-counts" not in tabs:                     # 合成 fixture 无 FR-00 → 数据不足分支
        assert "kw-dual-na" in tabs
        assert "数据不足" in str(tabs["kw-dual-na"]["rows"][0]["values"][1])
        return
    counts = {str(r["values"][0]): r["values"][1] for r in tabs["kw-dual-counts"]["rows"]}
    raw = int(str(counts["双榜原始出现次数(BS+NR)"]).replace(",", ""))
    uniq = int(str(counts["唯一 ASIN 数(分析分母)"]).replace(",", ""))
    assert raw >= uniq > 0
    assert int(str(counts["详情抓取成功数"]).replace(",", "")) == uniq
    for r in tabs["kw-dual"]["rows"]:
        v = r["values"]
        assert int(str(v[5]).replace(",", "")) == uniq, "覆盖率分母必须是唯一 ASIN 数"
        assert int(v[1]) <= int(v[2]), "唯一支持 ≤ 原始出现支持"
        assert int(v[3]) <= int(v[4]), "标题支持 ≤ 全文支持"
        if int(v[1]) == 0:
            assert v[9] == "无榜单支持(未命中)" and v[8] == "未提供", v
        else:
            assert v[9] in ("新品增强(≥+8pp)", "成熟榜更强(≤-8pp)", "两榜接近"), v


def test_google_season_gate_is_the_only_gate_and_blocks_are_conditional(ws_env):
    """季节门槛只用 Google 同尺度合成: 报年数/月序指数/峰谷月/强度/重复性; 不成立则**不生成**对比块。"""
    sheet, tabs = _kw(ws_env)
    gate = tabs["kw-season-gate"]
    txt = " ".join(str(x) for r in gate["rows"] for x in r["values"])
    if "共享尺度" not in txt:                            # 合成 fixture 无 FR-05 → 数据不足分支
        assert "季节性数据不足" in txt and "kw-season-detail" not in tabs
        return
    for need in ("共享尺度", "完整自然年", "月序指数", "强度(峰/谷)", "重复性", "门槛结论"):
        assert need in txt, need
    verdict = next(str(r["values"][1]) for r in gate["rows"] if r["values"][0] == "门槛结论")
    has_detail = "kw-season-detail" in tabs
    assert has_detail == ("季节性成立" in verdict), (verdict, has_detail)
    notes = _sheet_text(sheet)
    if not has_detail:
        assert "省略" in notes or "不生成" in notes
        assert "不作为本报告结论" in notes or "源报告" in notes
    # 站内月度块必须显式声明"不作为季节门槛"
    m = next(s for s in sheet["sections"] if s["id"] == "kw-3")
    assert "不作为季节门槛" in m["title"] or any("不作为季节门槛" in str(n) for n in m["notes"])


# ------------------------------- ⑪ 新品→Hero / Concept 画像 / 详细定价(contract §2 第 6-8 块)

_HERO_ROLES = {"销售型候选", "差异型候选", "相邻任务参考", "观察/反例"}
_CONCEPT_DIMS = ["Target", "Product", "Configuration", "Mechanism", "Business fit", "User fit", "Status"]


def _reco(ws_env):
    report, _ = birh.build(CAT, str(ws_env["data_root"]),
                           voc=str(ws_env["cat_dir"] / "19_VOC报告" / f"{CAT}-VOC报告.xlsx"))
    return next(s for s in report["sheets"] if s["name"] == "选品建议")


def _reco_tabs(ws_env):
    reco = _reco(ws_env)
    return reco, {t["id"]: t for s in reco["sections"] for t in s["tables"]}


def test_reco_section_order_places_hero_concept_price_before_risk(ws_env):
    """narrative 顺序: 定义 → 新品→Hero → Concept → 定价 → 风险 → Listing → 不建议。"""
    reco = _reco(ws_env)
    assert [s["id"] for s in _core_sections(reco)] == ["r-1", "r-2", "r-3", "r-4", "r-hero", "r-xsrc",
                                                   "r-concept", "r-price", "r-7"]


def test_hero_universe_covers_every_source_labelled_new_variant(ws_env):
    """新品宇宙必须覆盖**全部**源标注新品变体(不是只挑高销量的), 且四类角色都在允许集合内。"""
    src = birh.Sources(CAT, str(ws_env["cat_dir"]))
    recs = src.load_product(None)
    news = [r for r in birh._valid_variants(recs) if birh._s(r.get("lifecycle")) == "新品"]
    _, tabs = _reco_tabs(ws_env)
    u = tabs["r-hero-universe"]
    assert len(u["rows"]) == len(news), (len(u["rows"]), len(news))
    asins = [str(r["values"][0]) for r in u["rows"]]
    assert len(set(asins)) == len(asins), "同一变体不得重复出现"
    assert {birh._s(r.get("asin")) for r in news} == set(asins)
    roles = {str(r["values"][9]) for r in u["rows"]}
    assert roles <= _HERO_ROLES, roles
    # 角色分布 + 口径说明必须在汇总表里(无固定配额/无评分/四维分开/增长数据不足)
    txt = " ".join(str(v) for r in tabs["r-hero-summary"]["rows"] for v in r["values"])
    for need in ("无固定配额", "三态区分", "共享父体", "四个维度分开", "数据不足"):
        assert need in txt, need


def test_hero_roles_are_evidence_based(ws_env):
    """差异型必须同时满足"命中候选配置 + 销额≥新品中位"; 销售型必须是新品销额前 3。"""
    _, tabs = _reco_tabs(ws_env)
    u = tabs["r-hero-universe"]
    revs = sorted((birh._num_of(r["values"][4]) or 0 for r in u["rows"]), reverse=True)
    med = birh._median(revs) or 0
    for r in u["rows"]:
        role, why, hit = str(r["values"][9]), str(r["values"][10]), str(r["values"][7])
        if role == "差异型候选":
            assert "命中" in hit and (birh._num_of(r["values"][4]) or 0) >= med
        elif role == "销售型候选":
            assert "前 3" in why or "销额前 3" in why
        elif role == "观察/反例":
            assert "低量" in why or "低于中位" in why or "低于新品中位" in why


def test_attr_state_three_state_rule(ws_env):
    """未登记在引擎词表里的 `是否*` 列(Dog Dryer 实测是语义取值)必须按**词形**判三态:
    肯定=含核心词, 否定=明确否定词, 含糊/空白=未知(未知 ≠ 否)。"""
    assert birh._attr_state("是否含美容剪", "含美容剪") is True
    assert birh._attr_state("是否含美容剪", "不含美容剪") is False
    assert birh._attr_state("是否含美容剪", "未明确") is None
    assert birh._attr_state("是否含美容剪", "") is None
    assert birh._attr_state("是否带加热", "带加热") is True
    assert birh._attr_state("是否带加热", "不带加热") is False
    birh.validate_report(birh.build(CAT, "data")[0]) if False else None      # 不做重活


def test_concept_portrait_has_exactly_seven_dimensions(ws_env):
    _, tabs = _reco_tabs(ws_env)
    ids = sorted(k for k in tabs if k.startswith("r-concept"))
    assert ids, "至少要有 r-concept-* 表(无候选时是 r-concept-0)"
    for k in ids:
        tb = tabs[k]
        assert tb["headers"] == ["维度", "内容", "依据 / 来源"]
        assert [r["values"][0] for r in tb["rows"]] == _CONCEPT_DIMS, k
        for r in tb["rows"]:
            assert str(r["values"][1]).strip() and str(r["values"][2]).strip(), (k, r["values"][0])


def test_pricing_recommendation_has_five_numbered_points(ws_env):
    _, tabs = _reco_tabs(ws_env)
    ids = sorted(k for k in tabs if k.startswith("r-price"))
    assert ids, "至少要有 r-price-* 表(无候选时是 r-price-0)"
    for k in ids:
        tb = tabs[k]
        assert tb["headers"] == ["要点", "内容", "依据 / 来源"]
        assert len(tb["rows"]) == 5, k
        firsts = [str(r["values"][0]) for r in tb["rows"]]
        assert firsts[0].startswith("1 ") and firsts[4].startswith("5 "), firsts
        txt = " ".join(str(v) for r in tb["rows"] for v in r["values"])
        assert "不可断言" in txt or "不是" in txt                   # 边界声明(所有分支都要)
        if k != "r-price-0":                                       # 无候选分支只允许"未提供"
            assert "中位" in txt, k                                # 必须有锚(中位)
            assert "P25" in txt and "P75" in txt, k                # 段内分位区间
    # 无候选时不得编造价格
    if "r-price-0" in ids:
        assert "未提供" in str(tabs["r-price-0"]["rows"][2]["values"][1])


def test_pricing_never_claims_profit_or_wtp(ws_env):
    """定价模块不得出现"利润预测/支付意愿最优"这类断言(只能以否定形式出现)。"""
    _, tabs = _reco_tabs(ws_env)
    for k in [x for x in tabs if x.startswith("r-price")]:
        txt = " ".join(str(v) for r in tabs[k]["rows"] for v in r["values"])
        for bad in ("利润预测为", "预计利润", "支付意愿最优为"):
            assert bad not in txt, (k, bad)


# --------------------------------------------- ⑨ HTML 功能(参照 .codex skill 的 html-report-contract)

def _render_to(report: dict, out: Path) -> str:
    birh._render_html(report, out / "index.html")
    return (out / "index.html").read_text(encoding="utf-8")


def test_embedded_json_is_script_safe(ws_env, tmp_path):
    """内嵌 JSON 必须安全序列化: 源文本里的 `</script>` 不能提前闭合脚本块(contract §3)。

    同时证明转义**不丢数据**: `\\u003c` 在 JSON 里解析回 `<`。
    """
    import re as _re
    import json as _json
    report, _ = birh.build(CAT, str(ws_env["data_root"]),
                           voc=str(ws_env["cat_dir"] / "19_VOC报告" / f"{CAT}-VOC报告.xlsx"))
    evil = "</script><script>alert(1)</script> <!-- x --> <img src=x onerror=alert(2)>"
    report["title"] = "标题" + evil
    _sheet(report, "关键词分析")["subtitle"] = "副标题" + evil
    _sheet(report, "关键词分析")["sections"][0]["notes"][0]["text"] = "备注" + evil
    html = _render_to(report, tmp_path)

    m = _re.search(r"window\.REPORTS\s*=\s*(\[.*?\]);\s*\n", html, _re.S)
    assert m, "找不到内嵌 REPORTS"
    payload = m.group(1)
    assert "<" not in payload, "载荷里不应再有原始 '<'"
    assert "\\u003c" in payload
    # 解析后必须原样还原(转义无损)
    data = _json.loads(payload)
    assert evil in data[0]["title"]
    assert evil in _payload_sheet(data[0], "关键词分析")["sections"][0]["notes"][0]["text"]
    # 原始危险串**不得**出现在 HTML 任何位置(标题注入点曾未转义 → 真 bug)
    assert evil not in html, "源文本必须转义后才进 HTML"
    assert "&lt;/script&gt;" in html or "\\u003c" in payload
    # 脚本块配平: 闭合标签数 == 开标签数(源文本没有多出来闭合标签)
    assert html.count("</script>") == html.count("<script"), (html.count("</script>"), html.count("<script"))


def test_single_file_html_carries_contract_features(ws_env, tmp_path):
    """单文件 HTML 必须带上 contract §3 要求的功能: 打印样式 / 宽表局部横滚 / 行键盘可操作 /
    搜索"行 vs 正文"计数 / 键盘焦点可见。"""
    import re as _re
    report, _ = birh.build(CAT, str(ws_env["data_root"]),
                           voc=str(ws_env["cat_dir"] / "19_VOC报告" / f"{CAT}-VOC报告.xlsx"))
    html = _render_to(report, tmp_path)
    for probe, label in (("@media print", "打印样式"),
                         ("overflow-x:auto", "宽表局部横滚"),
                         ('tabindex="0" role="button"', "表格行键盘可操作"),
                         ("正文匹配", "行/正文匹配计数区分"),
                         ("focus-visible", "键盘焦点可见"),
                         ("@page", "打印分页")):
        assert probe in html, label
    # 离线 = 不引用外部**资源**(数据里的商品链接是内容, 不是资源)
    assert not _re.search(r'<(?:script|link|img|iframe)[^>]+(?:src|href)="https?://', html, _re.I)
    assert not _re.search(r'(?:src|href)="//', html)


def test_viewer_local_upgrades_are_documented():
    """vendored 查看器的本地升级必须写进 VENDOR.md(否则重新同步 kit 时会静默丢失)。"""
    v = (ROOT / "tools" / "market_report_html" / "VENDOR.md").read_text(encoding="utf-8")
    assert "本地升级记录" in v and "第二批本地升级" in v
    for probe in ("overflow-x:auto", "@media print", "tabindex", "正文匹配", "u003c"):
        assert probe in v, probe


_KW_CAT = "Dog Dryer-US-202608"


@pytest.mark.skipif(not (ROOT / "data" / _KW_CAT / f"{_KW_CAT}-bestseller-newrelease.json").is_file(),
                    reason="缺少 Dog Dryer 原始关键词流水线文件(FR-00/FR-05/FR-06/FR-07)")
def test_real_data_keyword_contract(tmp_path):
    """真数据(2026-10-12): 双榜计数与 FR-00 对得上; 四条准入**逐词**与源判定一致; 季节门槛可用。

    这是"独立重算"可信度的关键证据: 我用 FR-05/FR-06 逐月源行重算的判定必须与源 FR-07 完全一致,
    否则要么我算错、要么源有隐式口径 —— 两者都必须暴露而不是静默取一。
    """
    out = tmp_path / "out"
    assert birh.main(["--cat", _KW_CAT, "--out-dir", str(out)]) == 0
    data = json.loads((out / "report.json").read_text(encoding="utf-8"))
    kw = next(s for s in data["sheets"] if s["name"] == "关键词分析")
    tabs = {x["id"]: x for s in kw["sections"] for x in s["tables"]}
    base = ROOT / "data" / _KW_CAT
    fr00 = json.loads((base / f"{_KW_CAT}-bestseller-newrelease.json").read_text(encoding="utf-8"))
    # 双榜原始出现 = BS+NR; 唯一 = union; 分母必须是唯一数
    counts = {str(r["values"][0]): r["values"][1] for r in tabs["kw-dual-counts"]["rows"]}
    assert int(str(counts["双榜原始出现次数(BS+NR)"]).replace(",", "")) == len(fr00["bs"]) + len(fr00["nr"])
    assert int(str(counts["唯一 ASIN 数(分析分母)"]).replace(",", "")) == len(fr00["union"])
    assert int(str(counts["同时在两榜的 ASIN"]).replace(",", "")) == len(set(fr00["bs"]) & set(fr00["nr"]))
    rows = {str(r["values"][0]): r["values"] for r in tabs["kw-dual"]["rows"]}
    assert rows["dog grooming"][1] > 0 and rows["dog grooming"][2] >= rows["dog grooming"][1]
    assert int(rows["dog grooming"][5]) == len(fr00["union"])
    # 四条准入: 逐词与源 FR-07 判定一致(独立重算验证)
    fr07 = {e["keyword"]: e["verdict"] for e in
            json.loads((base / f"{_KW_CAT}-screen-results.json").read_text(encoding="utf-8"))}
    for r in tabs["kw-admission"]["rows"]:
        v = r["values"]
        assert fr07.get(str(v[0]), "未提供") == v[9], (v[0], fr07.get(str(v[0])), v[9])
    # 站内同比重算 = 源 yoy 字段(操作数级审计)
    for r in tabs["kw-admission"]["rows"]:
        v = r["values"]
        aud = str(v[11])
        if "站内同比重算" in aud:
            assert "源 yoy" in aud
    # Google 季节门槛: 真数据有 4 个完整年 → 必须给出强度与重复性(无论结论)
    gate_txt = " ".join(str(x) for r in tabs["kw-season-gate"]["rows"] for x in r["values"])
    assert "完整自然年" in gate_txt and "强度(峰/谷)" in gate_txt and "重复性" in gate_txt
    verdict = next(str(r["values"][1]) for r in tabs["kw-season-gate"]["rows"] if r["values"][0] == "门槛结论")
    assert ("季节性成立" in verdict) == ("kw-season-detail" in tabs), verdict


# --------------------------------------------------------------------------- ⑥ 真实数据冒烟(可选)

_REAL_CAT = "Cutting Boards-US-202608"
_REAL_VOC = None
for _c in sorted((ROOT / "_voc_test").glob("out/*/VOC报告.xlsx")):
    _REAL_VOC = _c
    break
if _REAL_VOC is None:
    for _c in sorted(Path(r"C:\Users\Jack_New\Desktop").glob("VOC测试-*/*/VOC报告.xlsx")):
        _REAL_VOC = _c
        break


@pytest.mark.skipif(_REAL_VOC is None or not (ROOT / "data" / _REAL_CAT / f"{_REAL_CAT}-keyword-report.xlsx").is_file(),
                    reason="缺少真实三份输入(关键词/属性交付/VOC 测试件)")
def test_real_data_smoke(tmp_path):
    """真实数据打通(不产成品, 只验证链路): 2000+ 变体 + 300 条 VOC 能出 4 模块报告。"""
    out = tmp_path / "out"
    rc = birh.main(["--cat", _REAL_CAT, "--voc", str(_REAL_VOC), "--out-dir", str(out)])
    assert rc == 0
    data = json.loads((out / "report.json").read_text(encoding="utf-8"))
    birh.validate_report(data)
    market = next(s for s in data["sheets"] if s["name"] == "市场大盘分析")
    top = next(t for s in market["sections"] for t in s["tables"] if t["id"] == "m-top20")
    assert len(top["rows"]) == 20
    assert (out / "index.html").stat().st_size > 60_000
    tabs = {x["id"]: x for sec in market["sections"] for x in sec["tables"]}
    # C 批: 价格配置阶梯必须真的给出"相较前一档"的变化标签(真数据有 7 档、配置确有迁移)
    stair = tabs["m-stair"]
    assert len(stair["rows"]) >= 4
    assert any(str(r["values"][i]) != "—" for r in stair["rows"][1:] for i in (2, 3, 4))
    # C 批: 材质梯子(段内主导材质随价格迁移)与功能热力图
    i_mat = tabs["m-band-mix"]["headers"].index("主导材质(段内销额占比)")
    mats = [r["values"][i_mat] for r in tabs["m-band-mix"]["rows"]]
    assert any(m != "未提供" for m in mats), mats
    assert len(set(mats)) >= 2, ("材质应随价格段迁移", mats)
    assert 2 <= len(tabs["m-band-func"]["rows"]) <= 20
    # D 批: VOC 四块 + 归口互斥 + 洞察主题覆盖(真数据里确实存在可靠性/安全问题)
    voc_sheet = next(s for s in data["sheets"] if s["name"] == "VOC差评分析")
    vtabs = {x["id"]: x for sec in voc_sheet["sections"] for x in sec["tables"]}
    assert {"v-star", "v-core", "v-scene", "v-safety"} <= set(vtabs)
    fset = {r["values"][0] for r in vtabs["v-func"]["rows"]}
    eset = {r["values"][0] for r in vtabs["v-exp"]["rows"]}
    assert fset and eset and not (fset & eset)
    vins = " ".join(i["text"] for sec in voc_sheet["sections"] for i in sec["insights"])
    for topic in ("最高频", "可靠", "安全", "场景", "改进"):
        assert topic in vins, topic
    assert "未归口" in vins or "未分类" in vins                  # 未分类必须有单列披露
    assert len([i for sec in voc_sheet["sections"] for i in sec["insights"]]) >= 6
