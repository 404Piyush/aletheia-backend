"""SQLite schema. No ORM for 24h speed — sqlite3 + row dicts."""
import sqlite3
import os

SCHEMA = """
CREATE TABLE IF NOT EXISTS actors(
  did TEXT PRIMARY KEY, pubkey TEXT, role TEXT, active INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS lots(
  lot_id TEXT PRIMARY KEY, gtin TEXT, size INTEGER, root TEXT, tx TEXT, frozen INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS products(
  serial TEXT PRIMARY KEY, lot_id TEXT, secret_hash TEXT, status TEXT DEFAULT 'CREATED',
  current_custodian TEXT, claimed_session_hash TEXT, claimed_ts INTEGER,
  container_id TEXT, last_counter INTEGER DEFAULT -1
);
CREATE TABLE IF NOT EXISTS containers(
  id TEXT PRIMARY KEY, type TEXT, parent_id TEXT, lot_id TEXT
);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, serial TEXT, type TEXT, actor TEXT,
  ts INTEGER, lat_mdeg INTEGER, lon_mdeg INTEGER, biz_step TEXT, to_actor TEXT,
  prev_hash TEXT,
  ref_hash TEXT, seq INTEGER, event_hash TEXT UNIQUE, signature TEXT, signer TEXT,
  batch_id INTEGER, raw TEXT
);
CREATE TABLE IF NOT EXISTS claims(
  id INTEGER PRIMARY KEY AUTOINCREMENT, serial TEXT, session_hash TEXT, ts INTEGER, outcome TEXT
);
CREATE TABLE IF NOT EXISTS unknown_attempts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, serial TEXT, lot_id TEXT, ts INTEGER, source TEXT
);
CREATE TABLE IF NOT EXISTS incidents(
  serial TEXT PRIMARY KEY, severity TEXT, status TEXT, findings TEXT, updated_ts INTEGER
);
CREATE TABLE IF NOT EXISTS batches(
  id INTEGER PRIMARY KEY AUTOINCREMENT, root TEXT, count INTEGER, tx TEXT, ts INTEGER
);
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    p = path or os.environ.get("DB_PATH", "trust.db")
    con = sqlite3.connect(p)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con
