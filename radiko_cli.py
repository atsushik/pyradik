import math
import os
import re
import tempfile
import rich_click as click
import sqlite3
import subprocess
from datetime import datetime, timedelta
from urllib import request as urllib_request
from rich.console import Console
from rich.table import Table
from pathlib import Path
from rich.progress import Progress, BarColumn, TextColumn, TimeElapsedColumn, TimeRemainingColumn
from rich.live import Live
from rich.table import Table
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import time
import subprocess
from rich.text import Text
from rich.panel import Panel
from rich.align import Align
from rich.layout import Layout

DB_PATH = "radiko.db"
ENABLED_STATIONS_PATH = "enabled_stations.txt"
RX2_PATH = "/home/atsushi/git/radish/rx2"

MAX_WORKERS = 3
RADISH_PATH = str(Path(__file__).parent / "radish-play.sh")

def parse_offset(s):
    """'1m', '90s', '2m30s' → total seconds as int"""
    s = s.strip()
    if not s or s in ("0", "0s", "0m"):
        return 0
    total = sum(
        int(v) * 60 if u == 'm' else int(v)
        for v, u in re.findall(r'(\d+)([ms])', s)
    )
    if not total:
        try:
            total = int(s)
        except ValueError:
            pass
    return total

def fmt_duration(total_secs):
    h, rem = divmod(total_secs, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h: parts.append(f"{h}時間")
    if m: parts.append(f"{m}分")
    if s: parts.append(f"{s}秒")
    return "".join(parts) or "0秒"

def get_program_info_at_time(station_id, date, ftime):
    """DB から station_id + date + ftime に一致する (url, title) を返す"""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT url, title FROM programs WHERE station_id = ? AND date = ? AND ftime = ? LIMIT 1",
        (station_id, date, ftime),
    )
    row = cur.fetchone()
    conn.close()
    return row

def fetch_og_image(url):
    """URL の og:image を取得して (image_bytes, ext) を返す。失敗時は None"""
    try:
        req = urllib_request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib_request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None
    # property と content の順序どちらにも対応
    m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html)
    if not m:
        m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', html)
    if not m:
        return None
    img_url = m.group(1)
    ext = img_url.rsplit(".", 1)[-1].split("?")[0].lower()
    if ext not in ("jpg", "jpeg", "png", "webp"):
        ext = "jpg"
    try:
        req2 = urllib_request.Request(img_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib_request.urlopen(req2, timeout=10) as resp:
            return resp.read(), ext
    except Exception:
        return None

def embed_cover_art(audio_path, image_bytes, ext):
    """m4a ファイルにカバーアートを埋め込む（上書き）。成功時 True"""
    with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
        tmp.write(image_bytes)
        tmp_path = tmp.name
    out_path = audio_path + ".arttmp.m4a"
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-i", audio_path, "-i", tmp_path,
                "-map", "0:a", "-map", "1",
                "-c", "copy", "-disposition:v:0", "attached_pic",
                "-y", out_path,
            ],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            os.replace(out_path, audio_path)
            return True
        if os.path.exists(out_path):
            os.unlink(out_path)
        return False
    finally:
        os.unlink(tmp_path)

click.rich_click.USE_RICH_MARKUP = True
click.rich_click.USE_MARKDOWN = True
click.rich_click.MAX_WIDTH = 100
console = Console()

def create_tables():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("DROP TABLE IF EXISTS stations")
    cur.execute("DROP TABLE IF EXISTS programs")

    cur.execute("""
    CREATE TABLE stations (
        station_id TEXT PRIMARY KEY,
        service TEXT,
        name TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE programs (
        station_id TEXT,
        prog_id TEXT,
        date TEXT,
        weekday TEXT,
        ftime TEXT,
        duration INTEGER,
        title TEXT,
        url TEXT,
        pfm TEXT,
        info TEXT,
        PRIMARY KEY (station_id, prog_id)
    )
    """)

    conn.commit()
    conn.close()
    console.print("[green]✅ DBを初期化しました[/green]")

def ensure_tables_exist():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    existing = {row[0] for row in cur.fetchall()}
    conn.close()
    if "stations" not in existing or "programs" not in existing:
        console.print("[blue]ℹ️ 必要なテーブルが存在しないため、自動的に初期化します[/blue]")
        create_tables()

def load_enabled_stations(filepath):
    if not Path(filepath).exists():
        return set()
    with open(filepath, "r", encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}

def update_stations_csv():
    """radish-play.sh -l を使って radiko 放送局一覧を stations.csv に保存"""
    try:
        result = subprocess.run(
            ["bash", "/home/atsushi/git/radish/radish-play.sh", "-l"],
            capture_output=True, text=True, check=True
        )
        with open("stations.csv", "w", encoding="utf-8") as f:
            for line in result.stdout.splitlines():
                if line.startswith("radiko,"):
                    f.write(line + "\n")
        console.print("[green]✅ stations.csv を更新しました（radiko局のみ）[/green]")
    except subprocess.CalledProcessError as e:
        console.print(f"[red]❌ radish-play.sh -l 実行エラー: {e}[/red]")

def load_station_ids():
    """stations.csv から radiko の放送局IDと局名を取得"""
    stations = []
    if not Path("stations.csv").exists():
        console.print("[red]❌ stations.csv が存在しません[/red]")
        return stations
    with open("stations.csv", "r", encoding="utf-8") as f:
        for line in f:
            parts = line.strip().split(",", maxsplit=2)
            if len(parts) == 3 and parts[0] == "radiko":
                station_id, name = parts[1], parts[2]
                stations.append((station_id, name))
    return stations

def test_station(station_id, timeout=6):
    """403 が返ってこなければ受信可能と判定"""
    try:
        proc = subprocess.Popen(
            ["bash", "/home/atsushi/git/radish/radish-play.sh", "-t", "radiko", "-s", station_id, "-m", "record", "-d", "60", "-o", "tmp"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE
        )
        start = time.time()
        while True:
            if proc.poll() is not None:
                _, err = proc.communicate()
                return b"403" not in err
            if time.time() - start > timeout:
                proc.terminate()
                return True  # 403が来なかったのでOKと判断
            time.sleep(0.2)
    except Exception as e:
        console.print(f"[red]❌ エラー ({station_id}): {e}[/red]")
        return False

def render_layout():
    layout = Layout()
    layout.split(
        Layout(name="main", ratio=3),
        Layout(name="status", ratio=1)
    )
    layout["main"].update(progress)
    layout["status"].update(
        Panel(
            Align.left(f"[bold magenta]🎧 現在確認中の放送局:[/bold magenta]\n[cyan]{current_checking_text}[/cyan]"),
            border_style="bright_blue"
        )
    )
    return layout

def detect_enabled_stations_parallel():
    update_stations_csv()
    station_list = load_station_ids()
    enabled = []

    progress = Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        "[progress.percentage]{task.percentage:>3.0f}%",
        TimeElapsedColumn(),
        TimeRemainingColumn(),
    )
    task = progress.add_task("⏳ 放送局確認中...", total=len(station_list))

    current_checking_text = "[dim]未開始[/dim]"

    # ✅ progress 定義後に render_layout を定義
    def render_layout():
        layout = Layout()
        layout.split(
            Layout(name="main", ratio=3),
            Layout(name="status", ratio=1)
        )
        layout["main"].update(progress)
        layout["status"].update(
            Panel(
                Align.left(
                    f"[bold magenta]🎧 現在確認中の放送局:[/bold magenta]\n[cyan]{current_checking_text}[/cyan]"
                ),
                border_style="bright_blue"
            )
        )
        return layout

    with Live(render_layout(), console=console, refresh_per_second=4) as live:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {}
            station_iter = iter(station_list)

            def submit_next():
                nonlocal current_checking_text
                try:
                    sid, name = next(station_iter)
                    f = executor.submit(test_station, sid)
                    futures[f] = (sid, name)
                    current_checking_text = f"{sid} - {name}"
                    live.update(render_layout())
                except StopIteration:
                    pass

            for _ in range(MAX_WORKERS):
                submit_next()

            while futures:
                done, _ = wait(futures, return_when=FIRST_COMPLETED)
                for f in done:
                    sid, name = futures.pop(f)
                    if f.result():
                        enabled.append(sid)
                    progress.advance(task)
                    submit_next()
                live.update(render_layout())

    with open(ENABLED_STATIONS_PATH, "w", encoding="utf-8") as f:
        for sid in enabled:
            f.write(sid + "\n")

    console.print(f"\n[green]✅ 有効な放送局 {len(enabled)} 件を書き出しました: {ENABLED_STATIONS_PATH}[/green]")
@click.group()
def cli():
    """📻 [bold green]radiko CLI[/bold green] - 番組表示とDB更新"""
    pass

@cli.command("show-now")
def show_now():
    """現在放送中の番組を [cyan]enabled_stations.txt[/cyan] に従って表示（なければ全局）"""
    ensure_tables_exist()
    now = datetime.now()
    now_date = now.strftime("%Y%m%d")
    now_minutes = now.hour * 60 + now.minute

    enabled = load_enabled_stations(ENABLED_STATIONS_PATH)
    use_all = not enabled
    if use_all:
        console.print("[blue]ℹ️ enabled_stations.txt がないため全局を表示します（auto-enable で絞り込めます）[/blue]")

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    if use_all:
        cur.execute("""
        SELECT p.station_id, COALESCE(s.name, p.station_id), p.ftime, p.duration, p.title, p.pfm, p.url
        FROM programs p
        LEFT JOIN stations s ON p.station_id = s.station_id
        WHERE p.date = ?
        """, (now_date,))
    else:
        placeholders = ",".join(["?"] * len(enabled))
        cur.execute(f"""
        SELECT p.station_id, COALESCE(s.name, p.station_id), p.ftime, p.duration, p.title, p.pfm, p.url
        FROM programs p
        LEFT JOIN stations s ON p.station_id = s.station_id
        WHERE p.date = ? AND p.station_id IN ({placeholders})
        """, (now_date, *enabled))

    rows = []
    for row in cur.fetchall():
        start = int(row[2][:2]) * 60 + int(row[2][2:])
        end = start + row[3]
        if start <= now_minutes < end:
            rows.append({
                "station_id": row[0],
                "station_name": row[1],
                "start": f"{row[2][:2]}:{row[2][2:]}",
                "duration": row[3],
                "title": row[4],
                "pfm": row[5],
                "url": row[6],
            })
    conn.close()

    if not rows:
        console.print("[blue]📭 現在放送中の番組はありません[/blue]")
        return

    title = "📡 現在放送中の番組（全局）" if use_all else "📡 現在放送中の番組 (enabled_stations.txt 限定)"
    table = Table(title=title)
    table.add_column("放送局ID", style="cyan")
    table.add_column("放送局", style="cyan")
    table.add_column("開始", style="green")
    table.add_column("番組名", style="bold")
    table.add_column("パーソナリティ", style="magenta")
    table.add_column("URL", style="blue", overflow="fold")

    for p in rows:
        table.add_row(p["station_id"], p["station_name"], p["start"], p["title"], p["pfm"], p["url"] or "-")

    console.print(table)

@cli.command("update-programs")
def update_db():
    """[green]rx2[/green] コマンドを実行してDBに番組情報を更新"""
    ensure_tables_exist()
    try:
        result = subprocess.run(["bash", RX2_PATH], capture_output=True, encoding='utf-8', errors='replace', check=True)
        lines = result.stdout.strip().splitlines()
    except subprocess.CalledProcessError as e:
        console.print(f"[red]❌ rx2 実行エラー: {e}[/red]")
        return

    if not lines or not lines[0].startswith("station_id"):
        console.print("[yellow]⚠ rx2 出力が不正です[/yellow]")
        return

    FIELD_NAMES = [
        "station_id", "prog_id", "date", "weekday", "ftime", "duration",
        "title", "url", "pfm", "info"
    ]

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    inserted = 0

    for line in lines[1:]:
        parts = line.strip().split("\t", maxsplit=9)
        if len(parts) < 6:
            continue
        row = dict(zip(FIELD_NAMES, parts + [""] * (10 - len(parts))))
        if not all(row.get(k) for k in ["station_id", "prog_id", "date", "ftime", "duration"]):
            continue
        try:
            cur.execute("""
                INSERT OR REPLACE INTO programs (
                    station_id, prog_id, date, weekday, ftime, duration,
                    title, url, pfm, info
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row["station_id"],
                    row["prog_id"],
                    row["date"],
                    row["weekday"],
                    row["ftime"],
                    int(row["duration"]),
                    row["title"],
                    row["url"],
                    row["pfm"],
                    row["info"]
                )
            )
            inserted += 1
        except Exception as e:
            console.print(f"[red]❌ エラー: {e} 行: {row}[/red]")

    conn.commit()
    conn.close()
    console.print(f"[green]✅ {inserted} 件の番組をDBに登録しました[/green]")
    _update_stations_inner()

def _update_stations_inner():
    try:
        result = subprocess.run(
            ["bash", "/home/atsushi/git/radish/radish-play.sh", "-l"],
            capture_output=True, text=True, check=True
        )
        lines = result.stdout.strip().splitlines()
    except subprocess.CalledProcessError as e:
        console.print(f"[red]❌ radish-play.sh 実行エラー: {e}[/red]")
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    inserted = 0
    skipped = 0

    for line in lines:
        parts = line.strip().split(",", maxsplit=2)
        if len(parts) != 3:
            skipped += 1
            continue
        service, station_id, name = parts
        if service != "radiko":
            continue
        try:
            cur.execute("""
                INSERT OR REPLACE INTO stations (service, station_id, name)
                VALUES (?, ?, ?)
            """, (service, station_id, name))
            inserted += 1
        except Exception as e:
            console.print(f"[red]❌ エラー: {e} 行: {line}[/red]")

    conn.commit()
    conn.close()
    console.print(f"[green]✅ {inserted} 局を登録（{skipped} 行スキップ）[/green]")

@cli.command("update-stations")
def update_stations():
    """[blue]radish-play.sh -l[/blue] から放送局一覧を更新"""
    ensure_tables_exist()
    _update_stations_inner()

@cli.command("list-stations")
@click.option("--service", default="radiko", show_default=True, help="対象サービス（例: radiko, nhk）")
def list_stations(service):
    """RADIKOで視聴可能な放送局一覧を表示"""
    ensure_tables_exist()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
    SELECT station_id, name FROM stations WHERE service = ? ORDER BY station_id
    """, (service,))
    rows = cur.fetchall()
    conn.close()

    if not rows:
        console.print(f"[yellow]⚠ サービス '{service}' に該当する放送局がありません[/yellow]")
        return

    table = Table(title=f"📡 放送局一覧（{service}）", show_lines=False)
    table.add_column("局ID", style="cyan")
    table.add_column("局名", style="bold")

    for station_id, name in rows:
        table.add_row(station_id, name)

    console.print(table)

@cli.command("auto-enable")
def auto_enable():
    """現在の地域で聞くことができる局を enabled_stations.txt に書き出す"""
    ensure_tables_exist()
    detect_enabled_stations_parallel()

@cli.command("init-db")
@click.option("--force", "-f", is_flag=True, help="確認せずにDBを初期化")
def init_db(force):
    """DB（stations / programs）を初期化（DROP + CREATE）"""
    if not force:
        confirm = input("⚠ 本当にDBを初期化しますか？（y/N）: ").strip().lower()
        if confirm not in {"y", "yes"}:
            console.print("[yellow]キャンセルしました[/yellow]")
            return
    else:
        console.print("[yellow]⚠ DBを確認なしで初期化します (--force 指定)[/yellow]")

    create_tables()

@cli.command("play")
@click.argument("station_id")
def play_station(station_id):
    """指定した放送局をバックグラウンドで再生（例: play YFM）"""
    ensure_tables_exist()
    console.print(f"[cyan]🎵 再生をバックグラウンドで開始: {station_id}[/cyan]")

    try:
        subprocess.Popen(
            ["bash", "/home/atsushi/git/radish/radish-play.sh", "-t", "radiko", "-s", station_id, "-m", "play"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )
        console.print("[green]▶ 再生開始しました（Ctrl+C なしで次の操作へ）[/green]")
    except Exception as e:
        console.print(f"[red]❌ 再生失敗: {e}[/red]")

@cli.command("stop")
def stop_station():
    """ffplay を強制終了してラジオ再生を停止"""
    try:
        result = subprocess.run(["pkill", "ffplay"], check=False)
        if result.returncode == 0:
            console.print("[yellow]⛔ ffplay プロセスを停止しました[/yellow]")
        else:
            console.print("[blue]ℹ️ ffplay プロセスは見つかりませんでした[/blue]")
    except Exception as e:
        console.print(f"[red]❌ 停止時にエラーが発生しました: {e}[/red]")

@cli.command("now-playing")
def now_playing():
    """現在再生中の放送局の番組情報を表示"""
    ensure_tables_exist()
    try:
        # プロセスから `-s STATION` を含む行を探す
        result = subprocess.run(["ps", "ax"], capture_output=True, text=True)
        station_id = None
        for line in result.stdout.splitlines():
            if "radish-play.sh" in line and "-m play" in line and "-s" in line:
                parts = line.strip().split()
                for i, part in enumerate(parts):
                    if part == "-s" and i + 1 < len(parts):
                        station_id = parts[i + 1]
                        break
                if station_id:
                    break

        if not station_id:
            console.print("[blue]ℹ️ 現在再生中の放送局は見つかりませんでした[/blue]")
            return

        now = datetime.now()
        now_date = now.strftime("%Y%m%d")
        now_minutes = now.hour * 60 + now.minute

        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()

        cur.execute("""
        SELECT p.ftime, p.duration, p.title, p.pfm, p.url, COALESCE(s.name, p.station_id)
        FROM programs p
        LEFT JOIN stations s ON p.station_id = s.station_id
        WHERE p.date = ? AND p.station_id = ?
        """, (now_date, station_id))

        for row in cur.fetchall():
            start = int(row[0][:2]) * 60 + int(row[0][2:])
            end = start + row[1]
            if start <= now_minutes < end:
                table = Table(title=f"📻 再生中の放送局: {station_id}")
                table.add_column("局名", style="cyan")
                table.add_column("開始", style="green")
                table.add_column("番組名", style="bold")
                table.add_column("パーソナリティ", style="magenta")
                table.add_column("URL", style="blue", overflow="fold")
                table.add_row(row[5], f"{row[0][:2]}:{row[0][2:]}", row[2], row[3], row[4] or "-")
                console.print(table)
                return

        console.print(f"[yellow]⚠ 現在、{station_id} に該当する番組は見つかりませんでした[/yellow]")
        conn.close()

    except Exception as e:
        console.print(f"[red]❌ エラー: {e}[/red]")

@cli.command("search")
@click.argument("keyword")
def search_program(keyword):
    """番組名・パーソナリティ・説明文からキーワード検索"""
    ensure_tables_exist()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    like = f"%{keyword}%"

    cur.execute("""
    SELECT p.date, p.ftime, p.station_id, COALESCE(s.name, p.station_id), p.duration, p.title, p.pfm, p.url
    FROM programs p
    LEFT JOIN stations s ON p.station_id = s.station_id
    WHERE p.title LIKE ? OR p.pfm LIKE ? OR p.info LIKE ?
    ORDER BY p.date, p.ftime
    """, (like, like, like))

    results = cur.fetchall()
    conn.close()

    if not results:
        console.print(f"[yellow]🔍 '{keyword}' に該当する番組は見つかりませんでした[/yellow]")
        return

    table = Table(title=f"🔍 キーワード検索結果: '{keyword}'", show_lines=False)
    table.add_column("日付", style="green")
    table.add_column("開始", style="cyan")
    table.add_column("局ID", style="dim")
    table.add_column("局名", style="bold")
    table.add_column("分", style="cyan", justify="right")
    table.add_column("番組名", style="magenta")
    table.add_column("パーソナリティ", style="dim")
    table.add_column("URL", style="blue", overflow="fold")

    for date, ftime, station_id, name, duration, title, pfm, url in results:
        table.add_row(
            f"{date[:4]}/{date[4:6]}/{date[6:]}",
            f"{ftime[:2]}:{ftime[2:]}",
            station_id,
            name,
            str(duration),
            title,
            pfm,
            url or "-"
        )

    console.print(table)

@cli.command("list-schedules")
def list_schedules():
    """atコマンドに登録済みの録音スケジュール一覧を表示"""
    try:
        atq = subprocess.run(["atq"], capture_output=True, text=True)
    except FileNotFoundError:
        console.print("[red]❌ at コマンドが見つかりません（sudo apt install at）[/red]")
        return

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

        # atq の日時フィールド: "123  Mon Jun  1 08:01:00 2026 a user"
        # → parts[1:6] が "Mon Jun  1 08:01:00 2026" 相当（空白次第でずれる）
        scheduled_str = " ".join(parts[1:6])
        try:
            scheduled_dt = datetime.strptime(scheduled_str, "%a %b %d %H:%M:%S %Y")
            scheduled_label = scheduled_dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            scheduled_label = scheduled_str

        # コマンドから -s, -d, -o を抽出
        def extract_opt(flag):
            m = re.search(rf'{flag}\s+(\S+)', cmd)
            return m.group(1) if m else "-"

        jobs.append((job_id, scheduled_label, extract_opt("-s"), extract_opt("-d"), extract_opt("-o")))

    if not jobs:
        console.print("[blue]ℹ️ 録音スケジュールは登録されていません[/blue]")
        return

    table = Table(title="📅 録音スケジュール一覧", show_lines=False)
    table.add_column("JobID", style="dim", justify="right")
    table.add_column("録音開始", style="green")
    table.add_column("局ID", style="cyan")
    table.add_column("時間(分)", style="cyan", justify="right")
    table.add_column("出力ファイル", style="dim", overflow="fold")
    for job_id, scheduled_label, station, dur, out in jobs:
        table.add_row(job_id, scheduled_label, station, dur, out)
    console.print(table)

@cli.command("cancel-schedule")
@click.argument("job_id")
def cancel_schedule(job_id):
    """指定した at ジョブ ID の録音スケジュールを取り消す"""
    try:
        result = subprocess.run(["atrm", job_id], capture_output=True, text=True)
        if result.returncode == 0:
            console.print(f"[green]✅ ジョブ {job_id} を取り消しました[/green]")
        else:
            console.print(f"[red]❌ 取り消し失敗: {result.stderr.strip()}[/red]")
    except FileNotFoundError:
        console.print("[red]❌ at コマンドが見つかりません（sudo apt install at）[/red]")

@cli.command("embed-art")
@click.argument("audio_file")
@click.argument("station_id")
@click.argument("date")
@click.argument("ftime")
def embed_art(audio_file, station_id, date, ftime):
    """録音ファイルに番組のカバーアートを埋め込む

    例: embed-art YFM_20260531_0800.m4a YFM 20260531 0800
    """
    ensure_tables_exist()
    info = get_program_info_at_time(station_id, date, ftime)
    if not info:
        console.print(f"[yellow]⚠ {station_id} {date} {ftime} に該当する番組が DB に見つかりません[/yellow]")
        return
    url, title = info
    if not url:
        console.print(f"[yellow]⚠ 番組 '{title}' の URL が登録されていません[/yellow]")
        return

    console.print(f"[blue]🖼 og:image を取得中: {url}[/blue]")
    result = fetch_og_image(url)
    if not result:
        console.print("[yellow]⚠ og:image が見つかりませんでした[/yellow]")
        return
    image_bytes, ext = result

    console.print("[blue]🎨 カバーアートを埋め込み中...[/blue]")
    if embed_cover_art(audio_file, image_bytes, ext):
        console.print(f"[green]✅ カバーアートを埋め込みました: {audio_file}[/green]")
    else:
        console.print("[red]❌ カバーアートの埋め込みに失敗しました[/red]")

@cli.command("schedule-record")
@click.argument("station_id")
@click.argument("date")
@click.argument("ftime")
@click.argument("duration", type=int)
@click.option("--start-offset", "start_offset_str", default="0s", show_default=True,
              metavar="OFFSET", help="録音開始オフセット（例: 1m, 90s）")
@click.option("--end-offset", "end_offset_str", default="0s", show_default=True,
              metavar="OFFSET", help="録音終了オフセット（例: 30s, 2m）")
@click.option("--output", "-o", default=None, help="出力ファイルパス（省略時は自動生成）")
@click.option("--register", is_flag=True, help="atコマンドで実際に登録する")
@click.option("--with-art", "with_art", is_flag=True, help="録音後に番組のカバーアートを埋め込む")
def schedule_record(station_id, date, ftime, duration, start_offset_str, end_offset_str, output, register, with_art):
    """search 結果の STATION_ID DATE FTIME DURATION から at 録音ジョブを生成・登録

    例: schedule-record YFM 20260531 0800 73 --start-offset 1m --end-offset 30s --with-art
    """
    start_offset_secs = parse_offset(start_offset_str)
    end_offset_secs = parse_offset(end_offset_str)

    scheduled_start = datetime.strptime(f"{date}{ftime}", "%Y%m%d%H%M")
    actual_start = scheduled_start + timedelta(seconds=start_offset_secs)
    actual_duration_secs = duration * 60 + end_offset_secs
    actual_duration_mins = math.ceil(actual_duration_secs / 60)
    actual_end = actual_start + timedelta(seconds=actual_duration_secs)

    if not output:
        output = str(Path.cwd() / f"{station_id}_{date}_{ftime}.m4a")
    else:
        output = str(Path(output).resolve())

    cli_path = str(Path(__file__).resolve())
    radish_cmd = (
        f"bash {RADISH_PATH} -t radiko -s {station_id}"
        f" -d {actual_duration_mins} -o {output} -m record"
    )
    if with_art:
        radish_cmd += (
            f" && python {cli_path} embed-art {output} {station_id} {date} {ftime}"
        )

    info = Table(show_header=False, box=None, padding=(0, 1))
    info.add_column("", style="bold")
    info.add_column("")
    info.add_row("局", f"[cyan]{station_id}[/cyan]")
    info.add_row("番組開始（予定）", scheduled_start.strftime("%Y-%m-%d %H:%M"))
    start_label = actual_start.strftime("%Y-%m-%d %H:%M:%S")
    if start_offset_secs:
        start_label += f" [dim](+{fmt_duration(start_offset_secs)})[/dim]"
    info.add_row("録音開始", start_label)
    dur_label = fmt_duration(actual_duration_secs)
    if actual_duration_secs % 60:
        dur_label += f" [dim](→ {actual_duration_mins}分 で -d 指定)[/dim]"
    info.add_row("録音時間", dur_label)
    info.add_row("録音終了（予定）", actual_end.strftime("%Y-%m-%d %H:%M:%S"))
    info.add_row("出力ファイル", output)
    if with_art:
        info.add_row("カバーアート", "[green]録音後に自動取得[/green]")
    console.print(info)
    console.print()

    at_time = actual_start.strftime("%H:%M %Y-%m-%d")
    console.print("[bold]at コマンド:[/bold]")
    console.print(f"  [green]echo '{radish_cmd}' | at {at_time}[/green]")

    if register:
        try:
            result = subprocess.run(
                ["at", actual_start.strftime("%H:%M"), actual_start.strftime("%Y-%m-%d")],
                input=radish_cmd + "\n",
                text=True,
                capture_output=True,
            )
            if result.returncode == 0:
                console.print(f"[green]✅ at ジョブを登録しました[/green]")
                if result.stderr:
                    console.print(f"[dim]{result.stderr.strip()}[/dim]")
            else:
                console.print(f"[red]❌ at 登録失敗: {result.stderr.strip()}[/red]")
        except FileNotFoundError:
            console.print("[red]❌ at コマンドが見つかりません（sudo apt install at）[/red]")

@cli.command("timefree-record")
@click.argument("station_id")
@click.argument("date")
@click.argument("ftime")
@click.argument("duration", type=int)
@click.option("--output", "-o", default=None, help="出力ファイルパス（省略時は自動生成）")
@click.option("--with-art", "with_art", is_flag=True, help="番組のカバーアートを埋め込む")
def timefree_record(station_id, date, ftime, duration, output, with_art):
    """過去の番組をタイムフリーで録音する（放送後7日以内）

    例: timefree-record YFM 20260531 0800 73
    """
    ensure_tables_exist()
    from_time = f"{date}{ftime}00"

    if not output:
        output = str(Path.cwd() / f"{station_id}_{date}_{ftime}.m4a")
    else:
        output = str(Path(output).resolve())

    console.print(f"[cyan]📻 タイムフリー録音開始: {station_id} {date[:4]}/{date[4:6]}/{date[6:]} {ftime[:2]}:{ftime[2:]} ({duration}分)[/cyan]")

    result = subprocess.run(
        ["bash", RADISH_PATH, "-t", "radiko", "-s", station_id,
         "-f", from_time, "-d", str(duration), "-o", output, "-m", "record"],
    )
    if result.returncode != 0:
        console.print("[red]❌ 録音失敗[/red]")
        return

    console.print(f"[green]✅ 録音完了: {output}[/green]")

    if with_art:
        info = get_program_info_at_time(station_id, date, ftime)
        if not info or not info[0]:
            console.print("[yellow]⚠ カバーアート: 番組 URL が見つかりません[/yellow]")
            return
        url, _ = info
        console.print(f"[blue]🖼 og:image を取得中: {url}[/blue]")
        art = fetch_og_image(url)
        if not art:
            console.print("[yellow]⚠ og:image が見つかりませんでした[/yellow]")
            return
        image_bytes, ext = art
        if embed_cover_art(output, image_bytes, ext):
            console.print(f"[green]✅ カバーアートを埋め込みました[/green]")
        else:
            console.print("[red]❌ カバーアートの埋め込みに失敗しました[/red]")

if __name__ == "__main__":
    cli()
