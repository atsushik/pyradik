"""pyradik の MCP サーバ（AI エージェント向けツール）。

FastAPI に /mcp としてマウントして使う（web_ui 側で streamable_http_app を
マウントし、session_manager.run() を lifespan で起動）。ツールは web_ui を
import せず下位モジュール（radiko_recording / radiko_state / radiko_scheduler /
radiko_recorder / radiko_audio）を直接呼ぶ（循環 import 回避）。状態変化は
サーバの SSE poll が拾うため、ここでは明示通知しない。
"""

import sqlite3
import subprocess
import sys

from mcp.server.fastmcp import FastMCP

import radiko_audio
import radiko_config
import radiko_recording
import radiko_scheduler
import radiko_state

mcp = FastMCP("pyradik", stateless_http=True, streamable_http_path="/")


def _db():
    conn = sqlite3.connect(str(radiko_config.DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _enabled():
    p = radiko_config.ENABLED_STATIONS_PATH
    if not p.exists():
        return set()
    return {l.strip() for l in p.read_text().splitlines() if l.strip()}


def _resolve_stations(token):
    conn = _db()
    try:
        r = conn.execute("SELECT station_id, name FROM stations WHERE station_id=?", (token,)).fetchone()
        if r:
            return [dict(r)]
        rows = conn.execute("SELECT station_id, name FROM stations WHERE name=? ORDER BY station_id", (token,)).fetchall()
        if not rows:
            like = f"%{token}%"
            rows = conn.execute(
                "SELECT station_id, name FROM stations WHERE name LIKE ? OR station_id LIKE ? ORDER BY station_id",
                (like, like),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---- 発見・再生 ---------------------------------------------------------

@mcp.tool()
def list_stations(include_all: bool = False) -> dict:
    """聴取可能な放送局の一覧を {"stations": [...]} で返す。include_all=True で radiko 全局。"""
    conn = _db()
    en = _enabled()
    try:
        if en and not include_all:
            ph = ",".join("?" * len(en))
            rows = conn.execute(
                f"SELECT station_id, name FROM stations WHERE station_id IN ({ph}) ORDER BY station_id",
                list(en),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT station_id, name FROM stations WHERE service='radiko' ORDER BY station_id"
            ).fetchall()
        return {"stations": [dict(r) for r in rows]}
    finally:
        conn.close()


@mcp.tool()
def now_playing() -> dict:
    """現在ライブ再生中の放送局・出力デバイス・音量を返す。"""
    ps = subprocess.run(["ps", "ax"], capture_output=True, text=True)
    base = {"device": radiko_audio.current_device(), "volume": radiko_audio.get_volume()}
    import re
    for line in ps.stdout.splitlines():
        m = re.search(r"radiko_recorder\.py\s+play\s+(\S+)", line)
        if m:
            return {"playing": True, "station_id": m.group(1), **base}
    return {"playing": False, "station_id": None, **base}


@mcp.tool()
def play(station: str) -> dict:
    """放送局をライブ再生する。station は局ID（例 YFM）でも局名（例 FMヨコハマ）でも可。
    候補が複数あるときは candidates を返すので、ユーザーに確認して局IDで再指定する。"""
    cands = _resolve_stations(station)
    if not cands:
        return {"error": f"放送局が見つかりません: {station}"}
    if len(cands) > 1:
        return {"error": "候補が複数あります", "candidates": cands}
    sid = cands[0]["station_id"]
    subprocess.run(["pkill", "ffplay"], check=False)
    subprocess.Popen(
        [sys.executable, radiko_config.RECORDER_PATH, "play", sid, "--live"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    return {"status": "playing", "station_id": sid, "station_name": cands[0]["name"]}


@mcp.tool()
def stop() -> dict:
    """ライブ再生を停止する。"""
    r = subprocess.run(["pkill", "ffplay"], capture_output=True)
    return {"status": "stopped" if r.returncode == 0 else "not_playing"}


@mcp.tool()
def get_volume() -> dict:
    """現在の音量（0.0〜1.0）・ミュート・出力デバイスを返す。"""
    return {
        "available": radiko_audio.available(), "volume": radiko_audio.get_volume(),
        "muted": radiko_audio.is_muted(), "device": radiko_audio.current_device(),
    }


@mcp.tool()
def set_volume(level: float) -> dict:
    """音量を 0.0〜1.0 で設定する。"""
    if not radiko_audio.available():
        return {"error": "音量操作が利用できません（Raspberry Pi OS / PipeWire 前提）"}
    if not radiko_audio.set_volume(level):
        return {"error": "音量の設定に失敗しました"}
    return {"status": "ok", "volume": radiko_audio.get_volume()}


@mcp.tool()
def search_programs(query: str, limit: int = 30) -> dict:
    """番組名・パーソナリティ・番組情報からキーワード検索し {"programs": [...]} で返す（最大 limit 件）。"""
    like = f"%{query}%"
    conn = _db()
    try:
        rows = conn.execute(
            """SELECT prog_id, station_id, date, ftime, duration, title, pfm
               FROM programs WHERE title LIKE ? OR pfm LIKE ? OR info LIKE ?
               ORDER BY date DESC, ftime LIMIT ?""",
            (like, like, like, limit),
        ).fetchall()
        return {"programs": [dict(r) for r in rows]}
    finally:
        conn.close()


@mcp.tool()
def programs_now() -> dict:
    """受信可能な放送局で現在放送中の番組を {"programs": [...]} で返す。"""
    from datetime import datetime
    now = datetime.now()
    mins = now.hour * 60 + now.minute
    en = _enabled()
    conn = _db()
    try:
        q = """SELECT prog_id, station_id, ftime, duration, title, pfm FROM programs WHERE date=?"""
        params = [now.strftime("%Y%m%d")]
        if en:
            ph = ",".join("?" * len(en))
            q += f" AND station_id IN ({ph})"; params += list(en)
        rows = conn.execute(q, params).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        start = int(r["ftime"][:2]) * 60 + int(r["ftime"][2:])
        if start <= mins < start + r["duration"]:
            out.append(dict(r))
    return {"programs": out}


# ---- 録音・予約・ジョブ -------------------------------------------------

@mcp.tool()
def record(prog_id: "str | None" = None, station_id: "str | None" = None,
           start_at: "str | None" = None, duration_min: "int | None" = None,
           with_art: bool = False) -> dict:
    """番組を録音する。prog_id 指定が簡単（過去→タイムフリー / 放送中→即時 / 未来→予約 を自動判定）。
    prog_id が無い場合は station_id + start_at(ISO8601 JST) + duration_min で指定する。"""
    body = {"prog_id": prog_id, "station_id": station_id, "start_at": start_at,
            "duration_min": duration_min, "with_art": with_art}
    try:
        t = radiko_recording.resolve_target(body)
    except ValueError as e:
        return {"error": str(e)}
    method = radiko_recording.choose_method(t)
    if method == "future":
        info = radiko_recording.schedule_reservation(t)
        return {"kind": "reservation", "method": "scheduled",
                "reservation_id": info["reservation_id"], "title": t.title,
                "start_at": t.start_at.isoformat(), "output": t.output}
    job_id = radiko_recording.start_recording_now(t)
    return {"kind": "job", "method": method, "job_id": job_id,
            "title": t.title, "output": t.output}


@mcp.tool()
def list_jobs() -> dict:
    """録音ジョブ（実行中・完了）の一覧を {"jobs": [...]} で返す。"""
    return {"jobs": radiko_state.list_jobs()}


@mcp.tool()
def list_reservations() -> dict:
    """予約（未来の録音）の一覧を {"reservations": [...]} で返す。"""
    return {"reservations": radiko_state.list_reservations(status="scheduled")}


@mcp.tool()
def cancel_reservation(reservation_id: int) -> dict:
    """予約を取り消す（at ジョブも削除）。"""
    r = radiko_state.get_reservation(reservation_id)
    if not r:
        return {"error": "Not found"}
    radiko_recording.cancel_at_job(r["at_job_id"])
    radiko_state.delete_reservation(reservation_id)
    return {"status": "cancelled", "reservation_id": reservation_id}


# ---- シリーズ録画ルール -------------------------------------------------

@mcp.tool()
def create_rule(query: str, station_id: "str | None" = None, with_art: bool = False) -> dict:
    """タイトルキーワードでシリーズ録画ルールを作る。一致する今後の放送回が自動予約され、
    放送時刻が変わっても追従、放送がなくなった予約は自動削除される。"""
    rule_id = radiko_state.create_rule(query, station_id=station_id, with_art=with_art)
    radiko_scheduler.reconcile_rules()  # 即時に未来回を予約化
    return {"id": rule_id, "rule": radiko_state.get_rule(rule_id)}


@mcp.tool()
def list_rules() -> dict:
    """シリーズ録画ルールの一覧を {"rules": [...]} で返す。"""
    return {"rules": radiko_state.list_rules()}


@mcp.tool()
def delete_rule(rule_id: int) -> dict:
    """ルールと、その由来の未来予約を削除する。"""
    if not radiko_state.get_rule(rule_id):
        return {"error": "Not found"}
    for r in radiko_state.list_reservations(status="scheduled", rule_id=rule_id):
        radiko_recording.cancel_at_job(r["at_job_id"])
        radiko_state.delete_reservation(r["id"])
    radiko_state.delete_rule(rule_id)
    return {"status": "deleted", "rule_id": rule_id}
