import unittest
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import geolib as G
import crawl


class FakeResp:
    """模拟 requests.get 的流式响应，够 fetch 用即可。"""

    def __init__(self, status, body=b"<html>ok</html>", ctype="text/html"):
        self.status_code = status
        self.headers = {"Content-Type": ctype}
        self.url = "http://x.test/"
        self._body = body
        self.encoding = "utf-8"

    def iter_content(self, n):
        yield self._body

    def close(self):
        pass


class TestFetchRetry(unittest.TestCase):
    def _run(self, responses, retries=1):
        with mock.patch.object(G.requests, "get", side_effect=responses) as get, \
             mock.patch.object(G.time, "sleep"):
            res = G.fetch("http://x.test/", retries=retries)
        return res, get.call_count

    def test_retry_500_then_200(self):
        res, calls = self._run([FakeResp(500), FakeResp(200)])
        self.assertEqual(res["status"], 200)
        self.assertEqual(calls, 2)

    def test_retry_429(self):
        res, calls = self._run([FakeResp(429), FakeResp(200)])
        self.assertEqual(res["status"], 200)
        self.assertEqual(calls, 2)

    def test_no_retry_404(self):
        res, calls = self._run([FakeResp(404)])
        self.assertEqual(res["status"], 404)
        self.assertEqual(calls, 1)

    def test_retry_exhausted_returns_last(self):
        res, calls = self._run([FakeResp(500), FakeResp(500)])
        self.assertEqual(res["status"], 500)
        self.assertEqual(calls, 2)


class TestCrawlHealth(unittest.TestCase):
    def _pages(self, statuses):
        return [{"status": s} for s in statuses]

    def test_all_dead_dies(self):
        with self.assertRaises(SystemExit):
            crawl.check_crawl_health(self._pages([0, 0, 0]))

    def test_low_ok_ratio_dies(self):
        with self.assertRaises(SystemExit):
            crawl.check_crawl_health(self._pages([200] + [0] * 9))

    def test_healthy_passes(self):
        crawl.check_crawl_health(self._pages([200] * 5))
        crawl.check_crawl_health(self._pages([200] + [0] * 4))  # 20% 刚好达标


class TestWordCountKana(unittest.TestCase):
    def test_pure_kana_counts(self):
        self.assertGreater(G.word_count("これはテストです"), 0)

    def test_cjk_unchanged(self):
        self.assertGreater(G.word_count("这是一个测试"), 0)


class TestCrawlerRoles(unittest.TestCase):
    def test_training_block_does_not_imply_search_block(self):
        grouped = crawl.group_bots_by_role(["GPTBot", "ClaudeBot", "Google-Extended"])
        self.assertEqual(grouped["search"], [])
        self.assertEqual(grouped["user"], [])
        self.assertEqual(grouped["training"], ["GPTBot", "ClaudeBot", "Google-Extended"])

    def test_search_and_user_bots_are_separate(self):
        grouped = crawl.group_bots_by_role(["OAI-SearchBot", "Perplexity-User"])
        self.assertEqual(grouped["search"], ["OAI-SearchBot"])
        self.assertEqual(grouped["user"], ["Perplexity-User"])

    def test_llms_check_ignores_training_only_robots_block(self):
        robots = "User-agent: GPTBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n"
        llms = "# Example\n- https://example.com/about"
        with mock.patch.object(G, "fetch", return_value={"status": 200}):
            result = crawl.check_llms_txt("https://example.com", llms, robots)
        self.assertEqual(result["robots_blocked"], [])


class TestLocaleStratifiedCandidates(unittest.TestCase):
    def test_truncated_bilingual_pool_is_balanced(self):
        ranked = ["https://example.com/"]
        ranked.extend(f"https://example.com/tool-{i}" for i in range(9))
        ranked.extend(f"https://example.com/zh/tool-{i}" for i in range(10))
        selected = crawl.stratified_candidates(ranked, 6)
        counts = {"default": 0, "zh": 0}
        for url in selected:
            counts[crawl.locale_bucket(url)] += 1
        self.assertEqual(counts, {"default": 3, "zh": 3})

    def test_non_locale_two_letter_product_prefix_stays_default(self):
        self.assertEqual(crawl.locale_bucket("https://example.com/ai/tools"), "default")
        self.assertEqual(crawl.locale_bucket("https://example.com/zh/tools"), "zh")


if __name__ == "__main__":
    unittest.main()

class RegisteredCoreTests(unittest.TestCase):
 def test_core_not_dropped_by_generic_rank_or_locale(self):
  root='https://example.com';core=root+'/ai-video-generator'
  pool=crawl.rank([root+'/pricing',root+'/about',root+'/zh/about',core],root)
  result=crawl.select_candidates([root,core],root,pool,3)
  self.assertIn(core,result);self.assertIn(root,result);self.assertEqual(len(result),3)
 def test_core_capacity_fails_explicitly(self):
  with self.assertRaises(ValueError):crawl.select_candidates(['https://x.test/core'],'https://x.test',['https://x.test'],1)


class TestGradeCrawler(unittest.TestCase):
    def _site(self, blocked=(), denied=()):
        return {"ai_bots_blocked_by_role": crawl.group_bots_by_role(list(blocked)),
                "ai_ua_probe_denied": list(denied), "ai_ua_probe_verified": False}

    def test_search_robots_block_is_p0(self):
        out = crawl.grade_crawler(self._site(blocked=["OAI-SearchBot"]), crawl.ALLOW_ALL)
        self.assertEqual([(f["level"], f["code"]) for f in out], [("P0", "SEARCH_ROBOTS_BLOCK")])

    def test_user_agent_denial_is_p1_with_data_gap(self):
        out = crawl.grade_crawler(self._site(denied=["Claude-User"]), crawl.ALLOW_ALL)
        self.assertEqual([f["code"] for f in out], ["UA_PROBE_DENIED_SEARCH_USER", "UNVERIFIED_WAF_SIGNAL"])

    def test_training_denial_respects_policy(self):
        policy = {"search": "allow", "user": "allow", "training": "deny"}
        out = crawl.grade_crawler(self._site(blocked=["GPTBot"], denied=["Bytespider"]), policy)
        self.assertEqual([f["code"] for f in out], ["UNVERIFIED_WAF_SIGNAL"])

    def test_probe_covers_every_user_bot(self):
        probed = {b for b in crawl.AI_UA_PROBES if crawl.AI_BOT_ROLES[b] == "user"}
        self.assertEqual(probed, {"ChatGPT-User", "Claude-User", "Perplexity-User"})

    def test_probe_fails_when_homepage_unreachable(self):
        with mock.patch.object(G, "fetch_text", return_value=""), \
             mock.patch.object(G, "fetch", return_value={"status": 503, "html": ""}):
            result = crawl.probe("https://example.com/")
        self.assertFalse(result["pass"])
        self.assertEqual(result["findings"][0]["code"], "HOMEPAGE_UNREACHABLE")
