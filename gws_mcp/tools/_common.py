"""ツール定義で共通に使う部品。"""

from gws_mcp.guard import FILE_ID_PATTERN, assert_writable_file


def obj(properties: dict, required: list | None = None) -> dict:
    """想定外の引数を受け付けない object スキーマを作る。"""
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def file_id(description: str = "Drive のファイル ID") -> dict:
    return {"type": "string", "pattern": FILE_ID_PATTERN, "description": description}


def writable(services, file_id_: str) -> dict:
    """書き込み前のガード。Docs/Sheets/Slides の ID も Drive のファイル ID として判定する。"""
    return assert_writable_file(services.get("drive"), file_id_)


def created(services, file_id_: str) -> dict:
    """作成・コピーしたファイルがマイドライブの自分のファイルであることを確かめる。"""
    meta = writable(services, file_id_)
    return {"id": meta["id"], "name": meta.get("name"), "mimeType": meta.get("mimeType")}
