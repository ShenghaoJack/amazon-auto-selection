"""Shared per-source cooldown; retry only explicit, temporary MCP rate limits."""
import threading
import time

from core.mcp import MCPBusinessError
from tools.voc.acquire import looks_minute_limited, looks_rate_limited


class SourceThrottle:
    def __init__(self, config, *, clock=time.monotonic, sleep=time.sleep):
        self.clock, self.sleep = clock, sleep
        self.backoff = max(0.1, min(60, float(config.get("VOC_MCP_RATE_BACKOFF_SECONDS", 5))))
        self.interval = max(0.1, min(60, float(config.get("VOC_MCP_RATE_INTERVAL_SECONDS", 2))))
        self.minute_cooldown = max(65, min(300, float(config.get("VOC_MCP_MINUTE_COOLDOWN_SECONDS", 65))))
        self.retries = max(0, min(3, int(config.get("VOC_MCP_RATE_RETRIES", 2))))
        self.lock = threading.Lock()
        self.sources = {}

    def wait(self, source):
        while True:
            with self.lock:
                state = self.sources.get(source)
                if state is None:
                    return
                delay = state["next"] - self.clock()
                if delay <= 0:
                    state["next"] = self.clock() + state["interval"]
                    return
            # Never sleep under the shared lock; the other MCP remains independent.
            self.sleep(min(1, delay))

    def limited(self, source, *, minute=False):
        with self.lock:
            state = self.sources.setdefault(source, {"next": 0, "failures": 0,
                                                     "interval": self.interval, "minute_until": 0})
            now = self.clock()
            if minute:
                # Errors already in flight belong to the same cooldown window.
                # Do not multiply the slowdown once for every parallel worker.
                if now >= state["minute_until"]:
                    state["interval"] = max(5, min(60, state["interval"] * 2))
                state["minute_until"] = max(state["minute_until"], now + self.minute_cooldown)
                state["next"] = max(state["next"], state["minute_until"])
                return state["next"] - now
            state["failures"] = min(5, state["failures"] + 1)
            delay = min(60, self.backoff * 2 ** (state["failures"] - 1))
            state["next"] = max(state["next"], now + delay)
            return state["next"] - now

    def call(self, source, function, log):
        for attempt in range(self.retries + 1):
            self.wait(source)
            try:
                result = function()
                if isinstance(result, dict) and result.get("code") not in (None, 0, "0", "OK", "ok", 200, "success"):
                    if looks_rate_limited(result.get("code"), result.get("message")):
                        raise MCPBusinessError("review", result.get("code"), str(result.get("message") or ""), result)
                with self.lock:
                    if source in self.sources:
                        self.sources[source]["failures"] = 0
                return result
            except MCPBusinessError as exc:
                if not looks_rate_limited(exc.code, exc.message):
                    raise
                minute = looks_minute_limited(exc.code, exc.message)
                delay = self.limited(source, minute=minute)
                reason = "每分钟访问已达上限" if minute else "请求过快"
                log(f"[VOC限流] {source} {reason}，同源统一冷却{delay:g}秒，恢复后错开请求；未判定额度耗尽")
                if attempt == self.retries:
                    raise
