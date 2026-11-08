"""Сбор комментариев и отправка одобренных ответов против поддельного Instagram API.

  python3 -m unittest discover -s tests -v
"""
import http.server
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOKEN = "IGAAtestTOKEN1234567890"


class Fake(http.server.BaseHTTPRequestHandler):
    calls = []
    comments = {}

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, q):
        path = urllib.parse.urlparse(self.path).path
        Fake.calls.append((self.command, path, q))
        if path == "/v25.0/me":
            return self._send(200, {"user_id": "1789", "username": "fitness_savtsov"})
        if path.endswith("/comments"):
            return self._send(200, {"data": Fake.comments.get(path.split("/")[2], [])})
        if path.endswith("/replies") and self.command == "POST":
            return self._send(200, {"id": "r-new"})
        return self._send(404, {"error": {"message": "unknown"}})

    def do_GET(self):
        self._handle({k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self._handle({k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(n).decode()).items()})


class CommentsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        Fake.calls.clear()
        self.dir = pathlib.Path(tempfile.mkdtemp())
        shutil.copytree(ROOT / "publisher", self.dir / "publisher")
        (self.dir / "stats").mkdir()
        (self.dir / "stats" / "reels.json").write_text(json.dumps({"reels": {
            "m1": {"timestamp": "2026-11-07T07:00:00+0000", "comments_count": 3, "caption": "Хук\nтекст",
                   "permalink": "https://www.instagram.com/reel/X/", "queue_id": "kruglaya-v1-0711"},
            "m2": {"timestamp": "2026-11-07T07:00:00+0000", "comments_count": 0},
        }}))
        Fake.comments = {"m1": [
            {"id": "c1", "username": "anna", "text": "А сколько подходов?", "timestamp": "2026-11-07T10:00:00+0000",
             "replies": {"data": [{"id": "c1r", "username": "fitness_savtsov", "text": "3–4",
                                   "timestamp": "2026-11-07T11:00:00+0000"}]}},
            {"id": "c2", "username": "ivan", "text": "Бред", "timestamp": "2026-11-07T12:00:00+0000"},
            {"id": "c3", "username": "olga", "text": "А мне?", "timestamp": "2026-11-07T12:30:00+0000",
             "replies": {"data": [{"id": "c3a", "username": "fitness_savtsov", "text": "Что именно?",
                                   "timestamp": "2026-11-07T13:00:00+0000"},
                                  {"id": "c3b", "username": "olga", "text": "Колени",
                                   "timestamp": "2026-11-07T14:00:00+0000"}]}},
        ]}

    def tearDown(self):
        shutil.rmtree(self.dir)

    def run_py(self, name, **env):
        e = {**os.environ, "IG_TOKEN": TOKEN, "IG_API": f"http://127.0.0.1:{self.srv.server_address[1]}",
             "GIT_PUSH": "0", "WINDOW_DAYS": "100000", **env}
        r = subprocess.run([sys.executable, "-I", str(self.dir / "publisher" / name)], env=e,
                           capture_output=True, text=True, timeout=60)
        self.assertNotIn(TOKEN, r.stdout + r.stderr, "токен попал в лог")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def data(self, name):
        return json.loads((self.dir / "comments" / name).read_text())

    def test_collects_and_marks_waiting(self):
        r = self.run_py("comments.py")
        d = self.data("comments.json")["comments"]
        self.assertFalse(d["c1"]["need_reply"])      # Михаил уже ответил
        self.assertTrue(d["c2"]["need_reply"])
        self.assertTrue(d["c3"]["need_reply"])       # последнее слово за Ольгой
        self.assertEqual(d["c3"]["last_id"], "c3b")
        self.assertEqual(d["c2"]["caption"], "Хук")
        self.assertNotIn("Бред", r.stdout)            # тексты в публичный лог не попадают
        self.assertFalse(any(c[1].startswith("/v25.0/m2") for c in Fake.calls))   # пустые ролики не трогаем
        self.assertFalse(any(c[0] == "POST" for c in Fake.calls))

    def test_author_in_from_and_emoji(self):
        Fake.comments = {"m1": [
            {"id": "d1", "from": {"id": "77", "username": "kate"}, "text": "А колени?", "timestamp": "2026-11-07T10:00:00+0000",
             "replies": {"data": [{"id": "d1r", "from": {"id": "1789", "username": "fitness_savtsov"}, "text": "Можно",
                                   "timestamp": "2026-11-07T11:00:00+0000"}]}},
            {"id": "d2", "text": "🔥🔥🔥", "timestamp": "2026-11-07T12:00:00+0000"},
        ]}
        self.run_py("comments.py")
        d = self.data("comments.json")["comments"]
        self.assertEqual(d["d1"]["username"], "kate")
        self.assertFalse(d["d1"]["need_reply"])
        self.assertFalse(d["d2"]["need_reply"])

    def test_empty_answer_rereads_tomorrow(self):
        Fake.comments = {}
        r = self.run_py("comments.py", WINDOW_DAYS="0")
        self.assertIn("режиме разработки", r.stdout)
        self.assertNotIn("m1", self.data("comments.json")["seen"])

    def test_reply_only_approved_and_listed(self):
        self.run_py("comments.py")
        (self.dir / "comments" / "drafts.json").write_text(json.dumps({
            "c2": {"reply": "Почему?", "status": "approved"},
            "c3": {"reply": "Про колени — так", "status": "draft"},
        }, ensure_ascii=False))
        self.run_py("reply.py", IDS="c3")
        self.assertFalse(any(c[0] == "POST" for c in Fake.calls))   # c3 не одобрен
        self.run_py("reply.py", IDS="c2")
        posts = [c for c in Fake.calls if c[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][1], "/v25.0/c2/replies")
        self.assertEqual(posts[0][2]["message"], "Почему?")
        self.assertEqual(self.data("drafts.json")["c2"]["status"], "posted")


if __name__ == "__main__":
    unittest.main()
