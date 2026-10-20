"""Минимальный клиент Instagram API (вход через Instagram, graph.instagram.com).

Только стандартная библиотека Python: в GitHub Actions ничего ставить не нужно.
Токен никогда не печатается: в ошибках остаётся только ответ Instagram.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

API = os.environ.get("IG_API", "https://graph.instagram.com").rstrip("/")
VERSION = os.environ.get("IG_API_VERSION", "v25.0")

# Подкоды ошибок Instagram, которые обрабатываем отдельно
TRIAL_LIMIT = 2207078      # «You reached maximum number of trial reels…» — потолок пробных
TRIAL_UNSUPPORTED = 2207081  # аккаунту недоступны пробные Reels


def scrub(text):
    """Убирает токен из любого текста перед выводом: логи Actions в публичном репозитории видны всем."""
    text = str(text)
    for t in {os.environ.get("IG_TOKEN", "").strip(), os.environ.get("NEW_IG_TOKEN", "").strip()}:
        if len(t) > 8:
            text = text.replace(t, "***").replace(urllib.parse.quote(t, safe=""), "***")
    return text


class IGError(Exception):
    def __init__(self, message, code=None, subcode=None, raw=None):
        super().__init__(scrub(message))
        self.code = code
        self.subcode = subcode
        self.raw = scrub(raw) if raw else raw

    @property
    def trial_limit(self):
        return self.subcode == TRIAL_LIMIT or str(TRIAL_LIMIT) in (self.raw or "")

    def __str__(self):
        parts = [super().__str__()]
        if self.code is not None:
            parts.append(f"code={self.code}")
        if self.subcode is not None:
            parts.append(f"subcode={self.subcode}")
        return " ".join(parts)


def _url(path):
    return path if path.startswith("http") else f"{API}/{VERSION}/{path.lstrip('/')}"


def call(method, path, token, params=None, timeout=120, retries=2):
    """GET/POST к Graph API. Сетевые сбои и 5xx повторяются, ошибки Instagram — нет."""
    params = dict(params or {})
    params["access_token"] = token
    url = _url(path)
    data = None
    if method == "GET":
        url += "?" + urllib.parse.urlencode(params)
    else:
        data = urllib.parse.urlencode(params).encode()
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            try:
                err = json.loads(raw).get("error", {})
            except ValueError:
                err = {}
            if e.code >= 500 and attempt < retries and not err:
                time.sleep(5 * (attempt + 1))
                continue
            msg = err.get("error_user_msg") or err.get("message") or f"HTTP {e.code}"
            raise IGError(msg, err.get("code"), err.get("error_subcode"), raw) from None
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < retries:
                time.sleep(5 * (attempt + 1))
                continue
            raise IGError(f"сеть: {type(e).__name__}") from None


def me(token):
    return call("GET", "me", token, {"fields": "user_id,username,account_type"})


def publishing_limit(token, user_id):
    data = call("GET", f"{user_id}/content_publishing_limit", token,
                {"fields": "quota_usage,config"}).get("data", [])
    return data[0] if data else {}


def create_reel(token, user_id, video_url, caption="", trial=True,
                graduation="MANUAL", cover_ms=None, share_to_feed=True):
    """Создаёт контейнер Reels. Пока не вызван publish, в Instagram ничего не видно."""
    params = {"media_type": "REELS", "video_url": video_url}
    if caption:
        params["caption"] = caption
    if cover_ms is not None:
        params["thumb_offset"] = str(int(cover_ms))
    if trial:
        params["trial_params"] = json.dumps({"graduation_strategy": graduation})
    else:
        params["share_to_feed"] = "true" if share_to_feed else "false"
    return call("POST", f"{user_id}/media", token, params)["id"]


def container_status(token, container_id):
    d = call("GET", container_id, token, {"fields": "status_code,status"})
    return d.get("status_code"), d.get("status")


def wait_ready(token, container_id, timeout=1200, every=int(os.environ.get("IG_POLL_SEC", "10"))):
    """Ждёт, пока Instagram обработает видео. Возвращает FINISHED или PUBLISHED."""
    deadline = time.time() + timeout
    while True:
        code, status = container_status(token, container_id)
        if code in ("FINISHED", "PUBLISHED"):
            return code
        if code in ("ERROR", "EXPIRED"):
            raise IGError(f"контейнер {code}: {status}", raw=status)
        if time.time() > deadline:
            raise IGError(f"видео обрабатывается дольше {timeout // 60} мин (статус {code})")
        time.sleep(every)


def publish(token, user_id, container_id):
    return call("POST", f"{user_id}/media_publish", token, {"creation_id": container_id})["id"]


def permalink(token, media_id):
    try:
        return call("GET", media_id, token, {"fields": "permalink"}).get("permalink")
    except IGError:
        return None


def refresh_token(token):
    """Продлевает долгоживущий токен ещё на 60 дней (токену должно быть больше суток)."""
    return call("GET", f"{API}/refresh_access_token", token, {"grant_type": "ig_refresh_token"})
