"""書き込みガード。

書き込み系ツールは Google API を呼ぶ前に必ずここを通す。
OAuth スコープ（drive）ではマイドライブと共有ドライブを区別できないため、
対象ファイルのメタデータを取得してコード側で判定する。

書き込みを許可する条件（すべて満たすこと）:
- 共有ドライブに属していない（driveId が無い）
- 自分が所有者（ownedByMe が true）
- ゴミ箱に入っていない
- ショートカットではない（リンク先が共有ドライブのファイルでも ID からは判別できないため）
"""

import re

from gws_mcp.tools import ToolError

# Drive のファイル ID の形式。クエリ文字列への埋め込みや不正な値の混入を防ぐ
FILE_ID_PATTERN = r"[A-Za-z0-9_-]{10,200}"

FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"

# マイドライブ直下を表す別名
ROOT_FOLDER = "root"

_GUARD_FIELDS = "id,name,mimeType,ownedByMe,driveId,trashed"


class GuardError(ToolError):
    """ガードにより書き込みを拒否したときに送出する。"""


def validate_id(file_id: str) -> str:
    if not isinstance(file_id, str) or not re.fullmatch(FILE_ID_PATTERN, file_id):
        raise GuardError(f"ファイル ID の形式が不正です: {file_id!r}")
    return file_id


def _get_metadata(drive, file_id: str) -> dict:
    from googleapiclient.errors import HttpError

    try:
        return (
            drive.files()
            .get(fileId=file_id, fields=_GUARD_FIELDS, supportsAllDrives=True)
            .execute()
        )
    except HttpError as e:
        if getattr(e.resp, "status", None) == 404:
            raise GuardError(f"ファイルが見つからないか、アクセスできません: {file_id}") from e
        raise


def assert_writable_file(drive, file_id: str) -> dict:
    """対象ファイルに書き込めるか判定し、メタデータを返す。書き込めなければ GuardError。"""
    validate_id(file_id)
    meta = _get_metadata(drive, file_id)
    name = meta.get("name", file_id)

    # 値の有無ではなくキーの有無で判定し、想定外の応答でも拒否側に倒す
    if "driveId" in meta:
        raise GuardError(f"「{name}」は共有ドライブのファイルです。共有ドライブは読み取り専用です。")
    if meta.get("trashed") is not False:
        raise GuardError(f"「{name}」はゴミ箱にあるため変更できません。")
    if meta.get("ownedByMe") is not True:
        raise GuardError(f"「{name}」は自分が所有者ではないため変更できません。")
    if meta.get("mimeType") == SHORTCUT_MIME:
        raise GuardError(f"「{name}」はショートカットです。リンク先のファイルを直接指定してください。")
    return meta


def assert_writable_parent(drive, folder_id: str) -> None:
    """ファイルの作成先フォルダがマイドライブの自分のフォルダか判定する。"""
    if folder_id == ROOT_FOLDER:
        return
    meta = assert_writable_file(drive, folder_id)
    if meta.get("mimeType") != FOLDER_MIME:
        raise GuardError(f"「{meta.get('name', folder_id)}」はフォルダではありません。")


def assert_own_event(event: dict) -> None:
    """カレンダーの予定が自分の主催か判定する（他人の予定の変更を防ぐ）。"""
    if (event.get("organizer") or {}).get("self") is not True:
        summary = event.get("summary", event.get("id", ""))
        raise GuardError(f"予定「{summary}」は自分が主催者ではないため変更できません。")
