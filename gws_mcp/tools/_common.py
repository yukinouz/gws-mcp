"""ツール定義で共通に使う部品。"""

import json
import re

from gws_mcp.guard import FILE_ID_PATTERN, assert_writable_file
from gws_mcp.tools import ToolError

# batchUpdate のリクエストを拒否したときのエラー文の書き出し
BATCH_REJECTED = "リクエストに問題があるため、何も変更していません。"

# リンク（Link.url）に許すスキーム。表示されるだけでクリックしない限り何も送られないため、Web のリンクは許可する
_LINK_URL_RE = re.compile(r"https?://", re.IGNORECASE)


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
    中身の検査（値の書き込みや外部 URL の関数など）はサービスごとのモジュールで行う。
    文字列全体の長さの上限は、呼び出し側のスキーマの maxLength で設ける。
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
            BATCH_REJECTED
            + "使えるリクエスト: "
            + " ".join(sorted(allowed))
            + "。問題: "
            + "、".join(problems)
        )
    return requests


def url_problems(value, path: str, blocked_keys: dict) -> list:
    """入れ子の中の、拒否するキーと url を探し、問題の説明を返す。

    blocked_keys は Google が取りに行く URL や別ファイルの ID を表すキーと、その説明。
    本文を URL に埋め込んで外部に送れるため、どこにあっても拒否する。url（リンク）は http(s) だけ許可する。
    種類の許可リストをすり抜ける指定も拾えるよう、リクエストの種類ではなくキーの名前で探す。
    入れ子が深くても再帰の上限に当たらないよう、スタックでたどる。
    """
    problems, stack = [], [(value, path)]
    while stack:
        v, p = stack.pop()
        if isinstance(v, dict):
            for k, child in v.items():
                if k in blocked_keys:
                    problems.append(f"{p}.{k}: {blocked_keys[k]}")
                elif k == "url" and not (isinstance(child, str) and _LINK_URL_RE.match(child)):
                    problems.append(f"{p}.{k}: リンクは http:// か https:// で始まる URL にしてください")
                else:
                    stack.append((child, f"{p}.{k}"))
        elif isinstance(v, list):
            stack.extend((child, f"{p}[{i}]") for i, child in enumerate(v))
    return problems
