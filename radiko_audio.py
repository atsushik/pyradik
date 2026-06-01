"""オーディオ出力デバイスと音量の取得・設定。

PipeWire の `wpctl`（WirePlumber）に subprocess で委譲する。
Raspberry Pi OS（PipeWire 構成）を前提に実装しており、PulseAudio のみ /
ALSA のみ等の他環境では動作しないことがある。

操作対象は常にデフォルト出力先（`@DEFAULT_AUDIO_SINK@`）。アプリ単位ではなく
デバイス全体の音量・デバイス名を扱う。
"""

import re
import subprocess

SINK = "@DEFAULT_AUDIO_SINK@"


def _wpctl(*args):
    """wpctl を実行し CompletedProcess を返す。wpctl 不在時は None。"""
    try:
        return subprocess.run(
            ["wpctl", *args], capture_output=True, text=True
        )
    except FileNotFoundError:
        return None


def available() -> bool:
    """wpctl が使えて PipeWire と通信できるか（＝この機能が利用可能か）。"""
    r = _wpctl("status")
    return r is not None and r.returncode == 0


def get_volume() -> float | None:
    """デフォルト出力先の音量（0.0〜）を返す。取得できなければ None。"""
    r = _wpctl("get-volume", SINK)
    if r is None or r.returncode != 0:
        return None
    m = re.search(r"Volume:\s*([\d.]+)", r.stdout)
    return float(m.group(1)) if m else None


def is_muted() -> bool | None:
    r = _wpctl("get-volume", SINK)
    if r is None or r.returncode != 0:
        return None
    return "MUTED" in r.stdout


def set_volume(vol: float) -> bool:
    """デフォルト出力先の音量を vol（0.0〜1.0）に設定。成功で True。"""
    vol = max(0.0, min(1.0, float(vol)))  # 1.0 超は増幅になるためクランプ
    r = _wpctl("set-volume", SINK, f"{vol}")
    return r is not None and r.returncode == 0


def current_device() -> str | None:
    """現在のデフォルト出力先デバイス名を返す。取得できなければ None。"""
    r = _wpctl("status")
    if r is None or r.returncode != 0:
        return None
    in_sinks = False
    for line in r.stdout.splitlines():
        if "Sinks:" in line:
            in_sinks = True
            continue
        if in_sinks and "Sources:" in line:
            break
        if in_sinks and "*" in line:  # デフォルト Sink を示す行
            m = re.search(r"\d+\.\s+(.+?)\s+\[vol", line)
            if m:
                return m.group(1).strip()
    return None
