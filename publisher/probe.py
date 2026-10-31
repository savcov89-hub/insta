import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ig
t = os.environ["IG_TOKEN"].strip()
mid = "17915943825520164"
for name, path, p in [
    ("nested", mid, {"fields": "comments_count,comments{id,text,username}"}),
    ("edge-min", mid + "/comments", {}),
    ("edge-v21", "https://graph.instagram.com/v21.0/" + mid + "/comments", {"fields": "id,text,username"}),
    ("mentions", "me", {"fields": "id,username"}),
]:
    try:
        d = ig.call("GET", path, t, p)
        print(name, "OK", json.dumps(d, ensure_ascii=False)[:300])
    except ig.IGError as e:
        print(name, "ERR", e, (e.raw or "")[:300])
