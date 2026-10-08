"""AI 答案采样：把问题库打到各个引擎上，量化「品牌在 AI 答案里的可见性」。

三种采样模式，证据等级从高到低：
  api      有 API 的引擎直接跑（DeepSeek / 千问 / Kimi / 任意 OpenAI 兼容端点）
  browser  网页端/App 端由 Claude 用浏览器工具逐条采，结果 import 回来
  manual   导出问题清单，人工粘贴答案后 import

重要口径：API 结果 ≠ 网页端结果。同一产品 Web 与 App 的信源集合都有系统性差异
（CN-GEO 论文结论），所以每个平台+终端单独记录，绝不混算。

产物：work/<slug>/samples/<日期>.jsonl + work/<slug>/metrics/<日期>.json
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import requests

import geolib as G

# 平台注册表：code -> 配置。market 决定这个平台该问哪一套问题库。
# 观测集合（2026-07 定）：国内 = 智谱GLM/豆包/DeepSeek/Kimi/MiniMax/纳米AI/百度AI；
# 海外 = Gemini/ChatGPT/Claude/Grok/Perplexity。纳米AI、百度AI 无公开 API，走人工采样。
# Logical model families only. Explicit catalog slugs live in replicate.json.
PROVIDERS = {
    code: {"name": name + " via Replicate", "market": market,
           "key_env": "REPLICATE_API_TOKEN", "model": "", "search": False,
           "note": "Pinned model API; not consumer AI-search visibility"}
    for code, name, market in [
        ("openai", "OpenAI", "global"), ("gemini", "Gemini", "global"),
        ("grok", "Grok", "global"), ("claude", "Claude", "global"),
        ("perplexity", "Perplexity", "global"), ("deepseek", "DeepSeek", "cn"),
        ("glm", "GLM", "cn"), ("doubao", "Doubao", "cn"),
        ("kimi", "Kimi", "cn"), ("minimax", "MiniMax", "cn")
    ]
}

# 没有公开联网问答 API 的平台，只能浏览器/人工采
MANUAL_ONLY = {
    "nano_ai": ("纳米AI搜索（360）", "cn"),
    "baidu": ("百度 AI 搜索", "cn"),
    "doubao_app": ("豆包 App / 网页版（与方舟 API 结果不同，需分开采）", "cn"),
    "chatgpt": ("ChatGPT 网页版（开 Search）", "global"),
    "claude_web": ("Claude 网页版（开 Web Search）", "global"),
    "google_aio": ("Google AI Overviews（搜索页顶部 AI 摘要，无则记「未触发」）", "global"),
    "metaso": ("秘塔AI搜索（引用为角标非链接，答案可采、引用常为 0 条）", "cn"),
}

# 买家意图分组：手动周检只查这几组就够了——商业价值最高、也最能反映「AI 推荐了谁」
BUYER_GROUPS = {"价格", "推荐", "比较", "替代"}


def market_of(platform: str) -> str:
    if platform in PROVIDERS:
        return PROVIDERS[platform]["market"]
    if platform in MANUAL_ONLY:
        return MANUAL_ONLY[platform][1]
    # 未识别的平台代码（多半是笔误）：绝不默认并入国内，标记 unknown 不进任何市场统计
    G.info(f"未识别的平台代码 {platform!r}，市场标记为 unknown（不进国内/海外统计）")
    return "unknown"


def label_of(platform: str) -> str:
    if platform in PROVIDERS:
        return PROVIDERS[platform]["name"]
    if platform in MANUAL_ONLY:
        return MANUAL_ONLY[platform][0]
    return platform


def questions_for(cfg: dict, platform: str) -> list[dict]:
    """问题按市场路由：中文问题不打海外平台，英文问题不打国内平台。

    问题没写 market 的，按项目 market 处理；项目是 both 时视为通用问题，两边都问。
    """
    m = market_of(platform)
    out = []
    for q in cfg.get("questions", []):
        qm = q.get("market") or cfg.get("market", "cn")
        if qm in ("both", m):
            out.append(q)
    return out


def model_for(platform: str) -> str:
    import replicate_gateway as gateway
    return gateway.model_for(platform) or ""


def available(platform: str) -> bool:
    return bool(platform in PROVIDERS and os.environ.get("REPLICATE_API_TOKEN") and model_for(platform))


LLM_PREFS = ("openai", "gemini", "claude")


def pick_llm(prefer: str | None = None):
    return next((c for c in ([prefer] if prefer else LLM_PREFS) if c and available(c)), None)


def ask(platform: str, question: str, timeout: int = 120, request_key=None) -> dict:
    import replicate_gateway as gateway
    return gateway.ask(platform, question, timeout, request_key=request_key)


# ------------------------------------------------------------ 答案解析

URL_RE = re.compile(r"https?://[^\s\)\]\"'，。；]+")


def entities_of(cfg: dict) -> tuple[list[str], dict[str, list[str]]]:
    """返回 (全部候选实体名, {规范名: 别名列表})"""
    alias = {}
    b = cfg["brand"]
    alias[b["name"]] = [b["name"]] + list(b.get("aliases", []) or [])
    for c in cfg.get("competitors", []) or []:
        alias[c["name"]] = [c["name"]] + list(c.get("aliases", []) or [])
    return list(alias.keys()), alias


_LATIN = re.compile(r"[A-Za-z0-9]")
_NEG_RE = re.compile(r"不是|并非|不属于|不同于|not |isn't|aren't", re.IGNORECASE)
_SENT_END = "。！？!?\n"

# 负面语境线索：只在品牌名附近窗口内找，命中≠负面定性，只标「疑似负面」进人工复核。
# 词表故意保守——误报会浪费复核时间，漏报还有样本回放兜底。
NEG_CUES = re.compile(
    r"不推荐|避雷|缺点|劣势|投诉|差评|跑路|骗局|割韭菜|不靠谱|慎用|翻车|已倒闭|停止运营|维权|退款难"
    r"|not recommended|avoid|scam|complaints?|lawsuit|shut ?down|worse than|downsides?",
    re.IGNORECASE)


def _alias_spans(text: str, alias: str) -> list[tuple[int, int]]:
    """别名命中区间。

    边界策略（权衡）：跨文种相邻（CJK↔拉丁）是天然分词边界，不算词延续；
    只有「拉丁接拉丁」才是真延续。所以：
    - 别名的拉丁侧边缘加 lookaround 排除 [A-Za-z0-9]，防 "AIGC" 命中 "AIGCLINK"；
      CJK 侧边缘不查——「推荐AIGC」「AIGCLINK定制家很好用」都是正常命中。
    - 纯 CJK 别名保持子串匹配：中文没有空格分词，右侧是 CJK 不代表另一个词。
      残留风险：「定制家居」里的「定制家」仍会命中——靠否定语境检查挡住
      「不是定制家居」这类，其余靠 needs_review 人工兜底。
    """
    left = r"(?<![A-Za-z0-9])" if _LATIN.match(alias[0]) else ""
    right = r"(?![A-Za-z0-9])" if _LATIN.match(alias[-1]) else ""
    if left or right:
        return [m.span() for m in re.finditer(left + re.escape(alias) + right, text, re.IGNORECASE)]
    return [m.span() for m in re.finditer(re.escape(alias), text)]


def _sentence_at(text: str, pos: int) -> str:
    start = max([text.rfind(c, 0, pos) for c in _SENT_END] + [-1]) + 1
    ends = [text.find(c, pos) + 1 for c in _SENT_END if text.find(c, pos) != -1]
    return text[start:min(ends) if ends else len(text)]


def _entity_hit(text: str, aliases: list[str]) -> tuple[int, bool]:
    """返回 (首个有效命中位置, 是否有命中因否定语境被丢弃待人工确认)。"""
    hits = sorted((s, e) for a in aliases if a for s, e in _alias_spans(text, a))
    valid, negated = [], False
    for s, e in hits:
        if _NEG_RE.search(_sentence_at(text, s)):
            negated = True  # 「不是 X」里的命中不算提及，但要人工确认
        else:
            valid.append(s)
    return (min(valid) if valid else -1), negated


def first_pos(text: str, names: list[str]) -> int:
    return _entity_hit(text, names)[0]


def brand_in_question(question: str, cfg: dict) -> bool:
    """问题本身是否点名了品牌。

    点名了的话，答案必然复述品牌名，「提及率」会变成 100% 的假阳性。
    这类问题要单独归到品牌认知，不能混进可见性指标。
    """
    b = cfg["brand"]
    names = [b["name"]] + list(b.get("aliases", []) or [])
    host = urlparse(b.get("site", "")).netloc.lower().removeprefix("www.")
    if host and host in question.lower():
        return True
    for n in names:
        if not n:
            continue
        # 多词 Latin 品牌很容易同时是普通品类短语（如 AI Fruit）。对这类名字，
        # canonical capitalization 才视为明确点名；"an AI fruit video creator"
        # 不能因为大小写不敏感的子串匹配而被误归为品牌题。
        if " " in n and re.fullmatch(r"[A-Za-z0-9 .&+_-]+", n):
            if re.search(rf"(?<![A-Za-z0-9]){re.escape(n)}(?![A-Za-z0-9])", question):
                return True
            continue
        if _entity_hit(question, [n])[0] >= 0:
            return True
    return False


def analyze_answer(answer: str, cfg: dict, citations: list | None = None) -> dict:
    brand = cfg["brand"]["name"]
    names, alias = entities_of(cfg)
    positions, needs_review = {}, False
    for n in names:
        pos, negated = _entity_hit(answer, alias[n])
        positions[n] = pos
        needs_review = needs_review or negated
    present = {n: p >= 0 for n, p in positions.items()}
    ordered = [n for n, p in sorted(positions.items(), key=lambda x: x[1]) if p >= 0]

    urls = [u for u in URL_RE.findall(answer)]
    for c in citations or []:
        if c.get("url"):
            urls.append(c["url"])
    domains = []
    for u in urls:
        try:
            h = urlparse(u).netloc.lower().removeprefix("www.")
            if h:
                domains.append(h)
        except Exception:  # noqa: BLE001
            pass

    # 无自有网站：官网引用率不适用（None），不能算成 0
    own = urlparse(cfg["brand"]["site"]).netloc.lower().removeprefix("www.") if G.has_site(cfg) else ""

    # 疑似负面：品牌每个命中点前 80 / 后 160 字符窗口内的负面线索词
    neg = set()
    if present.get(brand):
        for a in alias[brand]:
            for s, e in _alias_spans(answer, a):
                for mm in NEG_CUES.finditer(answer[max(0, s - 80):e + 160]):
                    neg.add(mm.group(0).lower())

    return {
        "brand_mentioned": present.get(brand, False),
        "brand_rank": (ordered.index(brand) + 1) if brand in ordered else 0,
        "candidates": ordered,
        "competitors_mentioned": [n for n in names if n != brand and present.get(n)],
        "cited_domains": sorted(set(domains)),
        "own_domain_cited": any(d == own or d.endswith("." + own) for d in domains),
        "answer_chars": len(answer),
        "needs_review": needs_review or bool(neg),
        "negative_cues": sorted(neg),
    }


def dedup_rows(rows: list[dict]) -> list[dict]:
    """同日重跑/重复导入去重：按 (platform, question_id, round, sample_mode) 保留最后一条。"""
    seen: dict[tuple, dict] = {}
    for r in rows:
        seen[(r.get("platform"), r.get("question_id"), r.get("round"), r.get("sample_mode"),
              r.get("requested_model") or r.get("raw_model"), r.get("method_version"),
              r.get("question_set_version"), r.get("cycle_id"), r.get("search_enabled"), r.get("sampling_config_fingerprint"), r.get("question_set_hash"))] = r
    return list(seen.values())


def aggregate(rows: list[dict], cfg: dict) -> dict:
    by_platform: dict[str, list[dict]] = {}
    identities = {}
    for r in rows:
        identity = (r.get("requested_model") or r.get("raw_model"), r.get("method_version"),
                    r.get("question_set_version"), r.get("terminal_class"), r.get("search_enabled"), r.get("sampling_config_fingerprint"), r.get("question_set_hash"))
        identities.setdefault(r["platform"], set()).add(identity)
    for r in rows:
        key = r["platform"]
        if len(identities[key]) > 1:
            identity = (r.get("requested_model") or r.get("raw_model"), r.get("method_version"),
                        r.get("question_set_version"), r.get("terminal_class"), r.get("search_enabled"), r.get("sampling_config_fingerprint"), r.get("question_set_hash"))
            key += "@" + __import__("hashlib").sha256(repr(identity).encode()).hexdigest()[:12]
        by_platform.setdefault(key, []).append(r)

    out = {}
    for plat, all_rs in by_platform.items():
        # 点名品牌的问题（品牌验证类）不能算进可见性——答案必然复述品牌名。
        # 它们单独统计成「品牌认知」：AI 到底知不知道这个品牌、说得对不对。
        # question 文本是可重算的 source of truth；旧样本里的 brand_in_question
        # 可能来自旧版误判，只在缺 question 时回退旧字段。
        probe = [
            r for r in all_rs
            if (r["question_intent"] == "brand" if r.get("question_intent") else
                (brand_in_question(r.get("question", ""), cfg) if r.get("question") else bool(r.get("brand_in_question"))))
        ]
        rs = [r for r in all_rs if r not in probe]
        # 绝不回退：某平台只采了点名题时，可见性指标就是「未测」（None），
        # 不能把点名样本塞回去凑出 mention_rate=1.0 的假阳性。
        n = len(rs)
        market = (rs[0].get("market") if rs else None) or market_of(plat)
        mentioned = [r for r in rs if r["analysis"]["brand_mentioned"]]
        ranks = [r["analysis"]["brand_rank"] for r in mentioned if r["analysis"]["brand_rank"]]
        comp = {}
        dom = {}
        citation_rs = [r for r in rs if r.get("search_enabled") is True]
        for r in rs:
            for c in r["analysis"]["competitors_mentioned"]:
                comp[c] = comp.get(c, 0) + 1
        for r in citation_rs:
            for d in r["analysis"]["cited_domains"]:
                dom[d] = dom.get(d, 0) + 1
        out[plat] = {
            "market": market,
            "label": label_of(all_rs[0]["platform"]),
            "model": all_rs[0].get("raw_model"),
            "method_version": all_rs[0].get("method_version"),
            "samples": n,
            "citation_samples": len(citation_rs),
            "top1_rate": None, "top3_rate": None, "avg_rank": None,
            "rank_limit": "Registered-name occurrence order is not recommendation ranking.",
            "search_enabled": bool(citation_rs),
            "mention_rate": round(len(mentioned) / n, 3) if n else None,
            "registered_brand_first_occurrence_rate": round(sum(1 for r in mentioned if r["analysis"]["brand_rank"] == 1) / n, 3) if n else None,
            "registered_brand_first_three_occurrence_rate": round(sum(1 for r in mentioned if 1 <= r["analysis"]["brand_rank"] <= 3) / n, 3) if n else None,
            "registered_brand_mean_occurrence_order": round(sum(ranks) / len(ranks), 2) if ranks else None,
            "own_domain_cite_rate": (
                round(sum(1 for r in citation_rs if r["analysis"]["own_domain_cited"])
                      / len(citation_rs), 3)
                if citation_rs and G.has_site(cfg) else None
            ),
            "competitor_mentions": dict(sorted(comp.items(), key=lambda x: -x[1])),
            "top_cited_domains": dict(sorted(dom.items(), key=lambda x: -x[1])[:15]),
            # 品牌认知：直接点名品牌时，AI 认不认识、有没有引到官网
            "probe": {
                "samples": len(probe),
                "recognized_rate": round(sum(1 for r in probe if r["analysis"]["brand_mentioned"]) / len(probe), 3) if probe else None,
                "own_domain_cite_rate": (
                    round(sum(1 for r in probe if r.get("search_enabled") is True
                              and r["analysis"]["own_domain_cited"])
                          / sum(1 for r in probe if r.get("search_enabled") is True), 3)
                    if any(r.get("search_enabled") is True for r in probe) and G.has_site(cfg)
                    else None
                ),
            },
        }
    return out


def confirm_competitors(slug: str, rows: list[dict]):
    """采样里真实出现过的竞品，把 geo.json 里对应候选的 confirmed 转正。
    只在值需要变化时才写配置（save_config 会自动备份）。"""
    seen = {c for r in rows for c in (r.get("analysis", {}).get("competitors_mentioned") or [])}
    if not seen:
        return
    cfg = G.load_config(slug)
    confirmed = []
    for c in cfg.get("competitors", []) or []:
        if c.get("confirmed") is False and c.get("name") in seen:
            c["confirmed"] = True
            confirmed.append(c["name"])
    if confirmed:
        G.save_config(slug, cfg)
        G.info("  竞品经采样确认：" + "、".join(confirmed))


# ------------------------------------------------------------ 命令


def run(slug: str, platforms: list[str] | None = None, repeat: int = 1, limit: int | None = None) -> dict:
    cfg = G.load_config(slug)
    if not cfg.get("questions"):
        G.die("geo.json 里还没有问题库，先让 Claude 生成 questions（见 SKILL.md 步骤 2）")

    plats = platforms or [p for p in cfg.get("platforms", []) if p in PROVIDERS]
    runnable = [p for p in plats if available(p)]
    skipped = [p for p in plats if not available(p)]
    if skipped:
        G.info("跳过（缺 API Key）：" + "、".join(f"{p}({PROVIDERS[p]['key_env']})" for p in skipped))
    if not runnable:
        G.info("没有可用的 API 平台。用 `geo.py sample-sheet` 导出人工/浏览器采样清单。")
        return {}

    # 任务清单：平台 × 问题 × 轮次
    jobs = []
    for plat in runnable:
        questions = questions_for(cfg, plat)
        if limit:
            questions = questions[:limit]
        if not questions:
            G.info(f"跳过 {plat}：问题库里没有 {market_of(plat)} 市场的问题")
            continue
        G.info(f"[{plat}] {market_of(plat)} 市场 · {len(questions)} 题 × {repeat} 轮")
        for q in questions:
            for k in range(repeat):
                jobs.append((plat, q, k + 1))

    pdir = G.project_dir(slug)
    path = pdir / "samples" / f"{G.today()}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)

    if cfg.get("cycle_id"):
        history = []
        for previous in sorted(path.parent.glob("*.jsonl"))[-30:]:
            history.extend(G.read_jsonl(previous))
        success = {(r.get("platform"), r.get("question_id"), r.get("round"))
            for r in dedup_rows(history) if r.get("ok")
            and r.get("cycle_id") == cfg["cycle_id"]
            and r.get("method_version") == cfg.get("method_version")
            and r.get("question_set_version") == cfg.get("question_set_version")
            and r.get("requested_model") == model_for(r.get("platform"))
            and r.get("question_set_hash") == cfg.get("question_set_hash")
            and r.get("sampling_config_fingerprint") == cfg.get("sampling_fingerprints",{}).get(r.get("platform"))}
        jobs = [j for j in jobs if (j[0], j[1]["id"], j[2]) not in success]

    def one(job):
        plat, q, rnd = job
        t0 = time.monotonic()
        res = ask(plat, q["text"], request_key=f"{cfg['cycle_id']}:{plat}:{q['id']}:{rnd}") if cfg.get("cycle_id") else ask(plat, q["text"])
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        rec = {
            "date": G.today(), "ts": G.now_iso(),
            "platform": plat, "platform_name": PROVIDERS[plat]["name"],
            "market": market_of(plat), "terminal": "api", "sample_mode": "api",
            "evidence_level": "B_api_可复现",
            "search_enabled": res.get("searched", PROVIDERS[plat].get("search", False)),
            "raw_model": res.get("raw_model"),
            "requested_model": res.get("requested_model") or model_for(plat),
            "gateway": "replicate", "terminal_class": "model_api_closed_book",
            "method_version": cfg.get("method_version", "replicate-closed-book-v1"),
            "question_set_version": cfg.get("question_set_version"),
            "cycle_id": cfg.get("cycle_id"), "question_intent": q.get("intent"),
            "question_set_hash": cfg.get("question_set_hash"),
            "sampling_config_fingerprint": res.get("sampling_config_fingerprint") or cfg.get("sampling_fingerprints",{}).get(plat),
            "usage": res.get("usage"), "generation_id": res.get("generation_id"),
            "budget_request_id": res.get("budget_request_id"),
            "cost_status": res.get("cost_status"), "estimated_cost_usd": res.get("estimated_cost_usd"), "model_version": res.get("model_version"),
            "question_id": q.get("id"), "question": q["text"], "round": rnd,
            "brand_in_question": brand_in_question(q["text"], cfg),
            "ok": res["ok"], "error": res.get("error"),
            "elapsed_ms": elapsed_ms,
            "answer": res.get("answer", ""), "citations": res.get("citations", []),
        }
        rec["analysis"] = analyze_answer(rec["answer"], cfg, rec["citations"]) if res["ok"] else {
            "brand_mentioned": False, "brand_rank": 0, "candidates": [],
            "competitors_mentioned": [], "cited_domains": [], "own_domain_cited": False,
            "answer_chars": 0, "needs_review": False, "negative_cues": [],
        }
        rec["needs_review"] = bool(rec["analysis"].get("needs_review"))
        return rec

    # 平台之间互不相干，并发跑；单个平台内部串行以免触发限流。
    # 推理型模型单次可达 90s，串行跑几十题会拖到一小时以上。
    rows, done, total = [], 0, len(jobs)
    lock = threading.Lock()
    fh = path.open("a", encoding="utf-8")  # 增量落盘：中途挂掉也不丢已采样本

    def worker(plat_jobs):
        nonlocal done
        out = []
        for job in plat_jobs:
            rec = one(job)
            with lock:
                done += 1
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fh.flush()
                flag = "✓" if rec["analysis"]["brand_mentioned"] else ("✗" if not rec["ok"] else "·")
                print(f"[geo] {done:3d}/{total} {flag} [{rec['platform']}] {rec['question'][:32]}",
                      file=sys.stderr, flush=True)
            out.append(rec)
            time.sleep(0.4)
        return out

    by_plat: dict[str, list] = {}
    for job in jobs:
        by_plat.setdefault(job[0], []).append(job)
    with ThreadPoolExecutor(max_workers=max(1, len(by_plat))) as ex:
        for fut in as_completed([ex.submit(worker, v) for v in by_plat.values()]):
            try:
                rows.extend(fut.result())
            except Exception as e:  # noqa: BLE001
                G.info(f"某平台采样中断：{type(e).__name__}: {e}")
    fh.close()

    all_rows = dedup_rows(G.read_jsonl(path))
    ok_rows = [r for r in all_rows if r.get("ok")]
    metrics = {
        "slug": slug, "date": G.today(), "generated_at": G.now_iso(),
        "question_count": len(cfg.get("questions", [])), "sample_count": len(all_rows),
        "platforms": aggregate(ok_rows, cfg),
    }
    G.write_json(pdir / "metrics" / f"{G.today()}.json", metrics)
    confirm_competitors(slug, ok_rows)
    G.info(f"采样完成：{len(rows)} 条 → {path}")
    return metrics


def sheet(slug: str, intent: str | None = None, limit: int | None = None) -> Path:
    """导出人工/浏览器采样清单（Markdown），采完把答案粘回同一文件再 import。

    intent="buyer" 只出买家意图题（价格/推荐/比较/替代），limit 控制每平台题数——
    「每周 15–20 条买家题」的轻量周检就是 --intent buyer --limit 20。"""
    cfg = G.load_config(slug)
    plats = [p for p in cfg.get("platforms", []) if p in MANUAL_ONLY or not available(p)]
    tag = "buyer" if intent == "buyer" else "manual"
    lines = [
        f"# {cfg['brand']['name']} · AI 答案人工采样表 · {G.today()}"
        + ("（买家意图周检）" if intent == "buyer" else ""),
        "",
        "用法：每个平台逐题提问，把**完整答案原文**（含引用链接）粘到对应的 ```answer 代码块里，",
        "然后运行 `python3 scripts/geo.py sample-import --slug " + slug + " --file <本文件>`。",
        "",
        "**采样纪律（违反任何一条，这份样本就不算 A 级证据）：**",
        "",
        "1. **无痕/隐私模式**，且不登录账号——登录态的个性化会污染样本，测出来的是「AI 对你的画像」不是「AI 对大众的回答」",
        "2. 每题**新开对话**，不连续追问——上下文会让后面的答案带着前面的偏置",
        "3. 复制**完整答案原文**，包括引用链接/来源列表，不要只摘品牌相关的句子",
        "4. 答案里没有你的品牌时照样粘贴——「没提到」正是最重要的数据，别只记提到的",
        "5. 留空的题目会被跳过，不会被当成「品牌未被提及」",
        "",
    ]
    for plat in plats:
        qs = questions_for(cfg, plat)
        if intent == "buyer":
            qs = [q for q in qs if q.get("group") in BUYER_GROUPS]
        if limit:
            qs = qs[:limit]
        if not qs:
            continue
        mk = "国内" if market_of(plat) == "cn" else "海外"
        lines += [f"## platform: {plat}", f"> {label_of(plat)}（{mk}市场 · {len(qs)} 题）", ""]
        for q in qs:
            lines += [f"### {q.get('id')} · {q['text']}", "", "```answer", "", "```", ""]
    path = G.project_dir(slug) / "samples" / f"{G.today()}-{tag}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), "utf-8")
    G.info(f"采样表已导出：{path}")
    return path


def sample_import(slug: str, file: str) -> dict:
    cfg = G.load_config(slug)
    text = Path(file).read_text("utf-8")
    qmap = {q.get("id"): q["text"] for q in cfg.get("questions", [])}

    rows, platform = [], "manual"
    blocks = re.split(r"(?m)^##\s+platform:\s*(\S+)\s*$", text)
    # blocks = [前言, plat1, body1, plat2, body2, ...]
    for i in range(1, len(blocks), 2):
        platform = blocks[i].strip()
        body = blocks[i + 1]
        for m in re.finditer(r"(?ms)^###\s+(\S+)\s*·\s*(.+?)\n(.*?)```answer\n(.*?)```", body):
            qid, qtext, _, answer = m.group(1), m.group(2).strip(), m.group(3), m.group(4).strip()
            if not answer:
                continue
            rec = {
                "date": G.today(), "ts": G.now_iso(),
                "platform": platform,
                "platform_name": label_of(platform),
                "market": market_of(platform),
                "terminal": "web", "sample_mode": "manual",
                "evidence_level": "A_人工真实样本", "search_enabled": True,
                "question_id": qid, "question": qmap.get(qid, qtext), "round": 1,
                "ok": True, "error": None, "answer": answer, "citations": [],
            }
            rec["analysis"] = analyze_answer(answer, cfg)
            rec["needs_review"] = bool(rec["analysis"].get("needs_review"))
            rows.append(rec)

    if not rows:
        G.die("没解析到任何答案，检查 ```answer 代码块是否填写")
    # CLI 路径也要拿项目锁：插件回传（dashboard 持锁）可能同时写同一份当日文件
    with G.project_lock(slug):
        metrics = store_manual_rows(slug, cfg, rows)
    G.info(f"导入 {len(rows)} 条人工样本")
    return metrics


# ---------------------------------------------------------------- 样本库（答案元数据）

def sample_key(r: dict) -> str:
    """样本唯一键。与 dedup_rows 同口径（同日同平台同题同轮同模式唯一）加上日期。"""
    return "|".join(str(r.get(k, "")) for k in
                    ("date", "platform", "question_id", "round", "sample_mode"))


def _sample_files(slug: str) -> list[Path]:
    d = G.project_dir(slug) / "samples"
    return sorted(d.glob("*.jsonl")) if d.exists() else []


def list_samples(slug: str, date: str = "", platform: str = "", qid: str = "",
                 flag: str = "", limit: int = 300) -> dict:
    """列出样本元数据（不含全文，全文按需单取）。flag: review=待复核 / edited=人工改过。"""
    rows, dates, plats = [], set(), set()
    for f in _sample_files(slug):
        for r in G.read_jsonl(f):
            d = r.get("date") or f.stem
            dates.add(d)
            plats.add(r.get("platform"))
            if date and d != date:
                continue
            if platform and r.get("platform") != platform:
                continue
            if qid and r.get("question_id") != qid:
                continue
            if flag == "review" and not r.get("needs_review"):
                continue
            if flag == "edited" and not r.get("manual_override"):
                continue
            a = r.get("analysis") or {}
            rows.append({
                "key": sample_key(r), "date": d, "ts": r.get("ts"),
                "platform": r.get("platform"), "platform_name": r.get("platform_name"),
                "market": r.get("market"), "terminal": r.get("terminal"),
                "sample_mode": r.get("sample_mode"), "evidence_level": r.get("evidence_level"),
                "session_mode": r.get("session_mode"), "session_label": r.get("session_label"),
                "question_id": r.get("question_id"), "question": r.get("question"),
                "ok": r.get("ok"), "answer_chars": a.get("answer_chars") or len(r.get("answer") or ""),
                "brand_mentioned": a.get("brand_mentioned"), "brand_rank": a.get("brand_rank"),
                "competitors": a.get("competitors_mentioned") or [],
                "cited_domains": a.get("cited_domains") or [],
                "own_domain_cited": a.get("own_domain_cited"),
                "citations": len(r.get("citations") or []),
                "needs_review": bool(r.get("needs_review")),
                "negative_cues": a.get("negative_cues") or [],
                "manual_override": bool(r.get("manual_override")),
                "review_note": r.get("review_note") or "",
            })
    rows.sort(key=lambda x: (x["date"], x["platform"], x["question_id"] or ""), reverse=True)
    return {"rows": rows[:limit], "total": len(rows),
            "dates": sorted(dates, reverse=True), "platforms": sorted(p for p in plats if p)}


def get_sample(slug: str, key: str) -> dict | None:
    for f in _sample_files(slug):
        for r in G.read_jsonl(f):
            if sample_key(r) == key:
                return r
    return None


# 只允许改这些：人工复核纠正机器判读，不能凭空改出一条新样本
_PATCHABLE = {"brand_mentioned", "brand_rank", "competitors_mentioned"}


def patch_sample(slug: str, key: str, patch: dict) -> dict:
    """人工复核：纠正判读、标注、或删除坏样本。改完重算当日指标。

    正则判读会有假阳性/假阴性（品牌名撞词、否定语境、竞品别名），这里是唯一的纠正入口；
    改过的样本打 manual_override，重跑采样不会覆盖人工结论。"""
    target_date = None
    with G.project_lock(slug):
        cfg = G.load_config(slug)
        for f in _sample_files(slug):
            rows = G.read_jsonl(f)
            hit = next((i for i, r in enumerate(rows) if sample_key(r) == key), None)
            if hit is None:
                continue
            r = rows[hit]
            target_date = r.get("date") or f.stem
            if patch.get("delete"):
                rows.pop(hit)
            else:
                a = r.setdefault("analysis", {})
                for k in _PATCHABLE:
                    if k in patch:
                        a[k] = patch[k]
                        r["manual_override"] = True
                if "evidence_level" in patch:
                    r["evidence_level"] = str(patch["evidence_level"])[:32]
                    r["manual_override"] = True
                if "review_note" in patch:
                    r["review_note"] = str(patch["review_note"])[:500]
                if "needs_review" in patch:
                    r["needs_review"] = bool(patch["needs_review"])
                r["reviewed_at"] = G.now_iso()
            G.write_jsonl(f, rows)
            break
        else:
            return {"ok": False, "error": "找不到该样本"}
        metrics = recompute_metrics(slug, cfg, target_date)
    return {"ok": True, "date": target_date, "sample_count": metrics.get("sample_count", 0)}


def recompute_metrics(slug: str, cfg: dict, date: str) -> dict:
    pdir = G.project_dir(slug)
    path = pdir / "samples" / f"{date}.jsonl"
    rows = [r for r in dedup_rows(G.read_jsonl(path)) if r.get("ok")]
    metrics = {
        "slug": slug, "date": date, "generated_at": G.now_iso(),
        "question_count": len(cfg.get("questions", [])), "sample_count": len(rows),
        "platforms": aggregate(rows, cfg),
    }
    G.write_json(pdir / "metrics" / f"{date}.json", metrics)
    return metrics


def store_manual_rows(slug: str, cfg: dict, rows: list[dict]) -> dict:
    """人工/插件样本的统一落库：追加 jsonl → 去重 → 重算当日指标 → 竞品确认。"""
    pdir = G.project_dir(slug)
    path = pdir / "samples" / f"{G.today()}.jsonl"
    G.write_jsonl(path, G.read_jsonl(path) + rows)
    all_rows = [r for r in dedup_rows(G.read_jsonl(path)) if r.get("ok")]
    metrics = {
        "slug": slug, "date": G.today(), "generated_at": G.now_iso(),
        "question_count": len(cfg.get("questions", [])), "sample_count": len(all_rows),
        "platforms": aggregate(all_rows, cfg),
    }
    G.write_json(pdir / "metrics" / f"{G.today()}.json", metrics)
    confirm_competitors(slug, all_rows)
    return metrics


# 采样会话环境。和「API≠Web、Web≠App」同一个道理：登录态的个性化会改变答案，
# 不同环境采的样本不该混在一起算平均。插件每次回传都必须带上它。
SESSION_MODES = {
    "sandbox": ("一次性沙箱（无历史无 Cookie，未登录）", "A_人工真实样本"),
    "incognito": ("无痕未登录", "A_人工真实样本"),
    "clean_profile": ("专用采样 Profile（已登录，无自查历史）", "A_人工真实样本"),
    "personal": ("个人日常账号（含个性化，仅供参考）", "D_待复核"),
}


def collect_import(slug: str, records: list[dict]) -> dict:
    """浏览器插件回传的样本。与手动表同口径：A 级证据、web 终端；
    区别是 citations 由插件从页面结构化提取，比手抄更全。

    session_mode 决定证据等级：个人日常账号采的样本降级为 D_待复核——
    它测的是「AI 对你的画像」，不是「陌生买家看到什么」，不能当可见性证据用。"""
    cfg = G.load_config(slug)
    qmap = {q.get("id"): q["text"] for q in cfg.get("questions", [])}
    known = set(PROVIDERS) | set(MANUAL_ONLY)
    rows = []
    for r in records:
        plat = str(r.get("platform") or "").strip()
        answer = str(r.get("answer") or "").strip()
        if plat not in known or not answer:
            continue
        cites = [{"url": str(c.get("url", ""))[:500], "title": str(c.get("title", ""))[:200]}
                 for c in (r.get("citations") or []) if isinstance(c, dict) and c.get("url")][:30]
        sm = str(r.get("session_mode") or "incognito")
        sm = sm if sm in SESSION_MODES else "incognito"
        rec = {
            "date": G.today(), "ts": G.now_iso(),
            "platform": plat, "platform_name": label_of(plat), "market": market_of(plat),
            "terminal": "web", "sample_mode": "extension",
            "session_mode": sm, "session_label": SESSION_MODES[sm][0],
            "evidence_level": SESSION_MODES[sm][1], "search_enabled": True,
            "question_id": str(r.get("question_id") or "")[:32],
            "question": qmap.get(r.get("question_id"), str(r.get("question") or "")[:500]),
            "round": 1, "ok": True, "error": None,
            "answer": answer[:20000], "citations": cites,
            "page_url": str(r.get("page_url") or "")[:500],
        }
        rec["analysis"] = analyze_answer(rec["answer"], cfg, citations=cites)
        rec["needs_review"] = bool(rec["analysis"].get("needs_review"))
        rows.append(rec)
    if not rows:
        return {"ok": False, "imported": 0, "error": "没有可导入的样本（平台码未知或答案为空）"}
    metrics = store_manual_rows(slug, cfg, rows)
    G.info(f"插件回传导入 {len(rows)} 条样本")
    return {"ok": True, "imported": len(rows), "sample_count": metrics["sample_count"]}
