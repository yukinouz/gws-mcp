"""ツール定義で共通に使う部品。"""

import json

from gws_mcp.guard import FILE_ID_PATTERN, assert_writable_file
from gws_mcp.tools import ToolError


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


def parse_batch_requests(text: str, allowed: frozenset, max_requests: int) -> list:
    """batchUpdate のリクエスト配列（JSON 文字列）を読み込み、件数と種類を検査する。

    リクエストは入れ子が深く schema.py の object 定義では書き切れないため、文字列で受け取ってここで検査する。
    種類はトップレベルのキーで判定し、許可リストにない種類が1つでもあれば全体を拒否する。
    中身の検査（値を変えないかなど）はサービスごとのモジュールで行う。
    """
    try:
        requests = json.loads(text)
    # 入れ子が深すぎる JSON は RecursionError になる
    except (ValueError, RecursionError) as e:
        raise ToolError(f"requests を JSON として読めません: {e}") from None
    if not isinstance(requests, list) or not requests:
        raise ToolError("requests はリクエストの配列（1件以上）を JSON 文字列で指定してください")
    if len(requests) > max_requests:
        raise ToolError(f"requests は {max_requests} 件以下にしてください（{len(requests)} 件）")

    problems = []
    for i, req in enumerate(requests):
        if not isinstance(req, dict) or len(req) != 1:
            problems.append(f"requests[{i}]: 種類のキーを1つだけ持つオブジェクトにしてください")
            continue
        kind, body = next(iter(req.items()))
        if kind not in allowed:
            problems.append(f"requests[{i}]: {kind} は使えません")
        elif not isinstance(body, dict):
            problems.append(f"requests[{i}].{kind}: オブジェクトにしてください")
    if problems:
        raise ToolError(
            "リクエストに問題があるため、何も変更していません。"
            + "使えるリクエスト: "
            + " ".join(sorted(allowed))
            + "。問題: "
            + "、".join(problems)
        )
    return requests
