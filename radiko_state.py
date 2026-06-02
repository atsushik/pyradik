"""予約(reservations)・ルール(rules)・録音ジョブ(jobs)を保持する state.db。

番組表 DB（radiko.db, init-db で再生成）とは別ファイルに分離し、初期化で
消えないようにする。アクセスは都度接続（SQLite, スレッド跨ぎを避ける）。
"""

import sqlite3
import uuid
from datetime import datetime

import radiko_config

SCHEMA = """
CREATE TABLE IF NOT EXISTS reservations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id      TEXT NOT NULL,
    start_at        TEXT NOT NULL,          -- ISO8601 JST（番組開始, offset 適用前）
    duration_min    INTEGER NOT NULL,
    prog_id         TEXT,                   -- アンカー。NULL=固定、有=時刻追従
    title           TEXT,
    with_art        INTEGER NOT NULL DEFAULT 0,
    start_offset_sec INTEGER NOT NULL DEFAULT 0,
    end_offset_sec  INTEGER NOT NULL DEFAULT 0,
    output          TEXT NOT NULL,
    at_job_id       TEXT,
    rule_id         INTEGER,
    status          TEXT NOT NULL DEFAULT 'scheduled',  -- scheduled/fired/cancelled
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    query           TEXT NOT NULL,
    match_fields    TEXT NOT NULL DEFAULT 'title',  -- csv: title,pfm,info
    station_id      TEXT,
    weekday         TEXT,                  -- csv: Mon,Tue...
    time_from       TEXT,                  -- HHMM
    time_to         TEXT,                  -- HHMM
    with_art        INTEGER NOT NULL DEFAULT 0,
    start_offset_sec INTEGER NOT NULL DEFAULT 0,
    end_offset_sec  INTEGER NOT NULL DEFAULT 0,
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,       -- uuid
    station_id      TEXT NOT NULL,
    start_at        TEXT NOT NULL,
    duration_min    INTEGER NOT NULL,
    prog_id         TEXT,
    title           TEXT,
    method          TEXT NOT NULL,          -- timefree/live
    output          TEXT NOT NULL,
    with_art        INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending/recording/completed/failed
    started_at      TEXT,
    finished_at     TEXT,
    error           TEXT,
    reservation_id  INTEGER,
    created_at      TEXT NOT NULL
);
"""


def _now():
    return datetime.now().isoformat(timespec="seconds")


def connect():
    conn = sqlite3.connect(str(radiko_config.STATE_DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init():
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


# ---- jobs ---------------------------------------------------------------

def create_job(station_id, start_at, duration_min, method, output,
               prog_id=None, title=None, with_art=False, reservation_id=None):
    job_id = uuid.uuid4().hex
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO jobs (id, station_id, start_at, duration_min, prog_id, title,
                                 method, output, with_art, status, reservation_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?, 'pending', ?, ?)""",
            (job_id, station_id, start_at, duration_min, prog_id, title,
             method, output, int(with_art), reservation_id, _now()),
        )
        conn.commit()
    finally:
        conn.close()
    return job_id


def set_job_status(job_id, status, error=None):
    conn = connect()
    try:
        if status == "recording":
            conn.execute("UPDATE jobs SET status=?, started_at=? WHERE id=?",
                         (status, _now(), job_id))
        elif status in ("completed", "failed"):
            conn.execute("UPDATE jobs SET status=?, finished_at=?, error=? WHERE id=?",
                         (status, _now(), error, job_id))
        else:
            conn.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))
        conn.commit()
    finally:
        conn.close()


def get_job(job_id):
    conn = connect()
    try:
        r = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def list_jobs(limit=200):
    conn = connect()
    try:
        rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---- reservations -------------------------------------------------------

def create_reservation(station_id, start_at, duration_min, output, prog_id=None,
                       title=None, with_art=False, start_offset_sec=0, end_offset_sec=0,
                       at_job_id=None, rule_id=None):
    conn = connect()
    try:
        cur = conn.execute(
            """INSERT INTO reservations (station_id, start_at, duration_min, prog_id, title,
                   with_art, start_offset_sec, end_offset_sec, output, at_job_id, rule_id,
                   status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?, 'scheduled', ?, ?)""",
            (station_id, start_at, duration_min, prog_id, title, int(with_art),
             start_offset_sec, end_offset_sec, output, at_job_id, rule_id, _now(), _now()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_reservation(res_id):
    conn = connect()
    try:
        r = conn.execute("SELECT * FROM reservations WHERE id=?", (res_id,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def list_reservations(status="scheduled", rule_id=None):
    conn = connect()
    try:
        q = "SELECT * FROM reservations WHERE 1=1"
        params = []
        if status:
            q += " AND status=?"; params.append(status)
        if rule_id is not None:
            q += " AND rule_id=?"; params.append(rule_id)
        q += " ORDER BY start_at"
        rows = conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def find_reservation(rule_id, prog_id):
    conn = connect()
    try:
        r = conn.execute(
            "SELECT * FROM reservations WHERE rule_id IS ? AND prog_id=? AND status='scheduled'",
            (rule_id, prog_id),
        ).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def update_reservation(res_id, **fields):
    if not fields:
        return
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k}=?" for k in fields)
    conn = connect()
    try:
        conn.execute(f"UPDATE reservations SET {cols} WHERE id=?", (*fields.values(), res_id))
        conn.commit()
    finally:
        conn.close()


def delete_reservation(res_id):
    conn = connect()
    try:
        conn.execute("DELETE FROM reservations WHERE id=?", (res_id,))
        conn.commit()
    finally:
        conn.close()


# ---- rules --------------------------------------------------------------

def create_rule(query, match_fields="title", station_id=None, weekday=None,
                time_from=None, time_to=None, with_art=False,
                start_offset_sec=0, end_offset_sec=0, enabled=True):
    conn = connect()
    try:
        cur = conn.execute(
            """INSERT INTO rules (query, match_fields, station_id, weekday, time_from, time_to,
                   with_art, start_offset_sec, end_offset_sec, enabled, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (query, match_fields, station_id, weekday, time_from, time_to, int(with_art),
             start_offset_sec, end_offset_sec, int(enabled), _now(), _now()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_rule(rule_id):
    conn = connect()
    try:
        r = conn.execute("SELECT * FROM rules WHERE id=?", (rule_id,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def list_rules(enabled_only=False):
    conn = connect()
    try:
        q = "SELECT * FROM rules"
        if enabled_only:
            q += " WHERE enabled=1"
        q += " ORDER BY id"
        return [dict(r) for r in conn.execute(q).fetchall()]
    finally:
        conn.close()


def update_rule(rule_id, **fields):
    if not fields:
        return
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k}=?" for k in fields)
    conn = connect()
    try:
        conn.execute(f"UPDATE rules SET {cols} WHERE id=?", (*fields.values(), rule_id))
        conn.commit()
    finally:
        conn.close()


def delete_rule(rule_id):
    conn = connect()
    try:
        conn.execute("DELETE FROM rules WHERE id=?", (rule_id,))
        conn.commit()
    finally:
        conn.close()
