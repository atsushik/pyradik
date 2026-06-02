import asyncio
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

import sqlite3
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

APP_DIR = Path(__file__).parent
PROJECT_DIR = APP_DIR.parent

sys.path.insert(0, str(PROJECT_DIR))
import radiko_recorder
import radiko_audio
import radiko_config
import radiko_state
import radiko_recording
import radiko_scheduler
import radiko_mcp

# 設定は radiko_config に一元化（環境変数で上書き可）
DB_PATH = radiko_config.DB_PATH
ENABLED_STATIONS_PATH = radiko_config.ENABLED_STATIONS_PATH
CLI_PATH = radiko_config.CLI_PATH
RECORDER_PATH = radiko_config.RECORDER_PATH
RECORDINGS_DIR = Path(radiko_config.RECORDINGS_DIR)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 起動時に状態 poll ループ＋保守ループを開始する
    global _loop, _wake, _maint_wake, _maint_lock
    radiko_state.init()  # state.db（予約・ルール・ジョブ）を用意
    _loop = asyncio.get_running_loop()
    _wake = asyncio.Event()
    _maint_wake = asyncio.Event()
    _maint_lock = asyncio.Lock()
    task = asyncio.create_task(_broadcast_loop())
    maint = asyncio.create_task(_maintenance_loop())

    port = _server.config.port if _server else 8470
    print(
        "\n"
        f"  ▶ radiko Web UI を起動しました  →  http://localhost:{port}/\n"
        f"  🔌 MCP エンドポイント         →  http://localhost:{port}/mcp\n"
        "  ■ 終了するには Ctrl-C を押してください\n",
        flush=True,
    )

    # MCP の StreamableHTTP セッションマネージャを起動（/mcp マウントに必要）
    async with radiko_mcp.mcp.session_manager.run():
        yield
    task.cancel()
    maint.cancel()


app = FastAPI(title="Radiko Web UI", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
# MCP（AIエージェント向け）を /mcp にマウント。streamable_http_app() 呼び出しで
# session_manager が生成され、起動は lifespan 内の session_manager.run() が担う。
app.mount("/mcp", radiko_mcp.mcp.streamable_http_app())


def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def load_enabled():
    if not ENABLED_STATIONS_PATH.exists():
        return set()
    return {l.strip() for l in ENABLED_STATIONS_PATH.read_text().splitlines() if l.strip()}


@app.get("/")
def root():
    # no-cache: 更新時にブラウザが古い index.html を握り続けないようにする
    # （毎回サーバへ検証に来るが、再生状態等の本体は SSE 経由なので負荷は小さい）
    return FileResponse(
        APP_DIR / "static" / "index.html",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/api/version", tags=["meta"], summary="バージョン情報")
def version():
    return {"version": radiko_config.VERSION}


@app.get("/api/health", tags=["meta"], summary="ヘルスチェック")
def health():
    checks = {}
    try:
        c = get_db(); c.execute("SELECT 1").fetchone(); c.close(); checks["db"] = True
    except Exception:
        checks["db"] = False
    checks["ffmpeg"] = shutil.which("ffmpeg") is not None
    try:
        subprocess.run(["atq"], capture_output=True, check=True); checks["at"] = True
    except Exception:
        checks["at"] = False
    guide_age = None
    if DB_PATH.exists():
        guide_age = int(time.time() - DB_PATH.stat().st_mtime)
    checks["guide_age_sec"] = guide_age
    ok = checks["db"] and checks["ffmpeg"]
    return {"status": "ok" if ok else "degraded", "version": radiko_config.VERSION, "checks": checks}


@app.get("/api/dates")
def get_dates():
    conn = get_db()
    rows = conn.execute("SELECT DISTINCT date FROM programs WHERE date GLOB '[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]' ORDER BY date DESC LIMIT 14").fetchall()
    conn.close()
    return {"dates": [r[0] for r in rows]}


@app.get("/api/stations")
def get_stations():
    conn = get_db()
    enabled = load_enabled()
    if enabled:
        ph = ",".join(["?"] * len(enabled))
        rows = conn.execute(
            f"SELECT station_id, name FROM stations WHERE station_id IN ({ph}) ORDER BY station_id",
            list(enabled),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT station_id, name FROM stations WHERE service='radiko' ORDER BY station_id"
        ).fetchall()
    conn.close()
    return {"stations": [{"station_id": r[0], "name": r[1]} for r in rows]}


@app.get("/api/programs")
def get_programs(date: str):
    conn = get_db()
    enabled = load_enabled()
    if enabled:
        ph = ",".join(["?"] * len(enabled))
        rows = conn.execute(
            f"""SELECT p.prog_id, p.station_id, COALESCE(s.name, p.station_id),
                       p.ftime, p.duration, p.title, p.pfm, p.url, p.info, p.image_url
                FROM programs p LEFT JOIN stations s ON p.station_id = s.station_id
                WHERE p.date = ? AND p.station_id IN ({ph})
                ORDER BY p.station_id, p.ftime""",
            [date] + list(enabled),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT p.prog_id, p.station_id, COALESCE(s.name, p.station_id),
                      p.ftime, p.duration, p.title, p.pfm, p.url, p.info, p.image_url
               FROM programs p LEFT JOIN stations s ON p.station_id = s.station_id
               WHERE p.date = ? ORDER BY p.station_id, p.ftime""",
            [date],
        ).fetchall()
    conn.close()
    return {
        "date": date,
        "programs": [
            {
                "prog_id": r[0], "station_id": r[1], "station_name": r[2],
                "ftime": r[3], "duration": r[4], "title": r[5],
                "pfm": r[6], "url": r[7], "info": r[8], "image_url": r[9],
            }
            for r in rows
        ],
    }


@app.get("/api/search")
def search_programs(q: str, scope: str = "enabled", aired: str = "upcoming,airing,aired"):
    """番組検索。

    scope: enabled=聴取可能な放送局のみ（デフォルト, enabled_stations.txt 基準） / all=すべて
    aired: 放送状態のカンマ区切り集合（upcoming=未放送 / airing=放送中 / aired=放送済）。
           デフォルトは全状態。含まれる状態の番組のみ返す。
    """
    states = {s for s in aired.split(",") if s}
    all_states = states >= {"upcoming", "airing", "aired"}
    conn = get_db()
    like = f"%{q}%"
    where = "(p.title LIKE ? OR p.pfm LIKE ? OR p.info LIKE ?)"
    params = [like, like, like]

    if scope == "enabled":
        enabled = load_enabled()
        if enabled:
            ph = ",".join(["?"] * len(enabled))
            where += f" AND p.station_id IN ({ph})"
            params += list(enabled)

    rows = conn.execute(
        f"""SELECT p.prog_id, p.station_id, COALESCE(s.name, p.station_id),
                   p.date, p.ftime, p.duration, p.title, p.pfm, p.url, p.info, p.image_url
            FROM programs p LEFT JOIN stations s ON p.station_id = s.station_id
            WHERE {where}
            ORDER BY p.date DESC, p.ftime""",
        params,
    ).fetchall()
    conn.close()

    now = datetime.now()
    programs = []
    for r in rows:
        if not all_states:
            try:
                start = datetime.strptime(f"{r[3]}{r[4]}", "%Y%m%d%H%M")
            except ValueError:
                continue
            end = start + timedelta(minutes=r[5])
            if now < start:
                st = "upcoming"
            elif now < end:
                st = "airing"
            else:
                st = "aired"
            if st not in states:
                continue
        programs.append({
            "prog_id": r[0], "station_id": r[1], "station_name": r[2],
            "date": r[3], "ftime": r[4], "duration": r[5], "title": r[6],
            "pfm": r[7], "url": r[8], "info": r[9], "image_url": r[10],
        })
        if len(programs) >= 300:
            break

    return {"q": q, "scope": scope, "aired": sorted(states), "programs": programs}


@app.get("/api/programs/{prog_id}")
def get_program(prog_id: str):
    conn = get_db()
    r = conn.execute(
        """SELECT p.prog_id, p.station_id, COALESCE(s.name, p.station_id),
                  p.date, p.ftime, p.duration, p.title, p.pfm, p.url, p.info
           FROM programs p LEFT JOIN stations s ON p.station_id = s.station_id
           WHERE p.prog_id = ?""",
        [prog_id],
    ).fetchone()
    conn.close()
    if not r:
        raise HTTPException(404, "Not found")
    return {
        "prog_id": r[0], "station_id": r[1], "station_name": r[2],
        "date": r[3], "ftime": r[4], "duration": r[5], "title": r[6],
        "pfm": r[7], "url": r[8], "info": r[9],
    }


class RecordRequest(BaseModel):
    prog_id: str
    with_art: bool = False
    do_register: bool = False


@app.post("/api/record")
def record_program(req: RecordRequest, bg: BackgroundTasks):
    conn = get_db()
    r = conn.execute(
        "SELECT station_id, date, ftime, duration, title FROM programs WHERE prog_id = ?",
        [req.prog_id],
    ).fetchone()
    conn.close()
    if not r:
        raise HTTPException(404, "Not found")

    station_id, date, ftime, duration, title = r[0], r[1], r[2], r[3], r[4]
    scheduled_start = datetime.strptime(f"{date}{ftime}", "%Y%m%d%H%M")
    scheduled_end = scheduled_start + timedelta(minutes=duration)
    now = datetime.now()

    safe_title = re.sub(r'[^\w぀-鿿]', '_', title)[:30]
    output = str(RECORDINGS_DIR / f"{station_id}_{date}_{ftime}_{safe_title}.m4a")

    def run_record(kind, value):
        client = radiko_recorder.RadikoClient()
        try:
            if kind == "timefree":
                client.record_timefree(station_id, value, duration, output)
            else:  # live: value は録音分数
                client.record_live(station_id, value, output)
        finally:
            client.logout()
        if req.with_art:
            subprocess.run(
                ["python", str(CLI_PATH), "embed-art", output, station_id, date, ftime],
                capture_output=True,
            )

    if scheduled_end < now:
        from_time = f"{date}{ftime}00"
        bg.add_task(run_record, "timefree", from_time)
        trigger_refresh()
        return {"status": "started", "type": "timefree", "output": output}

    if scheduled_start > now:
        if not req.do_register:
            return {"status": "preview", "type": "future", "scheduled": scheduled_start.isoformat()}
        radish_cmd = (
            f"python {RECORDER_PATH} record {station_id} --live {duration} -o {output}"
        )
        if req.with_art:
            radish_cmd += f" && python {CLI_PATH} embed-art {output} {station_id} {date} {ftime}"
        result = subprocess.run(
            ["at", scheduled_start.strftime("%H:%M"), scheduled_start.strftime("%Y-%m-%d")],
            input=radish_cmd + "\n", text=True, capture_output=True,
        )
        if result.returncode != 0:
            raise HTTPException(500, result.stderr.strip())
        trigger_refresh()
        return {"status": "scheduled", "type": "future", "scheduled": scheduled_start.isoformat()}

    # currently airing
    remaining = max(1, int((scheduled_end - now).total_seconds() / 60) + 1)
    bg.add_task(run_record, "live", remaining)
    trigger_refresh()
    return {"status": "started", "type": "live", "output": output}


class PlayRequest(BaseModel):
    station_id: str


@app.post("/api/play")
def play_station(req: PlayRequest):
    subprocess.run(["pkill", "ffplay"], check=False)
    subprocess.Popen(
        ["python", str(RECORDER_PATH), "play", req.station_id, "--live"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    trigger_refresh()
    return {"status": "playing", "station_id": req.station_id}


class PlayProgramRequest(BaseModel):
    prog_id: str


@app.post("/api/play-program")
def play_program(req: PlayProgramRequest):
    conn = get_db()
    r = conn.execute(
        "SELECT station_id, date, ftime, duration FROM programs WHERE prog_id = ?",
        [req.prog_id],
    ).fetchone()
    conn.close()
    if not r:
        raise HTTPException(404, "Not found")

    station_id, date, ftime, duration = r[0], r[1], r[2], r[3]
    scheduled_start = datetime.strptime(f"{date}{ftime}", "%Y%m%d%H%M")
    scheduled_end = scheduled_start + timedelta(minutes=duration)
    now = datetime.now()

    if scheduled_start > now:
        raise HTTPException(400, "Program has not started yet")

    subprocess.run(["pkill", "ffplay"], check=False)

    # 開始済みの番組はすべてタイムフリーで先頭から再生
    # （放送中の番組は to_time が未来になるが radiko が現時点まで再生してライブに追いつく）
    from_time = f"{date}{ftime}00"
    subprocess.Popen(
        ["python", str(RECORDER_PATH), "play", station_id, from_time, str(duration)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    trigger_refresh()
    return {"status": "playing", "type": "timefree", "station_id": station_id}


@app.post("/api/stop")
def stop_playback():
    r = subprocess.run(["pkill", "ffplay"], capture_output=True)
    trigger_refresh()
    return {"status": "stopped" if r.returncode == 0 else "not_playing"}


def _now_playing_data():
    result = subprocess.run(["ps", "ax"], capture_output=True, text=True)
    base = {"device": radiko_audio.current_device(), "volume": radiko_audio.get_volume()}
    for line in result.stdout.splitlines():
        m = re.search(r'radiko_recorder\.py\s+play\s+(\S+)', line)
        if m:
            return {"playing": True, "station_id": m.group(1), **base}
    return {"playing": False, "station_id": None, **base}


@app.get("/api/now-playing")
def now_playing():
    return _now_playing_data()


class VolumeRequest(BaseModel):
    level: float


@app.get("/api/volume")
def get_volume():
    # wpctl 前提（Raspberry Pi OS / PipeWire 想定）。他環境では null になることがある。
    return {
        "available": radiko_audio.available(),
        "volume": radiko_audio.get_volume(),
        "muted": radiko_audio.is_muted(),
        "device": radiko_audio.current_device(),
    }


@app.post("/api/volume")
def set_volume(req: VolumeRequest):
    if not radiko_audio.available():
        raise HTTPException(503, "wpctl unavailable (Raspberry Pi OS / PipeWire only)")
    if not radiko_audio.set_volume(req.level):
        raise HTTPException(500, "failed to set volume")
    return {"status": "ok", "volume": radiko_audio.get_volume()}


# ── 放送局の名前解決・再生（playback）・発見・統合ステータス ──────────────

def _resolve_stations(token):
    """token を放送局へ解決：id一致 → 名前完全一致 → 部分一致。候補リストを返す。"""
    conn = get_db()
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


def _current_programs(station_filter=None):
    """現在放送中の番組（date=今日 かつ 開始<=現在<終了）を返す。"""
    now = datetime.now()
    mins = now.hour * 60 + now.minute
    conn = get_db()
    q = """SELECT p.prog_id, p.station_id, COALESCE(s.name, p.station_id) name,
                  p.ftime, p.duration, p.title, p.pfm, p.url, p.image_url
           FROM programs p LEFT JOIN stations s ON p.station_id=s.station_id
           WHERE p.date=?"""
    params = [now.strftime("%Y%m%d")]
    if station_filter:
        ph = ",".join("?" * len(station_filter))
        q += f" AND p.station_id IN ({ph})"; params += list(station_filter)
    rows = conn.execute(q, params).fetchall()
    conn.close()
    out = []
    for r in rows:
        start = int(r["ftime"][:2]) * 60 + int(r["ftime"][2:])
        if start <= mins < start + r["duration"]:
            out.append({k: r[k] for k in r.keys()})
    return out


class PlaybackPlay(BaseModel):
    station_id: "str | None" = None
    station_name: "str | None" = None


@app.post("/api/playback/play", tags=["playback"], summary="再生（station_id または station_name で指定）")
def playback_play(req: PlaybackPlay):
    token = req.station_id or req.station_name
    if not token:
        raise HTTPException(400, "station_id か station_name が必要です")
    cands = _resolve_stations(token)
    if not cands:
        raise HTTPException(404, f"放送局が見つかりません: {token}")
    if len(cands) > 1:
        raise HTTPException(409, {"message": "候補が複数あります", "candidates": cands})
    sid = cands[0]["station_id"]
    subprocess.run(["pkill", "ffplay"], check=False)
    subprocess.Popen(
        [sys.executable, str(RECORDER_PATH), "play", sid, "--live"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    trigger_refresh()
    return {"status": "playing", "station_id": sid, "station_name": cands[0]["name"]}


@app.post("/api/playback/stop", tags=["playback"], summary="再生停止")
def playback_stop():
    r = subprocess.run(["pkill", "ffplay"], capture_output=True)
    trigger_refresh()
    return {"status": "stopped" if r.returncode == 0 else "not_playing"}


@app.get("/api/playback", tags=["playback"], summary="再生の現在状態（局・番組・音量）")
def playback_state():
    np = _now_playing_data()
    if np.get("playing") and np.get("station_id"):
        cur = _current_programs([np["station_id"]])
        np["program"] = cur[0] if cur else None
        np["station_name"] = cur[0]["name"] if cur else np["station_id"]
    return np


@app.get("/api/now", tags=["discovery"], summary="現在放送中（受信可能局）")
def now_api():
    enabled = load_enabled()
    return {"programs": _current_programs(list(enabled) if enabled else None)}


@app.get("/api/stations/{station_id}/now", tags=["discovery"], summary="その局の現在番組")
def station_now_api(station_id: str):
    cur = _current_programs([station_id])
    return cur[0] if cur else {"station_id": station_id, "program": None}


@app.get("/api/status", tags=["meta"], summary="再生・録音・予約の統合ステータス")
def status_api():
    active = [j for j in radiko_state.list_jobs() if j["status"] == "recording"]
    reservations = radiko_state.list_reservations(status="scheduled")
    nxt = min(reservations, key=lambda r: r["start_at"]) if reservations else None
    return {
        "playback": _now_playing_data(),
        "recording": {"active": len(active), "job_ids": [j["id"] for j in active]},
        "reservations": {"count": len(reservations), "next": nxt},
        "version": radiko_config.VERSION,
    }


def _schedules_data():
    """at ジョブ一覧を返す。at コマンドが無い場合は None。"""
    try:
        atq = subprocess.run(["atq"], capture_output=True, text=True)
    except FileNotFoundError:
        return None
    jobs = []
    for line in atq.stdout.splitlines():
        parts = line.split()
        if not parts:
            continue
        job_id = parts[0]
        detail = subprocess.run(["at", "-c", job_id], capture_output=True, text=True)
        cmd_lines = [l for l in detail.stdout.splitlines() if "radiko_recorder.py" in l]
        if not cmd_lines:
            continue
        cmd = cmd_lines[0].strip()
        scheduled_str = " ".join(parts[1:6])
        try:
            dt = datetime.strptime(scheduled_str, "%a %b %d %H:%M:%S %Y")
            scheduled = dt.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            scheduled = scheduled_str

        def opt(pattern, c=cmd):
            m = re.search(pattern, c)
            return m.group(1) if m else "-"

        # コマンド: python ... radiko_recorder.py record {sid} --live {dur} -o {out}
        jobs.append({
            "job_id": job_id, "scheduled": scheduled,
            "station_id": opt(r'radiko_recorder\.py\s+record\s+(\S+)'),
            "duration": opt(r'--live\s+(\S+)'),
            "output": opt(r'-o\s+(\S+)'),
            "prog": None,
        })

    conn = get_db()
    for job in jobs:
        fn = Path(job["output"]).name
        m = re.match(r'^([^_]+)_(\d{8})_(\d{4})_', fn)
        if m:
            sid, date, ftime = m.group(1), m.group(2), m.group(3)
            r = conn.execute(
                """SELECT p.prog_id, p.station_id, COALESCE(s.name, p.station_id),
                          p.date, p.ftime, p.duration, p.title, p.pfm, p.url, p.info, p.image_url
                   FROM programs p LEFT JOIN stations s ON p.station_id = s.station_id
                   WHERE p.station_id = ? AND p.date = ? AND p.ftime = ?""",
                [sid, date, ftime],
            ).fetchone()
            if r:
                job["prog"] = {
                    "prog_id": r[0], "station_id": r[1], "station_name": r[2],
                    "date": r[3], "ftime": r[4], "duration": r[5], "title": r[6],
                    "pfm": r[7], "url": r[8], "info": r[9], "image_url": r[10],
                }
    conn.close()
    return jobs


@app.get("/api/schedules")
def list_schedules():
    jobs = _schedules_data()
    if jobs is None:
        return {"schedules": [], "error": "at not installed"}
    return {"schedules": jobs}


@app.delete("/api/schedules/{job_id}")
def cancel_schedule(job_id: str):
    r = subprocess.run(["atrm", job_id], capture_output=True, text=True)
    if r.returncode != 0:
        raise HTTPException(400, r.stderr.strip())
    trigger_refresh()
    return {"status": "cancelled", "job_id": job_id}


def _recordings_data():
    ps = subprocess.run(["ps", "ax"], capture_output=True, text=True)
    active = set()
    for line in ps.stdout.splitlines():
        if "ffmpeg" in line:
            m = re.search(r'(\S+\.m4a)', line)
            if m:
                active.add(Path(m.group(1)).name)

    files = sorted(RECORDINGS_DIR.glob("*.m4a"), key=lambda f: f.stat().st_mtime, reverse=True)
    recordings = []
    conn = get_db()
    for f in files:
        entry = {"filename": f.name, "size": f.stat().st_size, "mtime": int(f.stat().st_mtime), "prog": None, "recording": f.name in active}
        m = re.match(r'^([^_]+)_(\d{8})_(\d{4})_', f.name)
        if m:
            sid, date, ftime = m.group(1), m.group(2), m.group(3)
            r = conn.execute(
                """SELECT p.prog_id, p.station_id, COALESCE(s.name, p.station_id),
                          p.date, p.ftime, p.duration, p.title, p.pfm, p.url, p.info, p.image_url
                   FROM programs p LEFT JOIN stations s ON p.station_id = s.station_id
                   WHERE p.station_id = ? AND p.date = ? AND p.ftime = ?""",
                [sid, date, ftime],
            ).fetchone()
            if r:
                entry["prog"] = {
                    "prog_id": r[0], "station_id": r[1], "station_name": r[2],
                    "date": r[3], "ftime": r[4], "duration": r[5], "title": r[6],
                    "pfm": r[7], "url": r[8], "info": r[9], "image_url": r[10],
                }
        recordings.append(entry)
    conn.close()
    return recordings


@app.get("/api/recordings")
def list_recordings():
    return {"recordings": _recordings_data()}


@app.delete("/api/recordings/{filename}")
def delete_recording(filename: str):
    path = RECORDINGS_DIR / filename
    if not path.exists() or not str(path.resolve()).startswith(str(RECORDINGS_DIR.resolve())):
        raise HTTPException(404, "Not found")
    path.unlink()
    trigger_refresh()
    return {"status": "deleted"}


@app.get("/api/recordings/{filename}")
def download_recording(filename: str):
    path = RECORDINGS_DIR / filename
    if not path.exists() or not str(path.resolve()).startswith(str(RECORDINGS_DIR.resolve())):
        raise HTTPException(404, "Not found")
    return FileResponse(str(path), media_type="audio/x-m4a", filename=filename)


# ── 録音の統一入口（正規化ターゲット → 即時ジョブ / 予約） ──────────────

class RecordingCreate(BaseModel):
    prog_id: "str | None" = None
    station_id: "str | None" = None
    start_at: "str | None" = None       # ISO8601（JST）
    duration_min: "int | None" = None
    title: "str | None" = None
    with_art: bool = False
    start_offset_sec: int = 0
    end_offset_sec: int = 0
    output: "str | None" = None


def _target_view(t):
    return {
        "station_id": t.station_id,
        "start_at": t.start_at.isoformat(),
        "duration_min": t.duration_min,
        "prog_id": t.prog_id,
        "title": t.title,
    }


@app.post("/api/recordings", tags=["recordings"],
          summary="録音を作成（過去/放送中は即時、未来は予約に自動振り分け）")
def create_recording(req: RecordingCreate):
    try:
        target = radiko_recording.resolve_target(req.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))

    method = radiko_recording.choose_method(target)
    if method == "future":
        try:
            info = radiko_recording.schedule_reservation(target)
        except Exception as e:
            raise HTTPException(500, str(e))
        trigger_refresh()
        return {"kind": "reservation", "method": "scheduled", "status": "scheduled",
                "target": _target_view(target), "output": target.output, **info}
    job_id = radiko_recording.start_recording_now(target)
    trigger_refresh()
    return {"kind": "job", "method": method, "status": "recording", "job_id": job_id,
            "target": _target_view(target), "output": target.output}


def _job_view(j):
    out = dict(j)
    out["with_art"] = bool(j["with_art"])
    if j["status"] == "recording" and j["started_at"]:
        total = j["duration_min"] * 60
        elapsed = (datetime.now() - datetime.fromisoformat(j["started_at"])).total_seconds()
        out["progress"] = {
            "elapsed_sec": int(elapsed), "total_sec": total,
            "percent": min(100, round(elapsed / total * 100)) if total else None,
        }
    p = Path(j["output"])
    out["size"] = p.stat().st_size if p.exists() else 0
    return out


@app.get("/api/jobs", tags=["jobs"], summary="録音ジョブ一覧（進捗つき）")
def list_jobs_api():
    return {"jobs": [_job_view(j) for j in radiko_state.list_jobs()]}


@app.get("/api/jobs/{job_id}", tags=["jobs"], summary="録音ジョブの詳細・進捗")
def get_job_api(job_id: str):
    j = radiko_state.get_job(job_id)
    if not j:
        raise HTTPException(404, "Not found")
    return _job_view(j)


@app.delete("/api/jobs/{job_id}", tags=["jobs"], summary="実行中の録音を停止")
def stop_job_api(job_id: str):
    j = radiko_state.get_job(job_id)
    if not j:
        raise HTTPException(404, "Not found")
    if j["status"] == "recording":
        subprocess.run(["pkill", "-f", j["output"]], capture_output=True)
        radiko_state.set_job_status(job_id, "failed", "stopped by user")
    trigger_refresh()
    return {"status": "stopped", "job_id": job_id}


# ── 予約（reservations, state.db） ────────────────────────────────────────

@app.get("/api/reservations", tags=["reservations"], summary="予約一覧")
def list_reservations_api():
    return {"reservations": radiko_state.list_reservations(status="scheduled")}


@app.delete("/api/reservations/{res_id}", tags=["reservations"], summary="予約を取消")
def cancel_reservation_api(res_id: int):
    r = radiko_state.get_reservation(res_id)
    if not r:
        raise HTTPException(404, "Not found")
    radiko_recording.cancel_at_job(r["at_job_id"])
    radiko_state.delete_reservation(res_id)
    trigger_refresh()
    return {"status": "cancelled", "reservation_id": res_id}


# ── ルール（タイトル定期＝シリーズ録画） ──────────────────────────────────

class RuleCreate(BaseModel):
    query: str
    match_fields: str = "title"
    station_id: "str | None" = None
    weekday: "str | None" = None       # csv: Mon,Tue...
    time_from: "str | None" = None     # HHMM
    time_to: "str | None" = None       # HHMM
    with_art: bool = False
    start_offset_sec: int = 0
    end_offset_sec: int = 0
    enabled: bool = True


class RuleUpdate(BaseModel):
    query: "str | None" = None
    match_fields: "str | None" = None
    station_id: "str | None" = None
    weekday: "str | None" = None
    time_from: "str | None" = None
    time_to: "str | None" = None
    with_art: "bool | None" = None
    start_offset_sec: "int | None" = None
    end_offset_sec: "int | None" = None
    enabled: "bool | None" = None


@app.get("/api/rules", tags=["rules"], summary="ルール一覧")
def list_rules_api():
    return {"rules": radiko_state.list_rules()}


@app.post("/api/rules", tags=["rules"], summary="ルールを作成し即時リコンサイル")
async def create_rule_api(req: RuleCreate):
    rule_id = radiko_state.create_rule(**req.model_dump())
    await _run_maintenance(refresh=False)  # 新ルールを即反映（番組表更新は省略）
    trigger_refresh()
    return {"id": rule_id, "rule": radiko_state.get_rule(rule_id)}


@app.get("/api/rules/{rule_id}", tags=["rules"], summary="ルール詳細")
def get_rule_api(rule_id: int):
    r = radiko_state.get_rule(rule_id)
    if not r:
        raise HTTPException(404, "Not found")
    return r


@app.patch("/api/rules/{rule_id}", tags=["rules"], summary="ルールを更新")
async def update_rule_api(rule_id: int, req: RuleUpdate):
    if not radiko_state.get_rule(rule_id):
        raise HTTPException(404, "Not found")
    fields = req.model_dump(exclude_unset=True)
    if fields:
        radiko_state.update_rule(rule_id, **fields)
    await _run_maintenance(refresh=False)
    trigger_refresh()
    return radiko_state.get_rule(rule_id)


@app.delete("/api/rules/{rule_id}", tags=["rules"], summary="ルールと、その由来の未来予約を削除")
def delete_rule_api(rule_id: int):
    if not radiko_state.get_rule(rule_id):
        raise HTTPException(404, "Not found")
    for r in radiko_state.list_reservations(status="scheduled", rule_id=rule_id):
        radiko_recording.cancel_at_job(r["at_job_id"])
        radiko_state.delete_reservation(r["id"])
    radiko_state.delete_rule(rule_id)
    trigger_refresh()
    return {"status": "deleted", "rule_id": rule_id}


@app.post("/api/rules/{rule_id}/reconcile", tags=["rules"], summary="このルールを今すぐ再同期")
async def reconcile_rule_api(rule_id: int):
    if not radiko_state.get_rule(rule_id):
        raise HTTPException(404, "Not found")
    result = await _run_maintenance(refresh=False)
    trigger_refresh()
    return result


# ── 保守（番組表更新＋予約同期＋ルールリコンサイル） ──────────────────────

async def _run_maintenance(refresh: bool):
    async with _maint_lock:
        return await asyncio.to_thread(radiko_scheduler.run_maintenance, refresh)


async def _maintenance_loop():
    # 起動直後は番組表更新せず予約同期だけ（再起動のたびの重い取得を避ける）
    try:
        await _run_maintenance(refresh=False)
    except Exception:
        pass
    while not (_server and _server.should_exit):
        try:
            await asyncio.wait_for(_maint_wake.wait(), timeout=radiko_config.GUIDE_REFRESH_SEC)
        except asyncio.TimeoutError:
            pass
        else:
            _maint_wake.clear()
        if _server and _server.should_exit:
            break
        try:
            await _run_maintenance(refresh=True)
            trigger_refresh()
        except Exception:
            pass


@app.post("/api/maintenance/run", tags=["maintenance"],
          summary="番組表更新＋予約同期＋ルール再同期を今すぐ実行")
async def run_maintenance_api():
    result = await _run_maintenance(refresh=True)
    trigger_refresh()
    return result


# ── SSE: サーバ側の共有状態（再生・録音・予約）を全クライアントへ push ──
#
# 状態の実体は OS 側（ffplay/ffmpeg プロセス・ディスク上の *.m4a・at ジョブ）にあり、
# HTTP リクエストを伴わずに変化する（録音終了・予約発火など）。そのため、サーバの
# poll ループで一定間隔ごとにスナップショットを取り、変化した時だけ配信する。
# 各 mutation は trigger_refresh() で poll を即時起床させ、反映を素早くする。

_subscribers: "set[asyncio.Queue]" = set()
_loop: "asyncio.AbstractEventLoop | None" = None
_wake: "asyncio.Event | None" = None
_maint_wake: "asyncio.Event | None" = None
_maint_lock: "asyncio.Lock | None" = None
_server = None  # uvicorn.Server。SSE ジェネレータ／保守ループが should_exit を見て抜けるために参照する

POLL_INTERVAL = 2.0       # 状態スナップショットの最大間隔（秒）
KEEPALIVE_INTERVAL = 15.0  # SSE コメント行で接続維持する間隔（秒）


def build_snapshot():
    """配信する共有状態のスナップショット。subprocess を含むので別スレッドで呼ぶ。"""
    return {
        "now_playing": _now_playing_data(),
        "recordings": _recordings_data(),
        "reservations": radiko_state.list_reservations(status="scheduled"),
        "rules": radiko_state.list_rules(),
    }


def trigger_refresh():
    """mutation 後に poll ループを即時起床させる（同期エンドポイントから安全に呼べる）。"""
    if _loop and _wake:
        _loop.call_soon_threadsafe(_wake.set)


async def _broadcast_loop():
    last_hash = None
    while True:
        snap = await asyncio.to_thread(build_snapshot)
        h = hashlib.md5(json.dumps(snap, sort_keys=True, default=str).encode()).hexdigest()
        if h != last_hash:
            last_hash = h
            for q in list(_subscribers):
                q.put_nowait(snap)
        try:
            await asyncio.wait_for(_wake.wait(), timeout=POLL_INTERVAL)
        except asyncio.TimeoutError:
            pass
        else:
            _wake.clear()


@app.get("/api/events")
async def events():
    q: "asyncio.Queue" = asyncio.Queue()
    _subscribers.add(q)

    async def gen():
        try:
            # 接続直後に現在の状態を 1 度流す
            snap = await asyncio.to_thread(build_snapshot)
            yield f"data: {json.dumps(snap, default=str)}\n\n"
            last_ka = time.monotonic()
            # should_exit を 1 秒ごとに確認し、シャットダウン時は自分から抜ける。
            # こうしないと uvicorn が「この SSE 接続が閉じるまで」終了を待ち続ける。
            while not (_server and _server.should_exit):
                try:
                    snap = await asyncio.wait_for(q.get(), timeout=1.0)
                    yield f"data: {json.dumps(snap, default=str)}\n\n"
                    last_ka = time.monotonic()
                except asyncio.TimeoutError:
                    if time.monotonic() - last_ka >= KEEPALIVE_INTERVAL:
                        yield ": keep-alive\n\n"
                        last_ka = time.monotonic()
        finally:
            _subscribers.discard(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    import uvicorn
    config = uvicorn.Config(app, host=radiko_config.HOST, port=radiko_config.PORT, timeout_graceful_shutdown=5)
    _server = uvicorn.Server(config)
    try:
        # シャットダウン完了後に asyncio.run が再送出する KeyboardInterrupt を
        # 握りつぶし、Ctrl-C 時のトレースバック表示を抑える（終了処理は正常に完了済み）
        _server.run()
    except KeyboardInterrupt:
        pass
