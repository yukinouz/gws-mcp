"""OAuth 資格情報の読み込み・更新・保存。

サーバー（stdio）実行中はブラウザ認証フローを起動しない。
初回認証やスコープ変更時の再認証は scripts/authorize.py で行う。
"""

import os
import sys
from pathlib import Path

# 最小スコープ。Docs/Sheets/Slides API は drive スコープで呼び出せる。
# マイドライブ/共有ドライブの区別はスコープでは不可能なため guard.py で強制する。
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]

# サービス名と API バージョンの対応
_API_VERSIONS = {
    "drive": "v3",
    "gmail": "v1",
    "calendar": "v3",
    "sheets": "v4",
    "docs": "v1",
    "slides": "v1",
}


class AuthError(Exception):
    """認証情報が無い・無効・スコープ不足のときに送出する。"""


def config_dir() -> Path:
    """資格情報の保存ディレクトリ（環境変数 GWS_MCP_CONFIG_DIR で変更可）。"""
    return Path(os.environ.get("GWS_MCP_CONFIG_DIR", "~/.config/gws-mcp")).expanduser()


def client_secrets_path() -> Path:
    return config_dir() / "credentials.json"


def token_path() -> Path:
    return config_dir() / "token.json"


def _warn_if_loose_permissions(path: Path) -> None:
    """グループ・他ユーザーから読める権限なら警告する。"""
    try:
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        return
    if mode & 0o077:
        print(
            f"[gws-mcp] 警告: {path} の権限が {oct(mode)} です。chmod 600 を推奨します。",
            file=sys.stderr,
        )


def save_token(creds) -> None:
    """トークンを 0600 で保存する（ディレクトリは 0700）。"""
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    path = token_path()
    # 作成時点から 0600 で開き、一時的にも他ユーザーが読めない状態を作らない
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(creds.to_json())
    os.chmod(path, 0o600)


def load_credentials():
    """保存済みトークンを読み込み、必要ならリフレッシュして返す。"""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    path = token_path()
    if not path.exists():
        raise AuthError(
            f"トークンがありません（{path}）。先に `python scripts/authorize.py` を実行してください。"
        )
    _warn_if_loose_permissions(path)

    creds = Credentials.from_authorized_user_file(str(path), SCOPES)
    granted = set(creds.scopes or [])
    missing = set(SCOPES) - granted
    if missing:
        raise AuthError(
            f"必要なスコープが不足しています: {sorted(missing)}。"
            "`python scripts/authorize.py` を再実行してください。"
        )
    extra = granted - set(SCOPES)
    if extra:
        print(f"[gws-mcp] 警告: 想定外のスコープが付与されています: {sorted(extra)}", file=sys.stderr)

    if not creds.valid:
        if not (creds.expired and creds.refresh_token):
            raise AuthError("トークンが無効です。`python scripts/authorize.py` を再実行してください。")
        try:
            creds.refresh(Request())
        except RefreshError as e:
            raise AuthError(
                f"トークンの更新に失敗しました（{e}）。`python scripts/authorize.py` を再実行してください。"
            ) from e
        save_token(creds)
    return creds


class Services:
    """Google API クライアントを遅延生成してキャッシュする。"""

    def __init__(self):
        self._creds = None
        self._cache = {}

    def get(self, name: str):
        from googleapiclient.discovery import build

        if name not in _API_VERSIONS:
            raise ValueError(f"未対応のサービスです: {name}")
        # 期限切れなら読み込み直す（リフレッシュ済みトークンで作り直す）
        if self._creds is None or not self._creds.valid:
            self._creds = load_credentials()
            self._cache.clear()
        if name not in self._cache:
            self._cache[name] = build(
                name, _API_VERSIONS[name], credentials=self._creds, cache_discovery=False
            )
        return self._cache[name]
