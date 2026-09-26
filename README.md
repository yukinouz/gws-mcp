# gws-mcp

アクセス範囲と操作権限を厳しく絞った、**Google Workspace MCP サーバー**です。

LLM の誤操作による意図しない変更・削除・情報流出を防ぐための制限をコード側で強制します。

- 外部依存は **Google 公式ライブラリのみ**です。サードパーティ製フレームワークは使いません。

## 対応サービスと操作範囲

| サービス              | 読み取り | 作成 | 更新  | 削除 |
| --------------------- | :------: | :--: | :---: | :--: |
| Drive（マイドライブ） |    ✅    |  ✅  | ✅ ※1 |  ❌  |
| Drive（共有ドライブ） |    ✅    |  ❌  |  ❌   |  ❌  |
| スプレッドシート      |    ✅    |  ✅  | ✅ ※1 |  ❌  |
| ドキュメント          |    ✅    |  ✅  | ✅ ※1 |  ❌  |
| スライド              |    ✅    |  ✅  | ✅ ※1 |  ❌  |
| カレンダー            |    ✅    |  ✅  | ✅ ※2 |  ❌  |
| Gmail                 |    ✅    |  ❌  |  ❌   |  ❌  |

※1 対象は「**自分が所有者**」かつ「**共有ドライブに属していない（`driveId` がない）**」ファイルだけです。どちらかを満たさないファイルへの書き込みは拒否します。
※2 自分が主催者の予定だけ更新できます。

## セキュリティ設計

### 書き込みガード

すべての書き込み系ツールは、実行前に Drive API（`files.get`）で対象ファイルの情報を取得し、次のいずれかに当てはまれば **API を呼ばずにエラーを返します**。

- `driveId` がある（共有ドライブ内のファイル）
- `ownedByMe` が `true` でない（他人が所有するファイル）
- ゴミ箱に入っている

新規作成では、作成先の親フォルダに同じ判定をかけます（既定の作成先はマイドライブ直下）。ファイル ID は形式をチェックしてから使います。

OAuth スコープではマイドライブと共有ドライブを区別できないため、この制限はスコープではなくコード（[gws_mcp/guard.py](gws_mcp/guard.py)）で強制しています。

### ツールとして公開しない操作

- ファイルの削除、ゴミ箱への移動、ゴミ箱を空にする
- 共有設定（`permissions`）の変更
- ファイルの移動（親フォルダの付け替え）
- 共有ドライブの作成・変更
- カレンダーの予定の削除、カレンダー自体の作成・削除・共有設定
- Gmail の送信・下書き作成・ラベル変更などの書き込み全般
- 任意の API を直接呼べる汎用ツール

### その他の安全策

- **スプレッドシート**: 書き込みは既定で `RAW`（数式として解釈しない）です。`IMPORTDATA` などの数式でデータが外部に送られるのを防ぎます。数式を入れたいときだけ `USER_ENTERED` を明示します。
- **カレンダー**: 予定を作成・更新しても、既定では招待メールを送りません（`sendUpdates="none"`）。
- **トークン**: 権限 `0600` で保存します（ディレクトリは `0700`）。サーバー実行中にブラウザ認証を始めることはありません。
- **ログ**: 標準エラー出力だけに出します（stdout は MCP 通信専用）。

### OAuth スコープ

| スコープ                                          | 用途                                         |
| ------------------------------------------------- | -------------------------------------------- |
| `https://www.googleapis.com/auth/drive`           | Drive / Docs / Sheets / Slides の読み書き    |
| `https://www.googleapis.com/auth/gmail.readonly`  | Gmail の読み取り                             |
| `https://www.googleapis.com/auth/calendar.events` | 予定の読み書き（カレンダー自体の操作は不可） |

## 必要なもの

- Python 3.10 以上
- Google アカウント
- Google Cloud プロジェクト（OAuth クライアント作成用）

## セットアップ

### 1. Google Cloud の準備

1. [Google Cloud Console](https://console.cloud.google.com/) でプロジェクトを作ります。
2. 「API とサービス」→「ライブラリ」で次の API を有効にします。
   - Google Drive API
   - Gmail API
   - Google Calendar API
   - Google Sheets API
   - Google Docs API
   - Google Slides API
3. 「OAuth 同意画面」を設定します。個人利用なら「外部」＋テストユーザーに自分のアカウントを追加すれば十分です。
4. 「認証情報」→「認証情報を作成」→「OAuth クライアント ID」で、種類に **デスクトップアプリ** を選びます。
5. JSON をダウンロードし、`~/.config/gws-mcp/credentials.json` として保存します。

```sh
mkdir -p ~/.config/gws-mcp && chmod 700 ~/.config/gws-mcp
mv ~/Downloads/client_secret_*.json ~/.config/gws-mcp/credentials.json
chmod 600 ~/.config/gws-mcp/credentials.json
```

保存場所は環境変数 `GWS_MCP_CONFIG_DIR` で変更できます。

### 2. インストール

```sh
cd /path/to/gws-mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

#### 依存の固定（推奨）

`requirements.txt` で固定しているのは公式4パッケージのバージョンだけです。推移的な依存までハッシュ付きで固定する場合は、初回インストール後に次を実行します。

```sh
.venv/bin/pip freeze > requirements.lock
# 以降の再インストールでは requirements.lock を使う
.venv/bin/pip install --require-hashes -r requirements.lock  # ハッシュを付けた場合
```

### 3. 初回認証

```sh
.venv/bin/python scripts/authorize.py
```

ブラウザが開くので、Google アカウントでログインして権限を許可します。トークンは `~/.config/gws-mcp/token.json` に保存されます。スコープを変えたときやトークンが無効になったときも、このコマンドを実行し直してください。

### 4. MCP クライアントへの登録

`/path/to/gws-mcp` は実際のパスに置き換えてください。[config/](config/) に設定例があります。

**Claude Code**

```sh
claude mcp add gws -- /path/to/gws-mcp/.venv/bin/python -m gws_mcp
```

（プロジェクト外から使う場合は `-e PYTHONPATH=/path/to/gws-mcp` を付けます）

**VS Code**（`.vscode/mcp.json`）

```json
{
  "servers": {
    "gws": {
      "type": "stdio",
      "command": "/path/to/gws-mcp/.venv/bin/python",
      "args": ["-m", "gws_mcp"],
      "env": { "PYTHONPATH": "/path/to/gws-mcp" }
    }
  }
}
```

## ツール一覧

### Drive

| ツール                   | 種別 | 説明                                                                        |
| ------------------------ | ---- | --------------------------------------------------------------------------- |
| `drive_search`           | 読取 | キーワードでファイルを検索（共有ドライブを含む）                            |
| `drive_list_folder`      | 読取 | フォルダ内のファイル一覧                                                    |
| `drive_get_metadata`     | 読取 | ファイルのメタデータを取得                                                  |
| `drive_read_file`        | 読取 | 本文をテキストで取得（Google ドキュメント類はエクスポート、バイナリは不可） |
| `drive_create_folder`    | 書込 | マイドライブにフォルダを作成                                                |
| `drive_create_text_file` | 書込 | マイドライブにテキストファイルを作成                                        |
| `drive_update_text_file` | 書込 | テキストファイルの本文を更新                                                |
| `drive_rename`           | 書込 | ファイル名を変更                                                            |
| `drive_copy_file`        | 書込 | ファイルをマイドライブのフォルダにコピー（コピー元は共有ドライブも可）      |

### Gmail（読み取りのみ）

| ツール              | 説明                                       |
| ------------------- | ------------------------------------------ |
| `gmail_search`      | Gmail の検索構文でメールを検索（最大50件） |
| `gmail_get_message` | メール本文を取得（text/plain 優先）        |
| `gmail_get_thread`  | スレッドを取得                             |
| `gmail_list_labels` | ラベル一覧                                 |

### カレンダー

| ツール                  | 種別 | 説明                         |
| ----------------------- | ---- | ---------------------------- |
| `calendar_list_events`  | 読取 | 期間・キーワードで予定を一覧 |
| `calendar_get_event`    | 読取 | 予定の詳細を取得             |
| `calendar_create_event` | 書込 | 予定を作成                   |
| `calendar_update_event` | 書込 | 自分が主催者の予定を部分更新 |

### スプレッドシート

| ツール                | 種別 | 説明                       |
| --------------------- | ---- | -------------------------- |
| `sheets_get_metadata` | 読取 | シート構成を取得           |
| `sheets_read_range`   | 読取 | 範囲の値を取得             |
| `sheets_create`       | 書込 | スプレッドシートを新規作成 |
| `sheets_write_range`  | 書込 | 範囲に値を書き込み         |
| `sheets_append_rows`  | 書込 | 行を追記                   |
| `sheets_add_sheet`    | 書込 | シート（タブ）を追加       |

### ドキュメント

| ツール              | 種別 | 説明                   |
| ------------------- | ---- | ---------------------- |
| `docs_read`         | 読取 | 本文をテキストで取得   |
| `docs_create`       | 書込 | ドキュメントを新規作成 |
| `docs_append_text`  | 書込 | 末尾にテキストを追記   |
| `docs_replace_text` | 書込 | テキストを一括置換     |

### スライド

| ツール                  | 種別 | 説明                           |
| ----------------------- | ---- | ------------------------------ |
| `slides_read`           | 読取 | 各スライドのテキストを取得     |
| `slides_create`         | 書込 | プレゼンテーションを新規作成   |
| `slides_add_text_slide` | 書込 | タイトル＋本文のスライドを追加 |
| `slides_replace_text`   | 書込 | テキストを一括置換             |

## ディレクトリ構成

```
gws-mcp/
├── gws_mcp/
│   ├── server.py      # JSON-RPC 2.0 stdio サーバー（MCP プロトコル処理）
│   ├── schema.py      # ツール入力の検証
│   ├── auth.py        # OAuth 資格情報の読み込み・更新
│   ├── guard.py       # 書き込みガード（マイドライブ判定）
│   └── tools/         # サービスごとのツール実装
├── scripts/
│   └── authorize.py   # 初回 OAuth 認証
├── tests/             # unittest（Google API はモック）
├── config/            # 各 MCP クライアント用の設定例
└── requirements.txt
```

## テスト

標準ライブラリの `unittest` だけで動きます（Google API はモックするので認証は不要です）。

```sh
.venv/bin/python -m unittest discover tests
```

手動で通信を確認する場合（MCP プロトコル `2026-07-28`。各リクエストの `_meta` にバージョンを付ける）:

```sh
META='"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}'
printf '%s\n' \
  "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"server/discover\",\"params\":{$META}}" \
  "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/list\",\"params\":{$META}}" \
  | .venv/bin/python -m gws_mcp
```

## 注意事項

- `credentials.json` と `token.json` は絶対にコミットしないでください（`.gitignore` で除外済み）。
- `drive` スコープはファイル全体への読み書き権限です。実際の書き込み制限はガードのコードに依存するので、ガードを変更するときはテストも更新してください。
- トークンを取り消すには、[Google アカウントのサードパーティ接続](https://myaccount.google.com/connections) からアプリへのアクセスを削除し、`token.json` を消します。
