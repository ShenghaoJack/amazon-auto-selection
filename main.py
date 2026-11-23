"""Amazon 类目选品流水线 CLI 入口。

用法:
  python main.py run                 # 全流程 0-19(断点续跑; 19=备份与最终体检)
  python main.py run --from 5        # 从步骤 5 续跑
  python main.py run --dry-run       # 离线模拟(生成模拟数据, 便于逐步骤审查)
  python main.py step 14             # 单独运行步骤 14(独立审查)
  python main.py login --site amz    # 一键登录亚马逊(有头浏览器, 手动完成)
  python main.py login --site ss     # 一键登录卖家精灵
  python main.py reset               # 重置类目(归档现有数据后清空实时目录)
  python main.py archive             # 手动完整快照
  python main.py status              # 查看步骤状态与耗时
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import TYPE_CHECKING

from steps.ids import PIPELINE_STEP_NUMBERS


class _SafeStream:
    """把 stdout/stderr 包一层, 写入失败(管道断裂)时静默丢弃。

    背景(2026-10-09 实测故障): webui 以 `stdout=subprocess.PIPE` 起 `python main.py step N`;
    看板重启/被强杀后**管道读端消失**, 之后子进程任何 print 都会抛
    `OSError: [Errno 22] Invalid argument`; 更麻烦的是解释器退出时对 stdout 的最后一次
    flush 也会失败 → 进程退出码 **120**, 于是一次已经跑了 48 分钟、花掉 2505 积分的步骤
    会被编排器判成 FAILED(实测就是这样挂的: 采集/去重全成功, 标注阶段第一行 print 炸掉)。

    包一层之后: 断管只表现为"这行输出丢了"(日志仍完整落在 pipeline-progress.log), 流程与退出码不受影响。
    """

    def __init__(self, raw):
        self._raw = raw
        self._broken = False

    def write(self, s):
        if self._broken:
            return len(s)
        try:
            return self._raw.write(s)
        except OSError:                   # 断管/无效句柄 → 之后一律丢弃
            self._broken = True
            return len(s)
        except UnicodeEncodeError:        # ★ 2026-10-14: 编码不可表示的字符(如 `⏸`)不得炸
            #   实测: Windows 默认 cp936 流上写 `⏸`(U+23F8) 抛 UnicodeEncodeError,
            #   而 logging 会把它吞成 "--- Logging error ---" → **整行日志丢失**
            #   (最典型的就是"⏸ 步骤 N 暂停(等待人工确认)"这条运行小结)。
            #   这里降级为可表示字符(errors="replace"), 输出难看一点, 但不丢日志。
            enc = getattr(self._raw, "encoding", None) or "utf-8"
            try:
                self._raw.write(s.encode(enc, "replace").decode(enc, "replace"))
            except (OSError, UnicodeEncodeError, LookupError):
                self._broken = True
            return len(s)

    def flush(self):
        if self._broken:
            return
        try:
            self._raw.flush()
        except OSError:
            self._broken = True

    def __getattr__(self, name):
        return getattr(self._raw, name)


def harden_streams() -> None:
    """CLI 入口统一加固 stdout/stderr(幂等): 断管时只丢输出, 不让长跑步骤失败(见 _SafeStream)。

    ★ 2026-10-14 修: 原来只**替换** `sys.stdout`/`sys.stderr`, 而 `core/logger.py` 在建
    `logging.StreamHandler(sys.stdout)` 时**已经抓住了原始流** —— 于是日志既没有断管保护、
    也没有编码保护(实测 `⏸` 让 logging 抛 UnicodeEncodeError 并吞掉整行运行小结)。
    现在替换之后**把已有 handler 的 stream 也重指到包装流**(root + pipeline logger)。
    """
    orig = {"stdout": getattr(sys, "stdout", None), "stderr": getattr(sys, "stderr", None)}
    for name in ("stdout", "stderr"):
        stream = orig[name]
        if stream is not None and not isinstance(stream, _SafeStream):
            setattr(sys, name, _SafeStream(stream))
    try:
        import logging
    except ImportError:                   # pragma: no cover — 标准库缺失不可能
        return
    loggers = [logging.getLogger()]
    try:
        loggers.append(logging.getLogger("pipeline"))
    except Exception:                     # noqa: BLE001 — 取不到 pipeline logger 不影响加固
        pass
    for lg in loggers:
        for h in list(getattr(lg, "handlers", []) or []):
            cur = getattr(h, "stream", None)
            # 按**身份**比对原始流(不能用 id(): stdout 与 stderr 可能是同一对象,
            # id 映射会互相覆盖 → handler 被指到另一条包装流, 日志串流)
            if cur is not None and cur is orig["stdout"]:
                h.stream = sys.stdout
            elif cur is not None and cur is orig["stderr"]:
                h.stream = sys.stderr

# 仅类型检查期导入(运行时仍走 main() 内的惰性导入, 避免 CLI 启动即加载整套依赖);
# 用于 _do_login 的 runner: PipelineRunner 注解, 修复类型检查器报"PipelineRunner 未定义"。
if TYPE_CHECKING:
    from core.orchestrator import PipelineRunner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Amazon 类目选品流水线 (0-19)")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_config(p) -> None:
        # default=SUPPRESS(不是 None): 子解析器解析时会把自身默认值写回同一个 namespace,
        # 若默认是 None 就会**把全局 --config 覆盖成 None**(argparse 的经典坑) ——
        # 于是 `main.py --config X step 4`(config 写在子命令前)被静默当成 `--config config.yaml`,
        # 实测会去跑根目录占位模板。SUPPRESS = 未显式给出时不写入, 保留全局值。
        p.add_argument("--config", default=argparse.SUPPRESS, help="配置文件路径(覆盖全局)")

    p_run = sub.add_parser("run", help="运行流水线(断点续跑)")
    add_config(p_run)
    p_run.add_argument("--from", dest="from_step", type=int, default=0, choices=PIPELINE_STEP_NUMBERS)
    p_run.add_argument("--dry-run", action="store_true", help="离线模拟模式")
    p_run.set_defaults(func="run")

    p_step = sub.add_parser("step", help="单独运行某个步骤")
    add_config(p_step)
    p_step.add_argument("number", type=int, choices=PIPELINE_STEP_NUMBERS)
    p_step.add_argument("--dry-run", action="store_true")
    p_step.set_defaults(func="step")

    p_kw = sub.add_parser("confirm-keywords", help="确认步骤 8 关键词并续跑")
    add_config(p_kw)
    p_kw.add_argument("--revision", required=True)
    p_kw.add_argument("--dry-run", action="store_true")
    p_kw.set_defaults(func="confirm_keywords")

    p_login = sub.add_parser("login", help="一键登录")
    add_config(p_login)
    p_login.add_argument("--site", choices=["amz", "ss"], default="amz")
    p_login.set_defaults(func="login")

    p_reset = sub.add_parser("reset", help="重置类目(归档+清空)")
    add_config(p_reset)
    p_reset.set_defaults(func="reset")

    p_reset_step = sub.add_parser("reset-step", help="重置某步骤及全部下游(保留上游)")
    add_config(p_reset_step)
    p_reset_step.add_argument("number", type=int, choices=PIPELINE_STEP_NUMBERS)
    p_reset_step.set_defaults(func="reset_step")

    p_archive = sub.add_parser("archive", help="手动完整快照")
    add_config(p_archive)
    p_archive.set_defaults(func="archive")

    p_status = sub.add_parser("status", help="查看步骤状态")
    add_config(p_status)
    p_status.set_defaults(func="status")
    return parser


def main(argv: list[str] | None = None) -> int:
    harden_streams()      # 断管(webui 重启)时只丢输出, 不让长跑步骤失败(见 _SafeStream)
    args = build_parser().parse_args(argv)
    from core.config import Config
    from core.orchestrator import PipelineRunner

    config_path = getattr(args, "config", None) or "config.yaml"
    webui_run = "PIPELINE_CONFIG_SOURCE" in os.environ   # webui 启动时已注入 → 类目配置由服务端同步, 不再切换
    # 注入源配置文件路径: 步骤11/12 目标月下拉缺失需写回 config 时使用
    # (webui 启动的流程会由服务器注入用户真实配置文件路径, 此处 setdefault 不覆盖)
    os.environ.setdefault("PIPELINE_CONFIG_SOURCE", os.path.abspath(config_path))
    config = Config.load(config_path)
    func = args.func
    cat = config.cat
    droot = str(config.get("DATA_ROOT", "data") or "data")
    # 防呆(2026-09-13): 类目配置里的 CAT 必须等于它所在目录名 —— 否则 "配置在 data/A/ 里却写着 CAT=B"
    # 会让运行静默读写 data/B/, 并可能在 webui 误选时把 A 的内容覆盖到 B 的配置上(实战遗留目录事故)。
    if func in ("run", "step", "confirm_keywords"):
        from core.category_config import check_cat_dir_consistency
        _mismatch = check_cat_dir_consistency(config_path, cat, droot)
        if _mismatch:
            print(f"[类目配置] {_mismatch}", flush=True)
            return 2
    # 配置随类目(2026-09): CLI 直接运行 run/step 时, 若该类目已有 data/<CAT>/config.yaml
    # 则以其为"真源"(webui 里改过的东西才会生效); 没有则用当前种子首次创建。
    if not webui_run and func in ("run", "step", "confirm_keywords"):
        if cat:
            from core.category_config import ensure_category_config, is_placeholder_cat
            if not is_placeholder_cat(cat):
                percat = ensure_category_config(config_path, droot, cat)
                config = Config.load(percat)
                os.environ["PIPELINE_CONFIG_SOURCE"] = str(percat)
    # 站点口径提示(2026-09-14): CAT 站点后缀 / SITE / MARKET 三者矛盾时告警(**不阻断运行**)。
    # 混用会静默产出"某国关键词数据 + 另一国商品页面"; SITE 写成非站点名还会静默回落美国站。
    if func in ("run", "step", "confirm_keywords"):
        try:
            from core.category_config import check_site_market_consistency
            for _w in check_site_market_consistency(
                    config.cat, str(config.get("SITE", "") or ""),
                    str(config.get("MARKET", "") or "")):
                print(f"[站点口径] WARN {_w}", flush=True)
        except Exception:
            pass
    runner = PipelineRunner(config, dry_run=getattr(args, "dry_run", None))

    # 每类目单实例锁(2026-09-08, 方案A): run/step 前检查 data/<CAT>/.run.lock——
    # 同类目已有存活进程(无论 web/CLI 谁启动)则拒绝, 防双跑/僵尸叠加
    lock = None
    if func in ("run", "step", "confirm_keywords") and cat:
        try:
            from core.run_lock import acquire_run_lock
            lock = acquire_run_lock(os.path.abspath(os.path.join(droot, cat)), f"{func} {cat}")
        except RuntimeError as exc:
            print(f"[单实例锁] {exc}", flush=True)
            return 2
        except Exception as exc:  # noqa: BLE001 - 锁异常不阻断运行(退化为无锁)
            print(f"[单实例锁] 获取异常(继续运行): {exc}", flush=True)
            lock = None

    try:
        if func == "run":
            return runner.run(from_step=args.from_step)
        if func == "step":
            return runner.run_single(args.number)
        if func == "confirm_keywords":
            return runner.confirm_keywords_and_resume(args.revision)
        if func == "login":
            return _do_login(runner, args.site)
        if func == "reset":
            snap = runner.reset_category()
            runner.log.info(f"重置完成, 归档: {snap}")
            return 0
        if func == "reset_step":
            runner.reset_steps_from(args.number)
            runner.log.info(f"步骤 {args.number}-19 已重置, 可执行 python main.py run 续跑")
            return 0
        if func == "archive":
            snap = runner.snapshot_all()
            runner.log.info(f"快照: {snap}")
            return 0
        if func == "status":
            print(runner.state.summary())
            return 0
    except KeyboardInterrupt:
        runner.log.warning(
            "收到中断信号(Ctrl+C): 进行中步骤已标记 INTERRUPTED, "
            "下次 python main.py run 将从该步骤自动续跑(或 python main.py step N 单步重跑)"
        )
        return 130
    finally:
        if lock is not None:
            lock.release()
    return 2


def _do_login(runner: PipelineRunner, site: str) -> int:
    ctx = runner.ctx
    if site == "amz":
        with ctx.browser("amazon", headed=True) as browser:
            page = browser.new_page()
            browser.goto(page, f"https://{ctx.config.market}/")
            print("请在打开的浏览器窗口中完成亚马逊登录(账号密码/验证码)")
            input("登录完成后回到此窗口按回车确认...")
            ok = browser.check_amazon_login(page)
            if ok:
                try:
                    from core import amazon_login_share
                    amazon_login_share.save(page.context, ctx.paths.profile_root, ctx.config.market)
                except (OSError, ValueError, TypeError) as exc:
                    print(f"亚马逊共享登录态保存失败: {type(exc).__name__}")
            print("亚马逊登录检查:", "通过" if ok else "失败")
            return 0 if ok else 2
    else:
        with ctx.browser("sellersprite", headed=True) as browser:
            page = browser.new_page()
            browser.goto(page, "https://www.sellersprite.com/w/user/login")
            print("请在打开的浏览器窗口中完成卖家精灵登录(或自动登录)")
            input("登录完成后回到此窗口按回车确认...")
            ok = browser.check_sellersprite_login(page)
            print("卖家精灵登录检查:", "通过" if ok else "失败")
            return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
