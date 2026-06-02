"""環境変数で上書きできる設定の一元管理。

既定値はプロジェクト直下を使うため、従来どおり（カレントで実行）でも動く。
Docker / 別データディレクトリ運用時は PYRADIK_DATA_DIR 等で差し替える。
"""

import os
from pathlib import Path

VERSION = "0.2.0"

_PROJECT_DIR = Path(__file__).resolve().parent

# 録音 DB・状態 DB・録音ファイルなどを置くデータディレクトリ
DATA_DIR = Path(os.environ.get("PYRADIK_DATA_DIR", _PROJECT_DIR)).resolve()

DB_PATH = DATA_DIR / "radiko.db"            # 番組表（init-db で再生成される）
STATE_DB_PATH = DATA_DIR / "state.db"       # 予約・ルール・ジョブ（init-db では消さない）
ENABLED_STATIONS_PATH = DATA_DIR / "enabled_stations.txt"
RECORDINGS_DIR = Path(os.environ.get("PYRADIK_RECORDINGS_DIR", DATA_DIR)).resolve()

HOST = os.environ.get("PYRADIK_HOST", "0.0.0.0")
PORT = int(os.environ.get("PYRADIK_PORT", "8470"))

# 番組表の自動更新（保守ループ）間隔。既定 6 時間。
GUIDE_REFRESH_SEC = int(os.environ.get("PYRADIK_GUIDE_REFRESH_SEC", str(6 * 3600)))

# 将来の認証用プレースホルダ（現状は未使用）
AUTH_TOKEN = os.environ.get("PYRADIK_AUTH_TOKEN") or None

RECORDER_PATH = str(_PROJECT_DIR / "radiko_recorder.py")
CLI_PATH = str(_PROJECT_DIR / "radiko_cli.py")
JOBRUNNER_PATH = str(_PROJECT_DIR / "radiko_jobrunner.py")

# データ/録音ディレクトリは存在しなければ作成（カスタム DATA_DIR 運用に対応）
DATA_DIR.mkdir(parents=True, exist_ok=True)
RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
