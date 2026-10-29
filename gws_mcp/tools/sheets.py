"""スプレッドシート ツール。

- 読み取り: シート構成と範囲の値を取得できる
- 書き込み: 新規作成と、自分が所有するマイドライブのファイルへの書き込み・行の追記・シート追加ができる
- できないこと: ファイルとシート丸ごとの削除、共有ドライブや他人のファイルへの書き込み

値は既定で RAW（入力をそのまま文字列・数値として保存）で書き込む。
電話番号（"+81-..."）や "=" で始まる文章が意図せず数式になるのを防ぐため。
数式は USER_ENTERED を指定すれば使える。ただし外部 URL にアクセスする関数
（IMPORTDATA など）は、シートの内容を外部に送信できてしまうため、指定にかかわらず拒否する。

書式・並べ替え・検索と置換・グラフは sheets_batch_update で行える。許可リストの種類だけを受け付け、
シートの削除や外部への接続はできない。repeatCell は範囲全体の値を一度に上書きできるため値を受け付けず、
値の書き込みは RAW と USER_ENTERED を選べる sheets_write_range に寄せる。
"""

import re

from gws_mcp.tools import GUARD_CREATED, GUARD_MYDRIVE, Tool, ToolError
from gws_mcp.tools._common import (
    BATCH_REJECTED,
    created,
    external_ref_problems,
    file_id,
    obj,
    parse_batch_requests,
    writable,
)

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

# sheets_batch_update で使えるリクエスト。シートの削除・外部接続・権限の変更は含めない
_BATCH_ALLOWED = frozenset(
    {
        "repeatCell",
        "updateBorders",
        "updateDimensionProperties",
        "autoResizeDimensions",
        "updateSheetProperties",
        "setBasicFilter",
        "clearBasicFilter",
        "addBanding",
        "addConditionalFormatRule",
        "setDataValidation",
        "mergeCells",
        "unmergeCells",
        "sortRange",
        "findReplace",
        # グラフ。データ範囲は同じスプレッドシートの中だけを指す
        "addChart",
        "updateChartSpec",
        "updateEmbeddedObjectPosition",
        "updateEmbeddedObjectBorder",
        "deleteEmbeddedObject",
        # 行・列・範囲とシートの複製。ファイルの中で完結し、版の履歴から戻せる。
        # シート丸ごとの削除（deleteSheet）は、中身に加えてほかのシートの数式・グラフ・名前付き範囲からの参照も壊すため含めない
        "insertDimension",
        "appendDimension",
        "moveDimension",
        "deleteDimension",
        "insertRange",
        "deleteRange",
        "duplicateSheet",
        # 見た目と整理。フィルタ表示・名前付き範囲・行と列のグループ化・スライサー・テーブル
        "addFilterView",
        "updateFilterView",
        "duplicateFilterView",
        "deleteFilterView",
        "addNamedRange",
        "updateNamedRange",
        "deleteNamedRange",
        "addDimensionGroup",
        "updateDimensionGroup",
        "deleteDimensionGroup",
        "addSlicer",
        "updateSlicerSpec",
        "updateConditionalFormatRule",
        "deleteConditionalFormatRule",
        "updateBanding",
        "deleteBanding",
        # テーブルの削除（deleteTable）は、セルのデータまで消えるかを API の説明で確かめられないため含めない
        "addTable",
        "updateTable",
        # ファイルの中のデータの移動と整理
        "copyPaste",
        "cutPaste",
        "autoFill",
        "deleteDuplicates",
        "trimWhitespace",
    }
)
# 許可した種類の中にも入れられる、外部データにつながるキー。
# グラフやピボットテーブルは dataSourceId で BigQuery などのデータソースを参照できる
_BLOCKED_KEYS = {"dataSourceId": "データソース（BigQuery などの外部データ）は使えません"}
_MAX_BATCH_REQUESTS = 200
_MAX_BATCH_TEXT = 200_000
# fields（上書きする項目）を絞る種類と、許可する項目。それ以外の項目は表示や構造を大きく変えるため
_BATCH_ALLOWED_FIELDS = {
    "updateSheetProperties": {
        "gridProperties.frozenRowCount",
        "gridProperties.frozenColumnCount",
        "tabColorStyle",
        "hidden",
    },
    # 隠した行・列は画面で分かるため、隠すことも許可する
    "updateDimensionProperties": {"pixelSize", "hiddenByUser"},
}
# fields の項目名。英数字とアンダースコアの名前をピリオドでつなぐ
_FIELD_PATH_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
# fields の括弧の入れ子の上限。書式の指定ではこれより深くならない
_MAX_FIELD_DEPTH = 5


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


def _split_fields(fields, depth: int = 0) -> list | None:
    """fields（FieldMask）を最上位のカンマで分け、各項目が "a.b" か "a.b(…)" の形かを確かめる。

    "a(b,c),d" は ["a(b,c)", "d"] になる。形が不正なら None を返す。
    値の項目を書式の項目に見せかける書き方を、API 側の解釈に任せず、呼ぶ前に止めるため。
    """
    if not isinstance(fields, str) or depth > _MAX_FIELD_DEPTH:
        return None
    # 空白（ASCII のみ）はカンマと括弧の前後だけ許す。項目名の途中の空白は不正として残す
    fields = re.sub(r"[ \t\r\n]*([,()])[ \t\r\n]*", r"\1", fields).strip(" \t\r\n")
    parts, level, start = [], 0, 0
    for i, ch in enumerate(fields):
        if ch == "(":
            level += 1
        elif ch == ")":
            level -= 1
            if level < 0:
                return None
        elif ch == "," and level == 0:
            parts.append(fields[start:i])
            start = i + 1
    if level != 0:
        return None
    parts.append(fields[start:])
    if parts == [""]:
        return []
    return parts if all(_is_valid_field(p, depth) for p in parts) else None


def _is_valid_field(part: str, depth: int) -> bool:
    """"a.b" か "a.b(…)" の形だけを許す。閉じ括弧の後ろに文字があれば不正。"""
    m = _FIELD_PATH_RE.match(part)
    if not m:
        return False
    rest = part[m.end() :]
    if not rest:
        return True
    if not (rest.startswith("(") and rest.endswith(")")):
        return False
    return bool(_split_fields(rest[1:-1], depth + 1))


def _is_format_field(field: str) -> bool:
    return field == "userEnteredFormat" or field.startswith(("userEnteredFormat.", "userEnteredFormat("))


def _check_fields(body: dict, path: str, is_allowed, hint: str) -> list:
    """fields を分けて、許可しない項目があれば問題の説明を返す。"""
    fields = _split_fields(body.get("fields"))
    if not fields:
        return [f"{path}.fields: 形が不正か空です。使える項目: {hint}"]
    return [f"{path}.fields: {f} は使えません。使える項目: {hint}" for f in fields if not is_allowed(f)]


def _check_batch_request(kind: str, body: dict, path: str) -> list:
    """値を書き込む指定や、許可しない fields を探し、問題の説明を返す。"""
    problems = []
    if kind == "repeatCell":
        cell = body.get("cell", {})
        # 値を消さないための本当の防御は fields の検査。cell の検査は、値を渡した意図に気づかせるためのもの
        if not isinstance(cell, dict):
            problems.append(f"{path}.cell: オブジェクトにしてください")
        elif value_keys := {"userEnteredValue", "user_entered_value"} & set(cell):
            problems.append(f"{path}.cell.{min(value_keys)}: 値は sheets_write_range で書き込んでください")
        # fields に値の項目や "*" を含めると、cell に値がなくても範囲の値が消える
        problems += _check_fields(body, path, _is_format_field, "userEnteredFormat で始まる項目")
    elif kind in _BATCH_ALLOWED_FIELDS:
        allowed = _BATCH_ALLOWED_FIELDS[kind]
        problems += _check_fields(body, path, allowed.__contains__, " ".join(sorted(allowed)))
    return problems


def _external_fetch_hits(value, path: str) -> list:
    """入れ子の中のすべての文字列から外部 URL にアクセスする関数を探す。

    条件付き書式やプルダウンの条件の値（ConditionValue.userEnteredValue）には数式を書けるため。
    入れ子が深くても再帰の上限に当たらないよう、スタックでたどる。
    """
    hits, stack = [], [(value, path)]
    while stack:
        v, p = stack.pop()
        if isinstance(v, str):
            if m := _EXTERNAL_FETCH_RE.search(v):
                hits.append(f"{p}（{m.group(1).upper()}）")
        elif isinstance(v, dict):
            stack.extend((child, f"{p}.{k}") for k, child in v.items())
        elif isinstance(v, list):
            stack.extend((child, f"{p}[{i}]") for i, child in enumerate(v))
    return hits


def _validate_batch_requests(text: str) -> list:
    """sheets_batch_update のリクエストを検査し、1つでも問題があれば API を呼ばずに全体を拒否する。"""
    requests = parse_batch_requests(text, _BATCH_ALLOWED, _MAX_BATCH_REQUESTS)
    problems, hits = [], []
    for i, req in enumerate(requests):
        kind, body = next(iter(req.items()))
        problems += _check_batch_request(kind, body, f"requests[{i}].{kind}")
        problems += external_ref_problems(body, f"requests[{i}].{kind}", _BLOCKED_KEYS)
        hits += _external_fetch_hits(body, f"requests[{i}].{kind}")
    if hits:
        problems.append(
            "外部 URL にアクセスする関数（" + " ".join(_EXTERNAL_FETCH_FUNCTIONS) + "）は使えません: " + "、".join(hits)
        )
    if problems:
        raise ToolError(BATCH_REJECTED + "問題: " + "、".join(problems))
    return requests


def sheets_get_metadata(services, args):
    return (
        services.get("sheets")
        .spreadsheets()
        .get(
            spreadsheetId=args["spreadsheet_id"],
            fields="spreadsheetId,spreadsheetUrl,properties(title,locale,timeZone),"
            # グラフの ID は、グラフの更新やスライドへの埋め込みで指定する
            "sheets(properties(sheetId,title,index,gridProperties(rowCount,columnCount)),charts(chartId,spec(title)))",
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


def sheets_batch_update(services, args):
    # 入力の検査は通信が要らないので、ガードより先に行う
    requests = _validate_batch_requests(args["requests"])
    meta = writable(services, args["spreadsheet_id"])
    res = (
        services.get("sheets")
        .spreadsheets()
        .batchUpdate(spreadsheetId=meta["id"], body={"requests": requests})
        .execute()
    )
    # addBanding などで作られた ID を後で使えるよう、replies を返す
    return {"replies": res.get("replies", [])}


TOOLS = [
    Tool(
        name="sheets_get_metadata",
        title="シート構成を取得",
        description="スプレッドシートのタイトルと、各シート（タブ）の名前・行数・列数、グラフの ID とタイトルを取得する。",
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
    Tool(
        name="sheets_batch_update",
        title="書式・並べ替え・置換・グラフ",
        description="Sheets API の spreadsheets.batchUpdate で書式の設定・並べ替え・検索と置換・グラフの作成と編集、"
        "行・列の挿入と削除、フィルタ表示・名前付き範囲・テーブルなどの整理、ファイルの中のコピーと貼り付けを行う。"
        "対象は自分が所有するマイドライブのファイルに限る。"
        "requests にはリクエストの配列を JSON 文字列で渡す（" + str(_MAX_BATCH_REQUESTS) + " 件まで）。"
        "使えるリクエスト: " + " ".join(sorted(_BATCH_ALLOWED)) + "。"
        "範囲は GridRange（sheetId と 0 始まり・終わりを含まない行・列の番号）で指定し、"
        "sheetId は sheets_get_metadata で取得する。"
        "repeatCell の fields は userEnteredFormat で始まる項目だけ、"
        "updateSheetProperties の fields は gridProperties.frozenRowCount・gridProperties.frozenColumnCount・"
        "tabColorStyle・hidden だけ、updateDimensionProperties の fields は pixelSize・hiddenByUser だけを指定できる。"
        "行・列・範囲の挿入・移動・削除とシートの複製はできるが、シート丸ごとの削除はできない。"
        "repeatCell での値（userEnteredValue）の書き込みは拒否される（値は sheets_write_range で書き込む）。"
        "グラフ（addChart など）のデータ範囲は同じスプレッドシートの GridRange で指定する。"
        "グラフの ID（chartId・objectId）は sheets_get_metadata で取得する。"
        "外部 URL にアクセスする " + "・".join(_EXTERNAL_FETCH_FUNCTIONS) + " を含むリクエストと、"
        "データソース（dataSourceId）は拒否される。"
        "mergeCells は左上以外のセルの値を消すため、値のない範囲か、消えてよいことを確かめてから使う。"
        "1件でも不正なリクエストがあれば、何も変更せずに全体を拒否する。",
        input_schema=obj(
            {
                "spreadsheet_id": _SPREADSHEET_ID,
                "requests": {
                    "type": "string",
                    "minLength": 2,
                    "maxLength": _MAX_BATCH_TEXT,
                    "description": "リクエストの配列の JSON 文字列"
                    '（例: [{"repeatCell": {"range": {"sheetId": 0, "endRowIndex": 1}, '
                    '"cell": {"userEnteredFormat": {"textFormat": {"bold": true}}}, '
                    '"fields": "userEnteredFormat.textFormat.bold"}}]）',
                },
            },
            ["spreadsheet_id", "requests"],
        ),
        handler=sheets_batch_update,
        read_only=False,
        guard=GUARD_MYDRIVE,
    ),
]
