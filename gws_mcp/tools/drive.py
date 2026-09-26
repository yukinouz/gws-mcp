"""Drive ツール。

読み取りは共有ドライブも対象にする（supportsAllDrives=True）。
書き込みは guard を通したうえで、API 呼び出しに supportsAllDrives を付けない。
付けなければ共有ドライブのファイルは API 側で見つからない扱いになるため、
ガードとは別にもう一段の防御になる。
"""

from googleapiclient.http import MediaInMemoryUpload

from gws_mcp.guard import (
    FILE_ID_PATTERN,
    FOLDER_MIME,
    ROOT_FOLDER,
    assert_writable_file,
    assert_writable_parent,
)
from gws_mcp.tools import Tool, ToolError
from gws_mcp.tools._common import created, file_id, obj

# 一覧・検索で返す項目
_LIST_FIELDS = "nextPageToken,files(id,name,mimeType,modifiedTime,driveId,ownedByMe,webViewLink)"
_META_FIELDS = (
    "id,name,mimeType,description,size,createdTime,modifiedTime,driveId,parents,"
    "ownedByMe,owners(displayName,emailAddress),webViewLink,trashed"
)

# Google ドキュメント形式のファイルをテキストで読むときのエクスポート形式
_EXPORT_MIME = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",  # 先頭のシートのみ
    "application/vnd.google-apps.presentation": "text/plain",
}

# テキストとして読み書きできる形式
_TEXT_MIME_TYPES = ["text/plain", "text/markdown", "text/csv", "application/json"]

# 読み込むファイルの上限（LLM のコンテキストを圧迫しないため）
MAX_DOWNLOAD_BYTES = 2_000_000
MAX_CONTENT_CHARS = 1_000_000

# ---- スキーマ部品 ----

_FILE_ID = file_id()
_PARENT_ID = {
    "type": "string",
    "pattern": f"root|{FILE_ID_PATTERN}",
    "description": "作成先フォルダの ID（省略時はマイドライブ直下 root）",
}
_NAME = {"type": "string", "minLength": 1, "maxLength": 255}
_PAGE_SIZE = {"type": "integer", "minimum": 1, "maximum": 100, "description": "取得件数（既定 20）"}
_PAGE_TOKEN = {"type": "string", "maxLength": 2000, "description": "前回結果の nextPageToken"}


def _escape_query(value: str) -> str:
    """Drive 検索クエリの文字列リテラル用にエスケープする。"""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _is_text_mime(mime: str) -> bool:
    return mime.startswith("text/") or mime in ("application/json", "application/xml")


# ---- 読み取り ----


def _list(drive, q: str, args: dict) -> dict:
    return (
        drive.files()
        .list(
            q=q,
            pageSize=args.get("page_size", 20),
            pageToken=args.get("page_token"),
            fields=_LIST_FIELDS,
            orderBy="modifiedTime desc",
            corpora="allDrives",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
    )


def drive_search(services, args):
    keyword = _escape_query(args["query"])
    q = f"(name contains '{keyword}' or fullText contains '{keyword}') and trashed = false"
    return _list(services.get("drive"), q, args)


def drive_list_folder(services, args):
    folder_id = args.get("folder_id", ROOT_FOLDER)
    # folder_id はスキーマの pattern で検証済みのため、クエリに直接埋め込んでよい
    return _list(services.get("drive"), f"'{folder_id}' in parents and trashed = false", args)


def drive_get_metadata(services, args):
    return (
        services.get("drive")
        .files()
        .get(fileId=args["file_id"], fields=_META_FIELDS, supportsAllDrives=True)
        .execute()
    )


def drive_read_file(services, args):
    drive = services.get("drive")
    file_id = args["file_id"]
    meta = drive.files().get(fileId=file_id, fields="id,name,mimeType,size", supportsAllDrives=True).execute()
    mime = meta.get("mimeType", "")

    if mime in _EXPORT_MIME:
        data = drive.files().export(fileId=file_id, mimeType=_EXPORT_MIME[mime]).execute()
    elif _is_text_mime(mime):
        if int(meta.get("size", 0)) > MAX_DOWNLOAD_BYTES:
            raise ToolError(f"ファイルが大きすぎます（上限 {MAX_DOWNLOAD_BYTES} バイト）。")
        data = drive.files().get_media(fileId=file_id, supportsAllDrives=True).execute()
    else:
        raise ToolError(f"この形式はテキストとして読めません: {mime}")

    text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)
    return f"# {meta.get('name')}（{mime}）\n\n{text}"


# ---- 書き込み ----


def drive_create_folder(services, args):
    drive = services.get("drive")
    parent = args.get("parent_id", ROOT_FOLDER)
    assert_writable_parent(drive, parent)
    body = {"name": args["name"], "mimeType": FOLDER_MIME, "parents": [parent]}
    new = drive.files().create(body=body, fields="id").execute()
    return created(services, new["id"])


def drive_create_text_file(services, args):
    drive = services.get("drive")
    parent = args.get("parent_id", ROOT_FOLDER)
    assert_writable_parent(drive, parent)
    mime = args.get("mime_type", "text/plain")
    media = MediaInMemoryUpload(args["content"].encode("utf-8"), mimetype=mime)
    body = {"name": args["name"], "mimeType": mime, "parents": [parent]}
    new = drive.files().create(body=body, media_body=media, fields="id").execute()
    return created(services, new["id"])


def drive_update_text_file(services, args):
    drive = services.get("drive")
    meta = assert_writable_file(drive, args["file_id"])
    mime = meta.get("mimeType", "")
    if not _is_text_mime(mime):
        raise ToolError(
            f"テキストファイルではないため更新できません: {mime}"
            "（Google ドキュメント・スプレッドシートは docs_* / sheets_* ツールを使ってください）"
        )
    media = MediaInMemoryUpload(args["content"].encode("utf-8"), mimetype=mime)
    updated = drive.files().update(fileId=meta["id"], media_body=media, fields="id,name,modifiedTime").execute()
    return updated


def drive_rename(services, args):
    drive = services.get("drive")
    meta = assert_writable_file(drive, args["file_id"])
    # 変更するのは名前だけ（移動・ゴミ箱・共有設定などは body に含めない）
    return drive.files().update(fileId=meta["id"], body={"name": args["new_name"]}, fields="id,name").execute()


def drive_copy_file(services, args):
    drive = services.get("drive")
    parent = args.get("parent_id", ROOT_FOLDER)
    assert_writable_parent(drive, parent)
    body = {"parents": [parent]}
    if "name" in args:
        body["name"] = args["name"]
    # コピー元は共有ドライブのファイルでもよい（読み取りのみ）ので supportsAllDrives を付ける
    # コピー先はガード済みのマイドライブのフォルダに限られる
    copied = drive.files().copy(fileId=args["file_id"], body=body, fields="id", supportsAllDrives=True).execute()
    return created(services, copied["id"])


# ---- ツール定義 ----

TOOLS = [
    Tool(
        name="drive_search",
        title="Drive を検索",
        description="ファイル名または本文に含まれるキーワードで Drive を検索する（共有ドライブを含む）。",
        input_schema=obj(
            {
                "query": {"type": "string", "minLength": 1, "maxLength": 200, "description": "検索キーワード"},
                "page_size": _PAGE_SIZE,
                "page_token": _PAGE_TOKEN,
            },
            ["query"],
        ),
        handler=drive_search,
        read_only=True,
    ),
    Tool(
        name="drive_list_folder",
        title="フォルダの中身を一覧",
        description="フォルダ内のファイルを更新日時の新しい順に一覧する（共有ドライブのフォルダも可）。",
        input_schema=obj(
            {
                "folder_id": {
                    "type": "string",
                    "pattern": f"root|{FILE_ID_PATTERN}",
                    "description": "フォルダ ID（省略時はマイドライブ直下 root）",
                },
                "page_size": _PAGE_SIZE,
                "page_token": _PAGE_TOKEN,
            }
        ),
        handler=drive_list_folder,
        read_only=True,
    ),
    Tool(
        name="drive_get_metadata",
        title="ファイル情報を取得",
        description="ファイルの名前・形式・所有者・更新日時・所属する共有ドライブなどを取得する。",
        input_schema=obj({"file_id": _FILE_ID}, ["file_id"]),
        handler=drive_get_metadata,
        read_only=True,
    ),
    Tool(
        name="drive_read_file",
        title="ファイルの中身を読む",
        description=(
            "ファイルの中身をテキストで取得する。Google ドキュメント・スライドはテキスト、"
            "スプレッドシートは先頭シートの CSV として取得する。画像などのバイナリは読めない。"
        ),
        input_schema=obj({"file_id": _FILE_ID}, ["file_id"]),
        handler=drive_read_file,
        read_only=True,
    ),
    Tool(
        name="drive_create_folder",
        title="フォルダを作成",
        description="マイドライブにフォルダを作成する。作成先は自分が所有するマイドライブのフォルダに限る。",
        input_schema=obj({"name": _NAME, "parent_id": _PARENT_ID}, ["name"]),
        handler=drive_create_folder,
        read_only=False,
    ),
    Tool(
        name="drive_create_text_file",
        title="テキストファイルを作成",
        description="マイドライブにテキストファイルを作成する。作成先は自分が所有するマイドライブのフォルダに限る。",
        input_schema=obj(
            {
                "name": _NAME,
                "content": {"type": "string", "maxLength": MAX_CONTENT_CHARS},
                "mime_type": {"type": "string", "enum": _TEXT_MIME_TYPES, "description": "既定 text/plain"},
                "parent_id": _PARENT_ID,
            },
            ["name", "content"],
        ),
        handler=drive_create_text_file,
        read_only=False,
    ),
    Tool(
        name="drive_update_text_file",
        title="テキストファイルを上書き",
        description="テキストファイルの本文を上書きする。対象は自分が所有するマイドライブのファイルに限る。",
        input_schema=obj(
            {"file_id": _FILE_ID, "content": {"type": "string", "maxLength": MAX_CONTENT_CHARS}},
            ["file_id", "content"],
        ),
        handler=drive_update_text_file,
        read_only=False,
        idempotent=True,
    ),
    Tool(
        name="drive_rename",
        title="名前を変更",
        description="ファイル・フォルダの名前を変更する。対象は自分が所有するマイドライブのファイルに限る。",
        input_schema=obj({"file_id": _FILE_ID, "new_name": _NAME}, ["file_id", "new_name"]),
        handler=drive_rename,
        read_only=False,
        idempotent=True,
    ),
    Tool(
        name="drive_copy_file",
        title="ファイルをコピー",
        description=(
            "ファイルをマイドライブのフォルダにコピーする。コピー元は共有ドライブのファイルでもよい。"
            "コピー先は自分が所有するマイドライブのフォルダに限る。"
        ),
        input_schema=obj(
            {"file_id": _FILE_ID, "name": _NAME, "parent_id": _PARENT_ID},
            ["file_id"],
        ),
        handler=drive_copy_file,
        read_only=False,
    ),
]
