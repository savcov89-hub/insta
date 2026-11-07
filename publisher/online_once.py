"""Разовый замер: в какие часы подписчики онлайн (online_followers) за последние 30 дней."""
import os, sys, json, time, datetime as dt
sys.path.insert(0, os.path.dirname(__file__))
import ig
tok = os.environ["IG_TOKEN"]
uid = ig.me(tok)["user_id"]
now = int(time.time())
out = []
for d in range(1, 31):
    until = now - (d - 1) * 86400
    since = until - 86400
    try:
        r = ig.call("GET", f"{uid}/insights", tok, {"metric": "online_followers", "period": "lifetime", "since": since, "until": until})
        out.append({"since": since, "until": until, "data": r.get("data")})
    except ig.IGError as e:
        out.append({"since": since, "until": until, "error": str(e), "code": e.args[1:] if len(e.args) > 1 else None})
for o in out:
    print("ROW", json.dumps(o, ensure_ascii=False))
try:
    r = ig.call("GET", f"{uid}/insights", tok, {"metric": "online_followers", "period": "lifetime"})
    print("PLAIN", json.dumps(r, ensure_ascii=False))
except ig.IGError as e:
    print("PLAIN_ERR", str(e))
