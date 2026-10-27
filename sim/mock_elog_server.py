"""Minimal stand-in for a PSI ELOG server - enough of its HTTP protocol for py_elog.

    python sim/mock_elog_server.py --port 8090 [--user daq --password secret]

  POST /<logbook>/             cmd=Submit multipart form -> 302 Location /<logbook>/<id>
                               (reply_to=<id> makes a reply)
  GET  /<logbook>/<id>         entry page (errormsg cell if it does not exist, like ELOG)
  GET  /<logbook>/<id>?cmd=download   ELOG plain-text export (attributes, =====, text)
  GET  /  and  /<logbook>/     browse entries in a browser (threads, attachments)

Entries are kept in memory and in --store (JSON + attachment files).
"""
from __future__ import annotations

import argparse
import html
import json
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response

RESERVED = {"cmd", "exp", "unm", "upwd", "reply_to", "edit_id", "skiplock", "Text", "Encoding", "suppress", "When"}


def make_app(store: Path, user: str | None, password: str | None, no_location: bool = False) -> FastAPI:
    app = FastAPI(title="mock ELOG")
    store.mkdir(parents=True, exist_ok=True)
    db_file = store / "entries.json"
    entries: dict[str, list[dict]] = json.loads(db_file.read_text()) if db_file.exists() else {}
    expected_pw = None
    if password:
        from passlib.hash import sha256_crypt

        expected_pw = sha256_crypt.using(salt="", rounds=5000).hash(password)[4:]

    def save() -> None:
        db_file.write_text(json.dumps(entries, indent=1))

    def find(book: str, msg_id: int) -> dict | None:
        return next((e for e in entries.get(book, []) if e["id"] == msg_id), None)

    def err(msg: str) -> HTMLResponse:
        return HTMLResponse(f'<html><body><table><tr><td class="errormsg">{html.escape(msg)}</td></tr></table>'
                            "</body></html>")

    @app.post("/{book}/")
    async def submit(book: str, request: Request):
        form = await request.form()
        if user and (form.get("unm") != user or form.get("upwd") != expected_pw):
            return Response(status_code=302, headers={"Location": f"/{book}/?fail=1"})
        if form.get("cmd") != "Submit":
            return err("unsupported command")
        if form.get("edit_id"):
            return err("editing not supported by the mock")
        book_entries = entries.setdefault(book, [])
        msg_id = max((e["id"] for e in book_entries), default=0) + 1
        attachments = []
        for key, value in form.multi_items():
            if key.startswith("attfile") and hasattr(value, "read"):
                name = f"{time.strftime('%y%m%d_%H%M%S')}_{msg_id}_{value.filename}"
                (store / book).mkdir(exist_ok=True)
                (store / book / name).write_bytes(await value.read())
                attachments.append(name)
        text_part = form.get("Text")
        text = (await text_part.read()).decode("utf-8", "replace") if hasattr(text_part, "read") else str(text_part or "")
        reply_to = int(form["reply_to"]) if form.get("reply_to") else None
        if reply_to and not find(book, reply_to):
            return err(f"message {reply_to} does not exist")
        entry = {"id": msg_id, "date": time.strftime("%a, %d %b %Y %H:%M:%S"), "reply_to": reply_to,
                 "encoding": form.get("Encoding", "plain"), "text": text, "attachments": attachments,
                 "attributes": {k: v for k, v in form.items() if k not in RESERVED and not k.startswith("att")}}
        book_entries.append(entry)
        if reply_to:
            find(book, reply_to).setdefault("replies", []).append(msg_id)
        save()
        if no_location:  # like some ELOG servers behind nginx: entry saved, reply without the new id
            return HTMLResponse("<html><body>ok</body></html>")
        return Response(status_code=302, headers={"Location": f"/{book}/{msg_id}"})

    @app.get("/{book}/page")
    def page_ids(book: str):  # py_elog get_message_ids(): links to the entries
        rows = "".join(f'<tr><td class="list1"><a href="/{book}/{e["id"]}">{e["id"]}</a></td></tr>'
                       for e in reversed(entries.get(book, [])))
        return HTMLResponse(f"<html><body><table>{rows}</table></body></html>")

    @app.get("/{book}/{msg_id:int}")
    def entry(book: str, msg_id: int, cmd: str | None = None):
        e = find(book, msg_id)
        if e is None:
            return err(f"This entry has been deleted or does not exist: {msg_id}")
        if cmd == "download":
            lines = [f"$@MID@$: {msg_id}", f"Date: {e['date']}"] + [f"{k}: {v}" for k, v in e["attributes"].items()]
            if e["reply_to"]:
                lines.append(f"In reply to: {e['reply_to']}")
            lines += [f"Encoding: {e['encoding']}", "Attachment: " + ",".join(e["attachments"]),
                      "========================================", e["text"]]
            return PlainTextResponse("\n".join(lines))
        return HTMLResponse(page(book, [e]))

    @app.get("/{book}/{name}")
    def attachment(book: str, name: str):
        path = store / book / Path(name).name
        return FileResponse(path) if path.exists() else err("no such attachment")

    def render(book: str, e: dict, depth: int = 0) -> str:
        attrs = " | ".join(f"<b>{html.escape(k)}</b>: {html.escape(str(v))}" for k, v in e["attributes"].items())
        body = e["text"] if e["encoding"] == "HTML" else f"<pre>{html.escape(e['text'])}</pre>"
        atts = "".join(f'<div><a href="/{book}/{a}">{html.escape(a)}</a>'
                       + (f'<br><img src="/{book}/{a}" style="max-width:480px">' if a.lower().endswith((".png", ".jpg"))
                          else "") + "</div>" for a in e["attachments"])
        out = (f'<div style="margin-left:{depth * 2}em;border-left:3px solid #8ab;padding:4px 10px;margin-top:8px">'
               f'<a href="/{book}/{e["id"]}">#{e["id"]}</a> {e["date"]}<br>{attrs}{body}{atts}</div>')
        for r in e.get("replies", []):
            out += render(book, find(book, r), depth + 1)
        return out

    def page(book: str, top: list) -> str:
        return (f"<html><head><title>mock ELOG {book}</title></head><body style='font-family:sans-serif'>"
                f"<h2>mock ELOG - {html.escape(book)}</h2>" + "".join(render(book, e) for e in reversed(top))
                + "</body></html>")

    @app.get("/{book}/")
    def logbook(book: str):
        return HTMLResponse(page(book, [e for e in entries.get(book, []) if not e["reply_to"]]))

    @app.get("/")
    def index():
        items = "".join(f'<li><a href="/{b}/">{html.escape(b)}</a> ({len(v)} entries)</li>' for b, v in entries.items())
        return HTMLResponse(f"<html><body style='font-family:sans-serif'><h2>mock ELOG</h2><ul>{items}</ul></body></html>")

    return app


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--store", default=str(Path(__file__).parent / "data" / "elog"))
    ap.add_argument("--user")
    ap.add_argument("--password")
    ap.add_argument("--no-location", action="store_true", help="answer a post without the entry id (real-server quirk)")
    a = ap.parse_args()
    uvicorn.run(make_app(Path(a.store), a.user, a.password, a.no_location), host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
