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
※2 作成・更新できるのは自分のメインカレンダー（primary）だけです。更新は自分が主催者の予定に限ります。

## セキュリティ設計

### 書き込みガード

Drive・スプレッドシート・ドキュメント・スライドの書き込み系ツールは、実行前に Drive API（`files.get`）で対象ファイルの情報を取得し、次のいずれかに当てはまれば **API を呼ばずにエラーを返します**。

- `driveId` がある（共有ドライブ内のファイル）
- `ownedByMe` が `true` でない（他人が所有するファイル）
- ゴミ箱に入っている
- ショートカットである（リンク先が共有ドライブのファイルかどうかを ID から判別できないため）

Drive のフォルダ作成・ファイル作成・コピーでは、作成先の親フォルダに同じ判定をかけます（既定の作成先はマイドライブ直下）。スプレッドシート・ドキュメント・スライドの新規作成は常にマイドライブ直下に作られ、作成後にできたファイルを同じ条件で確認します。ファイル ID は形式をチェックしてから使います。

カレンダーは Drive のファイルではないため、予定を取得して自分が主催者かを判定します（※2）。

OAuth スコープではマイドライブと共有ドライブを区別できないため、この制限はスコープではなくコード（[gws_mcp/guard.py](gws_mcp/guard.py)）で強制しています。

さらに Drive API の書き込み呼び出し（作成・更新・名前変更）には `supportsAllDrives` を付けません。付けなければ共有ドライブのファイルは API 側で「見つからない」扱いになるため、ガードとは別の二重の防御になります（共有ドライブのファイルを読むために必要なコピーだけは例外で、コピー先はガードで判定します）。

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
- **カレンダー**: 予定を作成・更新しても、既定では招待メールを送りません（`sendUpdates="none"`）。書き込み先は自分のメインカレンダーに固定しており、共有カレンダーは編集権限があっても変更しません。
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
- Google Workspace アカウント
- Google Cloud プロジェクト（OAuth クライアント作成用）

## セットアップ

以下の手順はスキル(`/setup`)で対話的に進められます

### 1. Google Cloud の準備

1. [Google Cloud Console](https://console.cloud.google.com/) でプロジェクトを作ります。
2. 「API とサービス」→「ライブラリ」で次の API を有効にします。
   - Google Drive API
   - Gmail API
   - Google Calendar API
   - Google Sheets API
   - Google Docs API
   - Google Slides API
3. 「Google Auth Platform」で OAuth 同意画面を設定し、「対象」でユーザーの種類に「内部」を選びます（組織利用が前提。個人利用で「外部」かつ「テスト中」にすると、トークンが 7 日で失効します）。
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
.venv/bin/pip install -r requirements.lock
```

`requirements.lock` は、推移的な依存まで動作確認済みのバージョンで固定したものです。直接の依存（Google 公式4パッケージ）は `requirements.txt` で管理し、更新したときは `.venv/bin/pip freeze > requirements.lock` で作り直します。

### 3. 初回認証

```sh
.venv/bin/python scripts/authorize.py
```

ブラウザが開くので、Google アカウントでログインして権限を許可します。トークンは `~/.config/gws-mcp/token.json` に保存されます。スコープを変えたときやトークンが無効になったときも、このコマンドを実行し直してください。

### 4. MCP クライアントへの登録

`/path/to/gws-mcp` は実際のパスに置き換えてください。[config/](config/) に設定例があります（Claude Code のプロジェクト設定 `.mcp.json` 用: [claude-code.mcp.example.json](config/claude-code.mcp.example.json)、VS Code 用: [vscode.mcp.example.json](config/vscode.mcp.example.json)）。

**Claude Code**

```sh
claude mcp add --scope user gws -e PYTHONPATH=/path/to/gws-mcp -- /path/to/gws-mcp/.venv/bin/python -m gws_mcp
```

（`--scope user` でどのプロジェクトからも使えるようにします。`PYTHONPATH` はリポジトリ外から起動しても `gws_mcp` を読み込めるようにするためです）

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

| ツール                  | 種別 | 説明                               |
| ----------------------- | ---- | ---------------------------------- |
| `calendar_list_events`  | 読取 | 期間・キーワードで予定を一覧       |
| `calendar_get_event`    | 読取 | 予定の詳細を取得                   |
| `calendar_create_event` | 書込 | 自分のメインカレンダーに予定を作成 |
| `calendar_update_event` | 書込 | 自分が主催者の予定を部分更新       |

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
│   └── tools/         # サービスごとのツール実装（drive / gmail / calendar / sheets / docs / slides）
├── scripts/
│   └── authorize.py   # 初回 OAuth 認証
├── tests/             # unittest（Google API はモック）
├── config/            # 各 MCP クライアント用の設定例
├── .claude/           # Claude Code 用の skill（/setup・/check-updates など）と、資格情報へのアクセスを防ぐフック・設定
├── requirements.txt   # 直接の依存
└── requirements.lock  # 推移的な依存まで固定したもの（インストールに使う）
```

## テスト

標準ライブラリの `unittest` だけで動きます（Google API はモックするので認証は不要です）。

```sh
.venv/bin/python -m unittest discover tests
```

## メンテナンス

- **書き込みガード**: 安全性を支える中心部分です。書き込み系のツールを追加・変更するときは、必ず [guard.py](gws_mcp/guard.py) の判定を通してください（[tools/\_common.py](gws_mcp/tools/_common.py) の `writable()` / `created()`）。また、書き込み用の API 呼び出しには `supportsAllDrives` を付けないでください。
- **旧方式のプロトコル対応**: Claude Code と VS Code の両方が新方式で動くことを確認できてから、削除を検討します。どちらの方式で接続したかは、標準エラー出力のログ（`initialize:` か `server/discover:` か）で分かります。
- **入力検証**: [schema.py](gws_mcp/schema.py) が対応している JSON Schema のキーワードは一部だけです。ツールの引数に新しい種類の制約が必要になったら、先に schema.py を拡張してください。
- **依存の更新**: テストを実行し、`requirements.lock` を作り直したうえで、実際の環境で主要なツールを動かして確認してください。テストは Google API をモックしているため、API 側の変更は検出できません。

### 確認方法(`/check-updates` スキル)

定期的な更新確認は、Claude Code の `/check-updates` skill（[.claude/skills/check-updates/SKILL.md](.claude/skills/check-updates/SKILL.md)）で行えます。

## 注意事項

- `credentials.json` と `token.json` は絶対にコミットしないでください（`.gitignore` で除外済み）。
- `drive` スコープはファイル全体への読み書き権限です。実際の書き込み制限はガードのコードに依存するので、ガードを変更するときはテストも更新してください。
- トークンを取り消すには、[Google アカウントのサードパーティ接続](https://myaccount.google.com/connections) からアプリへのアクセスを削除し、`token.json` を消します。
