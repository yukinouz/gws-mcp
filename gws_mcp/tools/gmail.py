"""Gmail ツール。

- 読み取り: メールの検索、本文・スレッド・ラベル一覧の取得ができる
- できないこと: 送信、下書き作成、ラベル変更などの書き込み全般、添付ファイルの中身の取得（名前と形式だけ返す）
"""

import base64
import html
import re
from html.parser import HTMLParser

from gws_mcp.tools import Tool
from gws_mcp.tools._common import obj

# Gmail のメッセージ ID・スレッド ID の形式
_ID_PATTERN = r"[A-Za-z0-9]{8,64}"

_HEADERS = ["From", "To", "Cc", "Subject", "Date"]

# 1 通あたりの本文の上限（スレッドをまとめて返すときにコンテキストを圧迫しないため）
MAX_BODY_CHARS = 20_000


class _TextExtractor(HTMLParser):
    """HTML メールからテキストだけを取り出す。"""

    _SKIP = {"script", "style", "head"}
    _BREAK = {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth:
            self.parts.append(data)


def _html_to_text(source: str) -> str:
    parser = _TextExtractor()
    parser.feed(source)
    text = html.unescape("".join(parser.parts))
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def _walk(part: dict):
    yield part
    for sub in part.get("parts", []) or []:
        yield from _walk(sub)


def _extract_body(payload: dict) -> str:
    """本文を text/plain 優先で取り出す。無ければ text/html をテキスト化する。"""
    plain, rich = [], []
    for part in _walk(payload):
        data = (part.get("body") or {}).get("data")
        if not data or part.get("filename"):
            continue
        mime = part.get("mimeType", "")
        if mime == "text/plain":
            plain.append(_decode(data))
        elif mime == "text/html":
            rich.append(_html_to_text(_decode(data)))
    body = "\n".join(plain) if plain else "\n".join(rich)
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + f"\n…（本文を {MAX_BODY_CHARS} 文字で切り詰めました）"
    return body


def _headers(payload: dict) -> dict:
    return {h["name"]: h["value"] for h in payload.get("headers", []) if h.get("name") in _HEADERS}


def _attachments(payload: dict) -> list:
    return [
        {"filename": p["filename"], "mimeType": p.get("mimeType"), "size": (p.get("body") or {}).get("size")}
        for p in _walk(payload)
        if p.get("filename")
    ]


def _format_message(msg: dict) -> dict:
    payload = msg.get("payload", {})
    return {
        "id": msg.get("id"),
        "threadId": msg.get("threadId"),
        "labelIds": msg.get("labelIds", []),
        **_headers(payload),
        "body": _extract_body(payload),
        "attachments": _attachments(payload),
    }


def gmail_search(services, args):
    gmail = services.get("gmail")
    res = (
        gmail.users()
        .messages()
        .list(userId="me", q=args["query"], maxResults=args.get("max_results", 10), pageToken=args.get("page_token"))
        .execute()
    )
    messages = []
    for m in res.get("messages", []):
        detail = (
            gmail.users()
            .messages()
            .get(userId="me", id=m["id"], format="metadata", metadataHeaders=_HEADERS)
            .execute()
        )
        messages.append(
            {
                "id": detail.get("id"),
                "threadId": detail.get("threadId"),
                **_headers(detail.get("payload", {})),
                "snippet": html.unescape(detail.get("snippet", "")),
                "labelIds": detail.get("labelIds", []),
            }
        )
    return {"messages": messages, "nextPageToken": res.get("nextPageToken")}


def gmail_get_message(services, args):
    msg = services.get("gmail").users().messages().get(userId="me", id=args["message_id"], format="full").execute()
    return _format_message(msg)


def gmail_get_thread(services, args):
    thread = services.get("gmail").users().threads().get(userId="me", id=args["thread_id"], format="full").execute()
    return {"id": thread.get("id"), "messages": [_format_message(m) for m in thread.get("messages", [])]}


def gmail_list_labels(services, args):
    res = services.get("gmail").users().labels().list(userId="me").execute()
    return [{"id": l["id"], "name": l["name"], "type": l.get("type")} for l in res.get("labels", [])]


TOOLS = [
    Tool(
        name="gmail_search",
        title="メールを検索",
        description=(
            "Gmail の検索構文（例: from:foo@example.com after:2026/09/01 is:unread）でメールを検索し、"
            "差出人・件名・日付・抜粋を返す。本文は gmail_get_message で取得する。"
        ),
        input_schema=obj(
            {
                "query": {"type": "string", "maxLength": 500, "description": "Gmail の検索クエリ"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 50, "description": "取得件数（既定 10）"},
                "page_token": {"type": "string", "maxLength": 2000, "description": "前回結果の nextPageToken"},
            },
            ["query"],
        ),
        handler=gmail_search,
        read_only=True,
    ),
    Tool(
        name="gmail_get_message",
        title="メールを読む",
        description="メール 1 通のヘッダー・本文（text/plain 優先）・添付ファイル名を取得する。",
        input_schema=obj({"message_id": {"type": "string", "pattern": _ID_PATTERN}}, ["message_id"]),
        handler=gmail_get_message,
        read_only=True,
    ),
    Tool(
        name="gmail_get_thread",
        title="スレッドを読む",
        description="スレッド内のすべてのメールのヘッダー・本文を取得する。",
        input_schema=obj({"thread_id": {"type": "string", "pattern": _ID_PATTERN}}, ["thread_id"]),
        handler=gmail_get_thread,
        read_only=True,
    ),
    Tool(
        name="gmail_list_labels",
        title="ラベル一覧",
        description="Gmail のラベル一覧を取得する（検索クエリの label: 指定に使う）。",
        input_schema=obj({}),
        handler=gmail_list_labels,
        read_only=True,
    ),
]
