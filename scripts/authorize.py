"""初回 OAuth 認証を行い、トークンを保存する。

使い方:
    .venv/bin/python scripts/authorize.py

ブラウザが開くので、Google アカウントでログインして権限を許可する。
スコープを変更したときやトークンが無効になったときも再実行する。
"""

import sys
from pathlib import Path

# リポジトリ直下を import パスに追加（scripts/ から直接実行するため）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gws_mcp.auth import (  # noqa: E402
    SCOPES,
    _warn_if_loose_permissions,
    client_secrets_path,
    config_dir,
    save_token,
    token_path,
)


def main() -> int:
    from google_auth_oauthlib.flow import InstalledAppFlow

    secrets = client_secrets_path()
    if not secrets.exists():
        print(f"OAuth クライアント情報がありません: {secrets}", file=sys.stderr)
        print("README の「Google Cloud の準備」を参照して配置してください。", file=sys.stderr)
        return 1
    _warn_if_loose_permissions(secrets)

    print("要求するスコープ:")
    for s in SCOPES:
        print(f"  - {s}")

    flow = InstalledAppFlow.from_client_secrets_file(str(secrets), SCOPES)
    # ローカルの空きポートで一時的にリダイレクトを受ける。ループバックのみで待ち受ける
    creds = flow.run_local_server(
        host="localhost",
        bind_addr="127.0.0.1",
        port=0,
        open_browser=True,
        # 放置されても待ち受けが残り続けないよう 5 分で打ち切る
        timeout_seconds=300,
        authorization_prompt_message="ブラウザで次の URL を開いて認証してください:\n{url}",
        success_message="認証が完了しました。このタブは閉じて構いません。",
    )

    granted = set(creds.scopes or [])
    missing = set(SCOPES) - granted
    if missing:
        print(f"一部のスコープが許可されませんでした: {sorted(missing)}", file=sys.stderr)
        print("同意画面ですべての項目にチェックを入れて再実行してください。", file=sys.stderr)
        return 1

    save_token(creds)
    print(f"トークンを保存しました: {token_path()}（ディレクトリ: {config_dir()}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
