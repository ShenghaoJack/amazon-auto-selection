"""跨进程远程人机验证桥接（方案 A：WebUI 交互画布 + 企业微信机器人提醒）。

架构说明：
- 流水线步骤运行在独立子进程中，WebUI 运行在 webui/server.py 进程中；
- 两者通过 `data/.remote_verify/<session_id>/` 下的原子文件通信：
  - `state.json`：会话状态、类目、步骤、URL、视口尺寸、最新帧序号、提示消息；
  - `frame.jpg`：Playwright 页面最新 JPEG 截图；
  - `actions/*.json`：WebUI 转发的用户操作（click / type / key / reload / confirm / abort）。
- 触发验证时自动通过企业微信 Webhook（`WECOM_WEBHOOK`）发送带类目/步骤/WebUI 入口链接的提醒。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from core import notify

_DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "data" / ".remote_verify"
DEFAULT_REMOTE_VERIFY_TIMEOUT = 600
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{6,80}$")
_ALLOWED_KEYS = frozenset({
    "Enter", "Backspace", "Tab", "Escape", "Space",
    "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
})


def remote_verify_root() -> Path:
    env = str(os.environ.get("PIPELINE_REMOTE_VERIFY_DIR") or "").strip()
    return Path(env) if env else _DEFAULT_ROOT


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _atomic_write_json(path: Path, obj: dict[str, Any]) -> None:
    payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    _atomic_write_bytes(path, payload)


def _read_json_safe(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _session_dir(session_id: str) -> Path:
    sid = str(session_id or "").strip()
    if not _SAFE_ID_RE.match(sid):
        raise ValueError("非法的远程验证会话 ID")
    return remote_verify_root() / sid


def cleanup_stale_sessions(max_age_seconds: float = 3600.0) -> None:
    """清理已结束或超时的旧远程验证目录。"""
    root = remote_verify_root()
    if not root.exists():
        return
    now = time.time()
    for child in root.iterdir():
        if not child.is_dir():
            continue
        st = _read_json_safe(child / "state.json")
        if st is None:
            try:
                if now - child.stat().st_mtime > 300:
                    shutil.rmtree(child, ignore_errors=True)
            except OSError:
                pass
            continue
        status = str(st.get("status") or "")
        updated = float(st.get("updated_at") or st.get("created_at") or 0)
        expires = float(st.get("expires_at") or 0)
        if status != "waiting" and now - updated > 180:
            shutil.rmtree(child, ignore_errors=True)
        elif status == "waiting" and expires > 0 and now > expires + 120:
            shutil.rmtree(child, ignore_errors=True)
        elif now - updated > max_age_seconds:
            shutil.rmtree(child, ignore_errors=True)


def list_active_sessions(allowed_cats: set[str] | None = None) -> list[dict[str, Any]]:
    """列出当前所有等待人工操作的远程验证会话（按创建时间升序）。"""
    root = remote_verify_root()
    if not root.exists():
        return []
    now = time.time()
    out: list[dict[str, Any]] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        st = _read_json_safe(child / "state.json")
        if not st or st.get("status") != "waiting":
            continue
        expires = float(st.get("expires_at") or 0)
        if expires > 0 and now > expires:
            continue
        cat = str(st.get("cat") or "").strip()
        if allowed_cats is not None and cat not in allowed_cats:
            continue
        out.append({
            "id": str(st.get("id") or child.name),
            "cat": cat,
            "step": str(st.get("step") or ""),
            "url": str(st.get("url") or ""),
            "title": str(st.get("title") or ""),
            "viewport": st.get("viewport") or {"width": 1280, "height": 800},
            "status": "waiting",
            "frame_seq": int(st.get("frame_seq") or 0),
            "message": str(st.get("message") or ""),
            "created_at": float(st.get("created_at") or 0),
            "expires_at": expires,
            "remaining_sec": max(0, int(expires - now)) if expires > 0 else 0,
            "wecom_sent": bool(st.get("wecom_sent")),
        })
    out.sort(key=lambda x: x["created_at"])
    return out


def get_session(session_id: str) -> dict[str, Any] | None:
    try:
        sdir = _session_dir(session_id)
    except ValueError:
        return None
    return _read_json_safe(sdir / "state.json")


def get_frame_bytes(session_id: str) -> bytes | None:
    try:
        sdir = _session_dir(session_id)
    except ValueError:
        return None
    fpath = sdir / "frame.jpg"
    if not fpath.is_file():
        return None
    try:
        return fpath.read_bytes()
    except OSError:
        return None


def enqueue_action(session_id: str, action: dict[str, Any]) -> dict[str, Any]:
    """将 WebUI 发来的交互动作写入会话队列（供步骤进程消费）。"""
    sdir = _session_dir(session_id)
    st = _read_json_safe(sdir / "state.json")
    if not st or st.get("status") != "waiting":
        raise ValueError("该远程验证会话已结束或不存在")
    atype = str((action or {}).get("type") or "").strip().lower()
    clean: dict[str, Any] = {"type": atype, "at": time.time()}
    if atype in ("click", "mousedown", "mousemove", "mouseup"):
        x = float(action.get("x"))
        y = float(action.get("y"))
        if not (0 <= x <= 4000 and 0 <= y <= 4000):
            raise ValueError("坐标超出有效范围")
        clean["x"] = round(x, 1)
        clean["y"] = round(y, 1)
    elif atype == "drag":
        raw_pts = action.get("points")
        if not isinstance(raw_pts, list) or len(raw_pts) < 2 or len(raw_pts) > 80:
            raise ValueError("拖拽轨迹点数须在 2~80 之间")
        pts: list[dict[str, float]] = []
        for p in raw_pts:
            if not isinstance(p, dict):
                raise ValueError("拖拽轨迹点格式非法")
            px, py = float(p.get("x")), float(p.get("y"))
            if not (0 <= px <= 4000 and 0 <= py <= 4000):
                raise ValueError("拖拽坐标超出有效范围")
            pts.append({"x": round(px, 1), "y": round(py, 1)})
        clean["points"] = pts
    elif atype == "type":
        text = str(action.get("text") or "")
        if not text or len(text) > 128:
            raise ValueError("输入文本为空或过长")
        clean["text"] = text
    elif atype == "key":
        key = str(action.get("key") or "").strip()
        if key not in _ALLOWED_KEYS:
            raise ValueError(f"不支持的按键: {key}")
        clean["key"] = key
    elif atype in ("reload", "confirm", "abort"):
        pass
    else:
        raise ValueError(f"不支持的操作类型: {atype}")

    adir = sdir / "actions"
    adir.mkdir(parents=True, exist_ok=True)
    fname = f"{int(time.time() * 1000):013d}-{uuid.uuid4().hex[:8]}.json"
    _atomic_write_json(adir / fname, clean)
    return clean


def _notify_wecom_human_verify(
    config,
    *,
    step_label: str,
    url: str,
    kind: str = "initial",
    remaining_sec: int | None = None,
) -> bool:
    """触发企业微信机器人提醒用户前往 WebUI 完成亚马逊人机验证。

    kind:
      - "initial": 首次触发提醒
      - "remind":  每 1 分钟周期提醒
      - "final":   最后剩余 20 秒时的最终提醒
    """
    webhook = str(config.get("WECOM_WEBHOOK", "") or "").strip() if config is not None else ""
    if not webhook:
        return False
    origin = str(os.environ.get("WEBUI_PUBLIC_ORIGIN") or "").strip().rstrip("/")
    entry_line = f"工作台入口：{origin}\n" if origin else "工作台入口：请打开选品工作台 WebUI\n"
    if kind == "resolved":
        msg = (
            f"【✅ 亚马逊人机验证已通过，流程已继续】\n"
            f"步骤：{step_label}\n"
            f"说明：验证已解除，无需再前往工作台操作。"
        )
        return bool(notify.push_wecom(webhook, notify.with_context(msg, config)))
    if kind == "final":
        header = "【最后提醒：亚马逊人机验证仅剩 20 秒超时】"
        tail = "超时后将自动保留断点并结束等待，请尽快处理。\n"
    elif kind == "remind":
        rem_min = max(1, int(round((remaining_sec or 60) / 60.0)))
        header = f"【再次提醒：等待人工完成亚马逊人机验证（约剩 {rem_min} 分钟）】"
        tail = ""
    else:
        header = "【需要人工完成亚马逊人机验证】"
        tail = ""
    msg = (
        f"{header}\n"
        f"步骤：{step_label}\n"
        f"{entry_line}"
        f"操作方式：打开工作台后在自动弹出的远程画面中直接点击确认或打字输入验证码，验证通过后流程会自动继续。\n"
        f"{tail}"
        f"触发页面：{str(url or '')[:200]}"
    )
    return bool(notify.push_wecom(webhook, notify.with_context(msg, config)))


def _capture_page_frame(page) -> tuple[bytes | None, dict[str, int], str]:
    """截取当前 Playwright 页面 JPEG 画面并读取视口尺寸与标题。"""
    vp = {"width": 1280, "height": 800}
    title = ""
    try:
        got_vp = page.viewport_size
        if isinstance(got_vp, dict) and got_vp.get("width") and got_vp.get("height"):
            vp = {"width": int(got_vp["width"]), "height": int(got_vp["height"])}
        else:
            dims = page.evaluate("() => ({w: window.innerWidth || 1280, h: window.innerHeight || 800})")
            if isinstance(dims, dict) and dims.get("w") and dims.get("h"):
                vp = {"width": int(dims["w"]), "height": int(dims["h"])}
    except Exception:
        pass
    try:
        title = str(page.title() or "")[:120]
    except Exception:
        pass
    try:
        raw = page.screenshot(type="jpeg", quality=70, full_page=False, timeout=8000)
        return raw, vp, title
    except Exception:
        return None, vp, title


def _consume_actions(sdir: Path) -> list[dict[str, Any]]:
    adir = sdir / "actions"
    if not adir.exists():
        return []
    out: list[dict[str, Any]] = []
    for f in sorted(adir.glob("*.json")):
        act = _read_json_safe(f)
        try:
            f.unlink()
        except OSError:
            pass
        if act:
            out.append(act)
    return out


def wait_for_human_verification(
    ctx,
    browser,
    page,
    *,
    step_label: str,
    url: str,
    is_resolved: Callable[[Any], bool],
    timeout: int | None = None,
    poll_interval: float = 0.5,
    remind_interval: float = 60.0,
    final_remind_sec: float = 20.0,
    log_fn: Callable[[str], None] | None = None,
) -> bool:
    """挂起等待远程用户在 WebUI 交互画布完成人机验证，同时发企业微信提醒。

    企业微信提醒策略：触发时发首条 → 每隔 1 分钟提醒一次 → 最后剩 20 秒发最后一次提醒。
    返回 True 表示验证已通过且页面恢复；返回 False 表示超时或人工中止。
    """
    cleanup_stale_sessions()
    cfg = getattr(ctx, "config", None)
    timeout = DEFAULT_REMOTE_VERIFY_TIMEOUT if timeout is None else max(1, int(timeout))

    cat = str(getattr(cfg, "cat", "") or os.environ.get("PIPELINE_CATEGORY_NAME") or "").strip()
    sid = f"rv-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    sdir = remote_verify_root() / sid
    sdir.mkdir(parents=True, exist_ok=True)

    now = time.time()
    expires_at = now + timeout
    wecom_sent = _notify_wecom_human_verify(cfg, step_label=step_label, url=url, kind="initial")
    last_remind_at = time.monotonic()
    final_reminded = False

    def _emit(msg: str) -> None:
        if log_fn is not None:
            log_fn(msg)
        elif hasattr(ctx, "log"):
            try:
                ctx.log.info(msg)
            except Exception:
                pass

    _emit(
        f"[{step_label}] 遇到亚马逊人机验证，已开启 WebUI 远程交互验证(会话={sid}, "
        f"企业微信提醒={'已发送' if wecom_sent else '未配置/跳过'})，等待人工点击确认(最多 {timeout}s)..."
    )

    frame_seq = 0
    last_frame_at = 0.0
    state: dict[str, Any] = {
        "id": sid,
        "cat": cat,
        "step": step_label,
        "url": str(url or ""),
        "title": "",
        "viewport": {"width": 1280, "height": 800},
        "status": "waiting",
        "frame_seq": 0,
        "message": "请直接在下方画面中点击按钮或输入验证码，验证通过后系统会自动关闭弹窗并继续流程",
        "created_at": now,
        "updated_at": now,
        "expires_at": expires_at,
        "wecom_sent": wecom_sent,
    }

    def _refresh_frame(force: bool = False) -> None:
        nonlocal frame_seq, last_frame_at
        t = time.monotonic()
        if not force and (t - last_frame_at) < max(0.35, poll_interval):
            return
        raw, vp, title = _capture_page_frame(page)
        if raw:
            _atomic_write_bytes(sdir / "frame.jpg", raw)
            frame_seq += 1
        state["viewport"] = vp
        if title:
            state["title"] = title
        try:
            cur_u = str(getattr(page, "url", "") or "")
            if cur_u:
                state["url"] = cur_u
        except Exception:
            pass
        state["frame_seq"] = frame_seq
        state["updated_at"] = time.time()
        _atomic_write_json(sdir / "state.json", state)
        last_frame_at = t

    _refresh_frame(force=True)
    deadline = time.monotonic() + timeout
    final_status = "timeout"
    try:
        while time.monotonic() < deadline:
            touch = getattr(browser, "touch_watchdog", None)
            if callable(touch):
                touch()

            actions = _consume_actions(sdir)
            acted = False
            confirm_clicked = False
            for act in actions:
                atype = str(act.get("type") or "")
                if atype == "abort":
                    final_status = "aborted"
                    state["message"] = "用户已中止本次验证等待"
                    _emit(f"[{step_label}] 远程验证已被用户中止(会话={sid})")
                    return False
                elif atype == "click":
                    try:
                        page.mouse.click(float(act["x"]), float(act["y"]))
                        acted = True
                    except Exception:
                        pass
                elif atype == "mousedown":
                    try:
                        page.mouse.move(float(act["x"]), float(act["y"]))
                        page.mouse.down()
                        acted = True
                    except Exception:
                        pass
                elif atype == "mousemove":
                    try:
                        page.mouse.move(float(act["x"]), float(act["y"]), steps=2)
                        acted = True
                    except Exception:
                        pass
                elif atype == "mouseup":
                    try:
                        page.mouse.move(float(act["x"]), float(act["y"]))
                        page.mouse.up()
                        acted = True
                    except Exception:
                        pass
                elif atype == "drag":
                    try:
                        pts = act.get("points") or []
                        if len(pts) >= 2:
                            page.mouse.move(float(pts[0]["x"]), float(pts[0]["y"]))
                            page.mouse.down()
                            for pt in pts[1:]:
                                page.mouse.move(float(pt["x"]), float(pt["y"]), steps=3)
                                time.sleep(0.015)
                            page.mouse.up()
                            acted = True
                    except Exception:
                        try:
                            page.mouse.up()
                        except Exception:
                            pass
                elif atype == "type":
                    try:
                        page.keyboard.type(str(act["text"]), delay=35)
                        acted = True
                    except Exception:
                        pass
                elif atype == "key":
                    try:
                        page.keyboard.press(str(act["key"]))
                        acted = True
                    except Exception:
                        pass
                elif atype == "reload":
                    try:
                        page.reload(wait_until="domcontentloaded", timeout=30000)
                        acted = True
                    except Exception:
                        pass
                elif atype == "confirm":
                    confirm_clicked = True
                    acted = True

            if acted:
                time.sleep(0.35)

            resolved = False
            try:
                resolved = bool(is_resolved(page))
            except Exception:
                resolved = False

            if resolved:
                final_status = "verified"
                state["message"] = "验证已通过，流程正在自动恢复"
                _emit(f"[{step_label}] 远程人机验证已通过，流程自动继续(会话={sid})")
                if wecom_sent:
                    _notify_wecom_human_verify(cfg, step_label=step_label, url=url, kind="resolved")
                return True

            if confirm_clicked:
                state["message"] = "系统检测页面仍在验证页，请确认画面已跳转后再点「我已完成验证」"

            # 企业微信周期提醒：每隔 1 分钟提醒一次；最后剩 20 秒发最后一次紧急提醒
            if wecom_sent:
                now_mono = time.monotonic()
                rem_sec = deadline - now_mono
                if not final_reminded and timeout > final_remind_sec and 0 < rem_sec <= final_remind_sec:
                    final_reminded = True
                    _notify_wecom_human_verify(cfg, step_label=step_label, url=url, kind="final")
                elif (
                    not final_reminded
                    and remind_interval > 0
                    and rem_sec > final_remind_sec
                    and (now_mono - last_remind_at) >= remind_interval
                ):
                    last_remind_at = now_mono
                    _notify_wecom_human_verify(
                        cfg, step_label=step_label, url=url, kind="remind", remaining_sec=int(rem_sec),
                    )

            _refresh_frame(force=acted)
            time.sleep(min(0.4, max(0.05, poll_interval)))

        final_status = "timeout"
        state["message"] = f"等待超过 {timeout} 秒未完成验证，已保留断点"
        _emit(f"[{step_label}] 远程人机验证超时({timeout}s)，已保留断点(会话={sid})")
        return False
    finally:
        state["status"] = final_status
        state["updated_at"] = time.time()
        try:
            _atomic_write_json(sdir / "state.json", state)
        except Exception:
            pass
