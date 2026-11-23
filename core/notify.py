"""消息推送与预警记录 (FR-17)。

- 预警记录追加写 20_备份与监控/monitor-alerts.txt (WARN/CRIT 分级)
- 企业微信 Webhook 推送 (未配置静默跳过, 最多推送前 8 条)
- **重置边界**: 重置步骤时文件内追加 [RESET] 行, 推送只取最近一次 [RESET] 之后的
  预警(文件内容保留可回溯, 但历史预警不随新运行重推)
- CRIT 级弹系统消息框(Windows msg 命令, 失败不影响主流程)
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime

WARN = "WARN"
CRIT = "CRIT"
RESET = "RESET"
RESET_MARK = "[RESET]"


def with_context(content: str, config=None, *, include_run_id: bool = False) -> str:
    """Attach actual category owner and run actor; never invent missing identities."""
    cat = str(getattr(config, 'cat', '') or os.environ.get('PIPELINE_CATEGORY_NAME') or '').strip()
    owner = str(os.environ.get('PIPELINE_CATEGORY_OWNER') or '').strip()
    actor = str(os.environ.get('PIPELINE_WORKBENCH_USER') or '').strip()
    if include_run_id:
        run = os.environ.get('PIPELINE_RUN_ID') or getattr(config, 'runtime_run_id', '')
        return (f"类目：{cat or '未记录'}\n创建人：{owner or '未记录'}\n"
                f"运行人：{actor or '未记录'}\n运行批次：{run or '未记录'}\n{content}")
    return (f"类目：{cat or '未记录'}\n创建人：{owner or '未记录'}\n"
            f"运行人：{actor or '未记录'}\n{content}")


def append_alert(alerts_file: str, level: str, message: str) -> None:
    """追加写预警记录(带时间戳)。"""
    os.makedirs(os.path.dirname(alerts_file), exist_ok=True)
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [{level}] {message}"
    with open(alerts_file, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    # stdout 仅作控制台提示, 尽力而为(2026-09-08): 父进程(webui)重启/退出后
    # 子进程 stdout 管道对端断开, 此处 print 会抛 OSError [Errno 22], 不能拖垮步骤
    try:
        print(f"[alert] {line}")
    except Exception:  # noqa: BLE001
        pass


def push_wecom(webhook: str, content: str) -> bool:
    """企业微信机器人推送。未配置 Webhook 静默返回 False。"""
    if not webhook:
        return False
    if not str(content).startswith('类目：'):
        content = with_context(str(content))
    try:
        import httpx
        resp = httpx.post(
            webhook,
            json={"msgtype": "text", "text": {"content": content[:4000]}},
            timeout=10,
        )
        if resp.status_code != 200:
            return False
        return resp.json().get('errcode') == 0
    except Exception:
        return False


def push_alerts(webhook: str, alerts_file: str, limit: int = 8, *, config=None) -> bool:
    """推送预警文件中的前 limit 条 (FR-17: 配置则推送前8条, 未配置静默)。

    重置边界: 若文件含 [RESET] 行(步骤重置时写入), 只推送**最近一次 [RESET] 之后**
    追加的预警——历史预警保留在文件里可回溯, 但不随新运行重推到企业微信。
    无 [RESET] 行时行为不变(推送文件末尾 limit 条)。
    """
    if not webhook or not os.path.exists(alerts_file):
        return False
    try:
        with open(alerts_file, "r", encoding="utf-8-sig") as fh:
            lines = [ln.strip() for ln in fh if ln.strip()]
    except OSError:
        return False
    if not lines:
        return False
    start = 0
    for i, ln in enumerate(lines):
        if RESET_MARK in ln:
            start = i + 1
    tail = lines[start:]
    if not tail:
        return False
    body = "Amazon选品流水线预警\n" + "\n".join(tail[-limit:])
    return push_wecom(webhook, with_context(body, config))


def popup_critical(message: str) -> None:
    """CRIT 级弹系统消息框(失败静默)。"""
    try:
        subprocess.Popen(
            ["msg", "%USERNAME%", message[:200]],
            shell=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass
