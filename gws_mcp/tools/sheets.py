"""スプレッドシート ツール。

- 読み取り: シート構成と範囲の値を取得できる
- 書き込み: 新規作成と、自分が所有するマイドライブのファイルへの書き込み・行の追記・シート追加ができる
- できないこと: 削除、共有ドライブや他人のファイルへの書き込み

値は既定で RAW（入力をそのまま文字列・数値として保存）で書き込む。
電話番号（"+81-..."）や "=" で始まる文章が意図せず数式になるのを防ぐため。
数式は USER_ENTERED を指定すれば使える。ただし外部 URL にアクセスする関数
（IMPORTDATA など）は、シートの内容を外部に送信できてしまうため、指定にかかわらず拒否する。
"""

import re

from gws_mcp.tools import GUARD_CREATED, GUARD_MYDRIVE, Tool, ToolError
from gws_mcp.tools._common import created, file_id, obj, writable

# Google が引数の URL にアクセスする関数。URL にセルの値を埋め込まれると外部に送られる。
# IMPORTRANGE（Google スプレッドシート間の参照）や HYPERLINK（表示のみ）は外部に出ないので対象外。
_EXTERNAL_FETCH_FUNCTIONS = ("IMPORTDATA", "IMPORTXML", "IMPORTHTML", "IMPORTFEED", "IMAGE")
# 見逃しより誤検知を優先する: 文字列リテラル内も単語の途中も区別せずに検索する
_EXTERNAL_FETCH_RE = re.compile(r"(" + "|".join(_EXTERNAL_FETCH_FUNCTIONS) + r")\s*\(", re.IGNORECASE)

_SPREADSHEET_ID = file_id("スプレッドシートの ID")
_RANGE = {"type": "string", "minLength": 1, "maxLength": 200, "description": "A1 形式の範囲（例: シート1!A1:D10）"}
_VALUES = {
    "type": "array",
    "maxItems": 10_000,
    "items": {"type": "array", "maxItems": 1_000, "items": {"type": ["string", "number", "boolean"]}},
    "description": "行の配列（各行はセル値の配列）",
}
_VALUE_INPUT = {
    "type": "string",
    "enum": ["RAW", "USER_ENTERED"],
    "description": "RAW（既定・そのまま文字列として保存）または USER_ENTERED（数式や日付を解釈する）。"
    "数式を使うときは USER_ENTERED を指定する",
}
_FORMULA_NOTE = (
    "数式は value_input_option に USER_ENTERED を指定すると使える"
    "（外部 URL にアクセスする " + "・".join(_EXTERNAL_FETCH_FUNCTIONS) + " は拒否される）。"
)
_SHEET_TITLE = {"type": "string", "minLength": 1, "maxLength": 100}


def _reject_external_fetch(values):
    """外部 URL にアクセスする関数を含むセルがあれば、該当セルを列挙して ToolError を送出する。

    "=" 以外（"+" "-" など）で始まっても数式になりうるため、文字列のセルはすべて検査する。
    """
    hits = []
    for r, row in enumerate(values):
        for c, cell in enumerate(row):
            if isinstance(cell, str) and (m := _EXTERNAL_FETCH_RE.search(cell)):
                hits.append(f"values[{r}][{c}]（{m.group(1).upper()}）")
    if hits:
        raise ToolError(
            "外部 URL にアクセスする関数（"
            + " ".join(_EXTERNAL_FETCH_FUNCTIONS)
            + "）は、データが外部に送られるのを防ぐため使えません。書き込みは行っていません。"
            + "該当セル（0 始まりの行・列番号）: "
            + "、".join(hits)
        )


def sheets_get_metadata(services, args):
    return (
        services.get("sheets")
        .spreadsheets()
        .get(
            spreadsheetId=args["spreadsheet_id"],
            fields="spreadsheetId,spreadsheetUrl,properties(title,locale,timeZone),"
            "sheets(properties(sheetId,title,index,gridProperties(rowCount,columnCount)))",
        )
        .execute()
    )


def sheets_read_range(services, args):
    return (
        services.get("sheets")
        .spreadsheets()
        .values()
        .get(
            spreadsheetId=args["spreadsheet_id"],
            range=args["range"],
            valueRenderOption=args.get("value_render", "FORMATTED_VALUE"),
        )
        .execute()
    )


def sheets_create(services, args):
    body = {"properties": {"title": args["title"]}}
    if args.get("sheet_titles"):
        body["sheets"] = [{"properties": {"title": t}} for t in args["sheet_titles"]]
    # Sheets API の create はマイドライブ直下に作成する
    res = services.get("sheets").spreadsheets().create(body=body, fields="spreadsheetId,spreadsheetUrl").execute()
    return {**created(services, res["spreadsheetId"]), "url": res.get("spreadsheetUrl")}


def sheets_write_range(services, args):
    meta = writable(services, args["spreadsheet_id"])
    if args.get("value_input_option") == "USER_ENTERED":
        _reject_external_fetch(args["values"])
    return (
        services.get("sheets")
        .spreadsheets()
        .values()
        .update(
            spreadsheetId=meta["id"],
            range=args["range"],
            valueInputOption=args.get("value_input_option", "RAW"),
            body={"values": args["values"]},
        )
        .execute()
    )


def sheets_append_rows(services, args):
    meta = writable(services, args["spreadsheet_id"])
    if args.get("value_input_option") == "USER_ENTERED":
        _reject_external_fetch(args["values"])
    res = (
        services.get("sheets")
        .spreadsheets()
        .values()
        .append(
            spreadsheetId=meta["id"],
            range=args["range"],
            valueInputOption=args.get("value_input_option", "RAW"),
            insertDataOption="INSERT_ROWS",
            body={"values": args["values"]},
        )
        .execute()
    )
    return res.get("updates", res)


def sheets_add_sheet(services, args):
    meta = writable(services, args["spreadsheet_id"])
    res = (
        services.get("sheets")
        .spreadsheets()
        .batchUpdate(
            spreadsheetId=meta["id"],
            body={"requests": [{"addSheet": {"properties": {"title": args["title"]}}}]},
        )
        .execute()
    )
    return res["replies"][0]["addSheet"]["properties"]


TOOLS = [
    Tool(
        name="sheets_get_metadata",
        title="シート構成を取得",
        description="スプレッドシートのタイトルと、各シート（タブ）の名前・行数・列数を取得する。",
        input_schema=obj({"spreadsheet_id": _SPREADSHEET_ID}, ["spreadsheet_id"]),
        handler=sheets_get_metadata,
        read_only=True,
    ),
    Tool(
        name="sheets_read_range",
        title="範囲の値を読む",
        description="指定した範囲のセル値を取得する（共有ドライブのファイルも可）。",
        input_schema=obj(
            {
                "spreadsheet_id": _SPREADSHEET_ID,
                "range": _RANGE,
                "value_render": {
                    "type": "string",
                    "enum": ["FORMATTED_VALUE", "UNFORMATTED_VALUE", "FORMULA"],
                    "description": "表示形式の値（既定）・生の値・数式",
                },
            },
            ["spreadsheet_id", "range"],
        ),
        handler=sheets_read_range,
        read_only=True,
    ),
    Tool(
        name="sheets_create",
        title="スプレッドシートを作成",
        description="マイドライブ直下にスプレッドシートを新規作成する。",
        input_schema=obj(
            {
                "title": {"type": "string", "minLength": 1, "maxLength": 255},
                "sheet_titles": {"type": "array", "maxItems": 50, "items": _SHEET_TITLE, "description": "作成するシート名"},
            },
            ["title"],
        ),
        handler=sheets_create,
        read_only=False,
        guard=GUARD_CREATED,
    ),
    Tool(
        name="sheets_write_range",
        title="範囲に書き込む",
        description="指定した範囲にセル値を上書きする。対象は自分が所有するマイドライブのファイルに限る。" + _FORMULA_NOTE,
        input_schema=obj(
            {
                "spreadsheet_id": _SPREADSHEET_ID,
                "range": _RANGE,
                "values": _VALUES,
                "value_input_option": _VALUE_INPUT,
            },
            ["spreadsheet_id", "range", "values"],
        ),
        handler=sheets_write_range,
        read_only=False,
        guard=GUARD_MYDRIVE,
        idempotent=True,
    ),
    Tool(
        name="sheets_append_rows",
        title="行を追記",
        description="表の末尾に行を追加する。対象は自分が所有するマイドライブのファイルに限る。" + _FORMULA_NOTE,
        input_schema=obj(
            {
                "spreadsheet_id": _SPREADSHEET_ID,
                "range": _RANGE,
                "values": _VALUES,
                "value_input_option": _VALUE_INPUT,
            },
            ["spreadsheet_id", "range", "values"],
        ),
        handler=sheets_append_rows,
        read_only=False,
        guard=GUARD_MYDRIVE,
    ),
    Tool(
        name="sheets_add_sheet",
        title="シートを追加",
        description="スプレッドシートにシート（タブ）を追加する。対象は自分が所有するマイドライブのファイルに限る。",
        input_schema=obj({"spreadsheet_id": _SPREADSHEET_ID, "title": _SHEET_TITLE}, ["spreadsheet_id", "title"]),
        handler=sheets_add_sheet,
        read_only=False,
        guard=GUARD_MYDRIVE,
    ),
]
