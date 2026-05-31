import rich_click as click
import sqlite3
import subprocess
from datetime import datetime
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
        with progress:
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

                # 最初に MAX_WORKERS 件まで submit
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
    SELECT p.date, p.ftime, COALESCE(s.name, p.station_id), p.title, p.pfm, p.url
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
    table.add_column("局名", style="bold")
    table.add_column("番組名", style="magenta")
    table.add_column("パーソナリティ", style="dim")
    table.add_column("URL", style="blue", overflow="fold")

    for date, ftime, name, title, pfm, url in results:
        table.add_row(
            f"{date[:4]}/{date[4:6]}/{date[6:]}",
            f"{ftime[:2]}:{ftime[2:]}",
            name,
            title,
            pfm,
            url or "-"
        )

    console.print(table)

if __name__ == "__main__":
    cli()
