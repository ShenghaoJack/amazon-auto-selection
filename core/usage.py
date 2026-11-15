"""LLM 用量与费用记账 (Step 1)。

## 核心口径

- **Token 不自己数**: 官方文档《Token 用量计算》明确「每一次实际处理 token 数量以模型
  返回为准, 您可以从返回结果的 `usage` 中查看」。所以这里只读响应里的 usage,
  不做字符估算、不引 tokenizer。
- **计价在 `record()` 时完成** —— 高峰/空闲取决于**调用发生的时刻**, 事后统一算就错了。
- `cost = 命中输入×命中价 + 未命中输入×未命中价 + 输出×输出价`。
  **绝不能只按 `prompt_tokens` 计价**: 命中价与未命中价最高差 50 倍(flash 0.02 vs 1),
  只按总量算会严重高估。
- 费用口径是**估算**(本地按价表正推), 不等于平台账单 —— 摘要里统一标「费用估算」。

## 并发为什么是安全的

- 类目并行 = **多进程**(webui 用 `subprocess.Popen`、`scripts/parallel_cats.py` 起
  `main.py run --config X`), 每个类目写**自己目录**下的文件 ⇒ 跨进程零冲突、零锁。
- 进程内有多线程并发调 LLM(`ATTR_STAB_WORKERS` / `FILTER_JUDGE_WORKERS`) ⇒
  累加与写文件都由 `threading.Lock` 保护。
- 因此"两个类目同时跑"时, 各自的 token 与费用**都是精确的**, 与并发数量无关。

## 落盘

- `llm-usage.jsonl`(append-only): 首行 meta(含价格来源) → 每次**计费调用**一行 detail
  → 每步结束一行 step-summary。保留明细才能事后换价重算。
  失败尝试(429/超时/截断)不产生费用, **不写明细行**, 只计入 `failed_attempts`。
- `llm-usage-steps.csv`(覆盖写): 本轮每步一行, 供 Excel / 后续看板使用。

**记账绝不能打断流水线**: 所有累加/落盘都包在 try 里, 出错只把 `errors` 计数 +1 并告警。
"""
from __future__ import annotations

import csv
import io
import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .prices import CN_TZ, PriceTable, builtin_table, is_peak, resolve_prices

JSONL_NAME = "llm-usage.jsonl"
CSV_NAME = "llm-usage-steps.csv"
WEB_STEP18_RUN_PREFIX = "web-step18-"
# 长表: 每 (步骤 × 通道 × 模型) 一行。
# 为什么按通道拆(2026-09-28): 项目有**两个模型通道** —— 文本(LLM_MODEL)与图片
# (VISION_MODEL), 它们是两个 key、两个端点、单价也可能不同; 而且图片 token 是服务端
# 按尺寸折算后并进 prompt_tokens 的, **从文本侧根本看不出图片花了多少**。
# 要判断"该不该把 VISION_DETAIL 降档 / 少送两张图", 必须先看到 视觉 vs 文本 的占比。
# 合并成"每步一行"会把这个信息永久丢掉, 所以这里用单一粒度的长表(Excel 数据透视
# 即可还原每步合计, 不会出现"合计行 + 明细行混排导致重复计数")。
CSV_HEADER = ["步骤", "状态", "通道", "模型", "调用数", "失败尝试",
              "命中token", "未命中token", "输出token", "费用估算元"]

# 通道中文标签(日志/报表用); 未知通道原样输出
CHANNEL_LABELS = {"text": "文本", "vision": "视觉", "unknown": "未分通道"}


def channel_label(channel: str) -> str:
    return CHANNEL_LABELS.get(str(channel or ""), str(channel or "") or "?")


def _int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _now_iso() -> str:
    return datetime.now(CN_TZ).isoformat(timespec="seconds")


@dataclass
class _Acc:
    """一组累加器(全局按 channel+model 一份; 当前步骤也按 channel+model 一份)。"""

    calls: int = 0
    failed_attempts: int = 0
    in_hit: int = 0
    in_miss: int = 0
    out: int = 0
    cost: float = 0.0
    peak_calls: int = 0
    unpriced_calls: int = 0
    no_usage_calls: int = 0

    @property
    def total_tokens(self) -> int:
        return self.in_hit + self.in_miss + self.out

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "failed_attempts": self.failed_attempts,
            "in_hit": self.in_hit,
            "in_miss": self.in_miss,
            "out": self.out,
            "total_tokens": self.total_tokens,
            # 9 位小数: 单次调用的费用常在 1e-4 量级, 只留 6 位会把单次成本抹掉
            "cost_cny": round(self.cost, 9),
            "peak_calls": self.peak_calls,
            "unpriced_calls": self.unpriced_calls,
            "no_usage_calls": self.no_usage_calls,
        }


def merge_accs(accs) -> _Acc:
    """把若干 `_Acc` 求和(用于"整步合计"与"按通道合计")。"""
    total = _Acc()
    for a in accs:
        total.calls += a.calls
        total.failed_attempts += a.failed_attempts
        total.in_hit += a.in_hit
        total.in_miss += a.in_miss
        total.out += a.out
        total.cost += a.cost
        total.peak_calls += a.peak_calls
        total.unpriced_calls += a.unpriced_calls
        total.no_usage_calls += a.no_usage_calls
    return total


def split_by_channel(keyed: dict[tuple[str, str], "_Acc"]) -> dict[str, dict[str, Any]]:
    """`{(channel, model): _Acc}` → `{channel: 该通道合计}`。"""
    buckets: dict[str, list[_Acc]] = {}
    for (channel, _model), acc in keyed.items():
        buckets.setdefault(str(channel), []).append(acc)
    return {ch: merge_accs(accs).as_dict() for ch, accs in buckets.items()}


def split_by_model(keyed: dict[tuple[str, str], "_Acc"]) -> dict[str, dict[str, Any]]:
    """`{(channel, model): _Acc}` → `{"channel/model": 该模型合计}`。"""
    return {f"{ch}/{model}": acc.as_dict() for (ch, model), acc in sorted(keyed.items())}


class UsageMeter:
    """线程安全的 LLM 用量/费用累加器 + 落盘。

    `enabled=False`(dry-run) 时全部 no-op: 不累加、不落盘, 避免 Mock 数据污染真实账。
    """

    def __init__(
        self,
        prices: PriceTable | None = None,
        *,
        enabled: bool = True,
        jsonl_path: str | None = None,
        csv_path: str | None = None,
        run_id: str | None = None,
        cat: str = "",
        log=None,
        max_mb: float = 0.0,
        keep: int = 5,
    ):
        self.prices = prices or builtin_table()
        self.enabled = bool(enabled)
        self.jsonl_path = jsonl_path
        self.csv_path = csv_path
        self.cat = cat or ""
        self.log = log
        # 账本超 max_mb 时轮转(0=关闭)。默认关闭, 由 build_meter 按配置打开 ——
        # 直接 new 出来的 meter 不该在测试里偷偷搬动文件。
        self.max_mb = float(max_mb or 0)
        self.keep = max(1, int(keep or 1))
        self.run_id = run_id or (
            datetime.now(CN_TZ).strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
        )
        self._lock = threading.RLock()
        # 全局(整轮)与当前步骤各一份, **都按 (channel, model) 分键** ——
        # 文本/图片两个通道必须分开, 否则无法回答"图片花了多少"(见 CSV_HEADER 注释)。
        self._by_key: dict[tuple[str, str], _Acc] = {}
        self._step_key: dict[tuple[str, str], _Acc] = {}
        self._cur_step: int | None = None
        self._steps: dict[int, dict[str, Any]] = {}
        self._meta_written = False
        self._warned_models: set[str] = set()
        self._failure_reasons: dict[str, int] = {}
        self.errors = 0

    # ------------------------------------------------------------ 记账

    def record(
        self,
        usage: Any,
        *,
        channel: str,
        model: str,
        purpose: str = "",
        attempt: int = 1,
        at: datetime | None = None,
    ) -> None:
        """记一次**计费成功**的调用。任何异常都被吞掉(记账不得打断流水线)。"""
        if not self.enabled:
            return
        try:
            self._record(usage, channel=channel, model=model,
                         purpose=purpose, attempt=attempt, at=at)
        except Exception as exc:                       # noqa: BLE001
            self._note_error(f"用量记账失败: {type(exc).__name__}: {exc}")

    def record_failure(
        self,
        *,
        channel: str,
        model: str,
        purpose: str = "",
        attempt: int = 1,
        reason: str = "",
    ) -> None:
        """记一次**未计费**的失败尝试(429/超时/截断等)。只计数, 不写明细行。"""
        if not self.enabled:
            return
        try:
            with self._lock:
                key = (str(channel), str(model or ""))
                self._by_key.setdefault(key, _Acc()).failed_attempts += 1
                self._step_key.setdefault(key, _Acc()).failed_attempts += 1
                if reason:
                    self._failure_reasons[reason[:120]] = (
                        self._failure_reasons.get(reason[:120], 0) + 1
                    )
        except Exception as exc:                       # noqa: BLE001
            self._note_error(f"失败计数出错: {type(exc).__name__}: {exc}")

    def _record(self, usage, *, channel, model, purpose, attempt, at) -> None:
        if not isinstance(usage, dict):
            usage = {}
        in_hit = _int(usage.get("prompt_cache_hit_tokens"))
        in_miss = _int(usage.get("prompt_cache_miss_tokens"))
        if not usage.get("prompt_cache_hit_tokens") and not usage.get("prompt_cache_miss_tokens"):
            # 兼容只给 prompt_tokens 的实现: 无从判断命中 → 全部按未命中计(会略高估, 不低估)
            in_miss = _int(usage.get("prompt_tokens"))
        out = _int(usage.get("completion_tokens"))
        no_usage = not usage

        peak = is_peak(at)
        cost, priced = self.prices.cost(
            model, in_hit=in_hit, in_miss=in_miss, out=out, peak=peak
        )

        with self._lock:
            key = (str(channel), str(model or ""))
            for acc in (self._by_key.setdefault(key, _Acc()),
                        self._step_key.setdefault(key, _Acc())):
                acc.calls += 1
                acc.in_hit += in_hit
                acc.in_miss += in_miss
                acc.out += out
                acc.cost += cost
                if peak:
                    acc.peak_calls += 1
                if not priced:
                    acc.unpriced_calls += 1
                if no_usage:
                    acc.no_usage_calls += 1
            if not priced:
                self._warn_unpriced(model)
            line = {
                "type": "call",
                "ts": _now_iso(),
                "run_id": self.run_id,
                "cat": self.cat,
                "step": self._cur_step,
                "channel": str(channel),
                "purpose": purpose,
                "model": str(model or ""),
                "pricing_model": self.prices.canonical(model),
                "peak": peak,
                "attempt": int(attempt),
                "in_hit": in_hit,
                "in_miss": in_miss,
                "out": out,
                "total": in_hit + in_miss + out,
                "cost_cny": round(cost, 9),
                "priced": bool(priced),
                "usage_missing": no_usage,
                "pid": os.getpid(),
            }
            self._append_jsonl(line)

    def _warn_unpriced(self, model: str) -> None:
        m = str(model or "")
        if m in self._warned_models:
            return
        self._warned_models.add(m)
        self._warn(
            f"模型 {m or '(空)'} 不在官方价目表内 → 费用未定价(不猜、不按 0 计)。"
            f"建议改用 deepseek-flash / deepseek-v4-pro"
        )

    def _note_error(self, msg: str) -> None:
        self.errors += 1
        self._warn(msg)

    def _warn(self, msg: str) -> None:
        if self.log is not None:
            try:
                self.log.warning(msg)
            except Exception:                          # noqa: BLE001
                pass

    def _info(self, msg: str) -> None:
        if self.log is not None:
            try:
                self.log.info(msg)
            except Exception:                          # noqa: BLE001
                pass

    # ------------------------------------------------------------ 步骤边界

    def begin_step(self, number: int) -> None:
        """进入某步: 重置本步累加器(全局累加不受影响)。"""
        if not self.enabled:
            return
        with self._lock:
            self._cur_step = int(number)
            self._step_key = {}

    def finish_step(self, number: int, status: str = "") -> str:
        """收尾某步: 落 step-summary + 重写 CSV + 打一行日志。返回该行文本。

        `by_channel` / `by_model` 是 step-summary 的**加性**字段(2026-09-28):
        老读者只取 calls/in_hit/... 不受影响, 新读者(体检/报表/看板)可以拆到通道与模型。
        """
        if not self.enabled:
            return ""
        try:
            with self._lock:
                acc = merge_accs(self._step_key.values())
                split_fields = {
                    "by_channel": split_by_channel(self._step_key),
                    "by_model": split_by_model(self._step_key),
                }
                self._steps[int(number)] = {
                    "step": int(number),
                    "status": str(status or ""),
                    **acc.as_dict(),
                    **split_fields,
                    "price_source": self.prices.source,
                }
                line = {
                    "type": "step-summary",
                    "run_id": self.run_id,
                    "cat": self.cat,
                    "ts": _now_iso(),
                    "step": int(number),
                    "status": str(status or ""),
                    # pid(2026-09-29): 与 call 记录同字段, 便于把"哪一次尝试"的两类记录对上
                    "pid": os.getpid(),
                    **acc.as_dict(),
                    **split_fields,
                    "price_source": self.prices.source,
                }
                self._append_jsonl(line)
                self._write_csv()
                text = self.step_line(number)
            if text:
                self._info(text)
            return text
        except Exception as exc:                       # noqa: BLE001
            self._note_error(f"步骤用量落盘失败: {type(exc).__name__}: {exc}")
            return ""

    def _step_row(self, num: int) -> dict | None:
        """取某步的汇总行: 已收尾的走 `_steps`, 未收尾的用当前累加器现算。"""
        row = self._steps.get(int(num))
        if row is not None:
            return row
        if self._cur_step == int(num) and self._step_key:
            acc = merge_accs(self._step_key.values())
            return {**acc.as_dict(), "by_channel": split_by_channel(self._step_key)}
        return None

    def step_line(self, number: int | None = None) -> str:
        """单行摘要(供进度日志)。该步无调用则返回空串(不刷无意义的 0 行)。

        **双通道时按通道拆开报**(2026-09-28): 文本与图片是两个模型/两个 key,
        图片 token 又是服务端按尺寸折算进 prompt 的 —— 只报合计就看不出图片花了多少。
        单通道时保持原样, 不啰嗦。
        """
        num = self._cur_step if number is None else int(number)
        if num is None:
            return ""
        row = self._step_row(int(num))
        if row is None:
            return ""
        acc = _Acc(
            calls=row["calls"], failed_attempts=row["failed_attempts"],
            in_hit=row["in_hit"], in_miss=row["in_miss"], out=row["out"],
            cost=row["cost_cny"], peak_calls=row["peak_calls"],
            unpriced_calls=row["unpriced_calls"], no_usage_calls=row["no_usage_calls"],
        )
        if not acc.calls and not acc.failed_attempts:
            return ""
        channels = {ch: v for ch, v in (row.get("by_channel") or {}).items()
                    if v.get("calls") or v.get("failed_attempts")}
        head = f"调用 {acc.calls} 次"
        cost_txt = f"费用估算 {acc.cost:.4f} 元"
        if len(channels) > 1:
            head += "(" + "/".join(
                f"{channel_label(ch)} {v['calls']}" for ch, v in sorted(channels.items())
            ) + ")"
            cost_txt += "(" + " + ".join(
                f"{channel_label(ch)} {v['cost_cny']:.4f}" for ch, v in sorted(channels.items())
            ) + ")"
        extra = ""
        if acc.unpriced_calls:
            extra += f" | 未定价 {acc.unpriced_calls} 次"
        if acc.no_usage_calls:
            extra += f" | 未返回用量 {acc.no_usage_calls} 次，费用未完整统计"
        if acc.failed_attempts:
            extra += f" | 失败尝试 {acc.failed_attempts} 次(未计费)"
        return (
            f"步骤 {num:02d} LLM 用量: {head} | "
            f"tokens {acc.total_tokens}"
            f"(命中 {acc.in_hit}/未命中 {acc.in_miss}/输出 {acc.out}) | "
            f"{cost_txt}{extra}"
        )

    # ------------------------------------------------------------ 查询

    def snapshot(self) -> dict[str, Any]:
        """全局汇总: 分组 + 合计 + **按通道/按模型拆分** + 价格来源。"""
        with self._lock:
            groups = split_by_model(self._by_key)
            by_channel = split_by_channel(self._by_key)
            total = merge_accs(self._by_key.values())
        return {
            "run_id": self.run_id,
            "cat": self.cat,
            "price_source": self.prices.source,
            "currency": self.prices.currency,
            "groups": groups,
            "by_channel": by_channel,
            "total": total.as_dict(),
            "errors": self.errors,
        }

    def totals(self) -> dict[str, Any]:
        return self.snapshot()["total"]

    def totals_by_channel(self) -> dict[str, dict[str, Any]]:
        """整轮按通道合计(文本/图片分开)。"""
        return self.snapshot()["by_channel"]

    def add(self, other: "UsageMeter") -> None:
        """把另一个 meter 的量并入本 meter(便于跨类目汇总)。"""
        if not self.enabled or not other.enabled:
            return
        with self._lock:
            for src, dst in ((other._by_key, self._by_key),
                             (other._step_key, self._step_key)):
                for key, acc in src.items():
                    mine = dst.setdefault(key, _Acc())
                    mine.calls += acc.calls
                    mine.failed_attempts += acc.failed_attempts
                    mine.in_hit += acc.in_hit
                    mine.in_miss += acc.in_miss
                    mine.out += acc.out
                    mine.cost += acc.cost
                    mine.peak_calls += acc.peak_calls
                    mine.unpriced_calls += acc.unpriced_calls
                    mine.no_usage_calls += acc.no_usage_calls

    def as_text(self) -> str:
        """人读摘要(冒烟脚本/报表用)。"""
        snap = self.snapshot()
        lines = [f"LLM 价格来源: {snap['price_source']}"]
        for key, g in snap["groups"].items():
            channel, _, model = key.partition("/")
            lines.append(
                f"  {channel:<6} {model:<32} {g['calls']:>4} 次 | "
                f"命中 {g['in_hit']} | 未命中 {g['in_miss']} | 输出 {g['out']} | "
                f"{g['cost_cny']:.4f} 元"
            )
        t = snap["total"]
        lines.append(
            f"  合计: {t['calls']} 次调用, {t['total_tokens']} tokens, "
            f"费用估算 {t['cost_cny']:.4f} {snap['currency']}"
        )
        if t["failed_attempts"]:
            lines.append(f"  (另有 {t['failed_attempts']} 次失败尝试, 未计费)")
        if t["unpriced_calls"]:
            lines.append(f"  (其中 {t['unpriced_calls']} 次为未定价模型, 费用未计入)")
        if self.errors:
            lines.append(f"  (记账自身出错 {self.errors} 次, 详见日志)")
        return "\n".join(lines)

    # ------------------------------------------------------------ 落盘

    def _append_jsonl(self, obj: dict) -> None:
        if not self.jsonl_path:
            return
        try:
            os.makedirs(os.path.dirname(self.jsonl_path) or ".", exist_ok=True)
            if not self._meta_written:
                # 只看一次体积(在首次写入前), 不在每次 append 上做 stat
                rotate_if_needed(self.jsonl_path, self.max_mb, self.keep,
                                 self.run_id, log=self.log)
            lines: list[str] = []
            if not self._meta_written:
                lines.append(json.dumps(self._meta(), ensure_ascii=False))
                self._meta_written = True
            lines.append(json.dumps(obj, ensure_ascii=False))
            with open(self.jsonl_path, "a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        except OSError as exc:
            self._note_error(f"用量明细写入失败: {exc}")

    def _meta(self) -> dict:
        return {
            "type": "meta",
            "ts": _now_iso(),
            "run_id": self.run_id,
            "cat": self.cat,
            "pid": os.getpid(),
            "price_source": self.prices.source,
            "currency": self.prices.currency,
            "prices": self.prices.prices,
        }

    def _write_csv(self) -> None:
        """长表: 每 (步骤 × 通道 × 模型) 一行 —— 文本与图片必须分开, 见 CSV_HEADER 注释。"""
        if not self.csv_path:
            return
        buf = io.StringIO()
        # lineterminator="\n": csv 默认写 "\r\n", 再经 Windows 文本模式会把 \n 翻成 \r\n
        # → 变成 "\r\r\n", Excel 里每行之间冒出一个空行。这里统一写 \n, 由底层翻成 CRLF。
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(CSV_HEADER)
        for num in sorted(self._steps):
            row = self._steps[num]
            per_model = row.get("by_model") or {}
            if not per_model:
                # 兼容: 没有任何 by_model 时(理论不会发生)退化成一行"未分通道"
                writer.writerow([f"{num:02d}", row.get("status", ""), "?", "",
                                 row.get("calls", 0), row.get("failed_attempts", 0),
                                 row.get("in_hit", 0), row.get("in_miss", 0),
                                 row.get("out", 0), f"{row.get('cost_cny', 0.0):.4f}"])
                continue
            for key in sorted(per_model, key=lambda k: -per_model[k]["cost_cny"]):
                channel, _, model = key.partition("/")
                g = per_model[key]
                writer.writerow([
                    f"{num:02d}", row.get("status", ""), channel, model,
                    g["calls"], g["failed_attempts"],
                    g["in_hit"], g["in_miss"], g["out"], f"{g['cost_cny']:.4f}",
                ])
        # \ufeff 开头 = UTF-8 BOM, 让 Excel 正确识别中文表头(与 step-duration.csv 同处理)
        _atomic_write(self.csv_path, "\ufeff" + buf.getvalue())

    def write_run_summary(self) -> None:
        """把本轮全局汇总追加进明细文件(便于事后按 run_id 直接读总账)。"""
        if not self.enabled or not self.jsonl_path:
            return
        try:
            with self._lock:
                self._append_jsonl({"type": "run-summary", **self.snapshot()})
        except Exception as exc:                       # noqa: BLE001
            self._note_error(f"运行汇总写入失败: {type(exc).__name__}: {exc}")


def _atomic_write(path: str, content: str) -> None:
    from .io_utils import atomic_write_text        # 延迟导入, 见 core/prices.py 顶部说明

    atomic_write_text(path, content)


# ------------------------------------------------------------------ 账本读取
#
# 体检(步骤19)与跨类目报表都走这里 —— 它们只需要"读账", 不该为此 new 一个 meter。
# 口径: 按 run_id **累加 step-summary**。同一轮若分多段跑完(强杀续跑), 每段各写一份
# 同名步骤的 step-summary, 累加即得该步本轮总账; 刻意**不用** run-summary 参与聚合
# (它是每段的快照, 拿它累加会重复计数)。

_SUM_FIELDS = ("calls", "failed_attempts", "in_hit", "in_miss", "out",
               "cost_cny", "peak_calls", "unpriced_calls", "no_usage_calls")
# 累加时统一用 float(容错), 但对账输出前必须还原成 int —— 否则日志里会出现
# "未定价 2.0 次" 这种浮点计数(2026-09-28 由测试抓出)。
_INT_FIELDS = ("calls", "failed_attempts", "in_hit", "in_miss", "out",
               "peak_calls", "unpriced_calls", "no_usage_calls")
# 未归属任何步骤的调用(独立脚本直连 meter 时 `step` 为 null)在累计里的桶号
_NO_STEP = -1


def rotate_if_needed(path: str, max_mb: float, keep: int, tag: str, *, log=None) -> bool:
    """账本超过 max_mb 时轮转为 `llm-usage-<tag>.jsonl`, 只保留最近 keep 份。

    append-only 的账本又位于**被归档的监控目录**里 → 每轮归档都会复制一份,
    不轮转会一直长。返回是否发生了轮转。任何失败都只告警不抛。
    """
    try:
        if not path or not max_mb or max_mb <= 0 or not os.path.exists(path):
            return False
        if os.path.getsize(path) < float(max_mb) * 1024 * 1024:
            return False
        base, ext = os.path.splitext(path)
        target = f"{base}-{tag}{ext}"
        n = 1
        while os.path.exists(target):                 # 同一 run 二次触发不覆盖旧账
            target = f"{base}-{tag}-{n}{ext}"
            n += 1
        os.replace(path, target)
        _prune_rotated(base, ext, keep)
        if log is not None:
            try:
                log.warning(f"LLM 用量账本超过 {max_mb:g} MB, 已轮转为 {os.path.basename(target)}")
            except Exception:                          # noqa: BLE001
                pass
        return True
    except OSError:
        return False


def _prune_rotated(base: str, ext: str, keep: int) -> None:
    """只保留最近 keep 份轮转账本(按 mtime), 其余删除。"""
    folder = os.path.dirname(base) or "."
    prefix = os.path.basename(base) + "-"
    try:
        candidates = [
            os.path.join(folder, f) for f in os.listdir(folder)
            if f.startswith(prefix) and f.endswith(ext)
        ]
    except OSError:
        return
    candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    for old in candidates[max(1, int(keep)):]:
        try:
            os.remove(old)
        except OSError:
            continue


def ledger_paths(monitor_dir: str) -> list[str]:
    """监控目录下的账本文件(主文件 + 轮转出来的), 主文件优先。"""
    folder = str(monitor_dir or "")
    out: list[str] = []
    main = os.path.join(folder, JSONL_NAME)
    if os.path.exists(main):
        out.append(main)
    try:
        stem = os.path.splitext(JSONL_NAME)[0]
        for name in sorted(os.listdir(folder)):
            if name.startswith(stem + "-") and name.endswith(".jsonl"):
                out.append(os.path.join(folder, name))
    except OSError:
        pass
    return out


def load_records(path: str) -> list[dict]:
    """读账本 JSONL; 坏行跳过(与项目其它 JSONL 读取同口径)。"""
    records: list[dict] = []
    if not path or not os.path.exists(path):
        return records
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(obj, dict):
                    records.append(obj)
    except OSError:
        return records
    return records


def _blank() -> dict:
    d: dict[str, Any] = {k: 0 for k in _SUM_FIELDS}
    d["cost_cny"] = 0.0
    return d


def _add_fields(dst: dict, src: dict) -> None:
    for f in _SUM_FIELDS:
        dst[f] += float(src.get(f) or 0)


def _finalize(d: dict) -> dict:
    d["cost_cny"] = round(d["cost_cny"], 9)
    for f in _INT_FIELDS:
        d[f] = int(d[f])
    d["total_tokens"] = d["in_hit"] + d["in_miss"] + d["out"]
    return d


def latest_run_id(records: list[dict]) -> str:
    """账本里**最后出现**的 run_id。

    账本是 append-only(轮转只把旧账整份搬走), 所以最后写入的那条就属于最近一轮 ——
    看板拿到它才能回答"这类目本轮花了多少"(而不必知道快照目录名)。
    """
    for r in reversed(records):
        rid = str(r.get("run_id") or "")
        if rid:
            return rid
    return ""


def summarize_run(records: list[dict], run_id: str) -> dict:
    """按 run_id 聚合。

    返回含三层口径:
      - `totals` / `steps[n]`: 合并口径(老读者照旧)
      - `steps[n]["by_channel"]` / `totals_by_channel`: **文本 vs 图片分开**
      - `steps[n]["by_model"]`  / `totals_by_model`:  按 `通道/模型` 分开

    通道维度来自 step-summary 的 `by_channel`(2026-09-28 起写入)。**老账本没有该字段时,
    把它的量归到 `unknown`** —— 这样"通道合计仍等于总账", 不会出现两个数对不上。
    """
    totals = _blank()
    ch_totals: dict[str, dict] = {}
    md_totals: dict[str, dict] = {}
    steps: dict[int, dict] = {}
    cat = ""
    price_source = ""
    currency = "CNY"

    for r in records:
        if str(r.get("run_id") or "") != str(run_id or ""):
            continue
        rtype = r.get("type")
        if rtype == "meta":
            cat = cat or str(r.get("cat") or "")
            price_source = price_source or str(r.get("price_source") or "")
            currency = str(r.get("currency") or currency)
        elif rtype == "step-summary":
            try:
                num = int(r.get("step"))
            except (TypeError, ValueError):
                continue
            row = steps.setdefault(num, {"step": num, "status": "",
                                         "by_channel": {}, "by_model": {}, **_blank()})
            row["status"] = str(r.get("status") or row.get("status") or "")
            _add_fields(row, r)
            _add_fields(totals, r)

            by_ch = r.get("by_channel")
            if isinstance(by_ch, dict) and by_ch:
                for ch, vals in by_ch.items():
                    _add_fields(row["by_channel"].setdefault(str(ch), _blank()), vals or {})
                    _add_fields(ch_totals.setdefault(str(ch), _blank()), vals or {})
            else:
                _add_fields(row["by_channel"].setdefault("unknown", _blank()), r)
                _add_fields(ch_totals.setdefault("unknown", _blank()), r)

            by_md = r.get("by_model")
            if isinstance(by_md, dict):
                for key, vals in by_md.items():
                    _add_fields(row["by_model"].setdefault(str(key), _blank()), vals or {})
                    _add_fields(md_totals.setdefault(str(key), _blank()), vals or {})

    _finalize(totals)
    for row in steps.values():
        _finalize(row)
        for v in row["by_channel"].values():
            _finalize(v)
        for v in row["by_model"].values():
            _finalize(v)
    for v in ch_totals.values():
        _finalize(v)
    for v in md_totals.values():
        _finalize(v)

    return {"run_id": str(run_id or ""), "cat": cat, "price_source": price_source,
            "currency": currency, "totals": totals, "steps": steps,
            "totals_by_channel": ch_totals, "totals_by_model": md_totals}


def _step_of(record: dict) -> int:
    """记录归属的步骤号; 未归属(独立脚本直连 meter, `step` 为 null)归到 -1。"""
    raw = record.get("step")
    if raw is None:
        return _NO_STEP
    try:
        return int(raw)
    except (TypeError, ValueError):
        return _NO_STEP


def _row_from_keyed(keyed: dict[tuple[str, str], "_Acc"]) -> dict[str, Any]:
    """`{(channel, model): _Acc}` → 一行汇总(含 by_channel / by_model 拆分)。"""
    acc = merge_accs(keyed.values())
    return {**acc.as_dict(),
            "by_channel": split_by_channel(keyed),
            "by_model": split_by_model(keyed)}


def _select_steps(rec_key: dict[int, dict[tuple[str, str], "_Acc"]],
                  sum_rows: dict[int, dict[str, Any]],
                  sum_ch: dict[int, dict[str, dict[str, Any]]],
                  sum_md: dict[int, dict[str, dict[str, Any]]],
                  meta_sum: dict[int, dict[str, Any]],
                  meta_rec: dict[int, dict[str, Any]]) -> tuple:
    """按「每步取 max(call 汇总, step-summary 汇总)」合并两个来源(见 summarize_category)。

    返回 `(steps, totals, totals_by_channel, totals_by_model, orphan_calls)`,
    入参已按 lifetime 或 本轮 过滤好 —— 累计与本轮用**同一套合并规则**, 不会两处口径打架。
    """
    steps: dict[int, dict[str, Any]] = {}
    orphan_calls = 0
    for num in sorted(set(rec_key) | set(sum_rows)):
        rec_row = _row_from_keyed(rec_key.get(num, {}))
        sum_row = dict(sum_rows.get(num, _blank()))
        sum_row["by_channel"] = dict(sum_ch.get(num, {}))
        sum_row["by_model"] = dict(sum_md.get(num, {}))
        rec_calls = int(rec_row.get("calls") or 0)
        sum_calls = int(sum_row.get("calls") or 0)
        use_rec = rec_calls >= sum_calls
        row = dict(rec_row if use_rec else sum_row)
        # 失败尝试只在 summary 里计数(失败尝试不写 call 明细行) → 恒取 summary 侧, 不会重复
        row["failed_attempts"] = int(sum_rows.get(num, {}).get("failed_attempts") or 0)
        if use_rec:
            orphan_calls += max(0, rec_calls - sum_calls)
        ms, mr = meta_sum.get(num) or {}, meta_rec.get(num) or {}
        row["step"] = num
        row["status"] = str(ms.get("status") or "")
        row["last_ts"] = str(max(str(ms.get("last_ts") or ""), str(mr.get("last_ts") or "")))
        attempts = int(ms.get("attempts") or 0)
        if rec_calls > sum_calls:
            attempts += 1        # 有一次被强杀/中断的尝试只留下 call 记录
        row["attempts"] = max(attempts, 1 if rec_calls else 0)
        steps[num] = row

    totals = _blank()
    ch_totals: dict[str, dict[str, Any]] = {}
    md_totals: dict[str, dict[str, Any]] = {}
    for row in steps.values():
        _add_fields(totals, row)
        for name, sub in (row.get("by_channel") or {}).items():
            _add_fields(ch_totals.setdefault(str(name), _blank()), sub or {})
        for name, sub in (row.get("by_model") or {}).items():
            _add_fields(md_totals.setdefault(str(name), _blank()), sub or {})
    _finalize(totals)
    for row in steps.values():
        _finalize(row)
        for v in (row.get("by_channel") or {}).values():
            _finalize(v)
        for v in (row.get("by_model") or {}).values():
            _finalize(v)
    for v in ch_totals.values():
        _finalize(v)
    for v in md_totals.values():
        _finalize(v)
    return steps, totals, ch_totals, md_totals, orphan_calls


def _summarize_category_core(records: list[dict]) -> dict:
    """类目**累计**口径(2026-09-29 用户定稿): 历史 + 本次叠加, 永不因重置清零。

    用户的记账规则(原话): "把历史和本次都叠加在一起, 就是累计的, 中途中断也要累计,
    然后如果重置了步骤, token 和花费不重置, 依然是累计"。据此:

    ① **以 `call` 记录为累计的主力**: call 记录是**每次调用即时落盘**的, 所以被 Ctrl+C /
       页面强杀掐断的步骤, 已经烧掉的 token 也算得出来。旧口径只累加 `step-summary`,
       而 step-summary 只在 DONE/SKIP/FAILED 时写 → 中断/强杀那一步的用量会**凭空消失**
       (2026-09-29 双保险修复: 编排器中断时补写 INTERRUPTED 汇总; 这里是硬强杀的第二道)。
    ② **不按 run_id 过滤**: 跨进程分段、跨重跑、跨"重置步骤 / 重置类目"只有**一个累计数**,
       永不清零;"本轮"(最近一轮 run_id)的量另放 `run_totals`, 便于对照。
    ③ **不重复计数**: 同一批调用既在 call 记录里、又在 step-summary 里, 所以**按步**取
       `max(call 汇总, step-summary 汇总)`。两个来源互为补集时取大的那个: 既覆盖"中断的
       孤儿调用"(只有 call 记录), 也兼容 2026-09-28 之前只有 summary 的老账本。
       局限: 同一步同时存在"老账本 summary"与"新版被强杀的 call"时, 会少算孤儿那一段
       (可接受的边角; 正常情况下两个来源等量, 且中断已由 ① 的汇总兜底)。

    返回(全部 JSON 友好):
      - `totals` / `steps[n]` / `totals_by_channel` / `totals_by_model`: **累计口径**
      - `steps[n]["status"]` / `["attempts"]` / `["last_ts"]`: 展示用(尝试次数、最近一次时间)
      - `run_id`(最近一轮) / `run_totals`(含 by_channel / by_model): 本轮口径
      - `runs`: 账本里出现过的 run_id(按出现顺序)
      - `orphan_calls`: 有 call 记录但该步没写汇总的调用数(= 被强杀那部分的调用数)
    """
    run_id = latest_run_id(records)
    cat = ""
    price_source = ""
    currency = "CNY"
    runs: list[str] = []
    runs_seen: set[str] = set()

    rec_key: dict[int, dict[tuple[str, str], _Acc]] = {}      # 累计: call 记录 → 步骤 → 通道/模型
    sum_rows: dict[int, dict[str, Any]] = {}                  # 累计: 步骤 → summary 累加行
    sum_ch: dict[int, dict[str, dict[str, Any]]] = {}
    sum_md: dict[int, dict[str, dict[str, Any]]] = {}
    meta_sum: dict[int, dict[str, Any]] = {}                  # 步骤 → 状态/尝试次数/最近时间
    meta_rec: dict[int, dict[str, Any]] = {}                  # 步骤 → call 记录条数/最近时间
    r_rec: dict[int, dict[tuple[str, str], _Acc]] = {}         # 本轮: 同上的四套
    r_sum: dict[int, dict[str, Any]] = {}
    r_ch: dict[int, dict[str, dict[str, Any]]] = {}
    r_md: dict[int, dict[str, dict[str, Any]]] = {}
    r_meta_sum: dict[int, dict[str, Any]] = {}
    r_meta_rec: dict[int, dict[str, Any]] = {}

    for r in records:
        rtype = r.get("type")
        if rtype == "meta":
            cat = cat or str(r.get("cat") or "")
            price_source = price_source or str(r.get("price_source") or "")
            currency = str(r.get("currency") or currency)
            continue
        rid = str(r.get("run_id") or "")
        if rid and rid not in runs_seen:
            runs_seen.add(rid)
            runs.append(rid)
        in_run = bool(run_id) and rid == run_id
        num = _step_of(r)
        ts = str(r.get("ts") or "")
        if rtype == "call":
            key = (str(r.get("channel") or "unknown"), str(r.get("model") or ""))
            targets = [rec_key.setdefault(num, {}).setdefault(key, _Acc())]
            if in_run:
                targets.append(r_rec.setdefault(num, {}).setdefault(key, _Acc()))
            for acc in targets:
                acc.calls += 1
                acc.in_hit += _int(r.get("in_hit"))
                acc.in_miss += _int(r.get("in_miss"))
                acc.out += _int(r.get("out"))
                acc.cost += float(r.get("cost_cny") or 0)
                if r.get("peak"):
                    acc.peak_calls += 1
                if r.get("priced") is False:
                    acc.unpriced_calls += 1
                if r.get("usage_missing"):
                    acc.no_usage_calls += 1
            for store in ([meta_rec] + ([r_meta_rec] if in_run else [])):
                mr = store.setdefault(num, {"calls": 0, "last_ts": ""})
                mr["calls"] += 1
                if ts > mr["last_ts"]:
                    mr["last_ts"] = ts
        elif rtype == "step-summary":
            for rows, ch, md, meta in ([(sum_rows, sum_ch, sum_md, meta_sum)]
                                       + ([(r_sum, r_ch, r_md, r_meta_sum)] if in_run else [])):
                _add_fields(rows.setdefault(num, _blank()), r)
                ms = meta.setdefault(num, {"attempts": 0, "status": "", "last_ts": ""})
                ms["attempts"] += 1
                ms["status"] = str(r.get("status") or ms["status"] or "")
                if ts > ms["last_ts"]:
                    ms["last_ts"] = ts
                ch_vals = r.get("by_channel")
                if isinstance(ch_vals, dict) and ch_vals:
                    for name, sub in ch_vals.items():
                        _add_fields(ch.setdefault(num, {}).setdefault(str(name), _blank()), sub or {})
                else:
                    # 老账本(2026-09-28 之前)没有 by_channel → 归到 unknown, 保证
                    # "通道合计 == 总账", 也让前端 tooltip 的拆分不落空
                    _add_fields(ch.setdefault(num, {}).setdefault("unknown", _blank()), r)
                for name, sub in (r.get("by_model") or {}).items():
                    _add_fields(md.setdefault(num, {}).setdefault(str(name), _blank()), sub or {})

    steps, totals, ch_totals, md_totals, orphan_calls = _select_steps(
        rec_key, sum_rows, sum_ch, sum_md, meta_sum, meta_rec)
    r_steps: dict[int, dict[str, Any]] = {}
    if run_id:
        r_steps, run_totals, r_ch_totals, r_md_totals, _orphans = _select_steps(
            r_rec, r_sum, r_ch, r_md, r_meta_sum, r_meta_rec)
    else:
        run_totals, r_ch_totals, r_md_totals = _blank(), {}, {}
        _finalize(run_totals)
    run_totals["by_channel"] = r_ch_totals
    run_totals["by_model"] = r_md_totals
    # 每步再挂一份"本轮该步"的数字(累计为主, 本轮备查; 不改变累计值)
    for num, row in steps.items():
        rrow = r_steps.get(num) or {}
        row["run_calls"] = int(rrow.get("calls") or 0)
        row["run_total_tokens"] = int(rrow.get("total_tokens") or 0)
        row["run_cost_cny"] = float(rrow.get("cost_cny") or 0.0)

    return {"run_id": run_id, "cat": cat, "price_source": price_source,
            "currency": currency, "totals": totals, "steps": steps,
            "totals_by_channel": ch_totals, "totals_by_model": md_totals,
            "run_totals": run_totals, "runs": runs, "orphan_calls": orphan_calls}


def summarize_category(records: list[dict]) -> dict:
    """累计流水线和步骤18面板任务；两类账本分开去重后再相加。"""
    prefix = WEB_STEP18_RUN_PREFIX
    panel = [r for r in records if str(r.get("run_id") or "").startswith(prefix)]
    if not panel:
        return _summarize_category_core(records)
    ordinary = [r for r in records if not str(r.get("run_id") or "").startswith(prefix)]
    if not ordinary:
        return _summarize_category_core(panel)
    base = _summarize_category_core(ordinary)
    extra = _summarize_category_core(panel)

    def add_rows(target, source):
        _add_fields(target, source)
        _finalize(target)

    def add_buckets(target, source):
        for key, row in source.items():
            add_rows(target.setdefault(key, _blank()), row)

    add_rows(base["totals"], extra["totals"])
    for name in ("totals_by_channel", "totals_by_model"):
        add_buckets(base[name], extra[name])
    for step, row in extra["steps"].items():
        if step not in base["steps"]:
            base["steps"][step] = {**row, "run_calls": 0, "run_total_tokens": 0, "run_cost_cny": 0.0}
            continue
        dest = base["steps"][step]
        add_rows(dest, row)
        for key in ("by_channel", "by_model"):
            add_buckets(dest.setdefault(key, {}), row.get(key) or {})
        dest["attempts"] = int(dest.get("attempts") or 0) + int(row.get("attempts") or 0)
        if str(row.get("last_ts") or "") > str(dest.get("last_ts") or ""):
            dest["last_ts"] = row["last_ts"]
            dest["status"] = row.get("status") or dest.get("status") or ""
    base["runs"] += [rid for rid in extra["runs"] if rid not in base["runs"]]
    base["orphan_calls"] += extra["orphan_calls"]
    base["cat"] = base["cat"] or extra["cat"]
    base["price_source"] = base["price_source"] or extra["price_source"]
    return base


def summarize_runs(records: list[dict]) -> list[dict]:
    """把整份账本按 (cat, run_id) 分组汇总, 费用从高到低排序(报表用)。"""
    seen: list[tuple[str, str]] = []
    keys: set[tuple[str, str]] = set()
    for r in records:
        key = (str(r.get("cat") or ""), str(r.get("run_id") or ""))
        if key not in keys:
            keys.add(key)
            seen.append(key)
    out = [summarize_run(records, run_id) for _cat, run_id in seen]
    out.sort(key=lambda x: x["totals"]["cost_cny"], reverse=True)
    return out


# ------------------------------------------------------------------ 便捷构造

_DEFAULT_METER: UsageMeter | None = None
_DEFAULT_LOCK = threading.Lock()


def default_meter() -> UsageMeter:
    """不传 meter 的 `LLMClient` 落到的全局兜底 meter。

    **刻意只用内置价表、不联网、不落盘** —— 它服务的是 `scripts/*.py` 里直接
    `LLMClient(config)` 这类临时入口; 让"随手 new 一个 client"就触发价格抓取
    既意外又会让单元测试碰网络。真实流水线的 meter 由 `build_meter()` 构造。
    """
    global _DEFAULT_METER
    with _DEFAULT_LOCK:
        if _DEFAULT_METER is None:
            _DEFAULT_METER = UsageMeter(
                builtin_table("内置快照(未绑定配置的兜底 meter)"),
                cat="(standalone)",
            )
        return _DEFAULT_METER


def reset_default_meter() -> None:
    """重建兜底 meter(测试用)。"""
    global _DEFAULT_METER
    with _DEFAULT_LOCK:
        _DEFAULT_METER = None


def build_meter(config, paths=None, *, log=None, run_id: str | None = None) -> UsageMeter:
    """按配置构造流水线用的 meter: 解析价格 + 绑定落盘路径。

    dry-run 返回 `enabled=False` 的 meter(Mock 数据不进真实账)。
    """
    prices = resolve_prices(config, log=log)
    if bool(config.get("DRY_RUN", False)):
        return UsageMeter(prices, enabled=False, log=log, cat=str(config.get("CAT", "") or ""))

    jsonl_path = csv_path = None
    if paths is not None and bool(config.get("LLM_USAGE_LOG", True)):
        try:
            mdir = paths.monitor_dir()
            jsonl_path = os.path.join(mdir, JSONL_NAME)
            csv_path = os.path.join(mdir, CSV_NAME)
        except Exception:                              # noqa: BLE001 — 落盘不可用则退化为纯内存
            jsonl_path = csv_path = None
    return UsageMeter(
        prices,
        jsonl_path=jsonl_path,
        csv_path=csv_path,
        run_id=run_id,
        cat=str(config.get("CAT", "") or ""),
        log=log,
        max_mb=float(config.get("LLM_USAGE_MAX_MB", 20) or 0),
        keep=int(config.get("LLM_USAGE_KEEP", 5) or 5),
    )
