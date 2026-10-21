"""Прогон publisher/run.py против поддельного Instagram API. Ничего наружу не отправляет.

  python3 -m unittest discover -s tests -v
"""
import datetime as dt
import http.server
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOKEN = "IGAAtestTOKEN1234567890"


class FakeIG(http.server.BaseHTTPRequestHandler):
    calls = []
    polls = {}
    cap = False
    insights_ok = {"views", "reach", "likes", "saved", "shares", "reels_skip_rate", "ig_reels_avg_watch_time"}
    feed = []          # ролики ленты для /me/media
    gone = set()       # удалённые ролики
    no_insights = False

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def _params(self):
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        if self.command == "POST":
            n = int(self.headers.get("Content-Length", 0))
            q.update(urllib.parse.parse_qsl(self.rfile.read(n).decode()))
        return u.path, q

    def handle_any(self):
        path, q = self._params()
        FakeIG.calls.append((self.command, path, q, time.time()))
        if q.get("access_token") != TOKEN:
            return self._send(400, {"error": {"message": "Invalid OAuth access token", "code": 190}})
        if path == "/v25.0/me/media":
            return self._send(200, {"data": FakeIG.feed, "paging": {"cursors": {"after": "x"}}})
        if path.endswith("/insights"):
            mid = path.split("/")[-2]
            if FakeIG.no_insights:
                return self._send(403, {"error": {"message": "(#10) Application does not have permission for this action", "code": 10}})
            if mid in FakeIG.gone:
                return self._send(400, {"error": {"message": f"Unsupported get request. Object with ID '{mid}' does not exist", "code": 100, "error_subcode": 33}})
            want = q["metric"].split(",")
            bad = [m for m in want if m not in FakeIG.insights_ok]
            if bad:
                return self._send(400, {"error": {"message": f"(#100) The Media Insights API does not support the {bad[0]} metric for this media product type.", "code": 100}})
            return self._send(200, {"data": [{"name": m, "period": "lifetime", "values": [{"value": len(m) * 10}]} for m in want]})
        if path == "/v25.0/me":
            return self._send(200, {"user_id": "1789", "username": "fitness_savtsov", "account_type": "BUSINESS"})
        if path == "/v25.0/1789/media":
            n = sum(1 for c in FakeIG.calls if c[1] == path)
            return self._send(200, {"id": f"c{n}"})
        if path == "/v25.0/1789/media_publish":
            if FakeIG.cap:
                return self._send(400, {"error": {"message": "You reached maximum number of trial reels that is allowed to be published by Content Publishing API.", "code": 9, "error_subcode": 2207078}})
            return self._send(200, {"id": "m" + q["creation_id"]})
        if path.startswith("/v25.0/c"):
            cid = path.rsplit("/", 1)[1]
            FakeIG.polls[cid] = FakeIG.polls.get(cid, 0) + 1
            published = any(c[1] == "/v25.0/1789/media_publish" and c[2].get("creation_id") == cid and not FakeIG.cap
                            for c in FakeIG.calls)
            code = "PUBLISHED" if published else ("FINISHED" if FakeIG.polls[cid] > 1 else "IN_PROGRESS")
            return self._send(200, {"status_code": code, "status": code})
        if path.startswith("/v25.0/m"):
            return self._send(200, {"permalink": f"https://www.instagram.com/reel/{path.rsplit('/', 1)[1]}/"})
        return self._send(404, {"error": {"message": "unknown path " + path}})

    do_GET = handle_any
    do_POST = handle_any


class PublisherTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeIG)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        FakeIG.calls.clear()
        FakeIG.polls.clear()
        FakeIG.cap = False
        FakeIG.feed, FakeIG.gone, FakeIG.no_insights = [], set(), False
        self.dir = pathlib.Path(tempfile.mkdtemp())
        shutil.copytree(ROOT / "publisher", self.dir / "publisher")
        (self.dir / "queue").mkdir()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def add(self, item_id, delta_sec, **kw):
        when = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=delta_sec)
        item = {"publish_at": when.isoformat(), "video_url": "https://example.com/v.mp4",
                "caption": "Подпись #тест", **kw}
        (self.dir / "queue" / f"{item_id}.json").write_text(json.dumps(item, ensure_ascii=False))
        return when

    def run_pub(self, **env):
        e = {**os.environ, "IG_TOKEN": TOKEN, "IG_API": f"http://127.0.0.1:{self.srv.server_address[1]}",
             "IG_POLL_SEC": "0", "GIT_PUSH": "0", **env}
        r = subprocess.run([sys.executable, str(self.dir / "publisher" / "run.py")], env=e,
                           capture_output=True, text=True, timeout=120)
        self.assertNotIn(TOKEN, r.stdout + r.stderr, "токен попал в лог")
        return r

    def status(self, item_id):
        p = self.dir / "status" / f"{item_id}.json"
        return json.loads(p.read_text()) if p.exists() else None

    def test_publishes_trial_on_time(self):
        when = self.add("a1", 4, cover_ms=1500)
        r = self.run_pub()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        st = self.status("a1")
        self.assertEqual(st["state"], "published")
        self.assertEqual(st["permalink"], "https://www.instagram.com/reel/mc1/")
        create = [c for c in FakeIG.calls if c[1] == "/v25.0/1789/media"][0][2]
        self.assertEqual(create["media_type"], "REELS")
        self.assertEqual(json.loads(create["trial_params"]), {"graduation_strategy": "MANUAL"})
        self.assertEqual(create["thumb_offset"], "1500")
        self.assertEqual(create["caption"], "Подпись #тест")
        self.assertNotIn("share_to_feed", create)
        pub_t = [c[3] for c in FakeIG.calls if c[1] == "/v25.0/1789/media_publish"][0]
        self.assertGreaterEqual(pub_t, when.timestamp() - 0.5, "опубликовал раньше времени")
        self.assertLess(pub_t, when.timestamp() + 5)

    def test_calendar_summary(self):
        self.add("a1", 1)
        self.add("later", 3 * 3600, caption="Потом")
        self.assertEqual(self.run_pub().returncode, 0)
        cal = json.loads((self.dir / "status" / "_calendar.json").read_text())
        rows = {r["id"]: r for r in cal["rows"]}
        self.assertEqual(rows["a1"]["st"]["state"], "published")
        self.assertEqual(rows["later"]["st"], {})
        self.assertEqual(rows["later"]["q"]["caption"], "Потом")
        self.assertNotIn(TOKEN, json.dumps(cal))

    def run_stats(self, **env):
        e = {**os.environ, "IG_TOKEN": TOKEN, "IG_API": f"http://127.0.0.1:{self.srv.server_address[1]}",
             "GIT_PUSH": "0", **env}
        r = subprocess.run([sys.executable, str(self.dir / "publisher" / "stats.py")], env=e,
                           capture_output=True, text=True, timeout=120)
        self.assertNotIn(TOKEN, r.stdout + r.stderr, "токен попал в лог")
        return r

    def test_stats_collects_ours_and_feed(self):
        self.add("a1", 1)
        self.assertEqual(self.run_pub().returncode, 0)
        FakeIG.feed = [{"id": "mf1", "media_type": "VIDEO", "media_product_type": "REELS",
                        "timestamp": "2026-10-19T11:00:00+0000", "permalink": "https://www.instagram.com/reel/F1/",
                        "caption": "Из приложения\nвторая строка", "like_count": 5},
                       {"id": "mg1", "media_type": "IMAGE", "media_product_type": "FEED"},
                       {"id": "mx1", "media_type": "VIDEO", "media_product_type": "REELS"}]
        FakeIG.gone = {"mx1"}
        r = self.run_stats()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("недоступны: comments, crossposted_views", r.stdout)
        self.assertNotIn(str(len("views") * 10), r.stdout.replace("[", " "), "цифры попали в лог")
        out = json.loads((self.dir / "stats" / "reels.json").read_text())
        ours = out["reels"]["mc1"]
        self.assertEqual(ours["queue_id"], "a1")
        self.assertTrue(ours["trial"])
        self.assertEqual(ours["insights"]["views"], 50)
        self.assertEqual(ours["insights"]["reels_skip_rate"], len("reels_skip_rate") * 10)
        self.assertEqual(out["reels"]["mf1"]["caption"], "Из приложения")
        self.assertNotIn("mg1", out["reels"])
        self.assertNotIn("mx1", out["reels"], "удалённый незнакомый ролик не нужен")
        hist = [json.loads(l) for l in (self.dir / "stats" / "history.jsonl").read_text().splitlines()]
        self.assertIn("mc1", {h.get("id") for h in hist})
        # ролик удалили: остаются последние цифры с пометкой
        FakeIG.gone = {"mc1"}
        r = self.run_stats()
        self.assertEqual(r.returncode, 0, r.stdout)
        out = json.loads((self.dir / "stats" / "reels.json").read_text())
        self.assertTrue(out["reels"]["mc1"]["deleted"])
        self.assertEqual(out["reels"]["mc1"]["insights"]["views"], 50)
        self.assertIn("reach", out["reels"]["mf1"]["insights"], "удалённый ролик испортил список метрик")

    def test_stats_without_permission(self):
        self.add("a1", 1)
        self.run_pub()
        FakeIG.no_insights = True
        r = self.run_stats()
        self.assertEqual(r.returncode, 2)
        self.assertIn("instagram_business_manage_insights", r.stdout)
        self.assertFalse((self.dir / "stats").exists())

    def test_regular_reel(self):
        self.add("r1", 1, trial=False)
        self.assertEqual(self.run_pub().returncode, 0)
        create = [c for c in FakeIG.calls if c[1] == "/v25.0/1789/media"][0][2]
        self.assertNotIn("trial_params", create)
        self.assertEqual(create["share_to_feed"], "true")

    def test_future_and_missed(self):
        self.add("later", 3 * 3600)
        self.add("old", -4 * 3600)
        r = self.run_pub()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIsNone(self.status("later"))
        self.assertEqual(self.status("old")["state"], "missed")
        self.assertFalse([c for c in FakeIG.calls if c[1].endswith("/media")])

    def test_without_token_waits_quietly(self):
        self.add("soon", 1)
        r = self.run_pub(IG_TOKEN="")
        self.assertEqual(r.returncode, 0)
        self.assertIn("нет секрета IG_TOKEN", r.stdout)
        self.assertIsNone(self.status("soon"))

    def test_trial_cap_waits_and_reuses_container(self):
        self.add("cap", 1)
        FakeIG.cap = True
        r = self.run_pub()
        self.assertEqual(r.returncode, 1)
        st = self.status("cap")
        self.assertEqual(st["state"], "cap_wait")
        self.assertIn("retry_after", st)
        # через час лимит отпустил: тот же контейнер, без повторной загрузки
        st["retry_after"] = dt.datetime.now(dt.timezone.utc).isoformat()
        (self.dir / "status" / "cap.json").write_text(json.dumps(st))
        r = self.run_pub()  # лимит ещё держит: повтор не красит запуск
        self.assertEqual(r.returncode, 0, r.stdout)
        st = self.status("cap")
        self.assertEqual(st["state"], "cap_wait")
        st["retry_after"] = dt.datetime.now(dt.timezone.utc).isoformat()
        (self.dir / "status" / "cap.json").write_text(json.dumps(st))
        FakeIG.cap = False
        FakeIG.calls.clear()
        r = self.run_pub()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.status("cap")["state"], "published")
        self.assertFalse([c for c in FakeIG.calls if c[1] == "/v25.0/1789/media"], "загрузил видео второй раз")

    def test_own_trial_cap_counter(self):
        now = dt.datetime.now(dt.timezone.utc)
        (self.dir / "status").mkdir()
        for i in range(2):
            self.add(f"done{i}", -3600)
            (self.dir / "status" / f"done{i}.json").write_text(json.dumps(
                {"state": "published", "published_at": (now - dt.timedelta(hours=1)).isoformat()}))
        self.add("third", 1)
        r = self.run_pub(TRIAL_CAP="2")
        self.assertEqual(r.returncode, 1)
        st = self.status("third")
        self.assertEqual(st["state"], "cap_wait")
        self.assertFalse([c for c in FakeIG.calls if c[1].endswith("media_publish")])

    def test_crash_after_publish_is_not_republished(self):
        self.add("x", 1)
        (self.dir / "status").mkdir()
        (self.dir / "status" / "x.json").write_text(json.dumps({"state": "uploading", "container_id": "c9"}))
        FakeIG.calls.append(("POST", "/v25.0/1789/media_publish", {"creation_id": "c9"}, 0))
        r = self.run_pub()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.status("x")["state"], "published")
        pubs = [c for c in FakeIG.calls if c[1].endswith("media_publish")]
        self.assertEqual(len(pubs), 1, "опубликовал второй раз")

    def test_bad_token_reports_without_leaking(self):
        self.add("t", 1)
        r = self.run_pub(IG_TOKEN="IGAAwrongTOKEN99999999")
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("IGAAwrongTOKEN99999999", r.stdout + r.stderr)
        self.assertIn("Invalid OAuth", r.stdout)

    def test_dry_run_sends_nothing(self):
        self.add("d", 1)
        r = self.run_pub(DRY_RUN="1")
        self.assertEqual(r.returncode, 0)
        self.assertFalse(FakeIG.calls)
        self.assertIsNone(self.status("d"))
        self.assertFalse((self.dir / "status" / "_calendar.json").exists())


if __name__ == "__main__":
    unittest.main()
