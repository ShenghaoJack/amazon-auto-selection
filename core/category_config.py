# -*- coding: utf-8 -*-
""""类目配置"模型(2026-09): 每类目一份配置, 放在该类目数据目录内。

目标形态(用户确认的模型):
  data/<类目名>/config.yaml   <- 该类目的"唯一当前配置": 读取/保存/运行都针对它
  data/archive/<类目名>-<时间戳>/config.yaml <- 归档快照时配置跟着该类目数据一起进
根目录只保留模板/种子(用于新建类目/CLI 兜底)。

约定:
  - 模板占位类目(CAT 含 xxx/template/示例/YYYYMM 字样)不算真实类目, 不迁移不扫描;
  - ensure_category_config(seed, cat): 目标不存在时用种子复制创建(首次运行引导);
    目标已存在则**不动**(该类目配置是唯一真源);
  - list_category_configs(data_root): 扫 data/*/config.yaml(排除 archive 等)。
  - **CAT 必须等于所在目录名**(2026-09-13 起强制校验): 见 check_cat_dir_consistency() ——
    配置的身份是文件里的 CAT, 而数据目录/下拉用的是目录名, 两者不一致会产出"孤儿配置"并
    在误选时覆盖活跃类目配置(实战案例与完整说明见该函数上方注释)。
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

_PLACEHOLDER = re.compile(r"(xxx|template|示例|样例|placeholder|YYYYMM|your-category)", re.I)


def is_placeholder_cat(cat: str) -> bool:
    return (not cat) or bool(_PLACEHOLDER.search(cat))


def category_config_path(data_root, cat: str) -> Path:
    return Path(data_root) / cat / "config.yaml"


def list_category_configs(data_root) -> list[Path]:
    """扫描 data_root 下各(真实)类目目录里的 config.yaml。"""
    root = Path(data_root)
    out: list[Path] = []
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name == "archive" or d.name.startswith("."):
            continue
        cfg = d / "config.yaml"
        if cfg.is_file():
            cat = ""
            try:
                import yaml  # noqa: PLC0415
                cat = str((yaml.safe_load(cfg.read_text(encoding="utf-8-sig")) or {}).get("CAT", "") or "").strip()
            except Exception:  # noqa: BLE001
                pass
            if cat and not is_placeholder_cat(cat):
                out.append(cfg)
    return out


def ensure_category_config(seed_path, data_root, cat: str) -> Path:
    """首次引导: data/<CAT>/config.yaml 不存在时用种子文件创建(含目录)。

    已存在则原样返回(该类目配置为真源, 不覆盖用户编辑)。
    """
    target = category_config_path(data_root, cat)
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        src = Path(seed_path)
        if src.is_file():
            shutil.copy2(src, target)
        else:
            target.write_text(f"CAT: {cat}\n", encoding="utf-8")
    return target


def sync_category_config(source_path, data_root, cat: str) -> Path:
    """把一份"生效配置"同步覆盖到类目目录(webui 每轮运行/保存时调用, 实时更新)。"""
    target = category_config_path(data_root, cat)
    target.parent.mkdir(parents=True, exist_ok=True)
    src = Path(source_path)
    if src.is_file() and src != target:
        try:
            text = src.read_text(encoding="utf-8-sig")
            target.write_text(text, encoding="utf-8", newline="\n")
        except OSError:  # noqa: BLE001
            pass
    elif src == target:
        pass
    return target


# ---------------------------------------------------------------- 防呆: CAT 与所在目录必须一致
# 背景(实战 2026-09-09, "Air Purifiers for Home Pets" 遗留目录):
#   配置的**身份**是文件里的 CAT 字段, 而数据目录/webui 下拉用的是**目录名** —— 两者原本没有任何校验。
#   有人在 webui 里把 data/A/config.yaml 的 CAT 改成 B(加月份后缀)后保存, 而 webui 只写回"打开的那个
#   文件路径"(不搬文件) → 下一次运行 sync_category_config() 按 CAT=B 新建/写入 data/B/, data/A/ 从此
#   成为**孤儿目录**(里面 CAT 已是 B); 之后"给所有 data/*/config.yaml 补键"的迁移按目录遍历又给它补齐,
#   它看起来就像个正常类目 —— 一旦在 webui 里误选(下拉标签取的就是 CAT, 两个条目**同名**), 运行会把孤儿
#   内容**覆盖到活跃类目配置**上(静默回退 CORE_SEARCH_PAGES/SCREEN_REQUIREMENT 等调优值)。
#   因此: 保存与运行前一律校验, 不一致就阻断(保存可显式 allow_move 自动搬到 data/<新CAT>/)。
CAT_DIR_MISMATCH_TOKEN = "[CAT_DIR]"


def find_data_cat_dir(config_path, data_root=None) -> tuple[Path, str] | None:
    """配置若是"类目配置"(位于 <数据根>/<目录名>/config.yaml), 返回 (数据根目录, 目录名); 否则 None。

    判定用"两侧绝对化比较" + "父目录同名兜底":
      - 绝对化: CLI 可能传相对路径(--config data/A/config.yaml)而 DATA_ROOT 是 "data";
      - 同名兜底: DATA_ROOT 通常是相对名 "data", 若调用方 CWD 与配置所在项目不同(webui 被从别处启动、
        测试用临时目录), 绝对化会对不上 —— 此时只要父目录同名, 仍按"类目配置"处理。
        该校验只关心"该文件自己的目录名与它里面的 CAT 是否一致", 所以宁可多校验一次。
    返回的数据根取配置文件**实际所在的父目录**(不是传入的 data_root), 保证后续拼路径/报错信息准确。
    """
    p = Path(config_path)
    if p.name != "config.yaml":
        return None
    cat_dir = p.parent
    root = Path(data_root) if data_root else Path("data")
    parent = cat_dir.parent
    try:
        same = parent.resolve() == root.resolve()
    except OSError:
        same = os.path.abspath(str(parent)) == os.path.abspath(str(root))
    if not same and parent.name != root.name:
        return None
    return parent, cat_dir.name


def check_cat_dir_consistency(config_path, cat: str | None = None, data_root=None) -> str | None:
    """校验"CAT 字段"与"配置文件所在目录名"是否一致。

    返回 None = 通过(含非类目配置: 根模板/configs/*.yaml); 返回字符串 = 不一致的**阻断理由**(含修复指引)。
    """
    found = find_data_cat_dir(config_path, data_root)
    if found is None:
        return None
    root, dir_name = found
    if cat is None:
        cat = _yaml_cat_of(config_path)
    cat = str(cat or "").strip()
    if not cat or is_placeholder_cat(cat) or cat == dir_name:
        return None
    target = category_config_path(root, cat)
    exists = "已存在" if target.is_file() else "尚不存在"
    return (
        f"{CAT_DIR_MISMATCH_TOKEN} CAT 与所在目录不一致: 文件在 data/{dir_name}/config.yaml, 但里面 CAT={cat}。"
        f" 这会产生/使用一个与活跃类目错位的配置(运行会读写 data/{cat}/, 并可能覆盖该类目的配置)。"
        f" 修复: ①把 CAT 改回 {dir_name}; 或 ②在 webui「新建类目」建 data/{cat}/ 后把内容复制过去"
        f"(目标配置文件 {exists}: data/{cat}/config.yaml)。"
    )


def _yaml_cat_of(config_path) -> str:
    try:
        import yaml  # noqa: PLC0415
        text = Path(config_path).read_text(encoding="utf-8-sig")
        return str((yaml.safe_load(text) or {}).get("CAT", "") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def orphan_category_configs(data_root) -> list[dict]:
    """扫描"CAT 与目录名不一致"的类目配置(孤儿/错位), 供 webui 标注与健康检查告警。"""
    out = []
    root = Path(data_root)
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name == "archive" or d.name.startswith("."):
            continue
        cfg = d / "config.yaml"
        if not cfg.is_file():
            continue
        cat = _yaml_cat_of(cfg)
        if not cat or is_placeholder_cat(cat) or cat == d.name:
            continue
        out.append({
            "dir": d.name,
            "cat": cat,
            "path": str(cfg),
            "target_exists": category_config_path(root, cat).is_file(),
        })
    return out


# ---------------------------------------------------------------- 防呆: 站点/市场三处口径要一致
# 背景(2026-09-14): 类目名的站点后缀(CAT 末尾 -UK/-DE/…)、卖家精灵站点名(SITE)、亚马逊市场
# (MARKET)是**三个独立字段**, 彼此没有任何校验 —— 写 "…-UK-202609" 却留着 `SITE: 美国`+`MARKET:
# amazon.com` 时, 会静默产出"美国站关键词/趋势数据 + 英国站商品页面"的混合结果; 站点名写错
# (如 `SITE: 德国站`)还会被 `core/mcp.py::_marketplace()` **静默回落成美国站(US)**。
# 这里只做**提示(WARN)不做阻断**: 换站点是正常操作, 阻断会挡住真实需求; 但提示必须显眼。
SITE_MARKET_TOKEN = "[SITE_MARKET]"

# 地区码 -> {"market": 亚马逊域名, "site": 卖家精灵站点名(未收录的站点留空)}
REGION_PROFILE: dict[str, dict] = {
    "us": {"market": "amazon.com", "site": "美国"},
    "usa": {"market": "amazon.com", "site": "美国"},
    "uk": {"market": "amazon.co.uk", "site": "英国"},
    "gb": {"market": "amazon.co.uk", "site": "英国"},
    "de": {"market": "amazon.de", "site": "德国"},
    "fr": {"market": "amazon.fr", "site": "法国"},
    "it": {"market": "amazon.it", "site": "意大利"},
    "es": {"market": "amazon.es", "site": "西班牙"},
    "ca": {"market": "amazon.ca", "site": "加拿大"},
    "jp": {"market": "amazon.co.jp", "site": "日本"},
    "mx": {"market": "amazon.com.mx", "site": "墨西哥"},
    "br": {"market": "amazon.com.br", "site": "巴西"},
    "au": {"market": "amazon.com.au", "site": "澳大利亚"},
    "in": {"market": "amazon.in", "site": "印度"},
    "ae": {"market": "amazon.ae", "site": "阿联酋"},
    "nl": {"market": "amazon.nl", "site": ""},
    "se": {"market": "amazon.se", "site": ""},
    "pl": {"market": "amazon.pl", "site": ""},
    "sg": {"market": "amazon.sg", "site": ""},
    "tr": {"market": "amazon.com.tr", "site": ""},
    "eu": {"market": "", "site": ""},
}

# 卖家精灵支持的站点名(= core/mcp.py::_SITE_TO_CODE 的键; 有单测保证两边不漂移)
KNOWN_SITES: tuple[str, ...] = (
    "美国", "英国", "日本", "德国", "法国", "意大利", "西班牙",
    "加拿大", "印度", "墨西哥", "巴西", "澳大利亚", "阿联酋",
)

SITE_TO_MARKET: dict[str, str] = {
    prof["site"]: prof["market"]
    for prof in REGION_PROFILE.values()
    if prof["site"] and prof["market"]
}

# ---------------------------------------------------------------- 站点分流: 非 US 口径
# 背景(2026-09-15 用户口径): FR-07 的筛选标准(月搜索量>=3万/站内同比/Google同比/CPC)
# 是按**美国站**的数据特征定的; 其他站点普遍拿不到同等质量的站内同比/Google 指数/CPC,
# 硬套这套阈值会把候选词全部判成"数据不足/不达标" → 步骤9 因"达标核心大词 0 个"中止。
# 因此**除 US 外的站点**改用:
#   ① 不做 SCREEN_REQUIREMENT 筛选, 直接取**站内月搜索量前 N 个**候选词作"达标核心大词";
#   ② 长尾词只以**搜索量最高的那一个**核心关键词为基座组装。
# US 站(以及无法判定站点的旧配置)完全保持原行为。
NON_US_QUALIFIED_TOP = 5
"""非 US 站点直接取"站内月搜索量前 N 个"候选词作达标核心(用户 2026-09-15 定)。"""


def is_us_site(site: str = "", market: str = "", cat: str = "") -> bool:
    """是否美国站(默认行为)。判定顺序: MARKET 域名 → SITE 站点名 → CAT 末尾地区码。

    三者都判不出来时返回 True(= 按原 US 口径走), 保证老配置行为不变。
    判定依据来自 `REGION_PROFILE` 与 {站点名: 域名} 反查表, 与
    `check_site_market_consistency()` 用同一份表, 不会出现两套口径。
    """
    site_name_market = {prof["site"]: prof["market"]
                        for prof in REGION_PROFILE.values() if prof.get("site")}
    keyword_map = {prof["market"]: (prof["market"] == "amazon.com")
                   for prof in REGION_PROFILE.values() if prof.get("market")}

    m = str(market or "").strip().lower().rstrip("/")
    if m:
        host = m.split("//")[-1].split("/")[0]        # 容忍写完整 URL
        if host.startswith("www."):
            host = host[4:]
        if host in keyword_map:                       # 精确命中(amazon.com / amazon.de …)
            return keyword_map[host]
        if "amazon." in host and not host.startswith("amazon.com"):
            return False                              # 其它国家域名(amazon.de / amazon.co.uk / amazon.com.mx …)
        if host.startswith("amazon.com"):
            return True

    s = str(site or "").strip()
    if s:
        dom = site_name_market.get(s)               # 卖家精灵站点名(美国/德国/…)
        if dom:
            return dom == "amazon.com"
        if s.lower() in REGION_PROFILE:             # 也容忍直接写地区码(us/de/…)
            dom = str(REGION_PROFILE[s.lower()].get("market") or "")
            if dom:
                return dom == "amazon.com"

    tok = cat_region_token(cat)
    if tok:
        dom = str(REGION_PROFILE.get(tok, {}).get("market") or "")
        if dom:
            return dom == "amazon.com"
    return True


def pick_top_volume_candidates(rows, limit: int = NON_US_QUALIFIED_TOP) -> list[str]:
    """按**站内月搜索量降序**取前 limit 个候选词(非 US 口径的"达标核心")。

    rows: FR-05 的 `<CAT>-amazon-keyword-data.json` 记录列表(每项含 keyword 与
          monthly_search_volume); **缺搜索量(无法转成数)的词直接排除**(用户口径:
          没有搜索量数据的词不该混进"搜索量前5"), 因此返回个数可能少于 limit。
    排序: 搜索量降序, 同量按词字典序(与步骤9 既有 `_top_volume_core` 的排序一致)。
    """
    ranked: list[tuple[float, str]] = []
    seen: set[str] = set()
    for rec in rows if isinstance(rows, list) else []:
        if not isinstance(rec, dict):
            continue
        kw = str(rec.get("keyword") or "").strip()
        if not kw or kw in seen:
            continue
        try:
            vol = float(rec.get("monthly_search_volume"))
        except (TypeError, ValueError):
            continue          # 缺/非数值 -> 排除(不参与"搜索量前5")
        seen.add(kw)
        ranked.append((vol, kw))
    ranked.sort(key=lambda x: (-x[0], x[1]))
    return [w for _, w in ranked[: max(0, int(limit))]]


def cat_region_token(cat: str) -> str:
    """取类目名末尾的站点/地区码(先剥掉 `-YYYYMM`/`-v2` 后缀), 无则返回 ""。"""
    c = re.sub(r"-RECENT30$", "", str(cat or "").strip(), flags=re.I)
    c = re.sub(r"-(?:20\d{4}|v\d{1,4})$", "", c, flags=re.I)
    m = re.search(r"-([A-Za-z]{2,3})$", c)
    tok = m.group(1).lower() if m else ""
    return tok if tok in REGION_PROFILE else ""


def check_site_market_consistency(cat: str, site: str, market: str) -> list[str]:
    """三条口径(CAT 站点后缀 / SITE / MARKET)互相矛盾的提示清单(空 = 一致或无从判断)。

    只提示不阻断: 换站点本身是正常操作; 但混用会静默产出国别错配的数据。
    """
    out: list[str] = []
    cat = str(cat or "").strip()
    site = str(site or "").strip()
    market = str(market or "").strip().lower()
    tok = cat_region_token(cat)
    prof = REGION_PROFILE.get(tok) if tok else None
    if prof:
        want_site, want_market = prof["site"], prof["market"]
        if want_site and site and site != want_site and site in KNOWN_SITES:
            out.append(
                f"{SITE_MARKET_TOKEN} CAT 后缀 -{tok.upper()} 表示{want_site}站, 但 SITE=「{site}」"
                f" → 关键词/趋势/竞品数据会按「{site}」取, 与商品页面(类目后缀)不是同一个站点。"
                f" 应为 SITE: {want_site}")
        if want_market and market and market != want_market:
            out.append(
                f"{SITE_MARKET_TOKEN} CAT 后缀 -{tok.upper()} 表示{want_site}站, 但 MARKET=「{market}」"
                f" → 抓取会用 {market}, 与该类目的站点不一致。 应为 MARKET: {want_market}")
    if site and market and site in KNOWN_SITES:
        want = SITE_TO_MARKET.get(site)
        if want and market != want:
            out.append(
                f"{SITE_MARKET_TOKEN} SITE=「{site}」与 MARKET=「{market}」不一致"
                f"(「{site}」对应 {want}) → 会出现"
                f"'某国关键词数据 + 另一国商品页面'的混合结果。")
    if site and site not in KNOWN_SITES and not is_placeholder_cat(site):
        out.append(
            f"{SITE_MARKET_TOKEN} SITE=「{site}」不是卖家精灵站点名(应为 "
            f"{'/'.join(KNOWN_SITES)} 之一) → 卖家精灵会**静默按美国站(US)取值**, "
            f"你会在以为跑外国站的情况下拿到美国数据。")
    return list(dict.fromkeys(out))
