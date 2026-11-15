"""LLM 客户端 (7.3 硬性规范)。

- 强制非思考模式: 默认在请求体注入 thinking 关闭参数(可通过 LLM_NO_THINK/LLM_EXTRA_BODY 配置);
  模型名含 reasoner 时直接报错, 防止模型输出复述文本
- 结构化输出优先 JSON 模式(response_format=json_object) + 容错解析
- 多温重试(换温度) + 指数退避; 连接被拒冷却 60s, 限流/SSL/截断冷却 20s (FR-16)
- 视觉通道: 图片 URL 直传 (detail 可调 low/high), 默认不截断图片张数
- dry_run 模式使用 MockLLM 返回模拟数据, 便于离线审查每步逻辑
"""
from __future__ import annotations

import json
import random
import re
import time
from typing import Any

import httpx

from .config import Config
from .errors import LLMError, LLMTruncatedError
from .mock import MockLLM
from .usage import UsageMeter, default_meter

TEMP_CYCLE_TEXT = [0.2, 0.1, 0.4]
# 2026-10-08: 原 [1.0, 0.7, 1.2] 是高温档。视觉主调用温度降到 0.2 后,
# 重试若跳回 1.0 会把刚压下去的随机性重新放进来(轴序/尺寸读数最怕这个),
# 故与文本档对齐, 保持 0.1~0.4 低温度带(仍保留跨次采样差异, 重试才有意义)。
TEMP_CYCLE_VISION = [0.2, 0.1, 0.4]
# DeepSeek 模型最大输出长度(官方文档: 384K)。原 16000 是盐胡椒类目(25属性)时代的保守值,
# 32 属性类目的标注输出会撞它 → 截断拆批变慢。2026-09-24 调到模型上限。
MAX_TOKENS_CAP = 384 * 1024


def extract_json(text: str) -> Any:
    """容错解析 LLM 返回中的 JSON。

    依次尝试: 直接解析 -> 提取最外层 {...} 或 [...] -> 修复未加引号 key/多余逗号。
    """
    if not text:
        raise LLMError("LLM 返回为空")
    text = text.strip()
    candidates = [text]
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = text.find(open_ch)
        end = text.rfind(close_ch)
        if start != -1 and end > start:
            candidates.append(text[start : end + 1])
    for cand in candidates:
        cand = _strip_code_fence(cand)
        try:
            return json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            repaired = _repair_json(cand)
            try:
                return json.loads(repaired)
            except (json.JSONDecodeError, ValueError):
                continue
    # ★ 2026-10-29: 输出被 max_tokens 截断时，**抢救已完整的行**而不是整批作废。
    #   实测 Salt and Pepper 步骤17：a2 某批 JSON 在第 362 行写到一半被切断
    #   → 整批 REJECT → 只剩 1 遍过闸门 → 步骤17 直接失败（44 分钟白跑）。
    salvaged = _salvage_rows(text)
    if salvaged is not None:
        return salvaged
    raise LLMError(f"无法解析 LLM JSON 输出: {text[:200]}")


def _salvage_rows(text: str):
    """从被截断的 `{"rows":[{...},{...}` 里救出**完整**的对象。

    只处理 `rows` 数组；逐对象按花括号配平扫描（跳过字符串内的括号），
    解析成功的收下、解析失败的丢弃。一个都没救到就返回 None（保持原有报错）。
    """
    i = text.find('"rows"')
    if i == -1:
        return None
    j = text.find("[", i)
    if j == -1:
        return None
    items: list = []
    depth, start, in_str, esc = 0, -1, False, False
    for k in range(j + 1, len(text)):
        ch = text[k]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = k
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start != -1:
                    try:
                        items.append(json.loads(text[start:k + 1]))
                    except (json.JSONDecodeError, ValueError):
                        pass
                    start = -1
        elif ch == "]" and depth == 0:
            break
    if not items:
        return None
    return {"rows": items}


def _strip_code_fence(text: str) -> str:
    text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _repair_json(text: str) -> str:
    """修复常见 JSON 错误: 未加引号的 key、多余逗号、单引号。

    2026-09-13: 单引号替换只针对**充当 JSON 定界符**的单引号 —— 原先 `re.sub(r"\\'", '"')` 会把
    字符串值内部的撇号一起换掉(如 "Amazon's Choice" → "Amazon"s Choice"), 把"本来能修好"的
    JSON 反而改成永久无法解析; 现在用非字母数字边界判定, 保留单词内撇号。
    """
    text = re.sub(r"([,{]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:", r'\1"\2":', text)
    text = re.sub(r",\s*([}\]])", r"\1", text)
    # 只替换**定界用的**单引号: 两侧都是字母数字的撇号(Amazon's / don't)原样保留。
    # (原实现无差别 re.sub(r"\'", '"') 会把这类撇号也换成双引号, 使"本来能修好"的 JSON 彻底解析失败)
    text = re.sub(
        r"'",
        lambda m: (
            "'"
            if 0 < m.start() < len(text) - 1
            and text[m.start() - 1].isalnum()
            and text[m.start() + 1].isalnum()
            else '"'
        ),
        text,
    )
    return text


class LLMClient:
    """OpenAI 兼容 chat completions 客户端(文本+视觉)。

    `meter`: 用量/费用累加器。传 None 时落到 `core.usage.default_meter()`
    (内置价表、不联网、不落盘) —— 这样 `scripts/*.py` 里直接 `LLMClient(config)`
    的临时入口也不会丢账。真实流水线由 `StepContext` 传入按步骤归属的 meter。
    """

    def __init__(self, config: Config, meter: UsageMeter | None = None):
        self.config = config
        self._mock = MockLLM() if config.dry_run else None
        self._meter = meter or default_meter()

    def meter(self) -> UsageMeter:
        return self._meter

    def _note_failure(self, exc: BaseException, channel: str, purpose: str,
                      model: str, attempt: int) -> None:
        """记一次未计费的失败尝试(429/超时/截断等), 供"重试浪费"统计。

        记账自身出错绝不冒泡 —— 它只是监控, 不能影响 LLM 调用的成败语义。
        """
        try:
            self._meter.record_failure(
                channel=channel, model=model, purpose=purpose,
                attempt=attempt, reason=str(exc),
            )
        except Exception:  # noqa: BLE001
            pass

    def _endpoint(self, base_url: str) -> str:
        base = base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return base + "/chat/completions"

    def _request(
        self,
        base_url: str,
        api_key: str,
        model: str,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
        json_mode: bool,
        images: list[str] | None = None,
        no_think: bool | None = None,
        detail: str = "low",
        max_images: int | None = None,
        channel: str = "text",
        purpose: str = "chat",
        attempt: int = 1,
    ) -> tuple[str, str]:
        if not api_key:
            raise LLMError(f"LLM 密钥未配置: {base_url}")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        think = self.config.get("LLM_NO_THINK", True) if no_think is None else no_think
        if think:
            extra = self.config.get("LLM_EXTRA_BODY", {}) or {}
            if "thinking" not in extra:
                extra["thinking"] = {"type": "disabled"}
            payload.update(extra)
        if images:
            image_content = []
            for url in (images[:max_images] if max_images else images):
                image_content.append(
                    {"type": "image_url", "image_url": {"url": url, "detail": detail}}
                )
            text_content = {"type": "text", "text": messages[0]["content"]}
            payload["messages"] = [
                {
                    "role": "user",
                    "content": [text_content] + image_content,
                }
            ]
        try:
            # trust_env=False(2026-09 修复): LLM 接口(DeepSeek 等)国内直连, 不读系统代理。
            # 否则 httpx 会走 Windows 系统代理(如 Veee 设的 localhost:15236), 而 localhost 先解析
            # 成 IPv6 ::1、代理常只监听 IPv4 127.0.0.1 → SYN_SENT 永久等待, LLM 调用"卡死"
            # (judge_batch/过滤阶段整批超时重试, 步骤看似 RUNNING 实则冻结)。
            timeout = max(10, int(self.config.get("LLM_TIMEOUT", 60) or 60))
            data = self._post_with_watchdog(
                self._endpoint(base_url), headers, payload, timeout
            )
        except LLMError as exc:
            self._note_failure(exc, channel, purpose, model, attempt)
            raise
        except (httpx.HTTPError, OSError) as exc:
            # SSL 断连/超时等传输层错误统一包装为 LLMError,
            # 让上层多温重试 + 冷却 + 失败全保留容错生效, 不炸掉整个步骤
            err = LLMError(f"LLM 请求失败: {type(exc).__name__}: {exc}")
            self._note_failure(err, channel, purpose, model, attempt)
            raise err from exc
        except ValueError as exc:
            err = LLMError(f"LLM 响应解析失败: {exc}")
            self._note_failure(err, channel, purpose, model, attempt)
            raise err from exc
        # —— 用量埋点(全项目唯一位置) ——
        # 放在"HTTP 成功返回之后、解析 choices 之前":
        #   ① 此时已经产生计费;
        #   ② finish_reason=length 的那次响应**确实花了钱**, 不能等"解析成功"才记
        #      (否则升 max_tokens 重试的浪费会被系统性漏计)。
        if not isinstance(data, dict):
            err = LLMError("LLM 返回空响应或无效结构")
            self._note_failure(err, channel, purpose, model, attempt)
            raise err
        self._meter.record(data.get("usage"), channel=channel, model=model,
                           purpose=purpose, attempt=attempt)
        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
            finish_reason = choice.get("finish_reason", "") or ""
            return content, finish_reason
        except (KeyError, IndexError, TypeError):
            raise LLMError(f"LLM 返回结构异常: {str(data)[:200]}")

    def _post_with_watchdog(self, url: str, headers: dict, payload: dict, timeout: float) -> dict:
        """发 LLM 请求并强制在 timeout 内返回(线程级 watchdog)。

        背景(2026-09 实测): httpx 的 timeout 不覆盖"服务端接受请求后静默挂起"
        (ssl.read 永不返回, select 无数据 → read timeout 不触发), 长判定 prompt 偶发
        该情况会把步骤拖成"看似卡死"。用 **daemon 线程**执行请求, 主线程等待 timeout:
        到点强制抛 LLMError(卡死线程为 daemon, 随进程退出不阻塞), 让上层快速失败/兜底。
        """
        import threading

        box: dict = {"data": None, "exc": None, "done": False}
        lock = threading.Lock()

        def _do() -> None:
            try:
                with httpx.Client(timeout=timeout, trust_env=False) as client:
                    resp = client.post(url, headers=headers, json=payload)
                    if resp.status_code == 429 or resp.status_code == 503:
                        raise LLMError(f"LLM 限流({resp.status_code}), 冷却后重试")
                    resp.raise_for_status()
                    data = resp.json()
                with lock:
                    box["data"] = data
            except BaseException as exc:  # noqa: BLE001 - 任何异常都收进 box
                with lock:
                    box["exc"] = exc
            finally:
                with lock:
                    box["done"] = True

        t = threading.Thread(target=_do, daemon=True)
        t.start()
        # 用轮询替代 join(timeout): Windows 下 daemon 线程卡在 ssl.read(C 扩展)时
        # join(timeout) 实测不返回(2026-09), 轮询 box 标志可保证准时超时。
        deadline = time.time() + timeout
        while time.time() < deadline:
            if box["done"]:
                break
            time.sleep(0.1)
        if not box["done"]:
            raise LLMError(f"LLM 请求超时(>{timeout:.0f}s, 服务端无响应/挂起)")
        with lock:
            if box["exc"] is not None:
                exc = box["exc"]
            else:
                return box["data"]
        if isinstance(exc, LLMError):
            raise exc
        if isinstance(exc, (httpx.HTTPError, OSError)):
            raise LLMError(f"LLM 请求失败: {type(exc).__name__}: {exc}") from exc
        raise LLMError(f"LLM 请求异常: {type(exc).__name__}: {exc}") from exc

    def chat(
        self,
        prompt: str,
        temperature: float = 0.2,
        max_tokens: int = 4000,
        json_mode: bool = False,
    ) -> str:
        """文本对话。失败自动换温度重试。"""
        if self._mock is not None:
            return self._mock.chat(prompt)
        model = str(self.config.get("LLM_MODEL", ""))
        if "reasoner" in model.lower():
            raise LLMError(f"模型 {model} 为思考模式, 违反非思考模式硬性要求")
        errors: list[str] = []
        for attempt, temp in enumerate((temperature, *TEMP_CYCLE_TEXT), start=1):
            if temp in TEMP_CYCLE_TEXT and temp == temperature:
                pass
            try:
                msg = [{"role": "user", "content": prompt}]
                content, _ = self._request(
                    str(self.config.get("LLM_BASE_URL", "")),
                    str(self.config.get("LLM_API_KEY", "")),
                    model,
                    msg,
                    temp,
                    max_tokens,
                    json_mode,
                    channel="text",
                    purpose="chat",
                    attempt=attempt,
                )
                return content
            except LLMError as exc:
                errors.append(str(exc))
                if self._is_hard_error(str(exc)):
                    # 超时/连接/限流等: 换温度也大概率失败, 直接抛给上层兜底, 不空转拖时间
                    raise
                self._cool_down(str(exc))
        raise LLMError("; ".join(errors[-3:]))

    def chat_json(
        self,
        prompt: str,
        temperature: float = 0.2,
        max_tokens: int = 4000,
        *,
        output_token_cap: int | None = None,
        purpose: str = "chat_json",
    ) -> Any:
        """JSON 模式对话(强制 JSON 输出)并容错解析。

        默认保留既有输出扩容策略；调用方可设 output_token_cap，截断后交上层拆批。
        """
        if self._mock is not None:
            return self._mock.chat_json(prompt)
        model = str(self.config.get("LLM_MODEL", ""))
        if "reasoner" in model.lower():
            raise LLMError(f"模型 {model} 为思考模式, 违反非思考模式硬性要求")
        last_err: str = ""
        attempt = 0
        cap = MAX_TOKENS_CAP if output_token_cap is None else int(output_token_cap)
        if cap < 1 or not 1 <= max_tokens <= cap:
            raise ValueError("max_tokens必须为正且不超过output_token_cap")
        token_limits = (max_tokens, min(max_tokens * 2, cap), cap)
        if output_token_cap is not None:
            token_limits = tuple(dict.fromkeys(token_limits))
        for temp in (temperature, *TEMP_CYCLE_TEXT):
            for tokens in token_limits:
                attempt += 1
                try:
                    msg = [{"role": "user", "content": prompt}]
                    text, finish_reason = self._request(
                        str(self.config.get("LLM_BASE_URL", "")),
                        str(self.config.get("LLM_API_KEY", "")),
                        model,
                        msg,
                        temp,
                        tokens,
                        True,
                        channel="text",
                        purpose=purpose,
                        attempt=attempt,
                    )
                    if finish_reason == "length":
                        # 输出被截断: 未到上限则升 max_tokens 重试, 已到上限则抛专用错误交上层拆批
                        if tokens >= cap:
                            raise LLMTruncatedError(
                                "输出超长被截断(finish_reason=length, max_tokens=%d 已到上限)" % tokens)
                        raise LLMError("输出截断(finish_reason=length), 升 max_tokens 重试")
                    return extract_json(text)
                except LLMTruncatedError:
                    raise
                except LLMError as exc:
                    last_err = str(exc)
                    if self._is_hard_error(str(exc)):
                        # 超时/连接/限流: 重试空转无意义, 直接抛(上层 judge_batch 全保留兜底)
                        raise
                    self._cool_down(str(exc))
        raise LLMError(f"chat_json 全部重试失败: {last_err}")

    def vision(
        self,
        prompt: str,
        image_urls: list[str],
        temperature: float = 0.2,
        max_tokens: int = 4000,
        detail: str = "low",
        max_images: int | None = None,
    ) -> Any:
        """视觉通道(图片 URL 直传, detail 可调, 默认不截断图片张数)。

        强制非思考模式(需求 7.3 全局硬性): no_think=True 注入 thinking disabled,
        不受 LLM_NO_THINK 开关影响, 防止图片模型输出思考/复述文本导致解析失败。

        `detail`: "low"=低分辨率(省 token, 够看颜色/结构); "high"=高清(读小字/标签/
        认证标记等 OCR 文字类属性时用)。`max_images`: 传给模型的图片张数上限,
        None=全部(默认); 显式给整数则只取前 N 张。
        """
        if self._mock is not None:
            return self._mock.vision(prompt, image_urls)
        if not self.config.get("VISION_API_KEY"):
            raise LLMError("VISION_API_KEY 未配置, 图片通道跳过")
        model = str(self.config.get("VISION_MODEL", ""))
        last_err: str = ""
        for attempt, temp in enumerate((temperature, *TEMP_CYCLE_VISION), start=1):
            try:
                msg = [{"role": "user", "content": prompt}]
                text, _ = self._request(
                    str(self.config.get("VISION_BASE_URL", "")),
                    str(self.config.get("VISION_API_KEY", "")),
                    model,
                    msg,
                    temp,
                    max_tokens,
                    True,
                    images=image_urls,
                    no_think=True,
                    detail=detail,
                    max_images=max_images,
                    channel="vision",
                    purpose="vision",
                    attempt=attempt,
                )
                return extract_json(text)
            except LLMError as exc:
                last_err = str(exc)
                if self._is_hard_error(str(exc)):
                    raise
                self._cool_down(str(exc))
        raise LLMError(f"vision 全部重试失败: {last_err}")

    def _cool_down(self, err: str) -> None:
        err_l = err.lower()
        if "connection refused" in err_l or "connect" in err_l:
            time.sleep(60)
        elif any(k in err_l for k in ("429", "503", "限流", "ssl", "timeout", "截断")):
            time.sleep(20)
        else:
            time.sleep(1 + random.random() * 2)

    @staticmethod
    def _is_hard_error(err: str) -> bool:
        """是否"硬错误"(重试也大概率失败)——直接抛给上层兜底, 避免空转。

        2026-09-23: 429/503/限流 从硬错误改为**软错误(冷却后重试)** —— 并发标注时
        撞上限流应退避重试, 而不是立即废掉整批; 冷却 20s 后重试一般能过。
        仍保留为硬错误的只有: 超时/连接/SSL(这些重试空转无意义)。
        """
        err_l = err.lower()
        return any(k in err_l for k in ("timeout", "timed out", "read timeout", "超时",
                                        "connection", "ssl"))
