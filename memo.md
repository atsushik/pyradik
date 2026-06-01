# タイムフリー再生調査メモ

## 問題

Web UI の番組表で過去の番組（昨日以前も含む）を選んで「再生」を押すと、
選んだ番組の先頭からではなく現在時刻からのリアルタイム再生になってしまう。

---

## 参考にしたリポジトリ

- https://github.com/rfriends/rfriends3  
  radiko タイムフリー録音・再生のPHP実装。スクリプト一式を zip でダウンロードして解析した。

---

## 調査で分かったこと

### 1. バックエンドのロジックは正しかった

`web_ui.py` の `play_program` は `scheduled_end <= now` のとき timefree、それ以外は live を選ぶ実装だったが、
**放送開始済み・まだ終了していない番組**（例: 17:00〜20:00 を 18:20 に選択）は
`scheduled_end > now` なので live 路線に入っていた。

→ 「過去の番組」とユーザーが感じていても backend は live 扱いにしていたケースがあった。  
→ `scheduled_start <= now` なら常に timefree を使うように変更済み。

### 2. radish-play.sh の timefree コマンドが実際には live を再生している

`radish-play.sh -t radiko -s FMT -f 20260531000000 -d 30 -m play` を実行すると  
ffplay が以下の URL で起動する：

```
https://tf-f-rpaa-radiko.smartstream.ne.jp/tf/playlist.m3u8
  ?station_id=FMT&ft=20260531000000&to=20260531003000&l=15&type=b&lsid=
```

この URL は HTTP 200 を返し、セッション URL を含む M3U8 を返す：

```
#EXTM3U
#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=52973,CODECS="mp4a.40.5"
https://tf-f-rpaa-radiko.smartstream.ne.jp/tf/medialist?session=1.xxxxxxx&station_id=FMT
```

**ところが、このセッションの medialist が常に「現在時刻」のセグメントを返す。**

```
#EXT-X-PROGRAM-DATE-TIME:2026-06-01T18:41:05.045+09:00   ← 今日の今の時刻
```

`ft=20260531000000`（昨日 00:00）を指定しているにもかかわらず、
タイムフリーではなくライブ配信のセグメントが来ている。

### 3. 試したこと（すべて live コンテンツが返ってきた）

| 試したこと | 結果 |
|---|---|
| areafree=0 URL (`tf-f-rpaa`) | 今日のライブ |
| areafree=1 URL (`tf-c-rpaa`) | 今日のライブ |
| `X-Radiko-AreaId: JP14` ヘッダー追加 | 今日のライブ |
| `type=b`, `type=c` | 今日のライブ（`a`,`d` はセッションなし） |
| `lsid` にランダム文字列 | 今日のライブ |
| `Referer` ヘッダー追加 | 今日のライブ |
| 深夜番組 → 昼番組に変更 | 今日のライブ |
| 認証トークンを取り直して即試行 | 今日のライブ |
| `ft`/`to` なし（パラメータ省略） | 今日のライブ（同じ） |

### 4. 参考実装（rfriends3）が使うエンドポイントは 404

rfriends3 は以下の v2 エンドポイントに `wget --post-data='flash=1'` で POST している：

```
https://radiko.jp/v2/api/ts/playlist.m3u8
  ?l=15&station_id=FMT&ft=20260531120000&to=20260531130000
```

実際に試したところ **HTTP 404 Not Found**。  
v2 API は廃止済みと思われる。  
v4, v5 も 404。

### 5. auth2 レスポンス

```
JP14,神奈川県,kanagawa Japan
```

エリア JP14（神奈川県）として正常に認証されている。  
ライブ再生は問題なく動作している。

### 6. web_ui.py の変更（ユーザー側）

調査中に `web_ui.py` が大幅に書き換えられた（`radiko_recorder.py` を import する方式）。  
現在の `web_ui.py` は `RadikoClient` クラスを使い、  
`record_timefree`, `record_live`, `play` などを呼び出す設計になっている模様。

---

## 現状の仮説

`tf-f-rpaa-radiko.smartstream.ne.jp/tf/playlist.m3u8` は  
**`ft`/`to` パラメータを auth token の権限確認なしに受け付け、
しかし実際にはライブセッションを返す**という動作をしている。

考えられる原因：
1. **認証トークンがタイムフリーに対応していない**  
   标準の auth1/auth2 で取得したトークンはライブ専用で、
   timefree には追加の認証ステップが必要かもしれない。
2. **v3 タイムフリー API の仕様変更**  
   `tf-f-rpaa` CDN の挙動が変わり、別のエンドポイントや
   パラメータが必要になった可能性。
3. **IP アドレス・地域制限**  
   ライブは動くのでこれは考えにくい。

---

## 未試行・次の手がかり

- `radiko_recorder.py` の実装内容を確認する（ユーザーが追加した新しいファイル）
- ブラウザの devtools で radiko.jp のタイムフリー再生の実際のリクエストを観察する
- radiko プレミアム認証ありのトークンで試す

---

## ✅ 解決（2026-06-01）

**原因**: `ft`/`to` だけでは radiko が無視してライブ配信を返す。
現在の仕様では **`start_at` / `end_at` パラメータが必須**になっていた。

**修正**: timefree の playlist URL に `start_at`/`end_at` を追加する。

```
{playlist_create_url}
  ?station_id={sid}
  &start_at={ft}&ft={ft}
  &end_at={to}&to={to}
  &l=15&type=b&lsid=
```

**検証**（要求 ft=2日前12:00）:

| URL | 先頭セグメントの内容時刻 |
|---|---|
| `ft`/`to` のみ（旧） | `20260601_193710`（＝今・ライブ）❌ |
| `start_at`/`end_at` 追加 | `20260530_120000`（＝要求どおり）✅ |

`radiko_recorder.py` の `timefree_url()` に反映済み。
録音（`record_timefree`）・再生（`play_timefree`）の両方がこの URL を使うので一度に解決した。
