"""Gmail / Calendar / Sheets / Docs / Slides ツールのテスト。Google API はモックする。"""

import base64
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
    "docs_append_text": ({"document_id": FILE_ID, "text": "x"}, lambda s: s.docs_documents.batchUpdate),
    "docs_replace_text": (
        {"document_id": FILE_ID, "find": "a", "replace": "b"},
        lambda s: s.docs_documents.batchUpdate,
    ),
    "slides_add_text_slide": ({"presentation_id": FILE_ID, "title": "x"}, lambda s: s.slides_presentations.batchUpdate),
    "slides_replace_text": (
        {"presentation_id": FILE_ID, "find": "a", "replace": "b"},
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

    def test_セル値は文字列_数値_真偽値のみ(self):
        schema = load_tools()["sheets_write_range"].input_schema
        validate(schema, {"spreadsheet_id": FILE_ID, "range": "A1", "values": [["a", 1, 1.5, False]]})
        with self.assertRaises(SchemaError):
            validate(schema, {"spreadsheet_id": FILE_ID, "range": "A1", "values": [[{"a": 1}]]})
        with self.assertRaises(SchemaError):
            validate(schema, {"spreadsheet_id": FILE_ID, "range": "A1", "values": [[None]]})


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


if __name__ == "__main__":
    unittest.main()
