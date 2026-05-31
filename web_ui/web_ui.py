import math
import re
import subprocess
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
RADISH_PATH = PROJECT_DIR / "radish-play.sh"
CLI_PATH = PROJECT_DIR / "radiko_cli.py"
RECORDINGS_DIR = PROJECT_DIR

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
    rows = conn.execute("SELECT DISTINCT date FROM programs ORDER BY date DESC LIMIT 14").fetchall()
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
                       p.ftime, p.duration, p.title, p.pfm, p.url, p.info
                FROM programs p LEFT JOIN stations s ON p.station_id = s.station_id
                WHERE p.date = ? AND p.station_id IN ({ph})
                ORDER BY p.station_id, p.ftime""",
            [date] + list(enabled),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT p.prog_id, p.station_id, COALESCE(s.name, p.station_id),
                      p.ftime, p.duration, p.title, p.pfm, p.url, p.info
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
                "pfm": r[6], "url": r[7], "info": r[8],
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

    def run_record(cmd):
        subprocess.run(cmd, capture_output=True)
        if req.with_art:
            subprocess.run(
                ["python", str(CLI_PATH), "embed-art", output, station_id, date, ftime],
                capture_output=True,
            )

    if scheduled_end < now:
        from_time = f"{date}{ftime}00"
        cmd = ["bash", str(RADISH_PATH), "-t", "radiko", "-s", station_id,
               "-f", from_time, "-d", str(duration), "-o", output, "-m", "record"]
        bg.add_task(run_record, cmd)
        return {"status": "started", "type": "timefree", "output": output}

    if scheduled_start > now:
        if not req.do_register:
            return {"status": "preview", "type": "future", "scheduled": scheduled_start.isoformat()}
        radish_cmd = (
            f"bash {RADISH_PATH} -t radiko -s {station_id} -d {duration} -o {output} -m record"
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
    cmd = ["bash", str(RADISH_PATH), "-t", "radiko", "-s", station_id,
           "-d", str(remaining), "-o", output, "-m", "record"]
    bg.add_task(run_record, cmd)
    return {"status": "started", "type": "live", "output": output}


class PlayRequest(BaseModel):
    station_id: str


@app.post("/api/play")
def play_station(req: PlayRequest):
    subprocess.Popen(
        ["bash", str(RADISH_PATH), "-t", "radiko", "-s", req.station_id, "-m", "play"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    return {"status": "playing", "station_id": req.station_id}


@app.post("/api/stop")
def stop_playback():
    r = subprocess.run(["pkill", "ffplay"], capture_output=True)
    return {"status": "stopped" if r.returncode == 0 else "not_playing"}


@app.get("/api/now-playing")
def now_playing():
    result = subprocess.run(["ps", "ax"], capture_output=True, text=True)
    for line in result.stdout.splitlines():
        if "radish-play.sh" in line and "-m play" in line:
            m = re.search(r'-s\s+(\S+)', line)
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
        cmd_lines = [l for l in detail.stdout.splitlines() if "radish-play.sh" in l]
        if not cmd_lines:
            continue
        cmd = cmd_lines[0].strip()
        scheduled_str = " ".join(parts[1:6])
        try:
            dt = datetime.strptime(scheduled_str, "%a %b %d %H:%M:%S %Y")
            scheduled = dt.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            scheduled = scheduled_str

        def opt(flag, c=cmd):
            m = re.search(rf'{flag}\s+(\S+)', c)
            return m.group(1) if m else "-"

        jobs.append({
            "job_id": job_id, "scheduled": scheduled,
            "station_id": opt("-s"), "duration": opt("-d"), "output": opt("-o"),
        })
    return {"schedules": jobs}


@app.delete("/api/schedules/{job_id}")
def cancel_schedule(job_id: str):
    r = subprocess.run(["atrm", job_id], capture_output=True, text=True)
    if r.returncode != 0:
        raise HTTPException(400, r.stderr.strip())
    return {"status": "cancelled", "job_id": job_id}


@app.get("/api/recordings")
def list_recordings():
    files = sorted(RECORDINGS_DIR.glob("*.m4a"), key=lambda f: f.stat().st_mtime, reverse=True)
    return {
        "recordings": [
            {"filename": f.name, "size": f.stat().st_size, "mtime": int(f.stat().st_mtime)}
            for f in files
        ]
    }


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
    return FileResponse(str(path), media_type="audio/mp4", filename=filename)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
