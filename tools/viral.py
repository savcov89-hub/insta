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
FB = os.environ.get("FB_TOKEN", "").strip()

def scrub(s):
    for t in (TOKEN, FB):
        if t: s = s.replace(t, "***")
    return s

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

def yt_hot(query, top=8):
    """Shorts по запросу: список, затем полные данные (подписчики, дата) для самых просматриваемых."""
    d = yt(f"https://www.youtube.com/results?search_query={requests.utils.quote(query)}&sp=EgIYAQ%253D%253D", 60)
    if "error" in d: return d
    ents = [e for e in d["entries"] if re.search("[а-яА-Я]", e.get("title", "")) and e.get("view_count")]
    ents.sort(key=lambda e: -e["view_count"])
    full = []
    for e in ents[:top]:
        f = ytdlp(["--skip-download", "--no-playlist", e["url"]], 120)
        full.append(slim(f) if "error" not in f else {**e, "full_error": f["error"][-300:]})
        time.sleep(1.5)
    return {"query": query, "all": ents, "top": full}

GRAPH = "https://graph.facebook.com/v25.0"
MEDIA = "id,caption,media_product_type,like_count,comments_count,timestamp,permalink"

def fb_get(path, **params):
    params["access_token"] = FB
    r = requests.get(f"{GRAPH}/{path}", params=params, timeout=60)
    return json.loads(scrub(r.text))

def bd_scan(list_file):
    """Конкуренты через Business Discovery (вход через Facebook). Рилс, набравшие в разы больше нормы автора."""
    if not FB: return {"error": "нет секрета FB_TOKEN"}
    acc = fb_get("me/accounts", fields="name,instagram_business_account{id,username}")
    ig = next((a["instagram_business_account"]["id"] for a in acc.get("data", []) if a.get("instagram_business_account")), None)
    if not ig: return {"error": "к странице не привязан Instagram", "accounts": acc}
    names = [l.split("#")[0].strip().lstrip("@") for l in open(list_file, encoding="utf-8")]
    names = [n for n in names if n]
    now = time.time(); accounts = []; hits = []; mentions = {}
    for n in names:
        q = f"business_discovery.username({n}){{username,name,followers_count,media_count,media.limit(60){{{MEDIA}}}}}"
        j = fb_get(ig, fields=q)
        if "error" in j:
            accounts.append({"username": n, "error": j["error"].get("message", "")[:200]}); time.sleep(1); continue
        b = j["business_discovery"]; media = b.get("media", {}).get("data", [])
        reels = [m for m in media if m.get("media_product_type") == "REELS" and m.get("like_count") is not None]
        likes = sorted(m["like_count"] for m in reels)
        med = likes[len(likes)//2] if likes else 0
        accounts.append({"username": b["username"], "followers": b.get("followers_count"), "reels": len(reels), "median_likes": med})
        for m in reels:
            for h in re.findall(r"@([A-Za-z0-9_.]{3,30})", m.get("caption") or ""):
                h = h.rstrip(".").lower()
                if h not in names: mentions[h] = mentions.get(h, 0) + 1
            try:
                age = (now - time.mktime(time.strptime(m["timestamp"][:19], "%Y-%m-%dT%H:%M:%S"))) / 86400
            except Exception:
                age = 999
            x = m["like_count"] / max(med, 1)
            if age <= 120 and x >= 3 and m["like_count"] >= 500:
                hits.append({"username": b["username"], "followers": b.get("followers_count"), "likes": m["like_count"],
                             "comments": m.get("comments_count"), "x_norm": round(x, 1), "age_days": round(age),
                             "permalink": m.get("permalink"), "caption": (m.get("caption") or "")[:400]})
        time.sleep(1)
    hits.sort(key=lambda h: -h["x_norm"])
    md = ["# Залетевшие рилс конкурентов", "", "лайки / медиана лайков автора (x), за 120 дней", ""]
    for h in hits:
        cap = " ".join(h["caption"].split())[:160]
        md.append(f"- x{h['x_norm']} · {h['likes']} лайков · {h['comments']} комм. · @{h['username']} ({h['followers']} подп.) · {h['age_days']} дн. · {h['permalink']}\n  {cap}")
    md += ["", "## Новые аккаунты из подписей (кандидаты)", ""] + [f"- {k} ({v})" for k, v in sorted(mentions.items(), key=lambda kv: -kv[1])[:60]]
    open(os.path.join(out_dir, "bd_top.md"), "w").write("\n".join(md))
    return {"accounts": accounts, "hits": hits, "mentions": mentions}

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
        elif mode == "bd_scan": data = bd_scan(arg)
        elif mode == "yt_hot": data = yt_hot(arg)
        elif mode == "yt_list": data = yt_hot(arg, top=0)
        elif mode == "yt_shorts": data = yt(arg.rstrip("/") + "/shorts")
        elif mode == "ig_tag": data = ytdlp(["--flat-playlist", "--playlist-end", "30", f"https://www.instagram.com/explore/tags/{arg}/"])
        else: data = {"error": "неизвестный режим"}
    except Exception as e:
        data = {"error": scrub(str(e))}
    data = {"job": line, "sec": round(time.time() - t, 1), "result": data}
    save(f"{mode}_{arg}", data)
    print(mode, arg, "→", "ошибка" if isinstance(data["result"], dict) and "error" in data["result"] else "ок", flush=True)
    time.sleep(3)
