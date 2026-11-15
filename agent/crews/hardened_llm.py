"""硬化 LLM: 让报告/问答 crew 走"主流水线同款"的稳健调用逻辑(Phase 6)。

自 `core/llm.py` 复制改造, 独立于 core。核心保证:
- 强制非思考模式(thinking disabled), 模型名含 reasoner 直接报错;
- JSON 模式(response_format=json_object) + 容错解析;
- **线程级 watchdog 硬超时**(daemon 线程 + 轮询, 解决服务端静默挂起拖死请求);
- `trust_env=False`(不读系统代理, 避免 IPv6/IPv4 冻结);
- 多温重试 + 冷却 + 硬错误快速失败;
- `DRY_RUN=true` 时走内置 mock, 完全离线可测。

**依赖策略**:
- `messages_to_prompt` / `_build_llm_payload` / `extract_json` / `HardenedLLM`: 纯函数/纯类,
  不依赖 crewAI, 可单测(DRY_RUN 下走 mock, 无网络);
- `build_hardened_llm(config)`: 才懒导入 `crewai.LLM` 并子类化, 把 `call(messages)` 路由到
  `HardenedLLM`。未装 crewAI 则抛清晰错误。
"""

from __future__ import annotations

import json
import random
import threading
import time
from typing import Any

import httpx

from agent._vendor import Config

# 多温重试(与 core/llm.py 一致)
TEMP_CYCLE_TEXT = [0.2, 0.1, 0.4]
MAX_TOKENS_CAP = 16000


# ---------------------------------------------------------------- 纯函数(可测)
def messages_to_prompt(messages) -> str:
    """把 crewAI 传入的 messages(str 或 list)归一化为 prompt 字符串。"""
    if isinstance(messages, str):
        return messages
    if not isinstance(messages, (list, tuple)):
        return str(messages)
    out = []
    for m in messages:
        if isinstance(m, dict):
            role = m.get("role", "user")
            content = m.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    str(p.get("text", p)) if isinstance(p, dict) else str(p)
                    for p in content
                )
            out.append(f"{role}: {content}")
        else:
            out.append(str(m))
    return "\n".join(out)


def _build_llm_payload(
    model: str,
    prompt: str,
    temperature: float,
    max_tokens: int,
    json_mode: bool,
    no_think: bool = True,
) -> dict:
    """构造 OpenAI 兼容请求体; 注入非思考 + (可选)JSON 模式。"""
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    if no_think:
        extra = {"thinking": {"type": "disabled"}}
        payload.update(extra)
    return payload


# 公开别名(供测试验证 payload 注入的非思考/JSON)
build_llm_payload = _build_llm_payload


def extract_json(text: str) -> Any:
    """容错解析 LLM 返回中的 JSON(自 core/llm.py 复制)。"""
    if not text:
        raise ValueError("LLM 返回为空")
    text = text.strip()
    candidates = [text]
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = text.find(open_ch)
        end = text.rfind(close_ch)
        if start != -1 and end > start:
            candidates.append(text[start : end + 1])
    for cand in candidates:
        try:
            return json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
    raise ValueError(f"无法解析 LLM JSON 输出: {text[:120]}")


# ---------------------------------------------------------------- watch dog(自 core 复制)
def _post_with_watchdog(url: str, headers: dict, payload: dict, timeout: float) -> dict:
    """daemon 线程发请求, 主线程轮询 timeout, 到点强制抛错(防服务端静默挂起)。"""
    box: dict = {"data": None, "exc": None, "done": False}
    lock = threading.Lock()

    def _do() -> None:
        try:
            with httpx.Client(timeout=timeout, trust_env=False) as client:
                resp = client.post(url, headers=headers, json=payload)
                if resp.status_code in (429, 503):
                    raise RuntimeError(f"LLM 限流({resp.status_code}), 冷却后重试")
                resp.raise_for_status()
                data = resp.json()
            with lock:
                box["data"] = data
        except BaseException as exc:  # noqa: BLE001
            with lock:
                box["exc"] = exc
        finally:
            with lock:
                box["done"] = True

    t = threading.Thread(target=_do, daemon=True)
    t.start()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if box["done"]:
            break
        time.sleep(0.1)
    if not box["done"]:
        raise TimeoutError(f"LLM 请求超时(>{timeout:.0f}s, 服务端无响应/挂起)")
    with lock:
        exc = box["exc"]
        if exc is None:
            return box["data"]
    if isinstance(exc, RuntimeError):
        raise exc
    raise RuntimeError(f"LLM 请求失败: {type(exc).__name__}: {exc}") from exc


def _is_hard_error(err: str) -> bool:
    err_l = err.lower()
    return any(k in err_l for k in ("timeout", "timed out", "read timeout", "超时",
                                    "connection", "ssl", "429", "503", "限流"))


def _cool_down(err: str) -> None:
    err_l = err.lower()
    if "connection" in err_l or "connect" in err_l:
        time.sleep(1)
    elif any(k in err_l for k in ("429", "503", "限流", "ssl", "timeout")):
        time.sleep(1)
    else:
        time.sleep(1 + random.random() * 1.5)


# ---------------------------------------------------------------- 内置 mock(离线可测)
class _MockLLM:
    """DRY_RUN 时返回结构化模拟 JSON, 使硬化 LLM 离线可测。"""

    def chat(self, prompt: str) -> str:
        return json.dumps(self.chat_json(prompt), ensure_ascii=False)

    def chat_json(self, prompt: str) -> dict:
        return {"ok": True, "note": "hardened-llm mock", "prompt_head": str(prompt or "")[:40]}


# ---------------------------------------------------------------- 硬化调用类
class HardenedLLM:
    """带看门狗/非思考/JSON/多温重试的 LLM 客户端(自 core/llm.py 提炼)。"""

    def __init__(self, config: Config, timeout: float | None = None):
        self.config = config
        self._mock = _MockLLM() if config.dry_run else None
        self._timeout = float(timeout if timeout is not None else config.get("LLM_TIMEOUT", 60) or 60)

    # ---- 端点 ----
    def _endpoint(self) -> str:
        base = str(self.config.get("LLM_BASE_URL", "") or "").rstrip("/")
        if not base:
            raise ValueError("LLM_BASE_URL 未配置")
        return base + "/chat/completions"

    def _headers(self) -> dict:
        api_key = str(self.config.get("LLM_API_KEY", "") or "")
        if not api_key:
            raise ValueError("LLM_API_KEY 未配置")
        return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    def _model(self) -> str:
        model = str(self.config.get("LLM_MODEL", "") or "")
        if "reasoner" in model.lower():
            raise ValueError(f"模型 {model} 为思考模式, 违反非思考模式硬性要求")
        return model

    # ---- 公共接口 ----
    def chat(
        self,
        prompt: str,
        temperature: float = 0.2,
        max_tokens: int = 4000,
        json_mode: bool = False,
    ) -> str:
        if self._mock is not None:
            return self._mock.chat(prompt)
        last_err = ""
        for temp in (temperature, *TEMP_CYCLE_TEXT):
            try:
                payload = _build_llm_payload(
                    self._model(), prompt, temp, max_tokens, json_mode
                )
                data = _post_with_watchdog(self._endpoint(), self._headers(), payload, self._timeout)
                return data["choices"][0]["message"]["content"]
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
                if _is_hard_error(last_err):
                    raise
                _cool_down(last_err)
        raise RuntimeError(f"chat 全部重试失败: {last_err}")

    def chat_json(self, prompt: str, temperature: float = 0.2, max_tokens: int = 4000) -> Any:
        if self._mock is not None:
            return self._mock.chat_json(prompt)
        last_err = ""
        for temp in (temperature, *TEMP_CYCLE_TEXT):
            for tokens in (max_tokens, min(max_tokens * 2, MAX_TOKENS_CAP), MAX_TOKENS_CAP):
                try:
                    payload = _build_llm_payload(self._model(), prompt, temp, tokens, True)
                    data = _post_with_watchdog(self._endpoint(), self._headers(), payload, self._timeout)
                    text = data["choices"][0]["message"]["content"]
                    return extract_json(text)
                except Exception as exc:  # noqa: BLE001
                    last_err = str(exc)
                    if _is_hard_error(last_err):
                        raise
                    _cool_down(last_err)
        raise RuntimeError(f"chat_json 全部重试失败: {last_err}")

    def call_messages(self, messages, temperature: float = 0.2, max_tokens: int = 4000) -> str:
        """把 crewAI 的 messages 转成 prompt 后做文本对话(供适配器调用)。"""
        prompt = messages_to_prompt(messages)
        return self.chat(prompt, temperature=temperature, max_tokens=max_tokens)


# ---------------------------------------------------------------- crewAI 适配器(懒导入)
def build_hardened_llm(config: Config, timeout: float | None = None):
    """构造 crewAI `LLM` 兼容对象: 内部把 `call(messages)` 路由到 HardenedLLM。

    crewAI 的 `LLM`/`BaseLLM` 接口会随版本漂移; 这里按常见签名子类化,
    装 crewAI 后请按实际版本核对 `call`/`get_context_window` 等方法名与签名。
    未装 crewAI → 抛清晰 RuntimeError。
    """
    try:
        from crewai import LLM as CrewLLM
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "构建硬化 LLM 需要 crewai, 请先安装: pip install -r agent/requirements.txt"
        ) from exc

    engine = HardenedLLM(config, timeout=timeout)

    class _HardenedCrewLLM(CrewLLM):
        def __init__(self, **kwargs):
            # 保留 crewai 的 model/name 元数据, 但实际调用走 engine
            self._engine = engine
            self._engine_model = engine._model()
            super().__init__(model=self._engine_model, **kwargs)

        def call(self, messages, **kwargs):
            temperature = kwargs.get("temperature", 0.2)
            max_tokens = kwargs.get("max_tokens", 4000)
            return self._engine.call_messages(messages, temperature=temperature, max_tokens=max_tokens)

        def get_context_window(self):
            return 0

    return _HardenedCrewLLM()


def describe_hardened(config: Config) -> str:
    """一行描述(供日志): 显示是否走硬化(mock/真实)与关键开关。"""
    mode = "mock(dry-run)" if config.dry_run else "真实(需 LLM key)"
    return (
        f"硬化LLM: 模式={mode} 模型={config.get('LLM_MODEL','')} "
        f"看门狗={config.get('LLM_TIMEOUT', 60)}s 非思考=开 JSON=可选 trust_env=False"
    )
