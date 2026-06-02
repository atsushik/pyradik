"""録音指定の正規化と方式判定。

各種入力（prog_id / 放送局＋開始＋長さ）を単一の Target に正規化し、
現在時刻から録音方式（timefree / live / future）を判定する。番組表 DB
（radiko.db）と `at` の参照はすべて radiko_config の絶対パス経由で行う
（`at` ジョブは cwd が異なるため相対パスは使わない）。
"""

import re
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import radiko_config
import radiko_state


def _db():
    conn = sqlite3.connect(str(radiko_config.DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def lookup_program(prog_id):
    conn = _db()
    try:
        r = conn.execute(
            "SELECT station_id, date, ftime, duration, title FROM programs WHERE prog_id=?",
            (prog_id,),
        ).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


_SEARCHABLE = ("title", "pfm", "info")


def find_programs(query, match_fields="title", station_id=None, weekday=None,
                  time_from=None, time_to=None, after=None):
    """ルール条件に一致する番組を返す（after 指定時はその時刻より後の開始のみ）。"""
    fields = [f.strip() for f in match_fields.split(",") if f.strip() in _SEARCHABLE] or ["title"]
    like = f"%{query}%"
    where = " OR ".join(f"{f} LIKE ?" for f in fields)
    params = [like] * len(fields)
    q = f"SELECT prog_id, station_id, date, weekday, ftime, duration, title FROM programs WHERE ({where})"
    if station_id:
        q += " AND station_id=?"; params.append(station_id)
    if weekday:
        wds = [w.strip() for w in weekday.split(",") if w.strip()]
        q += " AND weekday IN (%s)" % ",".join("?" * len(wds)); params += wds
    if time_from:
        q += " AND ftime>=?"; params.append(time_from)
    if time_to:
        q += " AND ftime<=?"; params.append(time_to)
    conn = _db()
    try:
        rows = [dict(r) for r in conn.execute(q, params).fetchall()]
    finally:
        conn.close()
    if after is not None:
        rows = [r for r in rows
                if datetime.strptime(f"{r['date']}{r['ftime']}", "%Y%m%d%H%M") > after]
    return rows


def lookup_art_source(station_id, date, ftime):
    """(url, title, image_url) を返す。アートワーク取得用。"""
    conn = _db()
    try:
        r = conn.execute(
            "SELECT url, title, image_url FROM programs WHERE station_id=? AND date=? AND ftime=? LIMIT 1",
            (station_id, date, ftime),
        ).fetchone()
        return (r["url"], r["title"], r["image_url"]) if r else None
    finally:
        conn.close()


def _safe_title(title):
    return re.sub(r"[^\w　-鿿゠-ヿ぀-ゟ]", "_", title or "")[:40]


def default_output(station_id, start_at, title=None):
    base = f"{station_id}_{start_at.strftime('%Y%m%d')}_{start_at.strftime('%H%M')}"
    if title:
        base += f"_{_safe_title(title)}"
    return str(Path(radiko_config.RECORDINGS_DIR) / f"{base}.m4a")


@dataclass
class Target:
    station_id: str
    start_at: datetime          # JST naive
    duration_min: int
    prog_id: Optional[str] = None
    title: Optional[str] = None
    with_art: bool = False
    start_offset_sec: int = 0
    end_offset_sec: int = 0
    output: Optional[str] = None


def _parse_dt(s):
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is not None:          # tz 付きはローカルへ変換し naive 化（既存コードは naive local）
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def resolve_target(body: dict) -> Target:
    """API/CLI の入力 dict を Target に正規化する（入力形式の追加はここに足す）。"""
    prog_id = body.get("prog_id")
    if prog_id:
        p = lookup_program(prog_id)
        if not p:
            raise ValueError(f"prog_id {prog_id} が番組表に見つかりません")
        start = datetime.strptime(f"{p['date']}{p['ftime']}", "%Y%m%d%H%M")
        t = Target(p["station_id"], start, int(p["duration"]), prog_id=prog_id, title=p["title"])
    else:
        sid, sa, dur = body.get("station_id"), body.get("start_at"), body.get("duration_min")
        if not (sid and sa and dur):
            raise ValueError("prog_id、または station_id + start_at + duration_min が必要です")
        t = Target(sid, _parse_dt(sa), int(dur), title=body.get("title"))

    t.with_art = bool(body.get("with_art", False))
    t.start_offset_sec = int(body.get("start_offset_sec", 0))
    t.end_offset_sec = int(body.get("end_offset_sec", 0))
    t.output = body.get("output") or default_output(t.station_id, t.start_at, t.title)
    return t


def choose_method(t: Target, now=None) -> str:
    """timefree（過去）/ future（未来）/ live（放送中）を返す。"""
    now = now or datetime.now()
    end = t.start_at + timedelta(minutes=t.duration_min)
    if end < now:
        return "timefree"
    if t.start_at > now:
        return "future"
    return "live"


# ---- at スケジューリング -------------------------------------------------

def create_at_job(reservation_id, fire_dt) -> Optional[str]:
    """fire_dt に jobrunner --reservation を実行する at ジョブを作り、ジョブ番号を返す。"""
    cmd = f"python {radiko_config.JOBRUNNER_PATH} --reservation {reservation_id}"
    r = subprocess.run(
        ["at", fire_dt.strftime("%H:%M"), fire_dt.strftime("%Y-%m-%d")],
        input=cmd + "\n", text=True, capture_output=True,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "at の登録に失敗しました")
    m = re.search(r"job\s+(\d+)", r.stderr)
    return m.group(1) if m else None


def cancel_at_job(at_job_id):
    if not at_job_id:
        return
    subprocess.run(["atrm", str(at_job_id)], capture_output=True)


# ---- オーケストレーション（即時ジョブ / 予約） ---------------------------

def start_recording_now(t: Target) -> str:
    """過去/放送中の Target を即時ジョブとして detached 起動し job_id を返す。"""
    method = choose_method(t)  # timefree または live
    job_id = radiko_state.create_job(
        t.station_id, t.start_at.isoformat(), t.duration_min, method, t.output,
        prog_id=t.prog_id, title=t.title, with_art=t.with_art,
    )
    subprocess.Popen(
        ["python", radiko_config.JOBRUNNER_PATH, job_id],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    return job_id


def schedule_reservation(t: Target, rule_id=None) -> dict:
    """未来の Target を予約として登録（reservations 行 + at ジョブ）。"""
    res_id = radiko_state.create_reservation(
        t.station_id, t.start_at.isoformat(), t.duration_min, t.output,
        prog_id=t.prog_id, title=t.title, with_art=t.with_art,
        start_offset_sec=t.start_offset_sec, end_offset_sec=t.end_offset_sec, rule_id=rule_id,
    )
    fire = t.start_at + timedelta(seconds=t.start_offset_sec)
    try:
        at_job_id = create_at_job(res_id, fire)
    except Exception:
        radiko_state.delete_reservation(res_id)
        raise
    radiko_state.update_reservation(res_id, at_job_id=at_job_id)
    return {"reservation_id": res_id, "at_job_id": at_job_id}
