#!/usr/bin/env python3
"""Отправляет в Instagram ответы на комментарии, которые Михаил одобрил. Только по кнопке, без расписания.

Отправляется ответ, только если выполнены оба условия:
  1. id комментария передан в IDS (запуск workflow reply-comments с полем ids);
  2. в comments/drafts.json у него status "approved" (ставится только после «да» Михаила)
     или запуск сделан кнопкой «Ответить» в календаре (APPROVE=1): нажатие кнопки и есть его одобрение.
     Кнопка может передать исправленный текст (TEXT) — тогда уходит он.
После отправки status становится "posted", в черновик пишутся id ответа и время.

Переменные окружения:
  IG_TOKEN   токен Instagram
  IDS        id комментариев через запятую
  DRY_RUN=1  ничего не отправлять, только показать, что ушло бы
"""
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ig  # noqa: E402
from comments import OUT, commit_push, log, read  # noqa: E402


def main():
    token = os.environ.get("IG_TOKEN", "").strip()
    ids = [i.strip() for i in os.environ.get("IDS", "").replace(" ", ",").split(",") if i.strip()]
    dry = os.environ.get("DRY_RUN") == "1"
    button = os.environ.get("APPROVE") == "1"
    text = (os.environ.get("TEXT") or "").strip()
    drafts = read(OUT / "drafts.json", {})
    if not ids:
        log("не передано ни одного id: ничего не отправляю")
        return 0
    sent = failed = 0
    for cid in ids:
        d = drafts.get(cid)
        if button and len(ids) == 1:
            d = drafts.setdefault(cid, {})
            if text:
                if d.get("reply") and d["reply"] != text:
                    d["draft_was"] = d["reply"]
                d["reply"] = text
            if d.get("status") in (None, "draft", "skip", "answered"):
                d["status"] = "approved"
                d["approved_by"] = "кнопка в календаре"
        if not d or d.get("status") != "approved" or not (d.get("reply") or "").strip():
            log(f"{cid}: нет одобренного ответа, пропускаю")
            continue
        if dry:
            log(f"{cid}: ушёл бы ответ на {len(d['reply'])} знаков")
            continue
        try:
            rid = ig.reply_comment(token, d.get("reply_to") or cid, d["reply"].strip())
        except ig.IGError as e:
            failed += 1
            d["error"] = str(e)
            log(f"{cid}: Instagram не принял ответ: {e}")
            continue
        d.update(status="posted", posted_id=rid,
                 posted_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
        d.pop("error", None)
        sent += 1
        log(f"{cid}: ответ отправлен")
    if not dry:
        (OUT / "drafts.json").write_text(json.dumps(drafts, ensure_ascii=False, indent=1) + "\n")
        commit_push(f"comments: отправлено ответов {sent}, ошибок {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"сбой: {type(e).__name__}: {e}")
        sys.exit(1)
