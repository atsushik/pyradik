import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import sqlite3
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

APP_DIR = Path(__file__).parent
PROJECT_DIR = APP_DIR.parent
DB_PATH = PROJECT_DIR / "radiko.db"
ENABLED_STATIONS_PATH = PROJECT_DIR / "enabled_stations.txt"
CLI_PATH = PROJECT_DIR / "radiko_cli.py"
RECORDER_PATH = PROJECT_DIR / "radiko_recorder.py"
RECORDINGS_DIR = PROJECT_DIR

sys.path.insert(0, str(PROJECT_DIR))
import radiko_recorder

app = FastAPI(title="Radiko Web UI")
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


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
    return FileResponse(APP_DIR / "static" / "index.html")


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
        return {"status": "scheduled", "type": "future", "scheduled": scheduled_start.isoformat()}

    # currently airing
    remaining = max(1, int((scheduled_end - now).total_seconds() / 60) + 1)
    bg.add_task(run_record, "live", remaining)
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
    return {"status": "playing", "type": "timefree", "station_id": station_id}


@app.post("/api/stop")
def stop_playback():
    r = subprocess.run(["pkill", "ffplay"], capture_output=True)
    return {"status": "stopped" if r.returncode == 0 else "not_playing"}


@app.get("/api/now-playing")
def now_playing():
    result = subprocess.run(["ps", "ax"], capture_output=True, text=True)
    for line in result.stdout.splitlines():
        m = re.search(r'radiko_recorder\.py\s+play\s+(\S+)', line)
        if m:
            return {"playing": True, "station_id": m.group(1)}
    return {"playing": False, "station_id": None}


@app.get("/api/schedules")
def list_schedules():
    try:
        atq = subprocess.run(["atq"], capture_output=True, text=True)
    except FileNotFoundError:
        return {"schedules": [], "error": "at not installed"}
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
    return {"schedules": jobs}


@app.delete("/api/schedules/{job_id}")
def cancel_schedule(job_id: str):
    r = subprocess.run(["atrm", job_id], capture_output=True, text=True)
    if r.returncode != 0:
        raise HTTPException(400, r.stderr.strip())
    return {"status": "cancelled", "job_id": job_id}


@app.get("/api/recordings")
def list_recordings():
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
    return {"recordings": recordings}


@app.delete("/api/recordings/{filename}")
def delete_recording(filename: str):
    path = RECORDINGS_DIR / filename
    if not path.exists() or not str(path.resolve()).startswith(str(RECORDINGS_DIR.resolve())):
        raise HTTPException(404, "Not found")
    path.unlink()
    return {"status": "deleted"}


@app.get("/api/recordings/{filename}")
def download_recording(filename: str):
    path = RECORDINGS_DIR / filename
    if not path.exists() or not str(path.resolve()).startswith(str(RECORDINGS_DIR.resolve())):
        raise HTTPException(404, "Not found")
    return FileResponse(str(path), media_type="audio/x-m4a", filename=filename)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8470)
