"""スプレッドシート ツール。

- 読み取り: シート構成と範囲の値を取得できる
- 書き込み: 新規作成と、自分が所有するマイドライブのファイルへの書き込み・行の追記・シート追加ができる
- できないこと: 削除、共有ドライブや他人のファイルへの書き込み

値は既定で RAW（入力をそのまま文字列・数値として保存）で書き込む。
USER_ENTERED だと "=IMPORTDATA(...)" のような数式が実行され、
シートの内容を外部に送信できてしまうため、明示したときだけ使う。
"""

from gws_mcp.tools import GUARD_CREATED, GUARD_MYDRIVE, Tool
from gws_mcp.tools._common import created, file_id, obj, writable

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
    "description": "RAW（既定・そのまま保存）または USER_ENTERED（数式や日付を解釈する）",
}
_SHEET_TITLE = {"type": "string", "minLength": 1, "maxLength": 100}


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
        description="指定した範囲にセル値を上書きする。対象は自分が所有するマイドライブのファイルに限る。",
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
        description="表の末尾に行を追加する。対象は自分が所有するマイドライブのファイルに限る。",
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
