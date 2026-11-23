# -*- coding: utf-8 -*-
"""融合版步骤4 的接线层（从 `scripts/_plan_pilot/kwgen_v2/kwgen_hybrid.py` 抽取并去 CLI 化）。

只做"配置→站点/登记根词/产品口径"解析与 LLM/MCP 客户端接线；引擎逻辑在 `engine.py`。
本模块**不写任何正式产物**，只供 `steps/step04_keywords.py` 调用。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlparse

import yaml

from core.catname import display_cat
from core.config import Config, apply_env_overrides
from core.llm import LLMClient
from core.mcp import MCPClient, _SITE_TO_CODE

from .engine import (
    HybridPipeline,
    discovery_valid,
    entity_decision_valid,
    judgment_valid,
    key,
    normalize_policy,
    registered_entries,
    write_outputs,
)

__all__ = [
    "DOMAIN_MARKETS", "ENGLISH_SITES", "site_code", "resolve_site", "read_config",
    "load_registered_roots", "digest", "CachedCalls", "LiveLLM", "LiveMiner",
    "ExperimentLLM", "error_category", "source_artifact", "load_product_policy",
    "collect_exclusions", "HybridPipeline", "write_outputs",
]

DOMAIN_MARKETS = {
    "amazon.com": "US", "amazon.co.uk": "UK", "amazon.de": "DE", "amazon.fr": "FR",
    "amazon.it": "IT", "amazon.es": "ES", "amazon.co.jp": "JP", "amazon.ca": "CA",
    "amazon.com.au": "AU", "amazon.in": "IN", "amazon.com.mx": "MX",
    "amazon.com.br": "BR", "amazon.ae": "AE",
}
ENGLISH_SITES = {"united states": "US", "usa": "US", "united kingdom": "UK", "gb": "UK",
                 "germany": "DE", "france": "FR", "japan": "JP", "italy": "IT",
                 "spain": "ES", "canada": "CA", "india": "IN", "mexico": "MX",
                 "brazil": "BR", "australia": "AU", "uae": "AE"}


def site_code(value):
    value = str(value or "").strip()
    if not value:
        return None
    domain = urlparse(value if "://" in value else "https://" + value).hostname or ""
    if domain.startswith("www."):
        domain = domain[4:]
    if domain in DOMAIN_MARKETS:
        return DOMAIN_MARKETS[domain]
    name = re.sub(r"(站点|站|商城)$", "", value)
    code = _SITE_TO_CODE.get(name) or ENGLISH_SITES.get(name.casefold())
    if not code and name.upper() in _SITE_TO_CODE.values():
        code = name.upper()
    if not code:
        raise ValueError(f"Unrecognized/unsupported marketplace: {value!r}; will not fall back to US")
    return code


def resolve_site(raw, cat):
    declared = [site_code(raw.get(field)) for field in ("MARKET", "SITE") if raw.get(field)]
    suffix = re.search(r"-([A-Za-z]{2})-\d{6}(?:\d{2})?(?:-RECENT30)?$", cat, flags=re.I)
    if suffix:
        declared.append(site_code(suffix[1]))
    if not declared or len(set(declared)) != 1:
        raise ValueError("MARKET/SITE/category marketplace is missing or inconsistent")
    return declared[0]


def read_config(path):
    """只读配置；返回 (原始 yaml dict, 已应用环境变量覆盖的 Config)。"""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    except yaml.YAMLError:
        raise ValueError("Invalid config.yaml; source preserved") from None
    if not isinstance(raw, dict):
        raise ValueError("config.yaml must be a mapping")
    return raw, Config(apply_env_overrides(dict(raw)))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_registered_roots(path, category, site):
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int \
            or data["schema_version"] != 1 or not isinstance(data.get("entries"), list):
        raise ValueError("Invalid registered-root registry schema")
    roots, matched = [], []
    for index, entry in enumerate(data["entries"]):
        if not isinstance(entry, dict) or not isinstance(entry.get("category"), str) \
                or not entry["category"].strip() or not isinstance(entry.get("marketplace"), str):
            raise ValueError("Registry entry requires category and marketplace strings")
        entry_site = site_code(entry["marketplace"])
        if entry_site is None:
            raise ValueError("Registry marketplace cannot be blank")
        values = registered_entries(entry.get("roots"))
        if key(entry["category"]) == key(display_cat(category)) and entry_site == site:
            roots.extend(values)
            matched.append(index)
    return roots, {"path": str(path.resolve()), "sha256": digest(path), "matched_entries": matched,
                   "category": display_cat(category), "marketplace": site, "loaded_count": len(roots)}


class CachedCalls:
    """缓存公开请求/结果，身份含站点/月份/模型/提示词摘要。"""
    def __init__(self, path, namespace):
        self.path, self.namespace = path, namespace
        self.data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.events = []
        self.current_requests = {}

    def call(self, stage, payload, invoke):
        signature = json.dumps([self.namespace, stage, payload], ensure_ascii=False, sort_keys=True)
        ident = hashlib.sha256(signature.encode()).hexdigest()
        self.current_requests[ident] = {"stage": stage, "payload": payload}
        if ident in self.data and not (stage == "keyword_miner" and isinstance(self.data[ident], dict)
                                       and self.data[ident].get("items") == []):
            self.events.append({"stage": stage, "cached": True, "key": ident})
            return self.data[ident]
        event = {"stage": stage, "cached": False, "key": ident}
        self.events.append(event)
        try:
            answer = invoke()
            if stage == "keyword_miner":
                HybridPipeline._items(answer)
            elif not isinstance(answer, dict):
                raise ValueError("LLM response must be an object")
        except Exception as exc:
            event["error_type"] = type(exc).__name__
            event["error_category"] = error_category(exc)
            event["output_truncated"] = "截断" in str(exc) or "finish_reason=length" in str(exc)
            raise
        cacheable = not (stage == "keyword_miner" and answer.get("items") == [])
        if stage == "discovery":
            cacheable = discovery_valid(answer)
        elif stage == "judge":
            requested = {r["id"] for r in json.loads(payload.split("DATA=", 1)[1])["keywords"]}
            judgments = answer.get("judgments")
            cacheable = (isinstance(judgments, list) and all(judgment_valid(j) for j in judgments)
                         and len(judgments) == len(requested)
                         and {j["id"] for j in judgments} == requested)
        elif stage == "entity_review":
            requested = {r["id"] for r in json.loads(payload.split("DATA=", 1)[1])["entities"]}
            reviews = answer.get("entity_reviews")
            cacheable = (isinstance(reviews, list) and all(entity_decision_valid(j) for j in reviews)
                         and len(reviews) == len(requested) and {j["id"] for j in reviews} == requested)
        if not cacheable:
            event["schema_incomplete"] = True
            return answer
        self.data[ident] = answer
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)
        return answer


class LiveLLM:
    def __init__(self, client, cache):
        self.client, self.cache = client, cache

    def ask(self, stage, prompt):
        return self.cache.call(stage, prompt, lambda: self.client.chat_json(
            prompt, temperature=0.0, max_tokens=8000 if stage == "discovery" else 6000))


class ExperimentLLM(LLMClient):
    @staticmethod
    def _is_hard_error(err):
        return True


def error_category(exc):
    message = str(exc).casefold()
    for category, markers in (("timeout", ("timeout", "timed out", "超时")),
                              ("authentication", ("401", "403", "鉴权", "api key")),
                              ("rate_limit", ("429", "限流", "quota")),
                              ("transport", ("connect", "ssl", "connection", "连接")),
                              ("response_schema", ("json", "schema", "解析", "截断"))):
        if any(marker in message for marker in markers):
            return category
    return type(exc).__name__


class LiveMiner:
    def __init__(self, client, cache):
        self.client, self.cache = client, cache

    def query(self, request):
        return self.cache.call("keyword_miner", request,
                               lambda: self.client._call("keyword_miner", {"request": request}))


class WeeklyEvidence:
    """One source per run: SIF first, then seller MCP if SIF fails."""
    def __init__(self, seller):
        self.seller = seller

    def query(self, words, site, month):
        from .weekly_fallback import complete_sif_rows, complete_sellersprite_rows
        try:
            import tomllib
            import httpx
            config = tomllib.loads((Path.home() / ".codex" / "config.toml").read_text(encoding="utf-8"))
            url = config["mcp_servers"]["sif"]["url"]
            headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
            found, meta = {}, None
            with httpx.Client(timeout=90) as client:
                init = client.post(url, headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                               "clientInfo": {"name": "keyword-workflow", "version": "1.0"}}})
                init.raise_for_status()
                if init.headers.get("mcp-session-id"):
                    headers["Mcp-Session-Id"] = init.headers["mcp-session-id"]
                for start in range(0, len(words), 10):
                    batch = words[start:start + 10]
                    response = client.post(url, headers=headers, json={"jsonrpc": "2.0", "id": 2,
                        "method": "tools/call", "params": {"name": "market_get_keyword_history",
                        "arguments": {"keywords": batch, "country": site, "granularity": "week",
                                      "returnFields": ["keywords"]}}})
                    response.raise_for_status()
                    result = response.json().get("result") or {}
                    if result.get("isError"):
                        raise ValueError("SIF 工具返回失败")
                    content = result.get("content") or []
                    payload = json.loads(next(x["text"] for x in content if x.get("type") == "text"))
                    current, meta = complete_sif_rows(payload, batch, month)
                    found.update(current)
            if not found:
                raise ValueError("SIF 未返回完整周数据")
            return found, meta
        except Exception:
            # Do not put exception text in logs: SIF credentials may be in the URL.
            found, meta = {}, None
            for word in words:
                response = self.seller._call("aba_research_trend", {"keyword": word,
                                            "marketplace": site, "timeGranularity": "W"})
                current, meta = complete_sellersprite_rows(response, word, month)
                found.update(current)
            return found, meta


def source_artifact(directory, name):
    path = Path(directory) / name
    if path.is_file():
        return path
    found = sorted(Path(directory).glob("*/" + name))
    if not found:
        raise FileNotFoundError(f"Source artifact not found: {name}")
    if len(found) != 1:
        raise ValueError(f"Expected exactly one source artifact: {name} (found {len(found)})")
    return found[0]


def load_product_policy(directory, cat):
    try:
        path = source_artifact(directory, f"filter-{cat}-prompt.json")
    except FileNotFoundError:
        return normalize_policy()
    obj = json.loads(path.read_text(encoding="utf-8-sig"))
    return normalize_policy({"product_definition": obj.get("产品", ""),
                             "requirement": obj.get("requirement", ""),
                             **{field: obj.get(field) or [] for field in
                                ("accessories", "consumables", "hard_exclude")}})


def collect_exclusions(directory, cat):
    policy = load_product_policy(directory, cat)
    return [term for field in ("accessories", "consumables", "hard_exclude") for term in policy[field]]
