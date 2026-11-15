# -*- coding: utf-8 -*-
"""在**隔离副本**上跑步骤17，验证步骤17 的全部修复是否对别的类目也成立。

用法（配合 `run_step16_isolated.py` 的产出目录）:
    python scripts/run_step17_isolated.py --work work/step16-isolated-XXX

它做的事:
  1. 把副本的 spec 状态改成 `confirmed`（步骤17 的前置条件）
  2. 用副本的 DATA_ROOT 跑 Step17
  3. 打印闸门结果（a1/a2 的 ERR 数、PASS/REJECT、是否触发键名归一/补漏）
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402
from core.config import Config, apply_env_overrides  # noqa: E402
from core.llm import LLMClient  # noqa: E402
from core.logger import setup_logger  # noqa: E402
from core.paths import Paths  # noqa: E402
from core.usage import UsageMeter  # noqa: E402
from steps.base import StepContext  # noqa: E402
from steps.step17_attr_annotation import Step17AttrAnnotation  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--work", required=True, help="run_step16_isolated.py 的产出目录")
    ap.add_argument("--cat", default="Cat Fountains-US-202608")
    ap.add_argument("--model", default="")
    args = ap.parse_args()

    work = Path(args.work).resolve()
    data_root = work / "data"
    cat_root = data_root / args.cat
    if not cat_root.is_dir():
        raise SystemExit("副本目录不存在: %s" % cat_root)

    # 配置从**生产目录**读（隔离副本里没有 config.yaml —— 与 run_step16_isolated 一致），
    # 但 DATA_ROOT 指向副本，保证写产物落在副本里、不碰生产。
    src_cfg = ROOT / "data" / args.cat / "config.yaml"
    if not src_cfg.exists():
        raise SystemExit("找不到生产 config.yaml: %s" % src_cfg)
    raw = yaml.safe_load(src_cfg.read_text(encoding="utf-8-sig")) or {}
    cfg = Config(apply_env_overrides(raw))
    cfg.set("CAT", args.cat)
    cfg.set("DATA_ROOT", str(data_root))
    cfg.set("DRY_RUN", False)
    if args.model:
        cfg.set("LLM_MODEL", args.model)
    if not cfg.get("LLM_API_KEY"):
        raise SystemExit("LLM_API_KEY unavailable")

    # 1) spec → confirmed
    spec_path = cat_root / ("%s-attribute-spec.json" % args.cat)
    spec = json.loads(spec_path.read_text(encoding="utf-8-sig"))
    spec["status"] = "confirmed"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    print("SPEC confirmed: %d attributes" % len(spec.get("attributes") or []), flush=True)

    log = setup_logger(str(work / "step17.log"))
    paths = Paths(cfg)
    ctx = StepContext(cfg, paths, logger=log)
    ctx._usage = UsageMeter(
        jsonl_path=str(work / "usage-step17.jsonl"),
        csv_path=str(work / "usage-step17-steps.csv"),
        cat=args.cat, log=log,
    )
    ctx._llm = LLMClient(cfg, meter=ctx._usage)

    step = Step17AttrAnnotation()
    result = {"cat": args.cat, "work": str(work), "status": "running"}
    try:
        ctx.begin_step(17)
        step.validate_upstream(ctx)
        step.run(ctx)
        step.validate_artifacts(ctx)
        result["status"] = "success"
        ctx.finish_step(17, "DONE")
    except Exception as exc:  # noqa: BLE001
        result.update(status="failed", error=str(exc), traceback=traceback.format_exc())
        try:
            ctx.finish_step(17, "FAILED")
        except Exception:  # noqa: BLE001
            pass
        log.exception("isolated step17 failed")

    # 2) 汇总闸门结果
    res_path = cat_root / "17_属性标注" / "稳定性结果.json"
    if res_path.exists():
        d = json.loads(res_path.read_text(encoding="utf-8-sig"))
        result["gates"] = {
            k: {"err_summary": g.get("err_summary"), "verdict": g.get("verdict")}
            for k, g in (d.get("gates") or {}).items()
        }
        result["deliverable_rows"] = len(d.get("deliverable") or [])
    (work / "step17-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("RESULT " + json.dumps(result, ensure_ascii=False, default=str), flush=True)
    return 0 if result.get("status") == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
