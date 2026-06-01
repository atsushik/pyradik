#!/usr/bin/env python3
"""radiko タイムフリー / ライブの録音・再生を純 Python で行うモジュール。

認証〜ストリーム URL 取得までを標準ライブラリのみで実装し、
HLS の取得そのものは ffmpeg / ffplay に委譲する（原作 radi.sh と同方式）。

CLI 例:
    # タイムフリー録音（放送後 7 日以内）
    python radiko_recorder.py record YFM 20260531000000 30 -o out.m4a
    # ライブ録音（30 分）
    python radiko_recorder.py record YFM --live 30 -o out.m4a
    # タイムフリー再生
    python radiko_recorder.py play YFM 20260531000000 30
    # ライブ再生
    python radiko_recorder.py play YFM --live

ライブラリ例:
    from radiko_recorder import RadikoClient
    cli = RadikoClient()                       # エリア内
    cli = RadikoClient("mail", "password")     # ラジコプレミアム（エリアフリー）
    cli.record_timefree("YFM", "20260531000000", 30, "out.m4a")
"""

import argparse
import base64
import json
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

# radiko の HLS 認証キー（https://radiko.jp/apps/js/playerCommon.js の固定値）
AUTHKEY_VALUE = "bcd151073c03b352e1ef2fd66c32209da9ca0afa"

AUTH1_URL = "https://radiko.jp/v2/api/auth1"
AUTH2_URL = "https://radiko.jp/v2/api/auth2"
LOGIN_URL = "https://radiko.jp/ap/member/webapi/member/login"
LOGOUT_URL = "https://radiko.jp/v4/api/member/logout"
STREAM_XML = "https://radiko.jp/v3/station/stream/pc_html5/{station_id}.xml"
REGION_XML = "https://radiko.jp/v3/station/region/full.xml"


def _get(url, headers=None, timeout=10):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(), {k.lower(): v for k, v in resp.headers.items()}


def list_stations():
    """radiko 全局の (station_id, name) リストを返す（認証不要）。"""
    xml, _ = _get(REGION_XML)
    root = ET.fromstring(xml)
    out, seen = [], set()
    for st in root.iter("station"):
        sid = st.findtext("id")
        if sid and sid not in seen:
            seen.add(sid)
            out.append((sid, st.findtext("name") or sid))
    return out


class RadikoClient:
    """radiko の認証状態を保持し、録音・再生コマンドを組み立てる。"""

    def __init__(self, mail=None, password=None):
        self.mail = mail
        self.password = password
        self.radiko_session = None
        self.authtoken = None
        # ログインできていればエリアフリー（プレミアム）扱い
        self.areafree = "0"
        self.area = None
        self._login()
        self._authorize()

    # ---- 認証 ----------------------------------------------------------

    def _login(self):
        """ラジコプレミアムにログイン（任意）。成功でエリアフリーになる。"""
        if not self.mail:
            return
        body = urllib.parse.urlencode({"mail": self.mail, "pass": self.password}).encode()
        req = urllib.request.Request(LOGIN_URL, data=body, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            j = json.loads(resp.read().decode())
        self.radiko_session = j.get("radiko_session")
        if not self.radiko_session or j.get("areafree") != "1":
            raise RuntimeError("ラジコプレミアムへのログインに失敗しました")
        self.areafree = "1"

    def _authorize(self):
        """auth1 → partial key → auth2 を実行し authtoken を確定する。"""
        _, h = _get(AUTH1_URL, headers={
            "X-Radiko-App": "pc_html5",
            "X-Radiko-App-Version": "0.0.1",
            "X-Radiko-Device": "pc",
            "X-Radiko-User": "dummy_user",
        })
        token = h.get("x-radiko-authtoken")
        offset = int(h.get("x-radiko-keyoffset"))
        length = int(h.get("x-radiko-keylength"))
        if not token:
            raise RuntimeError("auth1 に失敗しました")

        partial = base64.b64encode(
            AUTHKEY_VALUE[offset:offset + length].encode()
        ).decode()

        url = AUTH2_URL
        if self.radiko_session:
            url += f"?radiko_session={self.radiko_session}"
        body, _ = _get(url, headers={
            "X-Radiko-Device": "pc",
            "X-Radiko-User": "dummy_user",
            "X-Radiko-AuthToken": token,
            "X-Radiko-PartialKey": partial,
        })
        # auth2 のレスポンスは "JP14,神奈川県,kanagawa Japan" 形式
        self.authtoken = token
        self.area = body.decode().strip()

    def logout(self):
        if not self.radiko_session:
            return
        body = urllib.parse.urlencode({"radiko_session": self.radiko_session}).encode()
        try:
            urllib.request.urlopen(
                urllib.request.Request(LOGOUT_URL, data=body, method="POST"), timeout=10
            )
        except Exception:
            pass
        self.radiko_session = None

    # ---- ストリーム URL ------------------------------------------------

    def _playlist_base(self, station_id, timefree, prefer_second=False):
        """stream XML から条件に合う playlist_create_url を返す。

        属性の出現順は固定でないため ElementTree で属性選択する。
        ライブは _definst_ を含まない候補の 2 番目を優先（原作 radi.sh 準拠）。
        """
        xml, _ = _get(STREAM_XML.format(station_id=station_id))
        root = ET.fromstring(xml)
        candidates = []
        for url in root.findall("url"):
            if url.get("timefree") != timefree or url.get("areafree") != self.areafree:
                continue
            node = url.find("playlist_create_url")
            if node is None or not node.text:
                continue
            if "_definst_" in node.text:
                continue
            candidates.append(node.text)
        if not candidates:
            raise RuntimeError(f"{station_id}: 条件に合うストリーム URL がありません")
        if prefer_second and len(candidates) >= 2:
            return candidates[1]
        return candidates[0]

    def timefree_url(self, station_id, ft, to):
        # start_at / end_at が無いと radiko は ft/to を無視してライブ配信を返す。
        # この 2 つを付けて初めて指定時刻のタイムフリーが再生される。
        base = self._playlist_base(station_id, timefree="1")
        return (f"{base}?station_id={station_id}"
                f"&start_at={ft}&ft={ft}&end_at={to}&to={to}&l=15&type=b&lsid=")

    def live_url(self, station_id):
        base = self._playlist_base(station_id, timefree="0", prefer_second=True)
        return f"{base}?station_id={station_id}&l=15&type=c&lsid="

    def station_available(self, station_id):
        """現在の認証エリアでこの局のライブ配信を取得できるか（403 でないか）。"""
        try:
            uri = self.live_url(station_id)
        except Exception:
            return False
        req = urllib.request.Request(uri, headers={"X-Radiko-Authtoken": self.authtoken})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                r.read(1)
            return True
        except urllib.error.HTTPError as e:
            return e.code != 403
        except Exception:
            # ネットワーク不安定時は楽観的に有効扱い（bash 版の timeout 挙動に合わせる）
            return True

    # ---- ffmpeg / ffplay ----------------------------------------------

    @property
    def _ff_headers(self):
        # CDN が Range: bytes=0- に誤応答するため空 Range で上書きする
        return f"X-Radiko-Authtoken: {self.authtoken}\r\nRange: "

    def _run_ffmpeg(self, playlist_uri, output, duration_min=None):
        cmd = [
            "ffmpeg", "-loglevel", "error", "-fflags", "+discardcorrupt",
            "-headers", self._ff_headers,
            "-i", playlist_uri,
            "-acodec", "copy", "-vn", "-bsf:a", "aac_adtstoasc", "-y",
        ]
        if duration_min is not None:
            # ライブはストリームが終わらないので録音時間を指定
            cmd += ["-t", _hhmmss(duration_min)]
        cmd.append(output)
        return subprocess.run(cmd).returncode == 0

    def _run_ffplay(self, playlist_uri):
        cmd = [
            "ffplay", "-loglevel", "error", "-fflags", "+discardcorrupt",
            "-headers", self._ff_headers,
            "-i", playlist_uri, "-nodisp",
        ]
        return subprocess.run(cmd).returncode == 0

    # ---- 高レベル API --------------------------------------------------

    def record_timefree(self, station_id, ft, duration_min, output):
        """ft (YYYYMMDDHHMMSS) から duration_min 分を録音。タイムフリーは -t 不要。"""
        to = _add_minutes(ft, duration_min)
        uri = self.timefree_url(station_id, ft, to)
        return self._run_ffmpeg(uri, output, duration_min=None)

    def record_live(self, station_id, duration_min, output):
        uri = self.live_url(station_id)
        return self._run_ffmpeg(uri, output, duration_min=duration_min)

    def play_timefree(self, station_id, ft, duration_min):
        to = _add_minutes(ft, duration_min)
        uri = self.timefree_url(station_id, ft, to)
        return self._run_ffplay(uri)

    def play_live(self, station_id):
        uri = self.live_url(station_id)
        return self._run_ffplay(uri)


def _hhmmss(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}:00"


def _add_minutes(ft, minutes):
    dt = datetime.strptime(ft, "%Y%m%d%H%M%S") + timedelta(minutes=minutes)
    return dt.strftime("%Y%m%d%H%M%S")


# ---- CLI ---------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(description="radiko タイムフリー/ライブ 録音・再生")
    p.add_argument("--mail", help="ラジコプレミアム メールアドレス（エリアフリー）")
    p.add_argument("--password", help="ラジコプレミアム パスワード")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("record", help="録音")
    pr.add_argument("station_id")
    pr.add_argument("ft", nargs="?", help="開始時刻 YYYYMMDDHHMMSS（タイムフリー時）")
    pr.add_argument("duration", type=int, help="録音時間（分）")
    pr.add_argument("--live", action="store_true", help="ライブ録音")
    pr.add_argument("-o", "--output", required=True)

    pp = sub.add_parser("play", help="再生")
    pp.add_argument("station_id")
    pp.add_argument("ft", nargs="?", help="開始時刻 YYYYMMDDHHMMSS（タイムフリー時）")
    pp.add_argument("duration", type=int, nargs="?", help="再生時間（分・タイムフリー時）")
    pp.add_argument("--live", action="store_true", help="ライブ再生")

    args = p.parse_args(argv)
    client = RadikoClient(args.mail, args.password)
    try:
        if args.cmd == "record":
            if args.live:
                ok = client.record_live(args.station_id, args.duration, args.output)
            else:
                if not args.ft:
                    p.error("タイムフリー録音には ft（開始時刻）が必要です")
                ok = client.record_timefree(args.station_id, args.ft, args.duration, args.output)
            print(("✅ 録音完了: " + args.output) if ok else "❌ 録音失敗", file=sys.stderr)
            return 0 if ok else 1
        else:  # play
            if args.live:
                client.play_live(args.station_id)
            else:
                if not args.ft or args.duration is None:
                    p.error("タイムフリー再生には ft と duration が必要です")
                client.play_timefree(args.station_id, args.ft, args.duration)
            return 0
    finally:
        client.logout()


if __name__ == "__main__":
    sys.exit(main())
