"""页面 GEO 体检：把 citation-lab 的实证结论变成可计算的分数。

评分口径全部来自 references/method.md 里记录的实测数字，不是拍脑袋：
  长度   Top 四分位页面 1,943 词 / Bottom 四分位 170 词（11.4x）
  结构   Top 四分位 10.59 个标题、47.49 个段落、列表密度 0.428
  抽取块 含数字 +61.6%、含定义 +57.3%、含对比 +55.3%、含 how-to +41.2%
  对题性 llm_relevance_score 是影响力最强预测因子（r = 0.432）

产物：work/<slug>/audit.json
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

import geolib as G

# ------------------------------------------------------------ 抽取块识别

# 三语站点三语判：只认中英表述会把日文页整体误判成「缺块」
RE_DEFINITION = re.compile(
    r"(是一[款种个家类]|是指|指的是|定义为|全称[为是]|又称|简称为?|属于一[种类]"
    r"|とは|を指す|と呼ばれ|の略"
    r"|\bis an? \w+|\brefers to\b|\bis defined as\b|\bstands for\b)", re.I
)
RE_NUMBER = re.compile(
    r"\d[\d,\.]*\s*(%|％|万|亿|千|倍|元|美元|人|家|个|天|小时|分钟|秒|次|条|款|年|月|"
    r"件|社|名|回|億|円|時間|"
    r"percent|x\b|hours?|days?|users?|customers?|credits?|generations?|dollars?|usd|gb|mb|px|p\b)"
)
RE_PRICE = re.compile(r"(?:[$€£]\s*\d|\b\d+(?:\.\d+)?\s*(?:USD|EUR|GBP|credits?)\b)", re.I)
RE_COMPARE = re.compile(r"(对比|相比|区别|差异|优于|不如|竞品|替代|选型|哪个好|比較|違い|\bvs\.?\b|\bversus\b|\balternatives?\b)", re.I)
RE_HOWTO = re.compile(r"(第[一二三四五六七八九十\d]+步|步骤\s*[一二三四五六七八九十\d]|操作流程|手順|ステップ\s*\d|使い方|\bstep\s*\d|\bhow to\b)", re.I)
# 「如何/怎么」只是弱信号，必须与列表结构共现才算操作步骤块（否则问句标题就送分）
RE_HOWTO_SOFT = re.compile(r"(如何|怎么)")
RE_INSTRUCTION = re.compile(
    r"\b(open|choose|select|add|upload|enter|click|start|submit|follow|visit|download|review)\b",
    re.I,
)
# 登录/注册/购物车/联系页等功能页天然低内容，不按 SPA 空壳 P0 误报
FUNC_PAGE = re.compile(r"/(login|signin|signup|register|cart|checkout|account|auth|contact)(/|$)", re.I)
RE_FAQ = re.compile(
    r"(常见问题|常见疑问|问答|よくある質問|\bFAQ\b|\bfrequently\s+asked\s+questions?\b"
    r"|\bcommon\s+questions?\b|\bquestions?\s*(?:&|and)\s*answers?\b"
    r"|^\s*[问Q][:：]|答[:：])",
    re.I | re.M,
)
RE_DATE = re.compile(r"(20\d{2}[-/年]\s?\d{1,2}[-/月]\s?\d{1,2}|更新[于时间]*[:：]?\s*20\d{2}|最后更新|发布于|\bupdated\b|\bpublished\b)", re.I)
RE_AUTHOR = re.compile(r"(作者|撰文|编辑[:：]|著者|執筆|\bauthor\b|\bby\s+[A-Z][a-z]+)", re.I)

AUTHORITY_SCHEMA = {
    "Organization", "Corporation", "Product", "SoftwareApplication", "Service",
    "FAQPage", "Article", "TechArticle", "NewsArticle", "BlogPosting",
    "HowTo", "BreadcrumbList", "WebSite", "Review", "AggregateRating", "Offer",
}


# 页面类型决定“完整”的含义。相关性研究里的长页面均值不能机械外推成每个
# Pricing / Docs / About 页面都必须 1000+ 词、6 个 H2、五种抽取块齐全。
PAGE_EXPECTATIONS = {
    "functional": {"min_words": 0, "target_h2": 0, "required_blocks": set()},
    "pricing": {"min_words": 60, "target_h2": 0, "required_blocks": {"数字事实", "对比"}},
    "about": {"min_words": 120, "target_h2": 3, "required_blocks": {"定义"}},
    "docs_index": {"min_words": 40, "target_h2": 2, "required_blocks": set()},
    "docs_article": {"min_words": 80, "target_h2": 1, "required_blocks": set()},
    "faq": {"min_words": 120, "target_h2": 1, "required_blocks": {"FAQ"}},
    "collection": {"min_words": 80, "target_h2": 1, "required_blocks": set()},
    "article": {"min_words": 500, "target_h2": 3, "required_blocks": set()},
    "landing": {"min_words": 400, "target_h2": 3, "required_blocks": {"定义", "数字事实", "对比"}},
}

BOT_ROLES = {
    "Googlebot": "search", "bingbot": "search", "OAI-SearchBot": "search",
    "Claude-SearchBot": "search", "PerplexityBot": "search", "Baiduspider": "search",
    "Sogou web spider": "search", "YisouSpider": "search",
    "ChatGPT-User": "user", "Claude-User": "user", "Perplexity-User": "user",
    "GPTBot": "training", "ClaudeBot": "training", "Google-Extended": "training",
    "Bytespider": "training",
}


def blocked_bots_by_role(site: dict) -> dict[str, list[str]]:
    stored = site.get("ai_bots_blocked_by_role") or {}
    if stored:
        return {k: list(stored.get(k) or []) for k in ("search", "user", "training")}
    blocked = site.get("ai_bots_blocked") or []
    return {
        role: [bot for bot in blocked if BOT_ROLES.get(bot) == role]
        for role in ("search", "user", "training")
    }


def page_kind(page: dict) -> str:
    """按 URL / schema 做保守的页面类型识别，避免跨类型套同一阈值。"""
    path = (urlparse(page.get("final_url") or page.get("url") or "").path or "/").lower()
    types = set(page.get("jsonld_types", []))
    if FUNC_PAGE.search(path):
        return "functional"
    if re.search(r"/(pricing|price|plans?)(/|$)", path):
        return "pricing"
    if re.search(r"/(about|company)(/|$)", path):
        return "about"
    if re.search(r"/(faq|frequently-asked-questions)(/|$)", path) or "FAQPage" in types:
        return "faq"
    if re.search(r"/(docs?|documentation|help)(/)?$", path):
        return "docs_index"
    if re.search(r"/(docs?|documentation|help)/", path) or types & {"TechArticle", "HowTo"}:
        return "docs_article"
    if re.search(r"/(blog|news|guides?)(/)?$", path):
        return "collection"
    if re.search(r"/(blog|news|guides?)/", path) or types & {
        "Article", "NewsArticle", "BlogPosting"
    }:
        return "article"
    return "landing"


def required_blocks_for(page: dict, kind: str) -> set[str]:
    """根据页面的具体任务选择信息块，避免同一类型内部继续一刀切。"""
    path = (urlparse(page.get("final_url") or page.get("url") or "").path or "/").lower()
    required = set(PAGE_EXPECTATIONS[kind]["required_blocks"])
    if kind == "docs_article" and re.search(r"/(getting-started|quickstart|setup|how-to-)", path):
        required.add("操作步骤")
    if kind == "article":
        if re.search(r"/(what-is-|[^/]*-explained)", path):
            required.add("定义")
        if re.search(r"/(?:[^/]*-vs-|[^/]*comparison|[^/]*alternative|[^/]*review)", path):
            required.add("对比")
        if re.search(r"/(?:how-to-|[^/]*workflow)", path):
            required.add("操作步骤")
    return required


def _meaningful_static_content(page: dict) -> bool:
    """短页面也可能是完整静态 HTML；长度本身不是 CSR/SPA 证据。"""
    wc = page.get("word_count", 0) or 0
    semantic_nodes = (
        len(page.get("h1", [])) + len(page.get("h2", []))
        + (page.get("para_count", 0) or 0) + (page.get("li_count", 0) or 0)
        + (page.get("table_count", 0) or 0)
    )
    return wc >= 30 and semantic_nodes >= 3


def _canon_url_key(u: str) -> str:
    p = urlparse(u.strip())
    host = p.netloc.lower().removeprefix("www.")
    path = (p.path or "/").rstrip("/") or "/"
    return f"{host}{path}"


def _canon_mismatch(canonical: str, actual: str) -> bool:
    """canonical 指向「别的页面」才算问题；协议 / www / 末尾斜杠差异都不算。"""
    if not canonical.startswith("http"):
        return False  # 相对 canonical，不猜
    return _canon_url_key(canonical) != _canon_url_key(actual)


def _page_canonical_key(page: dict) -> str:
    """返回页面的规范身份；只有声明 canonical 时才主动忽略 query。"""
    actual = page.get("final_url") or page.get("url") or ""
    canonical = (page.get("canonical") or "").strip()
    if canonical:
        return _canon_url_key(urljoin(actual, canonical))
    p = urlparse(actual)
    host = p.netloc.lower().removeprefix("www.")
    path = (p.path or "/").rstrip("/") or "/"
    return f"{host}{path}?{p.query}" if p.query else f"{host}{path}"


def dedupe_pages_by_canonical(pages: list[dict]) -> tuple[list[dict], list[dict]]:
    """按 canonical 折叠重复抓取行，并优先保留规范 URL 本身。"""
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for page in pages:
        key = _page_canonical_key(page)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(page)

    kept: list[dict] = []
    duplicates: list[dict] = []
    for key in order:
        group = groups[key]

        def preference(page: dict):
            actual = page.get("final_url") or page.get("url") or ""
            parsed = urlparse(actual)
            return (
                (page.get("status") or 0) == 200,
                _canon_url_key(actual) == key,
                not bool(parsed.query),
                page.get("word_count", 0) or 0,
            )

        winner = max(group, key=preference)
        kept.append(winner)
        if len(group) > 1:
            duplicates.append({
                "canonical_key": key,
                "kept": winner.get("url"),
                "duplicates": [p.get("url") for p in group if p is not winner],
            })
    return kept, duplicates


def band(value: float, stops: list[tuple[float, float]]) -> float:
    """stops 为 [(阈值, 得分比例)]，从高到低取第一个满足的。"""
    for threshold, ratio in stops:
        if value >= threshold:
            return ratio
    return 0.0


def jsonld_has_key(obj, keys: set[str]) -> bool:
    """递归查 JSON-LD 原始 dict 的键（dateModified 是属性不是 @type，查 types 恒查不到）。"""
    if isinstance(obj, dict):
        return any(k in keys or jsonld_has_key(v, keys) for k, v in obj.items())
    if isinstance(obj, list):
        return any(jsonld_has_key(x, keys) for x in obj)
    return False


def split_sections(text: str, h2s: list[str]) -> list[str]:
    """按 H2 标题行把扁平正文切成段落组。检索的最小单元是段落而不是页面——
    一段能独立回答问题的文字可以赢过竞品的整页（GEO Readiness Manual 第 0 章）。"""
    heads = {h.strip() for h in h2s if h and h.strip()}
    if not heads:
        return []
    lines = text.splitlines()
    idx = [i for i, ln in enumerate(lines) if ln.strip() in heads]
    if not idx:
        return []
    bounds = idx + [len(lines)]
    return ["\n".join(lines[a + 1:b]).strip() for a, b in zip(bounds, bounds[1:])]


def quotable(seg: str) -> bool:
    """一段是否「可被独立引用」：有足够词量承载语义，且含至少一种硬信息
    （数字/定义/步骤）。纯观点段、导航段、口号段都不算。"""
    import geolib as _G
    if _G.word_count(seg) < 60:
        return False
    return bool(RE_NUMBER.search(seg) or RE_DEFINITION.search(seg) or RE_HOWTO.search(seg))


def score_page(page: dict, keywords: list[str]) -> dict:
    text = page.get("text", "") or ""
    wc = page.get("word_count", 0)
    h1, h2 = page.get("h1", []), page.get("h2", [])
    paras = page.get("para_count", 0)
    lis = page.get("li_count", 0)
    types = set(page.get("jsonld_types", []))
    kind = page_kind(page)
    expectation = PAGE_EXPECTATIONS[kind]
    min_words = expectation["min_words"]
    target_h2 = expectation["target_h2"]

    issues: list[str] = []
    issue_codes: list[str] = []

    def issue(code: str, msg: str):
        issue_codes.append(code)
        issues.append(msg)

    d: dict[str, float] = {}

    # 1. 可抓取性 15
    s = 0.0
    status = page.get("status") or 0
    if status == 200:
        s += 7
    elif 200 < status < 400:
        s += 3
        issue("NON_200_STATUS", "P1 页面返回非 200（如 202/3xx），部分抓取器会直接放弃")
    else:
        issue("PAGE_UNREACHABLE", "P0 页面不可访问，AI 抓取器同样拿不到")
    meta_noindex = "noindex" in (page.get("meta_robots") or "").lower()
    header_noindex = "noindex" in (page.get("x_robots_tag") or "").lower()
    if not (meta_noindex or header_noindex):
        s += 3
    elif header_noindex:
        # HTTP 头级 noindex 在页面源码里看不到，比 meta 更容易带病上线
        issue("XROBOTS_NOINDEX", "P0 X-Robots-Tag 响应头含 noindex（页面源码里看不到，通常是 CDN/中间件配置），等于主动退出候选池")
    else:
        issue("NOINDEX", "P0 meta robots 含 noindex，等于主动退出候选池")
    canon = page.get("canonical") or ""
    if canon:
        s += 2
        if _canon_mismatch(canon, page.get("final_url") or page.get("url") or ""):
            issue("CANONICAL_MISMATCH", "P1 canonical 指向别的 URL，抓取器会把权重记到别处；确认这是刻意的合并而不是配置错误")
    else:
        issue("NO_CANONICAL", "P2 缺 canonical，重复内容会稀释信号")
    semantic_nodes = len(h1) + len(h2) + paras + lis + (page.get("table_count", 0) or 0)
    if kind == "functional":
        s += 3
        if wc < 30:
            issue("LOW_CONTENT_PAGE", "P2 低内容功能页，内容少属正常；只需确认关键状态与说明可访问")
    elif wc < 10 and semantic_nodes <= 2:
        issue("SPA_SHELL", "P0 HTML 几乎没有正文或语义节点，疑似 CSR/SPA 空壳；需用 rendered page 与原始响应复核")
    elif wc < 30 and (len(h2) >= 2 or paras >= 3):
        s += 1
        issue(
            "RENDERING_RISK",
            "P1 HTML 中存在标题/段落结构，但主正文抽取极少；疑似 streaming SSR、多主内容容器或抽取兼容问题，需复核原始 HTML 与 rendered page",
        )
    elif _meaningful_static_content(page):
        s += 3
        if min_words and wc < min_words:
            issue(
                "THIN_CONTENT",
                f"P1 正文少于该页面类型的完整性参考值（{kind}: {min_words} 词）；优先补任务所需事实，不按统一长文阈值扩写",
            )
    else:
        s += 1
        issue(
            "THIN_CONTENT",
            "P1 HTML 有少量可读正文，但信息不足；这是 thin content，不足以据此断言 CSR/SPA 空壳",
        )
    d["可抓取性"] = s

    # 2. 内容长度 15：按页面任务判断完整性，不把描述性长页面均值当统一门槛。
    if min_words == 0:
        r = 1.0
    else:
        r = band(wc, [(min_words * 1.5, 1.0), (min_words, 0.85),
                      (min_words * 0.6, 0.6), (min_words * 0.3, 0.35)])
    d["内容长度"] = 15 * r

    # 3. 结构规范 20
    s = 0.0
    if len(h1) == 1:
        s += 4
    else:
        issue("BAD_H1", "P1 H1 不是唯一一个（0 个或多个），主题信号混乱")
    if target_h2 == 0:
        s += 6
    else:
        h2_ratio = len(h2) / target_h2
        s += 6 * band(h2_ratio, [(1.0, 1.0), (0.66, 0.75), (0.33, 0.4)])
        if len(h2) < target_h2:
            issue("FEW_H2", f"P1 {kind} 页的小节少于任务参考值 {target_h2}；只在有独立子问题时拆 H2")
    s += 5 * band(paras, [(40, 1.0), (25, 0.8), (15, 0.55), (8, 0.3)])
    density = lis / max(paras + lis, 1)
    if kind in {"article", "landing", "docs_article"}:
        s += 5 * band(density, [(0.35, 1.0), (0.2, 0.75), (0.1, 0.45), (0.03, 0.2)])
        if density < 0.1:
            issue("LOW_LIST_DENSITY", "P1 该内容页列表密度较低；仅在要点/步骤天然适合列表时调整，不为指标强拆")
    else:
        s += 5
    d["结构规范"] = s

    # 4. 可抽取块 25（GEO 的核心杠杆）
    has = {
        "定义": bool(RE_DEFINITION.search(text)),
        "数字事实": len(RE_NUMBER.findall(text)) >= 3,
        "对比": (bool(RE_COMPARE.search(text)) or page.get("table_count", 0) >= 1
               or (kind == "pricing" and len(RE_PRICE.findall(text)) >= 2 and lis >= 4)),
        "操作步骤": (bool(RE_HOWTO.search(text))
                 or (bool(RE_HOWTO_SOFT.search(text)) and lis >= 3)
                 or (kind == "docs_article" and len(RE_INSTRUCTION.findall(text)) >= 2)),
        "FAQ": bool(RE_FAQ.search(text)) or "FAQPage" in types,
    }
    block_codes = {"定义": "NO_DEFINITION", "数字事实": "NO_NUMBERS", "对比": "NO_COMPARISON",
                   "操作步骤": "NO_HOWTO", "FAQ": "NO_FAQ"}
    weights = {"定义": 6, "数字事实": 6, "对比": 5, "操作步骤": 5, "FAQ": 3}
    required_blocks = required_blocks_for(page, kind)
    required_weight = sum(weights[k] for k in required_blocks)
    d["可抽取块"] = (
        25 * sum(weights[k] for k in required_blocks if has[k]) / required_weight
        if required_weight else 25
    )
    for k in required_blocks:
        if not has[k]:
            issue(
                block_codes[k],
                f"P1 {kind} 页缺与页面任务相关的「{k}」块；研究只支持相关性观察，不代表格式本身保证引用增益",
            )

    # 4b. 段落级可引：检索按段落选材，页面长 ≠ 有可引之材
    segs = split_sections(text, h2)
    q_n = sum(1 for sg in segs if quotable(sg))
    if len(segs) >= 3 and wc >= 300 and q_n == 0:
        issue("NO_QUOTABLE_PASSAGE",
              "P1 整页没有一个可独立引用的段落——每段要么太短、要么没有数字/定义/步骤等硬信息；"
              "检索是按段落选材的，先把 2–3 个核心段落改成自包含的证据段")

    # 5. 权威信号 15
    s = 0.0
    if RE_DATE.search(text) or jsonld_has_key(page.get("jsonld_raw"), {"dateModified", "datePublished"}):
        s += 4
    else:
        issue("NO_DATE", "P1 正文没有可见的发布/更新日期，时效性无法判断")
    if RE_AUTHOR.search(text):
        s += 2
    ext = page.get("external_links", 0)
    if kind in {"article", "landing"}:
        s += 4 * band(ext, [(6, 1.0), (3, 0.7), (1, 0.4)])
        if ext < 3:
            issue("FEW_EXTERNAL_LINKS", "P2 证据型内容引用的外部来源较少；只为可核实 claims 补一手来源，不机械凑链接")
    else:
        s += 4
    hit_schema = types & AUTHORITY_SCHEMA
    s += 5 * band(len(hit_schema), [(3, 1.0), (2, 0.75), (1, 0.45)])
    if not hit_schema:
        issue("NO_JSONLD", "P2 未提供适合该页的 JSON-LD 显式线索；结构化数据可帮助消歧，但不是抓取或 AI 排名门票")
    # schema 与可见内容一致性：声明了 FAQPage 但正文没有可见问答 = 自我声明，
    # 检索系统会拿可见文本对账，对不上时结构化数据反而变成负信号
    if "FAQPage" in types and not RE_FAQ.search(text):
        issue("SCHEMA_CONTENT_MISMATCH", "P1 JSON-LD 声明了 FAQPage 但页面正文没有可见的问答内容，schema 必须与可见内容一致")
    # 作者实体关联：文章型页面的 schema 不挂 author，引擎无法把作者与出版方连起来，
    # 内容会被视为无主之作（GEO Readiness Manual：cannot connect the author to the publication）
    if types & {"Article", "TechArticle", "NewsArticle", "BlogPosting"} \
            and not jsonld_has_key(page.get("jsonld_raw"), {"author"}):
        issue("NO_AUTHOR_ENTITY", "P2 文章型 JSON-LD 没有 author 字段，作者与出版方连不起来，权威信号打折")
    d["权威信号"] = s

    # 6. 对题性 10（title / h1 / h2 是否覆盖目标问题里的词）
    surface = " ".join([page.get("title", "")] + h1 + h2).lower()
    hits = [k for k in keywords if k and k.lower() in surface]
    cover = len(hits) / max(len(keywords), 1) if keywords else 0
    if kind in {"article", "landing"}:
        d["对题性"] = 10 * band(cover, [(0.4, 1.0), (0.25, 0.8), (0.12, 0.55), (0.04, 0.3)])
        if cover < 0.12:
            issue("LOW_RELEVANCE", "P2 标题与当前合成问题库的词面覆盖较低；先用真实 query/demand 验证，再决定是否改标题")
    else:
        d["对题性"] = 10

    total = round(sum(d.values()), 1)
    return {
        "url": page.get("url"),
        "title": page.get("title", "")[:120],
        "word_count": wc,
        "page_kind": kind,
        "score": total,
        "grade": "A" if total >= 80 else "B" if total >= 65 else "C" if total >= 45 else "D",
        "dimensions": {k: round(v, 1) for k, v in d.items()},
        "sections_total": len(segs), "sections_quotable": q_n,
        "blocks": has,
        "required_blocks": sorted(required_blocks),
        "jsonld_types": sorted(types),
        "issues": issues,
        "issue_codes": issue_codes,
    }


def keywords_from_config(cfg: dict) -> list[str]:
    b = cfg.get("brand", {})
    # 品牌词不算对题性证据：标题里出现自家品牌名天经地义，用它撑覆盖率是自我安慰
    brand_terms = set()
    for k in [b.get("name")] + list(b.get("aliases", []) or []) + list(b.get("products", []) or []):
        if k:
            brand_terms.add(str(k).lower())
    kws = set()
    for q in cfg.get("questions", []):
        for token in re.findall(r"[一-鿿A-Za-z]{2,}", q.get("text", "")):
            if len(token) >= 2:
                kws.add(token)
    # 只保留最有区分度的一批，避免「的」「怎么」这种噪声撑高覆盖率
    stop = {"什么", "怎么", "哪个", "如何", "可以", "适合", "推荐", "有没有", "the", "and", "for", "how", "what", "which"}
    return sorted({k for k in kws if k.lower() not in stop and k.lower() not in brand_terms
                   and len(k) >= 2})[:40]


def run(slug: str) -> dict:
    cfg = G.load_config(slug)
    pdir = G.project_dir(slug)
    raw_pages = G.read_jsonl(pdir / "evidence" / "pages.jsonl")
    pages, canonical_duplicates = dedupe_pages_by_canonical(raw_pages)
    if not pages and not G.has_site(cfg):
        G.info("无自有网站项目：跳过站点体检（技术层不适用；内容与阵地诊断照常）")
        out = {"slug": slug, "audited_at": G.now_iso(), "market": cfg.get("market", "cn"),
               "no_site": True, "site": {}, "site_issues": [], "layers": [],
               "page_count": 0, "avg_score": None, "grade_distribution": {},
               "block_gap": [], "pages": [], "keywords_used": [],
               "language_coverage": {}}
        G.write_json(pdir / "audit.json", out)
        return out
    if not pages:
        G.die("没有抓取结果，先运行：python3 scripts/geo.py crawl --slug " + slug)
    site = G.read_json(pdir / "evidence" / "site.json", {})
    kws = keywords_from_config(cfg)

    results = [score_page(p, kws) for p in pages]
    # 均分分母只计能打开的页（含 0 分页）：和 grade_distribution 同口径，
    # 抓不到的页本来就不该参与内容质量均分
    ok = [r for r, p in zip(results, pages) if (p.get("status") or 0) == 200]
    avg = round(sum(r["score"] for r in ok) / max(len(ok), 1), 1)

    # 语言覆盖：做双市场时，「有没有英文原生内容」是海外 GEO 的门票
    market = cfg.get("market", "cn")
    lang_dist: dict[str, int] = {}
    for p in pages:
        if page_kind(p) != "functional" and _meaningful_static_content(p):
            # 有正文就从正文重算语言，不盲信存储字段——evidence 可能是旧版口径抓的
            if p.get("text"):
                lang = G.page_language(p["text"], p.get("lang", ""))
            else:
                lang = p.get("language", "unknown")
            lang_dist[lang] = lang_dist.get(lang, 0) + 1
    # mixed 单列，不双计进 zh/en——双计会让「中英对等」判断失真
    en_pages = lang_dist.get("en", 0)
    zh_pages = lang_dist.get("zh", 0)
    ja_pages = lang_dist.get("ja", 0)
    content_pages = sum(lang_dist.values())
    hreflang_pages = sum(
        1 for p in pages
        if page_kind(p) != "functional" and _meaningful_static_content(p)
        and p.get("hreflang_count", 0) > 0
    )
    # 多语言站才要求 hreflang：单语言站声明它没有意义
    multilingual = sum(1 for v in (zh_pages, en_pages, ja_pages) if v > 0) >= 2

    # 站点级问题
    site_issues = []
    lang_fail = lang_warn = False
    if market in ("global", "both") and en_pages == 0:
        lang_fail = True
        site_issues.append(
            "P0 抓到的页面里没有一页是英文原生内容，海外 AI 引用的可识别语言中英文占 82.90%–95.07%，"
            "翻译腔或中文页几乎进不了候选池")
    if market in ("cn", "both") and zh_pages == 0:
        lang_fail = True
        site_issues.append("P0 抓到的页面里没有中文内容，国内平台无从引用")
    if market == "both" and en_pages and zh_pages and abs(en_pages - zh_pages) > max(en_pages, zh_pages) * 0.7:
        thin = "英文" if en_pages < zh_pages else "中文"
        lang_warn = True
        site_issues.append(f"P1 中英内容严重不对等（中文 {zh_pages} 页 / 英文 {en_pages} 页），{thin}侧是明显短板")
    blocked_by_role = blocked_bots_by_role(site)
    if blocked_by_role["search"]:
        site_issues.append(
            "P0 robots.txt 封禁搜索 crawler：" + "、".join(blocked_by_role["search"])
            + "；对应搜索/AI Search 的发现与刷新会受阻"
        )
    if blocked_by_role["user"]:
        site_issues.append(
            "P1 robots.txt 封禁用户触发 crawler：" + "、".join(blocked_by_role["user"])
            + "；需结合各厂商对 user fetch 的 robots 规则复核"
        )
    if blocked_by_role["training"]:
        site_issues.append(
            "P2 robots.txt 拒绝训练/扩展用途 crawler：" + "、".join(blocked_by_role["training"])
            + "；这是内容使用策略，不等同于 AI Search 不可见"
        )
    ua_denied = site.get("ai_ua_probe_denied") or site.get("ai_ua_blocked") or []
    if ua_denied:
        site_issues.append(
            "P1 未验证的 WAF/UA 差异信号：GeoLook 当前来源换成 " + "、".join(ua_denied)
            + " UA 后被拒；UA 可伪装，这不能证明真实 crawler 被封。先用官方 IP + UA 或 CDN 日志核验，确证后再精确放行"
        )
    for p in site.get("ai_bots_partial", []) or []:
        site_issues.append(
            f"P1 robots.txt 对 {p['bot']} 封了部分内容路径（{p['count']}/{p['sampled']} 抽样页命中 "
            f"{p.get('rule') or ''}，如 {p['paths'][0]}），确认封的是低价值页而不是内容页")
    if not site.get("has_sitemap"):
        site_issues.append("P0 没有 sitemap.xml，收录效率和覆盖面都会打折")
    elif site.get("robots_sitemap_declared") is False:
        site_issues.append("P2 robots.txt 没有声明 Sitemap: 行，AI 抓取器发现新页面会更慢")
    if not site.get("has_llms_txt"):
        site_issues.append("P2 没有 /llms.txt，可以低成本给 AI 一份官方事实索引")
    # 重复检测：同题多 URL 会让检索在错误的候选里二选一，「错的那个」可能赢
    #（GEO Readiness Manual：duplicate URL increases the chance the wrong thing survives）
    import hashlib
    by_title: dict[str, list[str]] = {}
    by_body: dict[str, list[str]] = {}
    for p in pages:
        if (p.get("status") or 0) != 200 or p.get("word_count", 0) < 120:
            continue
        t = (p.get("title") or "").strip()
        if t:
            by_title.setdefault(t, []).append(p["url"])
        body_key = hashlib.md5(
            re.sub(r"\s+", "", (p.get("text") or "")[:600]).encode()).hexdigest()
        by_body.setdefault(body_key, []).append(p["url"])
    # 多语言站的不同语言版本标题几乎必不同，正文前段也不同，误报风险低
    dup_titles = [(t, us) for t, us in by_title.items() if len(us) > 1]
    dup_bodies = [us for us in by_body.values() if len(us) > 1]
    if dup_titles:
        ex = dup_titles[0]
        site_issues.append(
            f"P1 {len(dup_titles)} 组页面标题完全相同（如「{ex[0][:40]}」× {len(ex[1])} 个 URL），"
            "同题多 URL 会让检索在错误候选里二选一——合并或用 canonical 指向唯一版本")
    if dup_bodies:
        site_issues.append(
            f"P1 {len(dup_bodies)} 组页面正文开头完全一致（近重复内容），例：{dup_bodies[0][0]}"
            f" 与 {dup_bodies[0][1]}——保留一个规范版本，其余 301 或 canonical")

    if multilingual and content_pages and hreflang_pages / content_pages < 0.3:
        site_issues.append(
            f"P1 多语言站但只有 {hreflang_pages}/{content_pages} 个内容页声明 hreflang，"
            "引擎会把各语言版本当重复内容或串错语言，跨市场检索时挂错页面")
    if site.get("sitemap_noisy_urls"):
        site_issues.append(
            f"P2 sitemap 里有 {site['sitemap_noisy_urls']} 条带参数/搜索/翻页 URL"
            f"（如 {site.get('sitemap_noisy_example')}），低价值页会稀释实体表征——"
            "从 sitemap 移出，并用 robots 通配符（如 `Disallow: /*?session=`、`Disallow: /search?`）挡掉")
    lch = site.get("llms_txt_check") or {}
    if lch.get("broken"):
        site_issues.append(
            f"P1 llms.txt 里 {len(lch['broken'])}/{lch['checked']} 条抽样链接打不开"
            f"（如 {lch['broken'][0]['url']} → {lch['broken'][0]['status']}）。"
            "llms.txt 只有指向可抓取的有效页面才有意义")
    if lch.get("robots_blocked"):
        site_issues.append(
            f"P1 llms.txt 指向的页面反而被 robots 封禁 AI 爬虫（{lch['robots_blocked'][0]['url']}），"
            "一边给索引一边拦抓取，互相矛盾")
    grade_dist = {g: sum(1 for r in results if r["grade"] == g) for g in "ABCD"}

    # 只统计该页面类型真正需要的块；不再把 FAQ/数字/步骤机械要求到每一页。
    gap = {}
    for r in results:
        for k in r.get("required_blocks", []):
            stats = gap.setdefault(k, {"missing": 0, "total": 0})
            stats["total"] += 1
            stats["missing"] += 0 if r["blocks"].get(k) else 1
    block_gap = sorted(gap.items(), key=lambda x: -x[1]["missing"])
    block_gap_dicts = [
        {"block": k, "missing_pages": v["missing"], "total": v["total"]}
        for k, v in block_gap if v["missing"] > 0
    ]

    # —— 四层模型：访问 → 定向 → 理解 → 可引用 ——
    # 每层依赖上一层：访问失败时下游的一切优化在引擎侧不可见，修复顺序必须从上游开始。
    n = len(results) or 1
    lch = site.get("llms_txt_check") or {}

    def pages_with(code: str) -> int:
        return sum(1 for r in results if code in (r.get("issue_codes") or []))

    def layer(key, name, question, entries):
        entries = [e for e in entries if e]
        status = ("fail" if any(s == "fail" for s, _ in entries)
                  else "warn" if entries else "ok")
        return {"key": key, "name": name, "question": question, "status": status,
                "issues": [t for _, t in entries]}

    spa, render_risk = pages_with("SPA_SHELL"), pages_with("RENDERING_RISK")
    unreach = pages_with("PAGE_UNREACHABLE")
    noidx = pages_with("NOINDEX") + pages_with("XROBOTS_NOINDEX")
    nojld = pages_with("NO_JSONLD")
    layers = [
        layer("access", "访问", "抓取器能拿到内容吗", [
            ("fail", "robots.txt 封禁搜索 crawler：" + "、".join(blocked_by_role["search"]))
            if blocked_by_role["search"] else None,
            ("warn", "robots.txt 封禁用户触发 crawler：" + "、".join(blocked_by_role["user"]))
            if blocked_by_role["user"] else None,
            ("warn", "训练 crawler 被拒（内容使用策略，不是 Search blocker）："
             + "、".join(blocked_by_role["training"]))
            if blocked_by_role["training"] else None,
            ("warn", "UA 差异探测被拒但来源 IP 未验证：" + "、".join(ua_denied))
            if ua_denied else None,
            ("warn", f"robots 封了部分内容路径（{len(site['ai_bots_partial'])} 个爬虫受影响）")
            if site.get("ai_bots_partial") else None,
            (("fail" if spa >= n * 0.3 else "warn"), f"{spa} 页疑似前端渲染空壳，抓取器读不到正文") if spa else None,
            ("warn", f"{render_risk} 页存在 streaming/正文抽取兼容风险，需 rendered page 复核")
            if render_risk else None,
            (("fail" if noidx >= n * 0.3 else "warn"), f"{noidx} 页带 noindex（meta 或 X-Robots-Tag）") if noidx else None,
            ("warn", f"{unreach} 页抓取失败") if unreach else None,
        ]),
        layer("orient", "定向", "抓取器找得到、认得清每个 URL 吗", [
            ("fail", "没有 sitemap.xml") if not site.get("has_sitemap") else None,
            ("warn", "robots.txt 未声明 Sitemap: 行")
            if site.get("has_sitemap") and site.get("robots_sitemap_declared") is False else None,
            ("warn", "没有 /llms.txt") if not site.get("has_llms_txt") else None,
            ("warn", f"llms.txt 有 {len(lch['broken'])} 条失效链接") if lch.get("broken") else None,
            ("warn", "llms.txt 指向的页面被 robots 封禁") if lch.get("robots_blocked") else None,
            ("warn", f"{pages_with('NO_CANONICAL')} 页缺 canonical") if pages_with("NO_CANONICAL") else None,
            ("warn", f"{pages_with('CANONICAL_MISMATCH')} 页 canonical 指向别处")
            if pages_with("CANONICAL_MISMATCH") else None,
            ("warn", f"多语言站 hreflang 覆盖仅 {hreflang_pages}/{content_pages} 页")
            if multilingual and content_pages and hreflang_pages / content_pages < 0.3 else None,
            ("warn", f"sitemap 含 {site['sitemap_noisy_urls']} 条低价值 URL（索引污染）")
            if site.get("sitemap_noisy_urls") else None,
            ("warn", f"{len(dup_titles)} 组标题重复的页面") if dup_titles else None,
            ("warn", f"{len(dup_bodies)} 组近重复正文的页面") if dup_bodies else None,
        ]),
        layer("understand", "理解", "机器读得懂这是什么实体吗", [
            ("warn", f"{nojld} 页没有适合该页的 JSON-LD 显式线索（非抓取门票）") if nojld else None,
            ("warn", f"{pages_with('SCHEMA_CONTENT_MISMATCH')} 页 schema 与可见内容不一致")
            if pages_with("SCHEMA_CONTENT_MISMATCH") else None,
            ("fail", "目标市场缺原生语言内容") if lang_fail
            else ("warn", "中英内容严重不对等") if lang_warn else None,
        ]),
        layer("quote", "可引用", "有值得引用的具体内容吗", [
            (("fail" if avg < 45 else "warn"), f"页面均分 {avg}（70 以下属「需要改造」）") if avg < 70 else None,
            ("warn", f"{pages_with('NO_QUOTABLE_PASSAGE')} 页整页没有可独立引用的段落")
            if pages_with("NO_QUOTABLE_PASSAGE") else None,
            *[("warn", f"「{g['block']}」块缺失 {g['missing_pages']}/{g['total']} 页")
              for g in block_gap_dicts if g["missing_pages"] >= g["total"] * 0.5][:3],
        ]),
    ]
    first_fail = None
    for l in layers:
        if first_fail:
            l["blocked_by"] = first_fail
        if l["status"] == "fail" and not first_fail:
            first_fail = l["name"]

    out = {
        "slug": slug,
        "scoring_version": "page-kind-v2",
        "audited_at": G.now_iso(),
        "market": market,
        "site": site,
        "language_coverage": {"distribution": lang_dist, "zh_pages": zh_pages,
                              "en_pages": en_pages, "ja_pages": ja_pages,
                              "content_pages": content_pages, "hreflang_pages": hreflang_pages,
                              "multilingual": multilingual},
        "site_issues": site_issues,
        "layers": layers,
        "keywords_used": kws,
        "raw_page_count": len(raw_pages),
        "page_count": len(results),
        "canonical_duplicates": canonical_duplicates,
        "avg_score": avg,
        "grade_distribution": grade_dist,
        "block_gap": block_gap_dicts,
        "pages": sorted(results, key=lambda r: r["score"]),
    }
    G.write_json(pdir / "audit.json", out)
    G.info(f"体检完成：{len(results)} 页，均分 {avg}，分布 {grade_dist} → {pdir/'audit.json'}")
    return out


if __name__ == "__main__":
    import sys

    run(sys.argv[1])
