"""カレンダー ツール。

- 読み取り: 自分がアクセスできる任意のカレンダーの予定を取得できる
- 書き込み: 自分のメインカレンダーへの作成と、自分が主催者の予定の更新ができる
- できないこと: 予定の削除、共有カレンダーへの書き込み（編集権限があっても不可）、カレンダー自体の操作

参加者がいる予定では、通知メールを送るか（send_updates）の指定を必須にする。
送るかどうかを既定値に任せず、毎回ユーザーに確認させるため。
"""

import re
from datetime import datetime, timezone

from gws_mcp.guard import GuardError, assert_own_event
from gws_mcp.tools import GUARD_OWN_EVENT, Tool, ToolError
from gws_mcp.tools._common import obj

_PRIMARY = "primary"

# 参加者がいない予定の通知設定（通知先がいないため送らない）
_NO_ATTENDEES_SEND_UPDATES = "none"

_CALENDAR_ID = {
    "type": "string",
    "pattern": r"primary|[A-Za-z0-9._%+-]{1,200}@[A-Za-z0-9.-]{1,200}",
    "description": "カレンダー ID（省略時は自分のメインカレンダー primary）",
}
_EVENT_ID = {"type": "string", "pattern": r"[A-Za-z0-9_]{1,1024}", "description": "予定の ID"}

# 終日予定は日付（YYYY-MM-DD）、時刻指定は RFC3339（例: 2026-09-27T10:00:00+09:00）
_DATE = r"\d{4}-\d{2}-\d{2}"
_DATETIME = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})?"
_WHEN = {
    "type": "string",
    "pattern": f"{_DATE}|{_DATETIME}",
    "description": "終日なら YYYY-MM-DD、時刻指定なら 2026-09-27T10:00:00+09:00 の形式",
}
_RFC3339 = {
    "type": "string",
    "pattern": r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})",
    "description": "RFC3339 形式（例: 2026-09-27T00:00:00+09:00）",
}
_TIME_ZONE = {"type": "string", "pattern": r"[A-Za-z_]+(/[A-Za-z0-9_+-]+){0,2}", "description": "例: Asia/Tokyo"}
_ATTENDEES = {
    "type": "array",
    "maxItems": 100,
    "items": {"type": "string", "pattern": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"},
    "description": "参加者のメールアドレス",
}
_SEND_UPDATES = {
    "type": "string",
    "enum": ["none", "all", "externalOnly"],
    "description": (
        "参加者への通知メール。none: 送らない、all: 全員に送る、externalOnly: 組織外の参加者だけに送る。"
        "参加者がいる予定では必須。通知するかをユーザーに確認してから指定する"
    ),
}
_TEXT = {"type": "string", "maxLength": 8000}

# 作成・更新で受け付ける項目
_EVENT_PROPS = {
    "summary": {"type": "string", "maxLength": 1000, "description": "タイトル"},
    "start": _WHEN,
    "end": _WHEN,
    "time_zone": _TIME_ZONE,
    "description": _TEXT,
    "location": {"type": "string", "maxLength": 1000},
    "attendees": _ATTENDEES,
    "send_updates": _SEND_UPDATES,
}

_EVENT_FIELDS = ("id", "summary", "start", "end", "location", "description", "status", "htmlLink", "organizer", "attendees")


def _when(value: str, tz: str | None) -> dict:
    if re.fullmatch(_DATE, value):
        return {"date": value}
    return {"dateTime": value, **({"timeZone": tz} if tz else {})}


def _event_body(args: dict) -> dict:
    body = {}
    for key in ("summary", "description", "location"):
        if key in args:
            body[key] = args[key]
    tz = args.get("time_zone")
    if "start" in args:
        body["start"] = _when(args["start"], tz)
    if "end" in args:
        body["end"] = _when(args["end"], tz)
    if "attendees" in args:
        body["attendees"] = [{"email": e} for e in args["attendees"]]
    return body


def _send_updates(args: dict, has_attendees: bool) -> str:
    """通知メールの送信設定を返す。参加者がいるのに指定がなければ ToolError。"""
    if "send_updates" in args:
        return args["send_updates"]
    if has_attendees:
        raise ToolError(
            "参加者がいる予定のため、通知メールを送るか（send_updates）の指定が必要です。"
            "ユーザーに確認してから指定してください。"
        )
    return _NO_ATTENDEES_SEND_UPDATES


def _compact(event: dict) -> dict:
    out = {k: event[k] for k in _EVENT_FIELDS if k in event}
    if "attendees" in out:
        out["attendees"] = [
            {"email": a.get("email"), "responseStatus": a.get("responseStatus")} for a in out["attendees"]
        ]
    return out


# ---- 読み取り ----


def calendar_list_events(services, args):
    time_min = args.get("time_min") or datetime.now(timezone.utc).isoformat(timespec="seconds")
    res = (
        services.get("calendar")
        .events()
        .list(
            calendarId=args.get("calendar_id", _PRIMARY),
            timeMin=time_min,
            timeMax=args.get("time_max"),
            q=args.get("query"),
            maxResults=args.get("max_results", 50),
            pageToken=args.get("page_token"),
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )
    return {
        "timeZone": res.get("timeZone"),
        "events": [_compact(e) for e in res.get("items", [])],
        "nextPageToken": res.get("nextPageToken"),
    }


def calendar_get_event(services, args):
    event = (
        services.get("calendar")
        .events()
        .get(calendarId=args.get("calendar_id", _PRIMARY), eventId=args["event_id"])
        .execute()
    )
    return _compact(event)


# ---- 書き込み（primary のみ） ----


def calendar_create_event(services, args):
    body = _event_body(args)
    if "start" not in body or "end" not in body:
        raise ToolError("start と end は必須です。")
    send_updates = _send_updates(args, bool(args.get("attendees")))
    event = (
        services.get("calendar")
        .events()
        .insert(calendarId=_PRIMARY, body=body, sendUpdates=send_updates)
        .execute()
    )
    return _compact(event)


def calendar_update_event(services, args):
    calendar = services.get("calendar")
    current = calendar.events().get(calendarId=_PRIMARY, eventId=args["event_id"]).execute()
    assert_own_event(current)
    body = _event_body(args)
    if not body:
        raise GuardError("変更する項目が指定されていません。")
    # 参加者を外す場合も、外される側に通知が届きうるため既存の参加者も含めて判定する
    send_updates = _send_updates(args, bool(args.get("attendees") or current.get("attendees")))
    event = (
        calendar.events()
        .patch(
            calendarId=_PRIMARY,
            eventId=args["event_id"],
            body=body,
            sendUpdates=send_updates,
        )
        .execute()
    )
    return _compact(event)


TOOLS = [
    Tool(
        name="calendar_list_events",
        title="予定を一覧",
        description="期間・キーワードで予定を開始時刻順に一覧する（繰り返し予定は個別の回に展開）。time_min の既定は現在時刻。",
        input_schema=obj(
            {
                "calendar_id": _CALENDAR_ID,
                "time_min": _RFC3339,
                "time_max": _RFC3339,
                "query": {"type": "string", "maxLength": 200, "description": "キーワード"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 250, "description": "取得件数（既定 50）"},
                "page_token": {"type": "string", "maxLength": 2000},
            }
        ),
        handler=calendar_list_events,
        read_only=True,
    ),
    Tool(
        name="calendar_get_event",
        title="予定の詳細",
        description="予定 1 件の詳細を取得する。",
        input_schema=obj({"calendar_id": _CALENDAR_ID, "event_id": _EVENT_ID}, ["event_id"]),
        handler=calendar_get_event,
        read_only=True,
    ),
    Tool(
        name="calendar_create_event",
        title="予定を作成",
        description=(
            "自分のメインカレンダーに予定を作成する。参加者がいる場合は、招待メールを送るかをユーザーに確認して send_updates に指定する。"
        ),
        input_schema=obj(_EVENT_PROPS, ["summary", "start", "end"]),
        handler=calendar_create_event,
        read_only=False,
        guard=GUARD_OWN_EVENT,
    ),
    Tool(
        name="calendar_update_event",
        title="予定を更新",
        description=(
            "自分のメインカレンダーにある、自分が主催者の予定を部分更新する（指定した項目だけ変更）。"
            "attendees を指定すると参加者リストを置き換える。参加者がいる場合は、通知メールを送るかをユーザーに確認して send_updates に指定する。"
        ),
        input_schema=obj({"event_id": _EVENT_ID, **_EVENT_PROPS}, ["event_id"]),
        handler=calendar_update_event,
        read_only=False,
        guard=GUARD_OWN_EVENT,
        idempotent=True,
    ),
]
