"""Run production Step 16 on copied artifacts, with real LLM calls and isolation audit."""
from __future__ import annotations
import argparse
import copy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import pandas as pd
import yaml
from core.config import Config, apply_env_overrides
from core.llm import LLMClient
from core.logger import setup_logger
from core.paths import Paths
from core.usage import UsageMeter
from steps.base import StepContext
from steps.step16_attribute_annotation import Step16AttributeAnnotation, SPLIT_FILE, spec_extraction_settings
from tools.attr_stability.catalog import catalog_dir_of, parse_cat, resolve_spec_path
from tools.attr_stability.spec import resolve_rules_file
from tools.attr_stability.spec_io import assert_rule_alignment, load_attr_spec
from tools.cat_report.engine import choose_price_bands


def dump(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def manifest(*roots):
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for root in roots if root.exists() for p in sorted(root.rglob("*")) if p.is_file()}


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def bad_references(rules):
    names = {a["name"] for a in rules.get("attributes", [])}
    bad = []
    for rule in (rules.get("cross_checks") or {}).get("互斥", []):
        missing = sorted({str((rule.get(s) or {}).get("属性", "")) for s in ("左", "右")} - names)
        if missing:
            bad.append({"说明": rule.get("说明"), "缺失属性": missing})
    return bad


class TracedLLM:
    def __init__(self, inner, out):
        self.inner, self.out, self.records = inner, out, []
        self.config = inner.config

    def chat_json(self, prompt, **kwargs):
        i = len(self.records) + 1
        phase = ("extraction" if "多个变体商品" in prompt else
                 "global_merge" if "同义归并器" in prompt else
                 "hint_summary" if "属性说明精简" in prompt else "other")
        record = {"call": i, "phase": phase, "prompt": prompt, "kwargs": kwargs,
                  "status": "running", "started_at": datetime.now().astimezone().isoformat()}
        self.records.append(record)
        p = self.out / "llm" / f"{i:03d}-{phase}.json"
        dump(p, record)
        print(f"LLM {i:02d} {phase} started", flush=True)
        started = time.monotonic()
        try:
            result = self.inner.chat_json(prompt, **kwargs)
            record.update(status="success", result=result)
            return result
        except Exception as exc:
            record.update(status="failed", error=str(exc))
            raise
        finally:
            record["seconds"] = round(time.monotonic() - started, 3)
            dump(p, record)
            print(f"LLM {i:02d} {phase} {record['status']} {record['seconds']}s", flush=True)


class AuditedStep16(Step16AttributeAnnotation):
    def __init__(self, out):
        self.out = out
        self.rounds = []
        self.fresh = []

    def _extract_attrs(self, ctx, df, round_max=None):
        keys, desc = super()._extract_attrs(ctx, df, round_max=round_max)
        rec = {"attempt": len(self.rounds) + 1, "excel_rows": [int(i) + 2 for i in df.index],
               "asins": df["变体ASIN"].astype(str).tolist() if "变体ASIN" in df else [],
               "rows": len(df), "keys": keys, "descriptions": desc}
        self.rounds.append(rec)
        dump(self.out / "extraction-rounds.json", self.rounds)
        return keys, desc

    def _merge_prev_spec(self, ctx, fresh, exclude):
        self.fresh = copy.deepcopy(fresh)
        dump(self.out / "fresh-extracted-attributes.json", self.fresh)
        return super()._merge_prev_spec(ctx, fresh, exclude)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cat", required=True)
    parser.add_argument("--model", default="", help="覆盖 LLM_MODEL(如 deepseek-v4-pro / deepseek-chat)")
    parser.add_argument("--merge-strategy", default="baseline",
                        choices=["baseline", "vote", "cot", "twostage"],
                        help="归并策略: baseline=单次 / vote=自洽投票 / cot=思维链 / twostage=两阶段")
    parser.add_argument("--merge-mode", default="",
                        choices=["", "baseline", "fullhint", "oldspec", "both", "hybrid"],
                        help="归并 hint 粒度: 留空=用生产默认(hybrid) / baseline=典型取值 / fullhint=完整hint / "
                             "oldspec=旧spec进归并 / both=两者 / hybrid=布尔用完整hint+其余用典型取值")
    args = parser.parse_args()
    source = (ROOT / "data" / args.cat).resolve()
    if source.parent != (ROOT / "data").resolve() or not source.is_dir():
        raise ValueError("Unknown category or category path outside data")
    shared = catalog_dir_of(source.parent, parse_cat(args.cat)["类目"])
    out = ROOT / "work" / ("step16-isolated-" + args.cat.replace(" ", "_") + "-" +
                            datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
    out.mkdir(parents=True)
    print("OUTPUT " + str(out), flush=True)
    before = manifest(source, shared)
    dump(out / "source-before.json", before)
    data = out / "data"
    target = data / args.cat
    target.mkdir(parents=True)
    copies = []

    def cp(src, dst):
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copies.append({"source": str(src), "copy": str(dst)})

    split = source / SPLIT_FILE
    cp(split, target / SPLIT_FILE)
    sp = resolve_spec_path(source.parent, args.cat)
    old_spec = load_attr_spec(sp) if sp.exists() else {"attributes": []}
    if sp.exists():
        cp(sp, target / f"{args.cat}-attribute-spec.json")
    rp = resolve_rules_file(str(source))
    old_rules = read(rp) if rp else {"attributes": []}
    if rp:
        cp(Path(rp), target / Path(rp).name)
    if shared.exists():
        for file in shared.iterdir():
            if file.is_file():
                cp(file, catalog_dir_of(data, parse_cat(args.cat)["类目"]) / file.name)
    # ★ 2026-10-21: 全局同义词层文件也要复制(否则隔离测试看不到词库/规则)。
    for _gname in ("_synonyms.json", "_synonym_rules.json"):
        _gsrc = source.parent / _gname
        if _gsrc.exists():
            cp(_gsrc, data / "_catalog" / _gname)
    deliver = source / "18_标准化" / f"{args.cat}-属性标注-标准化交付.xlsx"
    if deliver.exists():
        cp(deliver, target / "18_标准化" / deliver.name)
    dump(out / "copied-inputs.json", copies)
    raw = yaml.safe_load((source / "config.yaml").read_text(encoding="utf-8-sig")) or {}
    cfg = Config(apply_env_overrides(raw))
    cfg.set("CAT", args.cat)
    cfg.set("DATA_ROOT", str(data))
    cfg.set("DRY_RUN", False)
    if args.model:
        cfg.set("LLM_MODEL", args.model)
    cfg.set("ATTR_MERGE_STRATEGY", args.merge_strategy)
    if args.merge_mode:
        cfg.set("ATTR_MERGE_MODE", args.merge_mode)
    if not cfg.get("LLM_API_KEY"):
        raise ValueError("LLM_API_KEY is unavailable")
    safe_keys = ["CAT", "DATA_ROOT", "DRY_RUN", "ATTR_SAMPLE_MODE", "ATTR_SPEC_RUNS",
                 "ATTR_SPEC_MIN_RUNS", "ATTR_BAND_ROUNDS", "ATTR_BAND_MIN_RUNS",
                 "ATTR_BAND_PER_ROUND", "ATTR_MAX", "ATTR_EXCLUDE", "LLM_MODEL", "LLM_TIMEOUT"]
    effective = spec_extraction_settings(cfg)
    dump(out / "config-safe.json", {"config": {k: cfg.get(k) for k in safe_keys}, "effective": effective})
    log = setup_logger(str(out / "run.log"))
    paths = Paths(cfg)
    assert paths.cat_root.resolve() == target.resolve()
    ctx = StepContext(cfg, paths, logger=log)
    ctx._usage = UsageMeter(jsonl_path=str(out / "usage.jsonl"), csv_path=str(out / "usage-steps.csv"),
                            cat=args.cat, log=log)
    traced = TracedLLM(LLMClient(cfg, meter=ctx._usage), out)
    ctx._llm = traced
    step = AuditedStep16(out)
    df = pd.read_excel(split, sheet_name="AI已补充")
    # 价格列名随市场不同（US=价格($) / DE=价格(€) / UK=价格(£) / 通用=价格）——按顺序找第一个存在的。
    _price_col = next((c for c in ("价格($)", "价格(€)", "价格(£)", "价格", "售价", "price")
                       if c in df.columns), None)
    if _price_col is None:
        raise SystemExit("拆分表缺少价格列；现有列: %s" % list(df.columns))
    prices = pd.to_numeric(df[_price_col].astype(str).str.strip().str.replace(r"^[$€£]\s*", "", regex=True), errors="coerce")
    valid = prices[prices.notna() & prices.gt(0)]
    info = choose_price_bands(valid.tolist())
    dump(out / "price-bands.json", info)
    started = time.monotonic()
    result = {"cat": args.cat, "status": "running", "out": str(out), "input_rows": len(df),
              "valid_prices": len(valid), "bands": info["bands"], "effective": effective,
              "old_attribute_count": len(old_spec["attributes"]), "real_llm": True,
              "model": cfg.get("LLM_MODEL"), "baseline_invalid_constraint_refs": bad_references(old_rules)}
    dump(out / "result.json", result)
    try:
        ctx.begin_step(16)
        step.validate_upstream(ctx)
        step.run(ctx)
        step.validate_artifacts(ctx)
        spec = load_attr_spec(target / f"{args.cat}-attribute-spec.json")
        rules = read(resolve_rules_file(str(target)))
        assert_rule_alignment(spec, rules)
        old_by = {a["name"]: a for a in old_spec["attributes"]}
        fresh_by = {a["name"]: a for a in step.fresh}
        band_file = target / "16_产品属性标注" / "属性-价格段.json"
        attr_bands = read(band_file).get("属性价格段", {}) if band_file.exists() else {}
        rows = []
        for i, a in enumerate(spec["attributes"], 1):
            n = a["name"]
            f = fresh_by.get(n)
            rows.append({"序号": i, "属性": n, "状态": "新增" if n not in old_by else "本轮再次命中" if f else "仅保留旧属性",
                         "本轮票数": f.get("runs") if f else None, "原票数": old_by.get(n, {}).get("runs"),
                         "最终票数": a.get("runs"), "价格段": attr_bands.get(n, []),
                         "说明": a.get("hint", ""), "精简说明": a.get("hint_short", "")})
        dump(out / "attribute-list.json", rows)
        result.update(status="success", final_attribute_count=len(rows), fresh_attribute_count=len(step.fresh),
                      added=[r["属性"] for r in rows if r["状态"] == "新增"],
                      reobserved=[r["属性"] for r in rows if r["状态"] == "本轮再次命中"],
                      retained_only=[r["属性"] for r in rows if r["状态"] == "仅保留旧属性"],
                      gate_bypassed=spec.get("gate_bypassed"),
                      spec_rules_aligned=True, final_invalid_constraint_refs=bad_references(rules),
                      extraction_attempts=len(step.rounds), successful_extractions=sum(bool(r["keys"]) for r in step.rounds),
                      llm_calls=len(traced.records), price_bands_path=str(band_file))
        lines = [f"# {args.cat} 步骤16隔离测试", "", f"输入 {len(df)} 行；{len(info['bands'])} 个价格段；每段 {effective['runs']} 轮；门槛 {effective['min_runs']}。", "",
                 f"原属性 {len(old_by)} 个，本轮采纳 {len(fresh_by)} 个，最终 {len(rows)} 个。", "",
                 "|序号|属性|状态|本轮票数|原票数|价格段|说明|", "|---:|---|---|---:|---:|---|---|"]
        clean = lambda s: str(s).replace("|", "/").replace("\n", " ")
        for r in rows:
            lines.append("|%s|%s|%s|%s|%s|%s|%s|" % (r["序号"],r["属性"],r["状态"],r["本轮票数"] if r["本轮票数"] is not None else "—",
                         r["原票数"] if r["原票数"] is not None else "—",clean("、".join(r["价格段"])),clean(r["说明"])))
        (out / "属性列表.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
        ctx.finish_step(16, "DONE")
    except Exception as exc:
        result.update(status="failed", error=str(exc), traceback=traceback.format_exc())
        ctx.finish_step(16, "FAILED")
        log.exception("Isolated Step 16 failed")
    finally:
        after = manifest(source, shared)
        changed = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
        dump(out / "source-after.json", after)
        result.update(seconds=round(time.monotonic()-started, 2), source_files_checked=len(before),
                      original_unchanged=not changed, changed_original_files=changed)
        dump(out / "result.json", result)
        print("RESULT " + json.dumps({k:v for k,v in result.items() if k not in ("bands","baseline_invalid_constraint_refs","final_invalid_constraint_refs")},ensure_ascii=False),flush=True)
    if result["status"] != "success":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
