"""Поиск залетевших роликов. job.txt: строки «режим аргумент».
  probe            — проверка, какие источники отвечают
  ig_reel URL      — данные одного рилс (yt-dlp)
  ig_user NAME     — список рилс аккаунта (yt-dlp)
  bd NAME          — Business Discovery через токен (graph.instagram.com)
  yt_search QUERY  — поиск YouTube Shorts с просмотрами
  yt_channel URL   — Shorts канала с просмотрами
Токен никогда не печатается."""
import json, os, subprocess, sys, time, re
import requests

out_dir = sys.argv[2]; os.makedirs(out_dir, exist_ok=True)
TOKEN = os.environ.get("IG_TOKEN", "").strip()

def scrub(s):
    return s.replace(TOKEN, "***") if TOKEN else s

def ytdlp(args, timeout=600):
    p = subprocess.run(["yt-dlp", "--no-warnings", "-J", *args], capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        return {"error": scrub(p.stderr[-1500:])}
    try:
        return json.loads(p.stdout)
    except Exception as e:
        return {"error": f"json {e}", "raw": p.stdout[:500]}

KEEP = ["id", "url", "webpage_url", "title", "description", "channel", "uploader", "uploader_id",
        "channel_follower_count", "view_count", "like_count", "comment_count", "duration", "timestamp", "upload_date"]

def slim(d):
    return {k: d.get(k) for k in KEEP if d.get(k) is not None}

def save(name, data):
    name = re.sub(r"[^\w.-]+", "_", name)[:80]
    with open(os.path.join(out_dir, name + ".json"), "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)

def ig_reel(url):
    d = ytdlp(["--no-playlist", url])
    return d if "error" in d else slim(d)

def ig_user(name):
    d = ytdlp(["--flat-playlist", "--playlist-end", "60", f"https://www.instagram.com/{name}/reels/"])
    if "error" in d: return d
    return {"entries": [slim(e) for e in d.get("entries", [])]}

def bd(name):
    if not TOKEN: return {"error": "нет IG_TOKEN"}
    fields = (f"business_discovery.username({name})" "{username,followers_count,media_count,"
              "media.limit(50){id,caption,media_product_type,like_count,comments_count,view_count,timestamp,permalink}}")
    res = {}
    for host in ["https://graph.instagram.com/v25.0", "https://graph.facebook.com/v25.0"]:
        try:
            me = requests.get(f"{host}/me", params={"fields": "id,user_id,username", "access_token": TOKEN}, timeout=30).json()
            uid = me.get("user_id") or me.get("id")
            r = requests.get(f"{host}/{uid}", params={"fields": fields, "access_token": TOKEN}, timeout=60)
            j = r.json()
            if "error" in j and "view_count" in json.dumps(j["error"]):
                j = requests.get(f"{host}/{uid}", params={"fields": fields.replace(",view_count", ""), "access_token": TOKEN}, timeout=60).json()
            res[host] = json.loads(scrub(json.dumps(j)))
        except Exception as e:
            res[host] = {"error": scrub(str(e))}
    return res

def yt(url_or_search, n=50):
    d = ytdlp(["--flat-playlist", "--playlist-end", str(n), url_or_search])
    if "error" in d: return d
    return {"title": d.get("title"), "channel_follower_count": d.get("channel_follower_count"),
            "entries": [slim(e) for e in d.get("entries", [])]}

for line in open(sys.argv[1], encoding="utf-8"):
    line = line.strip()
    if not line or line.startswith("#"): continue
    mode, _, arg = line.partition(" ")
    t = time.time()
    try:
        if mode == "ig_reel": data = ig_reel(arg)
        elif mode == "ig_user": data = ig_user(arg)
        elif mode == "bd": data = bd(arg)
        elif mode == "yt_search": data = yt(f"https://www.youtube.com/results?search_query={requests.utils.quote(arg)}&sp=EgIYAQ%253D%253D")
        elif mode == "yt_shorts": data = yt(arg.rstrip("/") + "/shorts")
        elif mode == "ig_tag": data = ytdlp(["--flat-playlist", "--playlist-end", "30", f"https://www.instagram.com/explore/tags/{arg}/"])
        else: data = {"error": "неизвестный режим"}
    except Exception as e:
        data = {"error": scrub(str(e))}
    data = {"job": line, "sec": round(time.time() - t, 1), "result": data}
    save(f"{mode}_{arg}", data)
    print(mode, arg, "→", "ошибка" if isinstance(data["result"], dict) and "error" in data["result"] else "ок", flush=True)
    time.sleep(3)
