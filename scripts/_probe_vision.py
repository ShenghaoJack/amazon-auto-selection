# -*- coding: utf-8 -*-
"""视觉服务探测: 配置模型 vs deepseek-vl2 vs 文本控制组(单发, 25s watchdog)。"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

from core.config import Config

CAT = "Salt and Pepper Mill Sets-US-202608"
CFG = os.path.join("data", CAT, "config.yaml")
IMG = "https://m.media-amazon.com/images/I/61hPnbEA1XL.jpg"
WATCHDOG = 25.0


def one_call(base_url: str, key: str, model: str, with_image: bool) -> tuple[float, str]:
    box: dict = {"data": None, "exc": None, "done": False}
    lock = threading.Lock()

    def _do() -> None:
        try:
            url = base_url.rstrip("/") + "/chat/completions"
            headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
            if with_image:
                content = [
                    {"type": "text", "text": '输出 JSON: {"ok": "yes"}'},
                    {"type": "image_url", "image_url": {"url": IMG, "detail": "low"}},
                ]
            else:
                content = '输出 JSON: {"ok": "yes"}'
            payload = {
                "model": model,
                "temperature": 1.0,
                "max_tokens": 4000,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "user", "content": content}],
            }
            payload.update({"thinking": {"type": "disabled"}})
            with httpx.Client(timeout=WATCHDOG, trust_env=False) as c:
                r = c.post(url, headers=headers, json=payload)
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
                box["data"] = r.json()
        except BaseException as exc:  # noqa: BLE001
            with lock:
                box["exc"] = exc
        finally:
            with lock:
                box["done"] = True

    t = threading.Thread(target=_do, daemon=True)
    t.start()
    t0 = time.time()
    while time.time() - t0 < WATCHDOG and not box["done"]:
        time.sleep(0.1)
    el = time.time() - t0
    if not box["done"]:
        return el, "TIMEOUT 服务端无响应/挂起(与流水线报错一致)"
    with lock:
        if box["exc"] is not None:
            return el, f"ERROR {type(box['exc']).__name__}: {str(box['exc'])[:200]}"
        data = box["data"]
    try:
        content = data["choices"][0]["message"]["content"]
        return el, f"OK 返回: {str(content)[:150]!r}"
    except Exception:  # noqa: BLE001
        return el, f"OK(结构异常?) {str(data)[:250]}"


def main() -> int:
    cfg = Config.load(CFG)
    tests = [
        ("视觉 配置模型 deepseek-v4-flash-vision-exp(VISION key)", "VISION_BASE_URL", "VISION_API_KEY", "VISION_MODEL", True),
        ("视觉 对照模型 deepseek-vl2(VISION key)", "VISION_BASE_URL", "VISION_API_KEY", None, True),
        ("文本 控制组 deepseek-v4-flash(LLM key)", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", False),
    ]
    for label, burl_k, key_k, model_k, img in tests:
        base = str(cfg.get(burl_k) or "")
        key = str(cfg.get(key_k) or "")
        model = str(cfg.get(model_k) or "deepseek-vl2") if model_k else str(cfg.get(model_k) or "")
        model = model if model_k is not None else (model_k and str(cfg.get(model_k)) or "deepseek-vl2")
        # 上面两行对 None model_k 处理啰嗦, 直接按分支取
        if model_k is None:
            model = "deepseek-vl2"
        print(f"--- {label}")
        print(f"    base={base} key={'*' * 8}{key[-4:]} model={model} 图={'是' if img else '否'}")
        try:
            el, msg = one_call(base, key, model, img)
            print(f"    耗时 {el:5.1f}s | {msg}")
        except Exception as exc:  # noqa: BLE001
            print(f"    调用框架异常: {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
