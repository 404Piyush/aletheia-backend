"""Demo login sessions for the 3 supply-chain actors.

Consumers never log in (verify/bundle/consumer-scan stay open). Actors log
in on their phone once; the act page then submits as their DID with no
spoofable `?actor=` param. Sessions are opaque tokens, httpOnly cookies,
24h expiry. Passwords are pbkdf2 + per-user salt (stdlib only).
"""

from __future__ import annotations

import hashlib
import os
import secrets
import time

SESSION_TTL_S = 24 * 3600
SESSION_COOKIE = "tf_session"

# Minted demo logins (override via ACTOR_PASSWORDS env as
# "maker:pw1,distributor:pw2,retailer:pw3"). Change on stage if shown.
DEFAULT_USERS = (
    ("maker", "did:maker1", "operator", "maker123"),
    ("distributor", "did:transporter1", "operator", "dist123"),
    ("retailer", "did:retailer1", "operator", "retail123"),
)


def _hash(pw: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100_000).hex()


def seed_users(c, passwords: dict[str, str] | None = None) -> None:
    passwords = passwords or {}
    for username, did, role, default_pw in DEFAULT_USERS:
        pw = passwords.get(username, default_pw)
        salt = secrets.token_hex(8)
        c.execute("INSERT OR REPLACE INTO users(username,pw_hash,salt,did,role) VALUES(?,?,?,?,?)",
                  (username, _hash(pw, salt), salt, did, role))
    c.commit()


def parse_password_env() -> dict[str, str]:
    out = {}
    for pair in os.environ.get("ACTOR_PASSWORDS", "").split(","):
        if ":" in pair:
            u, p = pair.split(":", 1)
            out[u.strip()] = p.strip()
    return out


def login(c, username: str, password: str) -> dict | None:
    row = c.execute("SELECT * FROM users WHERE username=?", (username.strip(),)).fetchone()
    if not row or _hash(password, row["salt"]) != row["pw_hash"]:
        return None
    token = secrets.token_urlsafe(32)
    c.execute("INSERT INTO sessions(token,username,did,role,expiry) VALUES(?,?,?,?,?)",
              (token, row["username"], row["did"], row["role"], int(time.time()) + SESSION_TTL_S))
    c.commit()
    return {"username": row["username"], "did": row["did"], "role": row["role"], "token": token}


def resolve(c, token: str | None) -> dict | None:
    if not token:
        return None
    row = c.execute("SELECT * FROM sessions WHERE token=?", (token,)).fetchone()
    if not row or row["expiry"] < int(time.time()):
        if row:
            c.execute("DELETE FROM sessions WHERE token=?", (token,))
            c.commit()
        return None
    return {"username": row["username"], "did": row["did"], "role": row["role"]}


def logout(c, token: str | None) -> None:
    if token:
        c.execute("DELETE FROM sessions WHERE token=?", (token,))
        c.commit()
