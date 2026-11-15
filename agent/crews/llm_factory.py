"""LLM 工厂: 复用现有 `config.yaml` 的 LLM 配置, 构造 crewAI LLM 实例。

Phase 3 目标: 让报告/问答 crew 使用与主流水线**同一套** LLM 密钥与模型
(LLM_BASE_URL / LLM_API_KEY / LLM_MODEL), 减少重复配置。

**依赖策略**(与 Phase 2 一致, 零依赖可测):
- `normalize_model` / `llm_kwargs` / `validate_config` / `describe_llm` 是**纯函数**,
  不 import crewAI, 可直接单测;
- `build_llm` 才懒导入 `crewai.LLM` 真正构造实例; 未装 crewAI 或缺密钥时抛清晰错误。

⚠️ 本层只读配置, 不触碰 `steps/*`、`core/orchestrator.py`。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent._vendor import Config

if TYPE_CHECKING:  # 仅类型标注
    from crewai import LLM


# ---------------------------------------------------------------- 纯函数(可测)
def normalize_model(model: str, default_provider: str = "deepseek") -> str:
    """把配置的 model 归一化为 litellm 的 'provider/model' 写法。

    若 model 已含 '/' (如 openai/gpt-4o) 则原样返回; 否则加 provider 前缀
    (默认 deepseek, 供 litellm 路由到对应厂商)。
    """
    model = (model or "").strip()
    if not model:
        return ""
    if "/" in model:
        return model
    provider = (default_provider or "deepseek").strip() or "deepseek"
    return f"{provider}/{model}"


def llm_kwargs(
    config: Config,
    temperature: float = 0.2,
    max_tokens: int = 4096,
    default_provider: str = "deepseek",
) -> dict:
    """根据配置组装 crewai LLM 的构造参数。"""
    model = str(config.get("LLM_MODEL", "") or "").strip()
    return {
        "model": normalize_model(model, default_provider),
        "base_url": str(config.get("LLM_BASE_URL", "https://api.deepseek.com") or "").strip(),
        "api_key": str(config.get("LLM_API_KEY", "") or "").strip(),
        "temperature": float(temperature),
        "max_tokens": int(max_tokens),
    }


def validate_config(config: Config) -> tuple[bool, list[str]]:
    """校验 LLM 配置是否可用于构造。返回 (ok, 缺失键列表)。"""
    missing: list[str] = []
    if not str(config.get("LLM_API_KEY", "") or "").strip():
        missing.append("LLM_API_KEY")
    if not str(config.get("LLM_MODEL", "") or "").strip():
        missing.append("LLM_MODEL")
    if not str(config.get("LLM_BASE_URL", "") or "").strip():
        missing.append("LLM_BASE_URL")
    return (len(missing) == 0, missing)


def describe_llm(config: Config, default_provider: str = "deepseek") -> str:
    """返回一行可打印的 LLM 描述(密钥掩码)。"""
    guard = "已配置" if str(config.get("LLM_API_KEY", "") or "").strip() else "未配置"
    return (
        f"模式: {normalize_model(config.get('LLM_MODEL', ''), default_provider) or '-'} | "
        f"接口: {config.get('LLM_BASE_URL', '') or '-'} | 密钥: {guard}"
    )


# ---------------------------------------------------------------- 构造(需 crewAI)
def build_llm(
    config: Config,
    temperature: float = 0.2,
    max_tokens: int = 4096,
    default_provider: str = "deepseek",
):
    """构造 crewAI `LLM` 实例(供 Crew/Agent 使用)。

    未装 crewAI → RuntimeError(带安装提示);
    配置缺 key/model → RuntimeError(列出缺失键)。
    """
    ok, missing = validate_config(config)
    if not ok:
        raise RuntimeError(
            f"LLM 配置缺失: {missing}。请先在配置文件中填写 LLM_API_KEY / LLM_MODEL"
        )
    try:
        from crewai import LLM as CrewLLM
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "构造 LLM 需要 crewai, 请先安装: pip install -r agent/requirements.txt"
        ) from exc
    try:
        return CrewLLM(**llm_kwargs(config, temperature, max_tokens, default_provider))
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(f"创建 crewai LLM 失败: {exc}") from exc
