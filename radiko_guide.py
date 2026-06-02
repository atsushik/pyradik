"""番組表 DB（radiko.db）の更新をライブラリとして提供する。

radiko_cli の update-programs と同等の処理を rich 非依存で行い、サーバ内の
保守ループから呼べるようにする。スキーマは radiko_cli.create_tables と一致。
"""

import sqlite3

import radiko_config
import radiko_programs
import radiko_recorder

_PROGRAMS_DDL = """
CREATE TABLE IF NOT EXISTS programs (
    station_id TEXT, prog_id TEXT, date TEXT, weekday TEXT, ftime TEXT,
    duration INTEGER, title TEXT, url TEXT, pfm TEXT, info TEXT, image_url TEXT,
    PRIMARY KEY (station_id, prog_id)
)
"""
_STATIONS_DDL = """
CREATE TABLE IF NOT EXISTS stations (
    station_id TEXT PRIMARY KEY, service TEXT, name TEXT
)
"""


def refresh_guide():
    """全局の週間番組表＋放送局一覧を radiko.db に反映し、登録件数を返す。"""
    stations = radiko_recorder.list_stations()
    conn = sqlite3.connect(str(radiko_config.DB_PATH))
    cur = conn.cursor()
    cur.execute(_PROGRAMS_DDL)
    cur.execute(_STATIONS_DDL)

    inserted = 0
    for _sid, progs in radiko_programs.iter_station_programs(stations):
        for row in progs:
            if not all(row.get(k) for k in ("station_id", "prog_id", "date", "ftime")):
                continue
            cur.execute(
                """INSERT OR REPLACE INTO programs
                   (station_id, prog_id, date, weekday, ftime, duration,
                    title, url, pfm, info, image_url)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (row["station_id"], row["prog_id"], row["date"], row["weekday"],
                 row["ftime"], row["duration"], row["title"], row["url"],
                 row["pfm"], row["info"], row["image_url"]),
            )
            inserted += 1

    for station_id, name in stations:
        cur.execute(
            "INSERT OR REPLACE INTO stations (service, station_id, name) VALUES (?,?,?)",
            ("radiko", station_id, name),
        )

    conn.commit()
    conn.close()
    return inserted
