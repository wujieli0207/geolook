"""抓取目标站点：站点级信号（robots / sitemap / llms.txt）+ 代表性页面正文。

产物：
  work/<slug>/evidence/site.json      站点级检查结果
  work/<slug>/evidence/pages.jsonl    每页一条（含正文、结构统计、JSON-LD）
  work/<slug>/evidence/html/<n>.html  原始 HTML 快照（供人工复核）
"""

from __future__ import annotations

import re
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import geolib as G

# 中文站常见的高价值路径关键词 → 优先抓
PRIORITY = [
    "product", "pricing", "price", "solution", "case", "customer", "doc", "docs",
    "help", "faq", "about", "news", "blog", "guide", "compare", "vs", "feature",
    "产品", "价格", "方案", "案例", "客户", "文档", "帮助", "关于", "新闻", "博客",
]

LOCALE_PREFIXES = {
    "ar", "cs", "da", "de", "en", "es", "fi", "fr", "he", "hi", "id", "it",
    "ja", "ko", "ms", "nl", "no", "pl", "pt", "ru", "sv", "th", "tr", "uk",
    "vi", "zh", "zh-cn", "zh-hans", "zh-hant", "zh-tw",
}


def discover_sitemap(root: str, limit: int = 300) -> list[str]:
    urls: list[str] = []
    seen_maps = set()
    queue = [G.normalize_url(root, "/sitemap.xml"), G.normalize_url(root, "/sitemap_index.xml")]

    robots = G.fetch_text(G.normalize_url(root, "/robots.txt"))
    for m in re.findall(r"(?im)^\s*sitemap:\s*(\S+)", robots):
        queue.append(m.strip())

    # 最多只跟 8 个 sitemap 文件：有些站的 sitemap index 挂着上百个分片，会把抓取拖死
    while queue and len(urls) < limit and len(seen_maps) < 8:
        sm = queue.pop(0)
        if not sm or sm in seen_maps:
            continue
        seen_maps.add(sm)
        xml = G.fetch_text(sm)
        if not xml:
            continue
        locs = re.findall(r"<loc>\s*([^<]+?)\s*</loc>", xml)
        if "<sitemapindex" in xml:
            queue.extend(locs[:20])
        else:
            urls.extend(locs)
    return urls


def discover_links(root: str, html: str, limit: int = 200) -> list[str]:
    soup = G.parse_html(html)
    out: list[str] = []
    for a in soup.find_all("a", href=True):
        u = G.normalize_url(root, a["href"])
        if u and G.same_site(root, u) and u not in out:
            out.append(u)
        if len(out) >= limit:
            break
    return out


def rank(urls: list[str], root: str) -> list[str]:
    """按「路径深度浅 + 命中高价值关键词」排序，保证抓到的是代表性页面。"""
    root_host = urlparse(root).netloc.lower().removeprefix("www.")

    def key(u: str):
        parts = urlparse(u)
        p = parts.path or "/"
        depth = len([x for x in p.split("/") if x])
        hit = 0 if any(k in u.lower() for k in PRIORITY) else 1
        # 主域优先于 chat./status. 这类子域：GEO 要看的是内容页，不是应用入口
        subdomain = 0 if parts.netloc.lower().removeprefix("www.") == root_host else 1
        return (0 if u.rstrip("/") == root.rstrip("/") else 1, subdomain, hit, depth, len(u))

    # 去掉非网页链接，并按去掉末尾斜杠后的形态去重（/about 和 /about/ 是同一页）
    seen: "OrderedDict[str, str]" = OrderedDict()
    for u in [root] + urls:
        if not G.is_fetchable(u):
            continue
        seen.setdefault(u.rstrip("/") or u, u)
    return sorted(seen.values(), key=key)


def locale_bucket(url: str) -> str:
    """从常见 locale path prefix 识别语言桶；无 prefix 的页面归 default。"""
    segments = [segment.lower() for segment in urlparse(url).path.split("/") if segment]
    return segments[0] if segments and segments[0] in LOCALE_PREFIXES else "default"


def stratified_candidates(ranked_urls: list[str], limit: int) -> list[str]:
    """在截断前按 locale 轮询取样，避免 /foo 系统性排在 /zh/foo 前面。"""
    if len(ranked_urls) <= limit:
        return ranked_urls
    buckets: "OrderedDict[str, list[str]]" = OrderedDict()
    for url in ranked_urls:
        buckets.setdefault(locale_bucket(url), []).append(url)
    if len(buckets) <= 1:
        return ranked_urls[:limit]

    selected: list[str] = []
    positions = {bucket: 0 for bucket in buckets}
    while len(selected) < limit:
        progressed = False
        for bucket, urls in buckets.items():
            pos = positions[bucket]
            if pos >= len(urls):
                continue
            selected.append(urls[pos])
            positions[bucket] += 1
            progressed = True
            if len(selected) >= limit:
                break
        if not progressed:
            break
    return selected


def select_candidates(seeds, root, ranked_candidates, limit):
    # Registered core pages are a coverage contract, before optional locale sampling.
    required = rank(seeds, root)
    required_keys = {u.rstrip('/') for u in required}
    remaining = [u for u in ranked_candidates if u.rstrip('/') not in required_keys]
    if len(required) > limit:
        raise ValueError('page limit is smaller than registered core-page count')
    return required + stratified_candidates(remaining, limit - len(required))


def analyze_page(url: str, res: dict) -> dict:
    soup = G.parse_html(res["html"])
    text = G.main_text(soup)
    blocks = G.jsonld(soup)

    h1 = [h.get_text(" ", strip=True) for h in soup.find_all("h1")]
    h2 = [h.get_text(" ", strip=True) for h in soup.find_all("h2")]
    h3 = [h.get_text(" ", strip=True) for h in soup.find_all("h3")]
    paras = [p for p in soup.find_all("p") if p.get_text(strip=True)]
    lis = soup.find_all("li")
    tables = soup.find_all("table")

    hreflangs = soup.find_all("link", rel=lambda v: v and "alternate" in v, hreflang=True)
    canonical = soup.find("link", rel=lambda v: v and "canonical" in v)
    desc = soup.find("meta", attrs={"name": "description"})
    robots_meta = soup.find("meta", attrs={"name": "robots"})

    # 外链引用（指向站外的正文链接数，粗略衡量证据引用习惯）
    ext = 0
    for a in soup.find_all("a", href=True):
        u = G.normalize_url(url, a["href"])
        if u and u.startswith("http") and not G.same_site(url, u):
            ext += 1

    return {
        "url": url,
        "final_url": res["final_url"],
        "status": res["status"],
        "error": res["error"],
        "title": (soup.title.get_text(" ", strip=True) if soup.title else ""),
        "meta_description": (desc.get("content", "") if desc else ""),
        "meta_robots": (robots_meta.get("content", "") if robots_meta else ""),
        "x_robots_tag": res.get("x_robots_tag", ""),
        "hreflang_count": len(hreflangs),
        "canonical": (canonical.get("href", "") if canonical else ""),
        "lang": (soup.html.get("lang", "") if soup.html else ""),
        "h1": h1,
        "h2": h2,
        "h3_count": len(h3),
        "para_count": len(paras),
        "li_count": len(lis),
        "table_count": len(tables),
        "img_count": len(soup.find_all("img")),
        "external_links": ext,
        "jsonld_types": G.jsonld_types(blocks),
        "jsonld_raw": blocks,
        "word_count": G.word_count(text),
        "language": G.page_language(text, (soup.html.get("lang", "") if soup.html else "")),
        "cjk_ratio": G.cjk_ratio(text),
        "text": text[:20000],
        "fetched_at": G.now_iso(),
    }


# robots 控制必须区分搜索、用户触发访问与训练。允许训练 crawler 不等于能进入
# AI Search，封训练 crawler 也不等于 AI Search 抓不到。
AI_BOT_ROLES = {
    "Googlebot": "search",
    "bingbot": "search",
    "OAI-SearchBot": "search",
    "Claude-SearchBot": "search",
    "PerplexityBot": "search",
    "Baiduspider": "search",
    "Sogou web spider": "search",
    "YisouSpider": "search",
    "ChatGPT-User": "user",
    "Claude-User": "user",
    "Perplexity-User": "user",
    "GPTBot": "training",
    "ClaudeBot": "training",
    "Google-Extended": "training",
    "Bytespider": "training",
}
AI_BOTS = list(AI_BOT_ROLES)


def group_bots_by_role(blocked: list[str]) -> dict[str, list[str]]:
    return {
        role: [bot for bot in blocked if AI_BOT_ROLES.get(bot) == role]
        for role in ("search", "user", "training")
    }

# UA 差异初筛使用各家公开 UA 串。robots 放行不保证 WAF/CDN 放行，但当前机器
# 伪装 UA 不能冒充官方来源 IP；拒绝结果只能进入待核验队列。
AI_UA_PROBES = {
    "GPTBot": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; "
              "GPTBot/1.2; +https://openai.com/gptbot",
    "ClaudeBot": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; "
                 "ClaudeBot/1.0; +claudebot@anthropic.com",
    "OAI-SearchBot": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; "
                     "OAI-SearchBot/1.0; +https://openai.com/bot)",
    "ChatGPT-User": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; "
                    "ChatGPT-User/1.0; +https://openai.com/bot",
    "Claude-User": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; "
                   "Claude-User/1.0; +Claude-User@anthropic.com)",
    "Perplexity-User": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; "
                       "Perplexity-User/1.0; +https://perplexity.ai/perplexity-user)",
    "Claude-SearchBot": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; "
                        "Claude-SearchBot/1.0; +https://claude.ai",
    "PerplexityBot": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; "
                     "PerplexityBot/1.0; +https://perplexity.ai/perplexitybot",
    "Google-Extended": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; "
                       "Google-Extended",
    "Bytespider": "Mozilla/5.0 (Linux; Android 5.0) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Mobile Safari/537.36 (compatible; Bytespider; spider-feedback@bytedance.com)",
}


def check_robots(robots_txt: str, sample_paths: list[str]) -> tuple[list[str], list[dict]]:
    """返回 (整站封禁的 AI 爬虫, 部分路径封禁 [{bot, rule, paths, count}])。"""
    groups = G.robots_parse(robots_txt)
    blocked, partial = [], []
    for bot in AI_BOTS:
        ok_root, rule = G.robots_decision(groups, bot, "/")
        if not ok_root:
            blocked.append(bot)
            continue
        bad = []
        for p in sample_paths:
            ok, r = G.robots_decision(groups, bot, p)
            if not ok:
                bad.append((p, r))
        if bad:
            partial.append({"bot": bot, "rule": bad[0][1], "paths": [p for p, _ in bad[:3]],
                            "count": len(bad), "sampled": len(sample_paths)})
    return blocked, partial


def probe_ai_ua(root: str, home: dict, robots_txt: str, delay: float) -> tuple[dict, list[str]]:
    """换 AI 爬虫 UA 抓一次首页，记录需要进一步核验的差异响应。

    该请求来自 GeoLook 当前机器，不来自 crawler 的官方 IP。UA 可以被伪装，
    所以 403 只能说明“此来源 + 此 UA 被拒”，不能直接证明真实 bot 被 WAF 封锁。
    只对 robots 放行的爬虫探测（robots 都封了的，被 WAF 拦是站长本意，不算问题）；
    普通 UA 拿不到 200 时也不探测，那是站点本身的问题，不是差异封锁。"""
    probe: dict[str, int] = {}
    ua_blocked: list[str] = []
    if (home.get("status") or 0) != 200:
        return probe, ua_blocked
    groups = G.robots_parse(robots_txt)
    for bot, ua in AI_UA_PROBES.items():
        if not G.robots_decision(groups, bot, "/")[0]:
            continue
        res = G.fetch(root, timeout=10, retries=0, ua=ua)
        probe[bot] = res["status"]
        if res["status"] in DENIED_STATUSES:
            ua_blocked.append(bot)
        time.sleep(delay)
    return probe, ua_blocked


DENIED_STATUSES = (401, 403, 406, 429, 451, 503)
ALLOW_ALL = {"search": "allow", "user": "allow", "training": "allow"}


def grade_crawler(site: dict, policy: dict) -> list[dict]:
    """按声明的 crawler 策略给可达性定级，mini-geo 周期监控与 probe 共用。

    search/user 角色决定 AI 搜索可见性：robots 拒绝为 P0，UA 探测被拒为 P1。
    UA 探测不来自官方 IP，被拒时另记 Data Gap。"""
    out = []
    by_role = site.get("ai_bots_blocked_by_role") or {}
    for role in ("search", "user"):
        blocked = by_role.get(role) or []
        if policy.get(role) == "allow" and blocked:
            out.append({"level": "P0", "code": f"{role.upper()}_ROBOTS_BLOCK",
                        "title": f"{role} crawler 被 robots 拒绝", "evidence": ", ".join(blocked), "key": role})
    denied = site.get("ai_ua_probe_denied") or []
    if denied:
        search_user = [b for b in denied if AI_BOT_ROLES.get(b) in ("search", "user")]
        training = [b for b in denied if b not in search_user]
        note = "（拒绝状态码，待官方 IP 或边缘日志确认）"
        if search_user:
            out.append({"level": "P1", "code": "UA_PROBE_DENIED_SEARCH_USER",
                        "title": "AI search/user agent 疑似被 WAF/CDN 差异拦截",
                        "evidence": ", ".join(search_user) + note, "key": "site"})
        if training and policy.get("training") == "allow":
            out.append({"level": "P2", "code": "UA_PROBE_DENIED_TRAINING",
                        "title": "AI training agent 疑似被拦截且与声明策略不一致",
                        "evidence": ", ".join(training) + note, "key": "training"})
        if not site.get("ai_ua_probe_verified"):
            out.append({"level": "Data Gap", "code": "UNVERIFIED_WAF_SIGNAL",
                        "title": "WAF/UA 拒绝尚未由官方 IP 或边缘日志验证",
                        "evidence": ", ".join(denied), "key": "site"})
    training = by_role.get("training") or []
    if policy.get("training") == "allow" and training:
        out.append({"level": "P1", "code": "TRAINING_POLICY_DRIFT", "title": "训练 crawler 策略与项目声明不一致",
                    "evidence": ", ".join(training), "key": "training"})
    return out


def probe(url: str, policy: dict = ALLOW_ALL, delay: float = 0.3) -> dict:
    """单 URL 的 crawler 可达性检查：不建项目、不落盘（mini-launch 上线检查用）。"""
    root = url.rstrip("/")
    robots_txt = G.fetch_text(G.normalize_url(root, "/robots.txt"))
    home = G.fetch(root)
    blocked, partial = check_robots(robots_txt, [])
    ua_probe, ua_denied = probe_ai_ua(root, home, robots_txt, delay)
    site = {
        "root": root,
        "checked_at": G.now_iso(),
        "home_status": home.get("status"),
        "has_robots": bool(robots_txt),
        "ai_bots_blocked": blocked,
        "ai_bots_blocked_by_role": group_bots_by_role(blocked),
        "ai_bots_partial": partial,
        "ai_ua_probe": ua_probe,
        "ai_ua_probe_denied": ua_denied,
        "ai_ua_probe_verified": False,
    }
    findings = grade_crawler(site, policy)
    if site["home_status"] != 200:
        findings.insert(0, {"level": "P0", "code": "HOMEPAGE_UNREACHABLE", "title": "首页普通 UA 未返回 200",
                            "evidence": str(site["home_status"]), "key": "site"})
    site["findings"] = findings
    site["pass"] = not any(f["level"] in ("P0", "P1") for f in findings)
    return site


def check_llms_txt(root: str, llms_txt: str, robots_txt: str) -> dict | None:
    """llms.txt 只有指向可抓取的有效页面才有意义：抽样验证里面的链接。"""
    if not llms_txt:
        return None
    urls = []
    for u in re.findall(r"https?://[^\s)\]>\"'`]+", llms_txt):
        u = u.rstrip(".,;:")
        if G.same_site(root, u) and G.is_fetchable(u) and u not in urls:
            urls.append(u)
    groups = G.robots_parse(robots_txt)
    broken, robots_blocked = [], []
    sample = urls[:6]
    for u in sample:
        path = urlparse(u).path or "/"
        bots_denied = [b for b in ("OAI-SearchBot", "Claude-SearchBot", "PerplexityBot",
                                    "Googlebot", "bingbot")
                       if not G.robots_decision(groups, b, path)[0]]
        if bots_denied:
            robots_blocked.append({"url": u, "bots": bots_denied})
        res = G.fetch(u, timeout=10, retries=0)
        if res["status"] != 200:
            broken.append({"url": u, "status": res["status"]})
        time.sleep(0.3)
    return {"total_links": len(urls), "checked": len(sample),
            "broken": broken, "robots_blocked": robots_blocked}


def check_crawl_health(pages: list[dict]):
    """抓取全灭（目标站挂掉/被 WAF 拦）时直接终止流水线：
    失败页 status=0 照样进均分，会产出「均分 3 分」的误导报告。"""
    if not pages:
        return
    ok = sum(1 for p in pages if p["status"] == 200)
    if ok == 0:
        G.die("抓取失败：没有页面返回 200，检查站点可达性/WAF")
    if len(pages) >= 5 and ok / len(pages) < 0.2:
        G.die(f"抓取失败：仅 {ok}/{len(pages)} 页可访问（<20%），检查 WAF/反爬")


def run(slug: str, max_pages: int | None = None, delay: float = 0.5) -> dict:
    cfg = G.load_config(slug)
    if not G.has_site(cfg):
        G.info("无自有网站项目：跳过抓取（采样、竞品、阵地、内容、验收不受影响）")
        return {"slug": slug, "no_site": True, "pages_crawled": 0, "pages_ok": 0}
    root = cfg["brand"]["site"].rstrip("/")
    limit = max_pages or cfg.get("pages", {}).get("max", 25)
    outdir = G.project_dir(slug) / "evidence"
    (outdir / "html").mkdir(parents=True, exist_ok=True)

    G.info(f"抓取 {root}（上限 {limit} 页）")

    robots_txt = G.fetch_text(G.normalize_url(root, "/robots.txt"))
    llms_txt = G.fetch_text(G.normalize_url(root, "/llms.txt"))
    sitemap_urls = discover_sitemap(root)

    home = G.fetch(root)
    link_urls = discover_links(root, home["html"]) if home["html"] else []

    seeds = [u for u in cfg.get("pages", {}).get("seed", []) if u]
    ranked_candidates = rank(seeds + sitemap_urls + link_urls, root)
    candidates = select_candidates(seeds, root, ranked_candidates, limit)
    pool_buckets: dict[str, int] = {}
    selected_buckets: dict[str, int] = {}
    for url in ranked_candidates:
        bucket = locale_bucket(url)
        pool_buckets[bucket] = pool_buckets.get(bucket, 0) + 1
    for url in candidates:
        bucket = locale_bucket(url)
        selected_buckets[bucket] = selected_buckets.get(bucket, 0) + 1
    complete_selection = len(ranked_candidates) <= limit
    locale_stratified = not complete_selection and len(pool_buckets) > 1

    def crawl_one(i: int, u: str) -> dict:
        res = home if u.rstrip("/") == root else G.fetch(u)
        if res["status"] and res["html"]:
            (outdir / "html" / f"{i:03d}.html").write_text(res["html"], "utf-8")
        page = analyze_page(u, res)
        page["snapshot"] = f"evidence/html/{i:03d}.html"
        return page

    # 按 host 分组：组内串行保持礼貌延迟，组间并发（不同站点互不打扰）。
    # 多 host 来源：sitemap/内链里可能混着 chat./docs. 这类子域。
    groups: "OrderedDict[str, list[tuple[int, str]]]" = OrderedDict()
    for i, u in enumerate(candidates, 1):
        groups.setdefault(urlparse(u).netloc.lower(), []).append((i, u))

    def crawl_group(items: list[tuple[int, str]]) -> dict[int, dict]:
        out = {}
        for i, u in items:
            page = crawl_one(i, u)
            out[i] = page
            G.info(f"  [{i}/{len(candidates)}] {page['status']} {u}")
            time.sleep(delay)
        return out

    pages_by_idx: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=max(1, min(3, len(groups)))) as pool:
        for out in pool.map(crawl_group, groups.values()):
            pages_by_idx.update(out)
    pages = [pages_by_idx[i] for i in range(1, len(candidates) + 1)]

    # AI 抓取器是否被 robots 拦截（GEO 的第一道门槛）。
    # 按 RFC 9309 语义判：通配符组封禁、多 UA 共享组、specificity 覆盖都能检出。
    blocked, partial = check_robots(robots_txt, [urlparse(u).path or "/" for u in candidates[:12]])
    # WAF/CDN 差异封锁：robots 说放行不代表真放行，换 AI 爬虫的 UA 实测一次
    ua_probe, ua_denied_unverified = probe_ai_ua(root, home, robots_txt, delay)
    llms_check = check_llms_txt(root, llms_txt, robots_txt)

    # 索引污染：sitemap 里的带参/搜索/翻页 URL 会把低质片段灌进检索索引，
    # 稀释实体表征——sitemap 该只装值得被引用的规范页
    noisy = [u for u in sitemap_urls
             if "?" in u or re.search(r"/(search|tag|page/\d+|sessions?)($|/|\?)", u, re.I)]

    site = {
        "slug": slug,
        "root": root,
        "crawled_at": G.now_iso(),
        "has_robots": bool(robots_txt),
        "has_llms_txt": bool(llms_txt),
        "has_sitemap": bool(sitemap_urls),
        "sitemap_url_count": len(sitemap_urls),
        "robots_sitemap_declared": bool(re.search(r"(?im)^\s*sitemap:", robots_txt or "")),
        "sitemap_noisy_urls": len(noisy),
        "sitemap_noisy_example": (noisy[0] if noisy else None),
        "ai_bots_blocked": blocked,
        "ai_bots_blocked_by_role": group_bots_by_role(blocked),
        "ai_bots_partial": partial,
        "ai_ua_probe": ua_probe,
        # 兼容旧消费者：没有官方来源 IP 证据时不再写成已确认 block。
        "ai_ua_blocked": [],
        "ai_ua_probe_denied": ua_denied_unverified,
        "ai_ua_probe_verified": False,
        "llms_txt_check": llms_check,
        "pages_crawled": len(pages),
        "pages_ok": sum(1 for p in pages if p["status"] == 200),
        "crawl_selection": {
            "method": (
                "complete" if complete_selection
                else "locale-stratified" if locale_stratified
                else "ranked-truncated"
            ),
            "complete": complete_selection,
            "locale_stratified": locale_stratified,
            "candidate_pool_count": len(ranked_candidates),
            "pool_locale_buckets": pool_buckets,
            "selected_locale_buckets": selected_buckets,
        },
    }
    G.write_json(outdir / "site.json", site)
    G.write_jsonl(outdir / "pages.jsonl", pages)
    G.info(f"完成：{site['pages_ok']}/{len(pages)} 页可访问 → {outdir}")
    check_crawl_health(pages)
    return site


if __name__ == "__main__":
    import sys

    run(sys.argv[1])
