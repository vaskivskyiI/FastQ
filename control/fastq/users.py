"""Dashboard users, sessions and the audit log.

    python -m fastq.users add alice --role operator      # asks for the password
    python -m fastq.users list | passwd alice | role alice admin | remove alice

<data>/users.json: {name: {"hash", "salt", "role", "display"}} (PBKDF2-SHA256). Roles:
  viewer    sees everything, changes nothing
  operator  runs the lab: queue, plans, instrument controls, ELOG, presets, views
  admin     + users, history / security settings, derived values
Without any user the dashboard is open (the lab PIN protects protected controls) - a lab
switches to accounts by adding the first admin. With accounts, a protected control asks for
the user's OWN password: the audit log then says who opened the shutter.

<data>/audit.jsonl: one line per change made through the dashboard / AI tools (time, user,
what, result). Sessions: a signed cookie (HMAC, key in <data>/session.key), 12 h.
"""
from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import json
import secrets
import sys
import threading
import time
from pathlib import Path

ROLES = ("viewer", "operator", "admin")
SESSION_S = 12 * 3600
ITERATIONS = 200_000
_lock = threading.Lock()


def _dir(lab: dict) -> Path:
    from fastq.session import data_root

    d = data_root(lab)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ITERATIONS).hex()


def load(lab: dict) -> dict:
    try:
        return json.loads((_dir(lab) / "users.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(lab: dict, users: dict) -> None:
    f = _dir(lab) / "users.json"
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(users, indent=1), encoding="utf-8")
    tmp.replace(f)


def set_user(lab: dict, name: str, password: str | None = None, role: str | None = None,
             display: str | None = None) -> dict:
    if not name or not name.replace("_", "").replace(".", "").isalnum():
        raise ValueError("user name: letters, digits, _ and . only")
    with _lock:
        users = load(lab)
        u = users.get(name, {"role": "viewer"})
        if role is not None:
            if role not in ROLES:
                raise ValueError(f"role must be one of {ROLES}")
            u["role"] = role
        if password is not None:
            if len(password) < 8:
                raise ValueError("password: at least 8 characters")
            u["salt"] = secrets.token_hex(16)
            u["hash"] = _hash(password, u["salt"])
        if display is not None:
            u["display"] = display
        if "hash" not in u:
            raise ValueError("a new user needs a password")
        users[name] = u
        if not any(x.get("role") == "admin" for x in users.values()):
            raise ValueError("at least one admin must remain")
        _save(lab, users)
    return public(name, u)


def remove_user(lab: dict, name: str) -> None:
    with _lock:
        users = load(lab)
        users.pop(name, None)
        if users and not any(x.get("role") == "admin" for x in users.values()):
            raise ValueError("at least one admin must remain")
        _save(lab, users)


def public(name: str, u: dict) -> dict:
    return {"name": name, "role": u.get("role", "viewer"), "display": u.get("display") or name}


def check_password(lab: dict, name: str, password: str) -> dict | None:
    u = load(lab).get(name)
    if not u or not password or not hmac.compare_digest(_hash(password, u["salt"]), u["hash"]):
        return None
    return public(name, u)


# ---------------------------------------------------------------- sessions
def _key(lab: dict) -> bytes:
    f = _dir(lab) / "session.key"
    with _lock:
        if not f.exists():
            f.write_text(secrets.token_hex(32))
    return bytes.fromhex(f.read_text().strip())


def make_session(lab: dict, name: str) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"u": name, "exp": time.time() + SESSION_S}).encode()).decode()
    sig = hmac.new(_key(lab), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def session_user(lab: dict, token: str | None) -> dict | None:
    """The user of a valid session cookie (role re-read: a demoted user is demoted at once)."""
    if not token or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(hmac.new(_key(lab), body.encode(), hashlib.sha256).hexdigest(), sig):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(body.encode()))
    except ValueError:
        return None
    u = load(lab).get(data.get("u"))
    if not u or data.get("exp", 0) < time.time():
        return None
    return public(data["u"], u)


def allowed(role: str, need: str) -> bool:
    return ROLES.index(role) >= ROLES.index(need)


# ---------------------------------------------------------------- audit log
def audit(lab: dict, who: str, what: str, detail: dict | None = None, ok: bool = True) -> None:
    line = json.dumps({"t": time.time(), "user": who, "what": what, "detail": detail or {}, "ok": ok}, default=str)
    with _lock, open(_dir(lab) / "audit.jsonl", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def audit_tail(lab: dict, n: int = 200, user: str = "", text: str = "") -> list[dict]:
    try:
        lines = (_dir(lab) / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        if (not user or f'"user": "{user}"' in line) and (not text or text.lower() in line.lower()):
            out.append(json.loads(line))
            if len(out) >= n:
                break
    return out


def main() -> None:
    from fastq.session import load_lab

    args = sys.argv[1:]
    lab = load_lab()
    if not args or args[0] == "list":
        for name, u in sorted(load(lab).items()):
            print(f"{name:16s} {u.get('role'):9s} {u.get('display') or ''}")
        return
    cmd, name = args[0], args[1] if len(args) > 1 else ""
    if cmd == "add":
        role = args[args.index("--role") + 1] if "--role" in args else ("admin" if not load(lab) else "operator")
        pw = getpass.getpass(f"password for {name}: ")
        print(set_user(lab, name, pw, role))
    elif cmd == "passwd":
        print(set_user(lab, name, getpass.getpass(f"new password for {name}: ")))
    elif cmd == "role":
        print(set_user(lab, name, role=args[2]))
    elif cmd == "remove":
        remove_user(lab, name)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
