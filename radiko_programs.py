"""radiko の週間番組表を取得して DB スキーマ相当の dict を生成するモジュール。

旧 `rx2`（bash + xmlstarlet + moreutils）を置き換える。HTTP は標準ライブラリの
urllib、XML 解析は ElementTree のみで行い、外部コマンド依存をなくす。

各局の週間番組 XML（http://radiko.jp/v3/program/station/weekly/{id}.xml）を取得し、
programs テーブルのカラムに対応する dict を返す:
    station_id, prog_id, date, weekday, ftime, duration, title, url, pfm, info, image_url
"""

import html
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from radiko_recorder import _get, list_stations

WEEKLY_URL = "http://radiko.jp/v3/program/station/weekly/{station_id}.xml"
MAX_WORKERS = 4

# date +%a (LANG=C) 相当。曜日番号(月=0)→英語3文字。
_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _detag(text):
    """info の HTML タグを除去し空白を整える（rx2 の --detag 相当）。"""
    if not text:
        return ""
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", text)).strip()


def _clean(text):
    """title / pfm の改行・連続空白を 1 つの空白に正規化する。"""
    if not text:
        return ""
    return _WS_RE.sub(" ", text).strip()


def _parse_station_xml(xml_bytes):
    """週間番組 XML（1 局以上）を programs 用 dict のリストに変換する。"""
    root = ET.fromstring(xml_bytes)
    out = []
    for station in root.iter("station"):
        sid = station.get("id")
        if not sid:
            continue
        for prog in station.iter("prog"):
            ft = prog.get("ft") or ""
            if len(ft) < 12:
                continue
            date = ft[:8]
            try:
                weekday = _WEEKDAYS[datetime.strptime(date, "%Y%m%d").weekday()]
            except ValueError:
                weekday = ""
            out.append({
                "station_id": sid,
                "prog_id": prog.get("id") or "",
                "date": date,
                "weekday": weekday,
                "ftime": ft[8:12],
                "duration": int(prog.get("dur") or 0) // 60,
                "title": _clean(prog.findtext("title")),
                "url": (prog.findtext("url") or "").strip(),
                "pfm": _clean(prog.findtext("pfm")),
                "info": _detag(prog.findtext("info")),
                "image_url": (prog.findtext("img") or "").strip(),
            })
    return out


def _fetch_station(station_id):
    try:
        xml, _ = _get(
            WEEKLY_URL.format(station_id=station_id),
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=30,
        )
        return station_id, _parse_station_xml(xml)
    except Exception:
        return station_id, []


def iter_station_programs(stations=None, max_workers=MAX_WORKERS):
    """各局の番組表を並列取得し (station_id, [program dict, ...]) を順に yield する。

    stations: (station_id, name) のリスト。省略時は radiko の全局を取得する。
    """
    if stations is None:
        stations = list_stations()
    sids = [s[0] for s in stations]
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        yield from ex.map(_fetch_station, sids)


def fetch_all_programs(stations=None, max_workers=MAX_WORKERS):
    """全局の番組 dict をまとめて 1 つのリストで返す。"""
    out = []
    for _sid, progs in iter_station_programs(stations, max_workers):
        out.extend(progs)
    return out
