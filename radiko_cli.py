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

import radiko_recorder
import radiko_programs

DB_PATH = "radiko.db"
ENABLED_STATIONS_PATH = "enabled_stations.txt"

MAX_WORKERS = 3
RECORDER_PATH = str(Path(__file__).parent / "radiko_recorder.py")

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

def fmt_size(n):
    """バイト数を人間可読な単位に整形する。"""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024

# 録音ファイル名 {局ID}_{日付8桁}_{開始時刻4桁}[_{タイトル}].m4a
RECORDING_RE = re.compile(r"^(?P<sid>[^_]+)_(?P<date>\d{8})_(?P<ftime>\d{4})(?:_.*)?\.m4a$", re.IGNORECASE)

def parse_recording_filename(path):
    """録音ファイル名から (station_id, date, ftime) を取り出す。形式不一致なら None。"""
    m = RECORDING_RE.match(Path(path).name)
    if not m:
        return None
    return m.group("sid"), m.group("date"), m.group("ftime")

def get_program_info_at_time(station_id, date, ftime):
    """DB から station_id + date + ftime に一致する (url, title, image_url) を返す"""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT url, title, image_url FROM programs WHERE station_id = ? AND date = ? AND ftime = ? LIMIT 1",
        (station_id, date, ftime),
    )
    row = cur.fetchone()
    conn.close()
    return row

def fetch_image_url(image_url):
    """CDN 直接 URL から画像を取得して (image_bytes, ext) を返す。失敗時は None"""
    ext = image_url.rsplit(".", 1)[-1].split("?")[0].lower()
    if ext not in ("jpg", "jpeg", "png", "webp"):
        ext = "jpg"
    try:
        req = urllib_request.Request(image_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib_request.urlopen(req, timeout=10) as resp:
            return resp.read(), ext
    except Exception:
        return None

def fetch_art(image_url, site_url):
    """image_url (CDN直接) → site_url の og:image の順で画像取得を試みる"""
    if image_url:
        result = fetch_image_url(image_url)
        if result:
            return result
    if site_url:
        return fetch_og_image(site_url)
    return None

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
        image_url TEXT,
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
    if "stations" not in existing or "programs" not in existing:
        conn.close()
        console.print("[blue]ℹ️ 必要なテーブルが存在しないため、自動的に初期化します[/blue]")
        create_tables()
        return
    cur.execute("PRAGMA table_info(programs)")
    cols = {row[1] for row in cur.fetchall()}
    if "image_url" not in cols:
        cur.execute("ALTER TABLE programs ADD COLUMN image_url TEXT")
        conn.commit()
        console.print("[blue]ℹ️ image_url カラムを追加しました[/blue]")
    conn.close()

def load_enabled_stations(filepath):
    if not Path(filepath).exists():
        return set()
    with open(filepath, "r", encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}

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
    client = radiko_recorder.RadikoClient()
    station_list = radiko_recorder.list_stations()
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
                    f = executor.submit(client.station_available, sid)
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
        SELECT p.station_id, COALESCE(s.name, p.station_id), p.ftime, p.duration, p.title, p.pfm, p.url, p.prog_id
        FROM programs p
        LEFT JOIN stations s ON p.station_id = s.station_id
        WHERE p.date = ?
        """, (now_date,))
    else:
        placeholders = ",".join(["?"] * len(enabled))
        cur.execute(f"""
        SELECT p.station_id, COALESCE(s.name, p.station_id), p.ftime, p.duration, p.title, p.pfm, p.url, p.prog_id
        FROM programs p
        LEFT JOIN stations s ON p.station_id = s.station_id
        WHERE p.date = ? AND p.station_id IN ({placeholders})
        """, (now_date, *enabled))

    rows = []
    for row in cur.fetchall():
        start = int(row[2][:2]) * 60 + int(row[2][2:])
        end = start + row[3]
        if start <= now_minutes < end:
            end_h, end_m = divmod(end, 60)
            remaining = end - now_minutes
            rows.append({
                "station_id": row[0],
                "station_name": row[1],
                "start": f"{row[2][:2]}:{row[2][2:]}",
                "end": f"{end_h % 24:02d}:{end_m:02d}",
                "remaining": remaining,
                "duration": row[3],
                "title": row[4],
                "pfm": row[5],
                "url": row[6],
                "prog_id": row[7],
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
    table.add_column("終了", style="green")
    table.add_column("残り", style="yellow", justify="right")
    table.add_column("番組名", style="bold")
    table.add_column("パーソナリティ", style="magenta")
    table.add_column("番組ID", style="dim")
    table.add_column("URL", style="blue", overflow="fold")

    for p in rows:
        remaining_str = f"{p['remaining']}分" if p['remaining'] >= 1 else "間もなく終了"
        table.add_row(p["station_id"], p["station_name"], p["start"], p["end"], remaining_str, p["title"], p["pfm"], p["prog_id"] or "-", p["url"] or "-")

    console.print(table)

@cli.command("show-program")
@click.argument("prog_id")
def show_program(prog_id):
    """番組IDを指定して番組の詳細情報を表示"""
    ensure_tables_exist()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT p.station_id, COALESCE(s.name, p.station_id), p.date, p.weekday,
               p.ftime, p.duration, p.title, p.pfm, p.url, p.info, p.image_url, p.prog_id
        FROM programs p
        LEFT JOIN stations s ON p.station_id = s.station_id
        WHERE p.prog_id = ?
    """, (prog_id,))
    row = cur.fetchone()
    conn.close()

    if not row:
        console.print(f"[red]❌ 番組ID {prog_id} が見つかりません[/red]")
        return

    station_id, station_name, date, weekday, ftime, duration, title, pfm, url, info, image_url, prog_id_ = row
    start_h, start_m = int(ftime[:2]), int(ftime[2:])
    start_min = start_h * 60 + start_m
    end_min = start_min + duration
    end_h, end_m = divmod(end_min, 60)
    date_fmt = f"{date[:4]}-{date[4:6]}-{date[6:]}"

    from rich.panel import Panel
    from rich.table import Table as RichTable

    detail = RichTable.grid(padding=(0, 2))
    detail.add_column(style="bold dim", justify="right")
    detail.add_column()

    detail.add_row("番組ID",    prog_id_)
    detail.add_row("放送局",    f"{station_name} ({station_id})")
    detail.add_row("放送日",    f"{date_fmt} ({weekday})")
    detail.add_row("時間",      f"{start_h:02d}:{start_m:02d} – {end_h % 24:02d}:{end_m:02d}  ({duration}分)")
    detail.add_row("番組名",    f"[bold]{title}[/bold]")
    if pfm:
        detail.add_row("パーソナリティ", pfm)
    detail.add_row("URL",   f"[blue]{url}[/blue]" if url else "-")
    if image_url:
        detail.add_row("画像URL", f"[blue]{image_url}[/blue]")
    if info:
        detail.add_row("番組情報", info)

    console.print(Panel(detail, title=f"[bold cyan]📻 番組詳細[/bold cyan]", border_style="cyan"))

@cli.command("update-programs")
def update_db():
    """radiko API から番組表を取得してDBに更新"""
    ensure_tables_exist()
    try:
        stations = radiko_recorder.list_stations()
    except Exception as e:
        console.print(f"[red]❌ 放送局一覧の取得エラー: {e}[/red]")
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    inserted = 0

    progress = Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        "[progress.percentage]{task.percentage:>3.0f}%",
        TimeElapsedColumn(),
        TimeRemainingColumn(),
    )
    with progress:
        task = progress.add_task("⏳ 番組表取得中...", total=len(stations))
        for _sid, progs in radiko_programs.iter_station_programs(stations):
            for row in progs:
                if not all(row.get(k) for k in ("station_id", "prog_id", "date", "ftime")):
                    continue
                try:
                    cur.execute("""
                        INSERT OR REPLACE INTO programs (
                            station_id, prog_id, date, weekday, ftime, duration,
                            title, url, pfm, info, image_url
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            row["station_id"], row["prog_id"], row["date"],
                            row["weekday"], row["ftime"], row["duration"],
                            row["title"], row["url"], row["pfm"],
                            row["info"], row["image_url"],
                        ))
                    inserted += 1
                except Exception as e:
                    console.print(f"[red]❌ エラー: {e} 行: {row}[/red]")
            progress.advance(task)

    conn.commit()
    conn.close()
    console.print(f"[green]✅ {inserted} 件の番組をDBに登録しました[/green]")
    _update_stations_inner(stations)

def _update_stations_inner(stations=None):
    if stations is None:
        try:
            stations = radiko_recorder.list_stations()
        except Exception as e:
            console.print(f"[red]❌ 放送局一覧の取得エラー: {e}[/red]")
            return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    inserted = 0

    for station_id, name in stations:
        try:
            cur.execute("""
                INSERT OR REPLACE INTO stations (service, station_id, name)
                VALUES (?, ?, ?)
            """, ("radiko", station_id, name))
            inserted += 1
        except Exception as e:
            console.print(f"[red]❌ エラー: {e} 局: {station_id}[/red]")

    conn.commit()
    conn.close()
    console.print(f"[green]✅ {inserted} 局を登録[/green]")

@cli.command("update-stations")
def update_stations():
    """radiko API から放送局一覧を更新"""
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
    subprocess.run(["pkill", "ffplay"], check=False)
    console.print(f"[cyan]🎵 再生をバックグラウンドで開始: {station_id}[/cyan]")

    try:
        subprocess.Popen(
            ["python", RECORDER_PATH, "play", station_id, "--live"],
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
        # radiko_recorder.py の play プロセスから放送局IDを探す
        result = subprocess.run(["ps", "ax"], capture_output=True, text=True)
        station_id = None
        for line in result.stdout.splitlines():
            m = re.search(r'radiko_recorder\.py\s+play\s+(\S+)', line)
            if m:
                station_id = m.group(1)
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
    SELECT p.prog_id, p.date, p.ftime, p.station_id, COALESCE(s.name, p.station_id), p.duration, p.title, p.pfm, p.url
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
    table.add_column("prog_id", style="dim")
    table.add_column("日付", style="green")
    table.add_column("開始", style="cyan")
    table.add_column("局ID", style="dim")
    table.add_column("局名", style="bold")
    table.add_column("分", style="cyan", justify="right")
    table.add_column("番組名", style="magenta")
    table.add_column("パーソナリティ", style="dim")

    for prog_id, date, ftime, station_id, name, duration, title, pfm, url in results:
        table.add_row(
            prog_id,
            f"{date[:4]}/{date[4:6]}/{date[6:]}",
            f"{ftime[:2]}:{ftime[2:]}",
            station_id,
            name,
            str(duration),
            title,
            pfm,
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
        cmd_lines = [l for l in detail.stdout.splitlines() if "radiko_recorder.py" in l]
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

        # コマンド: python ... radiko_recorder.py record {sid} --live {dur} -o {out}
        sm = re.search(r'radiko_recorder\.py\s+record\s+(\S+)', cmd)
        dm = re.search(r'--live\s+(\S+)', cmd)
        om = re.search(r'-o\s+(\S+)', cmd)
        station = sm.group(1) if sm else "-"
        dur = dm.group(1) if dm else "-"
        out = om.group(1) if om else "-"

        jobs.append((job_id, scheduled_label, station, dur, out))

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
@click.argument("station_id", required=False)
@click.argument("date", required=False)
@click.argument("ftime", required=False)
def embed_art(audio_file, station_id, date, ftime):
    """録音ファイルに番組のカバーアートを埋め込む

    STATION_ID / DATE / FTIME を省略するとファイル名から自動判別する
    （{局ID}_{日付}_{開始時刻}_... 形式）。

    例: embed-art YFM_20260531_0800_朝の番組.m4a
    例: embed-art recording.m4a YFM 20260531 0800
    """
    ensure_tables_exist()
    if not (station_id and date and ftime):
        parsed = parse_recording_filename(audio_file)
        if not parsed:
            console.print(
                f"[red]❌ ファイル名から局ID/日付/時刻を判別できません: {Path(audio_file).name}[/red]\n"
                "[dim]STATION_ID DATE FTIME を引数で指定してください[/dim]"
            )
            return
        station_id, date, ftime = parsed
        console.print(f"[dim]ファイル名から判別: {station_id} {date} {ftime}[/dim]")

    info = get_program_info_at_time(station_id, date, ftime)
    if not info:
        console.print(f"[yellow]⚠ {station_id} {date} {ftime} に該当する番組が DB に見つかりません[/yellow]")
        return
    url, title, image_url = info
    if not url and not image_url:
        console.print(f"[yellow]⚠ 番組 '{title}' の画像情報が登録されていません[/yellow]")
        return

    console.print(f"[blue]🖼 アートワークを取得中...[/blue]")
    result = fetch_art(image_url, url)
    if not result:
        console.print("[yellow]⚠ アートワーク画像が見つかりませんでした[/yellow]")
        return
    image_bytes, ext = result

    console.print("[blue]🎨 カバーアートを埋め込み中...[/blue]")
    if embed_cover_art(audio_file, image_bytes, ext):
        console.print(f"[green]✅ カバーアートを埋め込みました: {audio_file}[/green]")
    else:
        console.print("[red]❌ カバーアートの埋め込みに失敗しました[/red]")

@cli.command("list-recordings")
@click.option("--dir", "directory", default=".", show_default=True, help="走査するディレクトリ")
def list_recordings(directory):
    """ディレクトリ内の録音ファイル（*.m4a）を一覧表示

    表示されるファイル名・局ID・日付・時刻はそのまま embed-art に渡せます。
    """
    ensure_tables_exist()
    base = Path(directory)
    files = sorted(base.glob("*.m4a"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not files:
        console.print(f"[blue]📭 {base} に録音ファイル(*.m4a)は見つかりませんでした[/blue]")
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    table = Table(title=f"🎙 録音ファイル一覧（{base}）", show_lines=False)
    table.add_column("ファイル名", style="bold", overflow="fold")
    table.add_column("サイズ", style="cyan", justify="right")
    table.add_column("局ID", style="dim")
    table.add_column("日付", style="green")
    table.add_column("開始", style="green")
    table.add_column("番組名", style="magenta")

    for f in files:
        size = fmt_size(f.stat().st_size)
        parsed = parse_recording_filename(f.name)
        if not parsed:
            table.add_row(f.name, size, "-", "-", "-", "[dim]（名前解析不可）[/dim]")
            continue
        sid, date, ftime = parsed
        cur.execute(
            """SELECT p.title FROM programs p
               WHERE p.station_id = ? AND p.date = ? AND p.ftime = ? LIMIT 1""",
            (sid, date, ftime),
        )
        row = cur.fetchone()
        title = row[0] if row else "[dim]（DB未登録）[/dim]"
        table.add_row(f.name, size, sid, f"{date[:4]}/{date[4:6]}/{date[6:]}", f"{ftime[:2]}:{ftime[2:]}", title)

    conn.close()
    console.print(table)

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
        f"python {RECORDER_PATH} record {station_id} --live {actual_duration_mins} -o {output}"
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

    client = radiko_recorder.RadikoClient()
    try:
        ok = client.record_timefree(station_id, from_time, duration, output)
    finally:
        client.logout()
    if not ok:
        console.print("[red]❌ 録音失敗[/red]")
        return

    console.print(f"[green]✅ 録音完了: {output}[/green]")

    if with_art:
        info = get_program_info_at_time(station_id, date, ftime)
        if not info or (not info[0] and not info[2]):
            console.print("[yellow]⚠ カバーアート: 番組の画像情報が見つかりません[/yellow]")
            return
        url, _, image_url = info
        console.print(f"[blue]🖼 アートワークを取得中...[/blue]")
        art = fetch_art(image_url, url)
        if not art:
            console.print("[yellow]⚠ アートワーク画像が見つかりませんでした[/yellow]")
            return
        image_bytes, ext = art
        if embed_cover_art(output, image_bytes, ext):
            console.print(f"[green]✅ カバーアートを埋め込みました[/green]")
        else:
            console.print("[red]❌ カバーアートの埋め込みに失敗しました[/red]")

@cli.command("record")
@click.argument("prog_id")
@click.option("--start-offset", "start_offset_str", default="0s", show_default=True,
              metavar="OFFSET", help="録音開始オフセット（例: 1m, 90s）")
@click.option("--end-offset", "end_offset_str", default="0s", show_default=True,
              metavar="OFFSET", help="録音終了オフセット（例: 30s, 2m）")
@click.option("--output", "-o", default=None, help="出力ファイルパス（省略時は自動生成）")
@click.option("--with-art", "with_art", is_flag=True, help="番組のカバーアートを埋め込む")
@click.option("--register", is_flag=True, help="未来の番組をatコマンドで登録する")
def record_by_prog_id(prog_id, start_offset_str, end_offset_str, output, with_art, register):
    """prog_id を指定して録音（過去→タイムフリー、未来→atスケジュール、放送中→即時）

    例: record 13392705 --with-art
    """
    ensure_tables_exist()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT p.station_id, p.date, p.ftime, p.duration, p.title, COALESCE(s.name, p.station_id)
        FROM programs p
        LEFT JOIN stations s ON p.station_id = s.station_id
        WHERE p.prog_id = ?
    """, (prog_id,))
    row = cur.fetchone()
    conn.close()

    if not row:
        console.print(f"[yellow]⚠ prog_id '{prog_id}' が DB に見つかりません[/yellow]")
        return

    station_id, date, ftime, duration, title, station_name = row
    scheduled_start = datetime.strptime(f"{date}{ftime}", "%Y%m%d%H%M")
    scheduled_end = scheduled_start + timedelta(minutes=duration)
    now = datetime.now()

    safe_title = re.sub(r'[^\w　-鿿゠-ヿ぀-ゟ]', '_', title)[:40]
    if not output:
        output = str(Path.cwd() / f"{station_id}_{date}_{ftime}_{safe_title}.m4a")
    else:
        output = str(Path(output).resolve())

    console.print(f"[bold]📻 {station_name} / {title}[/bold]")
    console.print(f"   {scheduled_start.strftime('%Y-%m-%d %H:%M')} - {scheduled_end.strftime('%H:%M')} ({duration}分)")

    if scheduled_end < now:
        # 過去の番組 → タイムフリー
        console.print("[blue]→ 過去の番組: タイムフリー録音[/blue]")
        from_time = f"{date}{ftime}00"
        client = radiko_recorder.RadikoClient()
        try:
            ok = client.record_timefree(station_id, from_time, duration, output)
        finally:
            client.logout()
        if not ok:
            console.print("[red]❌ 録音失敗[/red]")
            return
        console.print(f"[green]✅ 録音完了: {output}[/green]")

    elif scheduled_start > now:
        # 未来の番組 → at スケジュール
        console.print("[blue]→ 未来の番組: at スケジュール録音[/blue]")
        start_offset_secs = parse_offset(start_offset_str)
        end_offset_secs = parse_offset(end_offset_str)
        actual_start = scheduled_start + timedelta(seconds=start_offset_secs)
        actual_duration_mins = math.ceil((duration * 60 + end_offset_secs) / 60)

        cli_path = str(Path(__file__).resolve())
        radish_cmd = (
            f"python {RECORDER_PATH} record {station_id} --live {actual_duration_mins} -o {output}"
        )
        if with_art:
            radish_cmd += f" && python {cli_path} embed-art {output} {station_id} {date} {ftime}"

        at_time = actual_start.strftime("%H:%M %Y-%m-%d")
        console.print(f"  録音開始: {actual_start.strftime('%Y-%m-%d %H:%M:%S')}")
        console.print(f"  録音時間: {fmt_duration(duration * 60 + end_offset_secs)}")
        console.print(f"  出力: {output}")
        console.print()
        console.print(f"[bold]at コマンド:[/bold]")
        console.print(f"  [green]echo '{radish_cmd}' | at {at_time}[/green]")

        if register:
            try:
                result = subprocess.run(
                    ["at", actual_start.strftime("%H:%M"), actual_start.strftime("%Y-%m-%d")],
                    input=radish_cmd + "\n", text=True, capture_output=True,
                )
                if result.returncode == 0:
                    console.print(f"[green]✅ at ジョブを登録しました[/green]")
                    if result.stderr:
                        console.print(f"[dim]{result.stderr.strip()}[/dim]")
                else:
                    console.print(f"[red]❌ at 登録失敗: {result.stderr.strip()}[/red]")
            except FileNotFoundError:
                console.print("[red]❌ at コマンドが見つかりません（sudo apt install at）[/red]")
        return

    else:
        # 放送中 → 即時録音
        remaining = int((scheduled_end - now).total_seconds() / 60) + 1
        console.print(f"[blue]→ 放送中: 残り約 {remaining} 分を即時録音[/blue]")
        client = radiko_recorder.RadikoClient()
        try:
            ok = client.record_live(station_id, remaining, output)
        finally:
            client.logout()
        if not ok:
            console.print("[red]❌ 録音失敗[/red]")
            return
        console.print(f"[green]✅ 録音完了: {output}[/green]")

    if with_art:
        info = get_program_info_at_time(station_id, date, ftime)
        if info and (info[0] or info[2]):
            url_val, _, image_url_val = info
            console.print(f"[blue]🖼 アートワークを取得中...[/blue]")
            art = fetch_art(image_url_val, url_val)
            if art:
                image_bytes, ext = art
                if embed_cover_art(output, image_bytes, ext):
                    console.print("[green]✅ カバーアートを埋め込みました[/green]")
                else:
                    console.print("[red]❌ カバーアートの埋め込みに失敗しました[/red]")
            else:
                console.print("[yellow]⚠ アートワーク画像が見つかりませんでした[/yellow]")

if __name__ == "__main__":
    cli()
