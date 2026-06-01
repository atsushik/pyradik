# pyradik

[radiko](http://radiko.jp/) で配信中・配信済みの番組を保存・再生するツール群です。配信形式と同じフォーマットで保存するため、別形式へのエンコードは行いません。

うる。 ([@uru_2](https://twitter.com/uru_2)) による `radi.sh` をベースに、Python で再構成し、CLI・録音エンジン・Web UI などの機能を追加したものです（原作は radiko / NHK らじる★らじる / ListenRadio に対応していましたが、本リポジトリは radiko に絞っています）。


## スクリーンショット

### 番組表

![番組表](img/番組表.png)

- 聴取可能な放送局と番組を一覧できます
- 放送局をクリックするとその放送局が再生されます

### 番組情報

![番組情報](img/番組情報.png)

- 番組をクリックすると番組情報を確認できます
- 放送済もしくは放送中の番組はその場で再生できます
- 未放送の番組は録音を予約できます

### 検索

![検索](img/検索.png)

- 番組名・パーソナリティ・番組情報からキーワード検索できます
- 聴取可能な放送局のみ／全局、放送状態（放送済・放送中・未放送）で絞り込めます


## 構成

| ファイル | 役割 |
|:-|:-|
| `radiko_cli.py` | メイン CLI。番組表 DB の更新・検索、録音、スケジュール、再生制御、音量操作、カバーアート埋め込み |
| `radiko_recorder.py` | 録音・再生エンジン。radiko 認証〜HLS URL 取得を標準ライブラリのみで実装し、取得は ffmpeg / ffplay に委譲。CLI / Web UI 両方から利用される |
| `radiko_programs.py` | radiko API から週間番組表を取得し DB スキーマ相当の dict を生成（urllib + ElementTree のみ） |
| `radiko_audio.py` | 出力デバイス・音量の取得／設定（PipeWire の `wpctl` に委譲、Raspberry Pi OS 想定） |
| `web_ui/web_ui.py` | FastAPI 製 REST API（ポート 8470） |
| `web_ui/static/index.html` | シングルページ Web UI（番組表・検索・録音・再生・予約管理） |

### データの流れ

```
radiko API ──→ radiko_programs.py ──→ radiko_cli.py update-programs ──→ radiko.db (SQLite)
                                                                          │ 参照
        radiko_cli.py (CLI)  ──┐                                          │
        web_ui.py (REST API) ──┴─→ radiko_recorder.py ────────────────────┴─→ ffmpeg / ffplay
                                   (認証・HLS URL 取得)
```


## 必要なもの

### 共通

- FFmpeg（3.x 以降、AAC / HLS サポート）— 録音・再生に使用
- Python 3

```
sudo apt install ffmpeg
```

### radiko_cli.py

```
pip install -r requirements.txt   # rich, rich-click
```

Raspberry Pi OS / Debian 系では apt でもインストールできます。
```
sudo apt install python3-rich python3-rich-click
```

`schedule-record` などの予約録音には `at` も必要です。

```
sudo apt install at
sudo systemctl enable --now atd
```

### web_ui/web_ui.py（Web UI）

```
pip install fastapi uvicorn
# 環境によっては: pip install --break-system-packages fastapi uvicorn
```


## 使い方（radiko_cli.py）

### 初回セットアップ

番組表を取得して DB に格納します。

```
$ python radiko_cli.py update-programs   # 番組表＋放送局を更新
$ python radiko_cli.py auto-enable       # 受信可能な放送局を enabled_stations.txt に書き出す
```

### よく使うコマンド

```
$ python radiko_cli.py search 秀島史香             # 番組名・パーソナリティ・説明文から検索
$ python radiko_cli.py show-now                   # 現在放送中の番組を表示（受信可能局のみ）
$ python radiko_cli.py show-program 13392705      # 番組 ID で詳細表示
$ python radiko_cli.py record 13392705 --with-art # 録音（方法は自動判定、カバーアート埋め込み）
$ python radiko_cli.py play YFM                   # ライブ再生（バックグラウンド）
$ python radiko_cli.py stop                       # 再生停止
```

### コマンド一覧

| コマンド | 説明 |
|:-|:-|
| `update-programs` | radiko API から番組表・放送局を DB に更新 |
| `update-stations` | 放送局一覧のみ更新 |
| `list-stations` | DB に登録された放送局一覧を表示 |
| `auto-enable` | 受信可能な放送局を検出し `enabled_stations.txt` に書き出す |
| `show-now` | 現在放送中の番組を表示（`enabled_stations.txt` 準拠、なければ全局） |
| `show-program <prog_id>` | 番組 ID で詳細表示 |
| `search <keyword>` | 番組名・パーソナリティ・説明文を全文検索 |
| `record <prog_id>` | 録音。過去→タイムフリー、未来→`at` 予約、放送中→即時録音を自動判定 |
| `timefree-record` | タイムフリー録音（放送後 7 日以内） |
| `schedule-record` | `at` による予約録音を生成・登録 |
| `list-schedules` / `cancel-schedule` | `at` 予約の一覧・取り消し |
| `list-recordings` | ディレクトリ内の録音ファイル（*.m4a）を一覧表示（`--dir` で指定、既定はカレント） |
| `play` / `stop` / `now-playing` | ライブ再生・停止・再生中番組の表示 |
| `volume [LEVEL]` | 音量の取得（引数なし）／設定（`0.0`〜`1.0`）と出力デバイスの表示 |
| `embed-art` | 録音ファイルに番組のカバーアートを埋め込む（局ID/日付/時刻はファイル名から自動判別） |
| `init-db` | DB（stations / programs）を初期化 |

ヘルプは各コマンドに `--help` を付けると表示されます。

> **タイムフリー録音推奨**: radiko のライブ配信はタイムラグが大きいため、過去番組のタイムフリー保存のほうが速く、ラグも小さくなります（原作者の知見）。`record` コマンドは放送終了後に自動でタイムフリーへ切り替わります。


## Web UI

```
$ python web_ui/web_ui.py
# → http://localhost:8470
```

ブラウザから以下が行えます。

- **番組表**: タイムテーブル形式で表示、放送局クリックでライブ再生、番組クリックで録音・再生
- **検索**: キーワード検索。聴取可能な放送局のみ／全局、放送状態（放送済・放送中・未放送）の絞り込みに対応
- **録音ファイル**: 一覧・ダウンロード・削除、録音中の状態表示
- **録音予約一覧**: `at` 予約の確認・取り消し

再生・録音・予約の状態は Server-Sent Events で配信され、複数ブラウザ間でリアルタイムに同期されます。事前に `radiko_cli.py update-programs` で番組表を取得しておく必要があります。


## 注意点

- 録音手法は radiko の仕様変更等で利用できなくなる可能性があります。
- radiko のタイムフリーは放送後 7 日以内が対象です。
- 音量・出力デバイスの取得／設定（CLI の `volume`、Web UI の音量操作）は PipeWire の `wpctl` に委譲しています。**Raspberry Pi OS（PipeWire 構成）を想定した実装**のため、PulseAudio のみ・ALSA のみといった他環境では動作しないことがあります。


## 作った人

- 原作 `radi.sh`: うる。 ([@uru_2](https://twitter.com/uru_2))
- Python での再構成・機能追加（CLI・録音エンジン・Web UI）: フォーク版メンテナ


## ライセンス

[MIT License](LICENSE)


## 技術情報（実装メモ）

過去の調査で得た、radiko API・データ仕様に関する知見をまとめます。

### タイムフリー再生・録音には `start_at` / `end_at` が必須

過去番組を `ft` / `to`（再生開始・終了時刻）だけ指定して playlist URL を叩くと、radiko はそれらを無視して**現在時刻のライブ配信**を返します。現在の仕様では `start_at` / `end_at` パラメータが必須です。

```
{playlist_create_url}
  ?station_id={sid}
  &start_at={ft}&ft={ft}
  &end_at={to}&to={to}
  &l=15&type=b&lsid=
```

| URL | 先頭セグメントの内容時刻 |
|---|---|
| `ft`/`to` のみ | 現在（＝ライブ）❌ |
| `start_at`/`end_at` 追加 | 要求どおりの過去時刻 ✅ |

`radiko_recorder.py` の `timefree_url()` に反映済み。録音・再生の両方がこの URL を共有します。なお、旧 v2 API（`radiko.jp/v2/api/ts/playlist.m3u8`）は廃止済み（404）です。

### 番組情報（`info`）に含まれる HTML

radiko の番組情報（`info`）は外部由来で、約 6 割が HTML を含みます（`radiko.db` 実測）。出現の多いタグは `<br>`（改行）、`<table>` / `<tr>` / `<td>`（**データ表ではなく出演者写真を横並びにするレイアウト用**）、`<div>`（ラッパー）、`<img>`（出演者サムネ、`alt` に氏名）、`<a>`（リンク）など。典型構造は「上部に出演者写真のレイアウトテーブル＋下に本文テキスト」。

表示・整形時の注意点:

- **XSS**: `info` は外部由来。タグ除去によるプレーンテキスト化なら除去後に HTML エスケープすれば安全。実 HTML として描画する場合はタグ／属性の allowlist によるサニタイズが必須（特に `<a href>` の `javascript:` 排除）。
- **検索ハイライトとの順序**: 検索結果は `info` 内の検索語を `<mark>` で囲む。整形と併用する場合は「タグ除去 → エスケープ → ハイライト挿入」の順を守る。
- 出演者写真は番組アートワーク（`image_url`）が別にあるため、本文からは除去しても実害は小さい。
