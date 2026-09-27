---
name: check-updates
description: "gws-mcp の依存・プロトコル・Google API などに更新が必要かを確認し、修正プランを提案する（修正はしない）"
---

## 実行ルール

- 読み取りと調査のみ行う。
- スキル実行時点では、ファイルの変更・パッケージのインストール・コミットはせず、修正プランを提案する。
- 資格情報（credentials.json / token.json / ~/.config/gws-mcp）には触れない
- 結果は次の形式で報告し、ユーザーの判断を待つ
  - 項目ごとの判定（対応不要／要対応／要確認）と根拠
  - 要対応の項目の修正プラン

## 確認項目

1. **依存パッケージ**: requirements.txt の Google 公式4パッケージに新しい版があるか。更新する場合、破壊的な変更がないか
2. **MCP プロトコル**
   - 新方式（server.py の `PROTOCOL_VERSION`）の仕様が改訂されていないか
   - Claude Code と VS Code が、それぞれ新方式にどこまで対応しているか
   - 旧方式の処理を削除できるのは、両方のクライアントが新方式で動くことを確認できた場合のみ
3. **Google API**: auth.py の `_API_VERSIONS` にある各 API に、廃止や非推奨のお知らせがないか。スコープの扱いが変わっていないか
4. **書き込みガード**
   - `read_only=False` のツールがすべて、tools/\_common.py の `writable()` / `created()`、または guard.py の判定を通っているか
   - 書き込み用の API 呼び出しに `supportsAllDrives` が付いていないか（drive_copy_file のコピー元の読み取りは例外）
5. **テスト**: `.venv/bin/python -m unittest discover tests` が通るか
