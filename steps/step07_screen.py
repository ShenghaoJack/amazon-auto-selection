"""FR-07 生成筛选脚本并筛选。

输入: 筛选标准(配置 SCREEN_REQUIREMENT) + 关键词指标数据(FR-05 产物)
输出: 类目筛选脚本 screen-<CAT>.py + 筛选结果 <CAT>-screen-results.json
      (每词 达标/不达标/数据不足 + 原因)

逻辑: LLM 生成筛选脚本(参考通用模板, 非思考 + JSON 模式)
      -> 逐词判定(数值比较 + 缺数据检测) -> 结果写 JSON(保留全部词)
容错: LLM 生成缺失/无效 -> 回退通用模板; 脚本执行异常 -> 回退通用模板逻辑重算
      (screen-<CAT>.py 始终写出, LLM 失败也写通用模板版本)
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid

from core.catname import display_cat
from core.errors import StepDataError, StepFailedError
from core.io_utils import atomic_write_text, read_json, write_json
from core.validation import require_file
from steps.base import BaseStep, StepContext


def parse_screen_requirement(req: str) -> dict | None:
    """从 SCREEN_REQUIREMENT 文本解析 FR-07 判定阈值(前端可视/可编辑)。

    例: '月搜索量>=3万, 站内同比>=-21%, Google同比>=-21%, CPC<3.5'
    -> {'volume': 30000.0, 'mom_min': -21.0, 'google_min': -21.0, 'cpc_max': 3.5}
    返回 None 表示无法解析(调用方回退单键/默认值)。
    """
    if not req:
        return None

    def _num(x: str) -> float:
        x = x.strip()
        if x.endswith("万"):
            return float(x[:-1]) * 10000
        return float(x)

    vals = {}
    m = re.search(r"月搜索量\s*>=\s*([\d.]+万?|\d+)", req)
    if m:
        vals["volume"] = _num(m.group(1))
    m = re.search(r"站内同比\s*>=\s*(-?\d+(?:\.\d+)?)\s*%", req)
    if m:
        vals["mom_min"] = float(m.group(1))
    m = re.search(r"Google同比\s*>=\s*(-?\d+(?:\.\d+)?)\s*%", req)
    if m:
        vals["google_min"] = float(m.group(1))
    m = re.search(r"CPC\s*<\s*([\d.]+)", req)
    if m:
        vals["cpc_max"] = float(m.group(1))
    return vals if vals else None


_BOILERPLATE = '''

if __name__ == "__main__":
    import json
    import sys

    data_path = sys.argv[1]
    out_path = sys.argv[2]
    with open(data_path, "r", encoding="utf-8-sig") as fh:
        rows = json.load(fh)
    results = []
    for row in rows:
        verdict, reason = screen(row)
        results.append(
            {"keyword": row.get("keyword", ""), "verdict": verdict, "reason": reason}
        )
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2)
'''


class Step07Screen(BaseStep):
    number = 7
    title = "筛选脚本与筛选"
    description = "FR-07: LLM 生成筛选脚本并逐词判定"

    def validate_upstream(self, ctx: StepContext) -> None:
        self._cat = ctx.config.cat
        if not self._cat:
            raise ValueError("配置缺少 CAT")
        require_file(
            ctx.resolve_artifact(f"{self._cat}-amazon-keyword-data.json"),
            f"{self._cat}-amazon-keyword-data.json",
        )

    @property
    def artifacts(self) -> list[str]:
        return [f"screen-{self._cat}.py", f"{self._cat}-screen-results.json"]

    def run(self, ctx: StepContext) -> None:
        cat = ctx.config.cat
        data_path = ctx.resolve_artifact(f"{cat}-amazon-keyword-data.json")
        script_path = ctx.resolve_artifact(self.artifacts[0])
        results_path = ctx.resolve_artifact(self.artifacts[1])
        self.log(ctx, f"开始筛选: CAT={cat}")

        rows = read_json(data_path, None)
        if not isinstance(rows, list):
            raise StepDataError(f"FR-05 产物格式错误(应为数组): {data_path}")
        augmented = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            item = dict(row)
            evidence = row.get("google_evidence")
            item["google_yoy"] = (evidence.get("yoy") if isinstance(evidence, dict) and evidence.get("target_month") == ctx.config.trend_month
                                  else _compute_google_yoy(row.get("google_monthly_index"), ctx.config.trend_month))
            augmented.append(item)
        self.log(ctx, f"关键词数据 {len(augmented)} 条")

        thresholds = self._thresholds(ctx)
        requirement = str(
            ctx.config.get(
                "SCREEN_REQUIREMENT", "月搜索量>=3万, 站内同比>=-20%, Google同比>=-20%, CPC<3"
            )
        )
        script_source, script_kind = self._generate_script(ctx, cat, requirement, thresholds)
        atomic_write_text(script_path, script_source + _BOILERPLATE)
        self.log(ctx, f"筛选脚本已写入: {script_path}(来源: {script_kind})")

        if not augmented:
            self.log(ctx, "无关键词数据, 写出空筛选结果")
            write_json(results_path, [])
            return

        try:
            self._exec_subprocess(ctx, script_path, augmented, results_path)
        except Exception as exc:
            self.log(ctx, f"子进程执行失败({exc}), 尝试进程内执行")
            try:
                self._exec_in_process(ctx, script_path, augmented, results_path)
            except Exception as exc2:
                self.log(ctx, f"进程内执行失败({exc2}), 回退通用模板重算")
                self._exec_general(ctx, script_path, augmented, results_path, thresholds)

        results = read_json(results_path, None)
        if not isinstance(results, list):
            raise StepFailedError(f"筛选结果无效: {results_path}")
        self._verify_results(results, augmented)
        # 逐词判定明细写入日志(达标大词/长尾候选一目了然)
        for r in results:
            if not isinstance(r, dict):
                continue
            kw = str(r.get("keyword") or "?")
            verdict = str(r.get("verdict") or "?")
            reason = str(r.get("reason") or "")
            suffix = f"({reason})" if reason else ""
            self.log(ctx, f"  筛选: {kw} -> {verdict}{suffix}")
        counts = {}
        for r in results:
            v = r.get("verdict", "")
            counts[v] = counts.get(v, 0) + 1
        self.log(
            ctx,
            f"筛选完成: 达标{counts.get('达标', 0)} 不达标{counts.get('不达标', 0)} "
            f"数据不足{counts.get('数据不足', 0)}, 产物: {results_path}",
        )

    def _thresholds(self, ctx: StepContext) -> dict:
        # 阈值来源优先级: SCREEN_REQUIREMENT 文本(前端可改, 真正参与判定) -> 单键 -> 默认。
        th = {
            "volume": float(ctx.config.get("SEARCH_VOLUME_THRESHOLD", 30000)),
            "mom_min": float(ctx.config.get("MOM_YOY_MIN", -20.0)),
            "google_min": float(ctx.config.get("GOOGLE_YOY_MIN", -20.0)),
            "cpc_max": float(ctx.config.get("CPC_MAX", 3.0)),
        }
        parsed = parse_screen_requirement(ctx.config.get("SCREEN_REQUIREMENT"))
        if parsed:
            for k in ("volume", "mom_min", "google_min", "cpc_max"):
                if k in parsed:
                    th[k] = parsed[k]
        return th

    def _generate_script(self, ctx: StepContext, cat: str, requirement: str, thresholds: dict):
        """LLM 生成筛选脚本, 失败或无效时回退通用模板。"""
        prompt = f"""你是亚马逊关键词筛选脚本生成助手。

类目: {display_cat(cat)}
筛选标准: {requirement}

请生成一个可直接执行的 Python 函数 screen(row) -> (verdict, reason):
- row 是关键词数据字典, 字段: keyword, monthly_search_volume, mom_yoy, google_yoy,
  purchase_volume, purchase_rate, mom_change, growth_3m, cpc, cpc_min, cpc_max,
  weekly_search_volume, search_rank, google_monthly_index, status
- 判定规则: monthly_search_volume/mom_yoy/google_yoy/cpc 任一缺失或 None -> 数据不足;
  monthly_search_volume < {thresholds['volume']:g} 或 mom_yoy < {thresholds['mom_min']:g}
  或 google_yoy < {thresholds['google_min']:g} 或 cpc >= {thresholds['cpc_max']:g} -> 不达标;
  否则 -> 达标
- verdict 只允许取值 "达标"/"不达标"/"数据不足", reason 为简短中文原因
- 函数体内禁止文件读写, 只做数值判定

严格输出 JSON(不要输出其他任何文字):
{{"script": "def screen(row):\\n    ..."}}
"""
        try:
            raw = ctx.llm().chat_json(prompt, temperature=0.2, max_tokens=4000)
        except Exception as exc:
            self.log(ctx, f"LLM 生成筛选脚本失败: {exc}, 回退通用模板")
            return _general_template(thresholds), "通用模板(LLM失败)"
        script = ""
        if isinstance(raw, dict):
            script = raw.get("script")
        script = str(script).strip() if script else ""
        if not script or "def screen" not in script:
            self.log(ctx, "LLM 脚本缺失/无效, 回退通用模板")
            return _general_template(thresholds), "通用模板(脚本无效)"
        return script, "LLM"

    def _exec_subprocess(self, ctx: StepContext, script_path: str, rows: list, out_path: str) -> None:
        """以子进程 python 执行筛选脚本(数据文件路径参数, 输出 JSON 结果文件)。"""
        tmp_path = ""
        try:
            fd, tmp_path = tempfile.mkstemp(suffix=".json", prefix="screen-data-", text=True)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(rows, fh, ensure_ascii=False)
            proc = subprocess.run(
                [sys.executable, script_path, tmp_path, out_path],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
            )
            if proc.returncode != 0:
                raise StepFailedError(
                    f"筛选脚本退出码 {proc.returncode}: {(proc.stderr or '')[-500:]}"
                )
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def _exec_in_process(self, ctx: StepContext, script_path: str, rows: list, out_path: str) -> None:
        """安全 import 执行: 加载脚本模块并逐词调用 screen。"""
        module_name = f"_screen_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
        spec = importlib.util.spec_from_file_location(module_name, script_path)
        if spec is None or spec.loader is None:
            raise StepFailedError("无法加载筛选脚本模块")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if not hasattr(module, "screen"):
            raise StepFailedError("筛选脚本缺少 screen 函数")
        results = []
        for row in rows:
            verdict, reason = module.screen(row)
            results.append(
                {"keyword": row.get("keyword", ""), "verdict": verdict, "reason": reason}
            )
        write_json(out_path, results)

    def _exec_general(
        self, ctx: StepContext, script_path: str, rows: list, out_path: str, thresholds: dict
    ) -> None:
        """回退通用模板逻辑重算, 并把脚本文件覆盖为通用模板版本。"""
        results = []
        for row in rows:
            verdict, reason = general_screen(row, thresholds)
            results.append(
                {"keyword": row.get("keyword", ""), "verdict": verdict, "reason": reason}
            )
        write_json(out_path, results)
        atomic_write_text(script_path, _general_template(thresholds) + _BOILERPLATE)
        self.log(ctx, "已用通用模板重算, 并将 screen-<CAT>.py 覆盖为通用模板版本")

    def _verify_results(self, results: list, rows: list) -> None:
        data_kws = {str(row.get("keyword", "")) for row in rows}
        result_kws = {str(r.get("keyword", "")) for r in results if isinstance(r, dict)}
        if result_kws != data_kws:
            raise StepFailedError("筛选结果关键词与输入不一致")
        for r in results:
            if not isinstance(r, dict) or r.get("verdict") not in ("达标", "不达标", "数据不足"):
                raise StepFailedError(f"筛选结果含非法判定: {r}")


def general_screen(row: dict, thresholds: dict):
    """通用模板判定逻辑(阈值从配置读), 供回退重算与单元测试。"""
    volume = _num(row.get("monthly_search_volume"))
    mom = _num(row.get("mom_yoy"))
    google = _num(row.get("google_yoy"))
    cpc = _num(row.get("cpc"))
    if volume is None or mom is None or google is None or cpc is None:
        return ("数据不足", "缺数据")
    if (
        volume < thresholds["volume"]
        or mom < thresholds["mom_min"]
        or google < thresholds["google_min"]
        or cpc >= thresholds["cpc_max"]
    ):
        return ("不达标", "未达筛选标准")
    return ("达标", "全部通过")


def _general_template(thresholds: dict) -> str:
    """通用模板源码(阈值内嵌, 可直接执行)。"""
    return f'''def screen(row):
    volume = row.get("monthly_search_volume")
    mom = row.get("mom_yoy")
    google = row.get("google_yoy")
    cpc = row.get("cpc")
    if volume is None or mom is None or google is None or cpc is None:
        return ("数据不足", "缺数据")
    if volume < {thresholds["volume"]:g} or mom < {thresholds["mom_min"]:g} or google < {thresholds["google_min"]:g} or cpc >= {thresholds["cpc_max"]:g}:
        return ("不达标", "未达筛选标准")
    return ("达标", "全部通过")
'''


def _compute_google_yoy(index_map, target_month: str | None = None) -> float | None:
    """Google 同比: **目标月指数 vs 去年同月指数**(严格同比语义)。

    如目标月 2026-07 -> 对比 2025-07。目标月未指定时取序列最后一个月;
    目标月或其去年同月缺失时, 回退滚动同比(最近 N 月均值 vs 前 N 月均值,
    月份对齐, N=min(12, 月数//2)); 数据不足返回 None。
    """
    if not isinstance(index_map, dict) or not index_map:
        return None
    months = sorted(index_map.keys())
    target = _resolve_target_month(target_month, months)
    if target:
        yoy = _single_month_yoy(index_map, target)
        if yoy is not None:
            return yoy
    return _rolling_yoy(index_map)


def _resolve_target_month(target_month, months: list) -> str | None:
    """把配置月份解析为序列中的 YYYY-MM; 未指定或不在序列时返回 None。"""
    if not target_month:
        return months[-1] if months else None
    t = str(target_month).strip()
    if len(t) == 6:
        t = f"{t[:4]}-{t[4:6]}"
    return t if t in months else None


def _single_month_yoy(index_map: dict, target: str) -> float | None:
    """单月同比: 目标月 vs 去年同月。"""
    cur = index_map.get(target)
    if cur is None:
        return None
    try:
        cur_v = float(cur)
    except (TypeError, ValueError):
        return None
    y, m = int(target[:4]), target[5:7]
    prev = index_map.get(f"{y - 1}-{m}")
    if prev is None:
        return None
    try:
        prev_v = float(prev)
    except (TypeError, ValueError):
        return None
    if prev_v <= 0:
        return None
    return (cur_v - prev_v) / prev_v * 100.0


def _rolling_yoy(index_map: dict) -> float | None:
    """滚动同比(回退): 最近 N 月均值 vs 前 N 月均值, 月份对齐。"""
    values = []
    for _, v in sorted(index_map.items()):
        try:
            values.append(float(v))
        except (TypeError, ValueError):
            continue
    if len(values) < 6:
        return None
    n = min(12, len(values) // 2)
    if n < 3:
        return None
    recent = sum(values[-n:]) / n
    prev = sum(values[-2 * n:-n]) / n
    if prev <= 0:
        return None
    return (recent - prev) / prev * 100.0


def _num(value):
    """转为 float, 缺失或不可解析返回 None。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
