"""Drive ツールのテスト。Drive API はモックする。"""

import unittest
from unittest.mock import MagicMock

from gws_mcp.guard import FOLDER_MIME
from gws_mcp.server import MCPServer
from gws_mcp.tools import load_tools
from gws_mcp.tools.drive import _escape_query

FILE_ID = "1AbCdEfGhIjKlMnOp"
FOLDER_ID = "1FoLdErIdXyZ12345"
NEW_ID = "1NeWfIlEiDxYz9876"

MY_TEXT = {"id": FILE_ID, "name": "memo.txt", "mimeType": "text/plain", "ownedByMe": True, "trashed": False}
MY_FOLDER = {"id": FOLDER_ID, "name": "作業", "mimeType": FOLDER_MIME, "ownedByMe": True, "trashed": False}
SHARED_DRIVE_FILE = {**MY_TEXT, "driveId": "0ABCDEF", "ownedByMe": False}
OTHERS_FILE = {**MY_TEXT, "ownedByMe": False}
NEW_FILE = {"id": NEW_ID, "name": "new", "mimeType": "text/plain", "ownedByMe": True, "trashed": False}

META = {
    "_meta": {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {},
    }
}


class FakeServices:
    """files().get() が fileId ごとに metadata を返す Drive モックを持つ。"""

    def __init__(self, files: dict):
        self.drive = MagicMock()
        files_api = self.drive.files.return_value

        def get(fileId, **kwargs):
            req = MagicMock()
            req.execute.return_value = files[fileId]
            return req

        files_api.get.side_effect = get
        files_api.create.return_value.execute.return_value = {"id": NEW_ID}
        files_api.copy.return_value.execute.return_value = {"id": NEW_ID}
        files_api.update.return_value.execute.return_value = {"id": FILE_ID, "name": "updated"}
        files_api.list.return_value.execute.return_value = {"files": []}
        self.files_api = files_api

    def get(self, name):
        assert name == "drive"
        return self.drive


def call(services, name, arguments):
    server = MCPServer(load_tools(), services)
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {**META, "name": name, "arguments": arguments}}
    return server.handle(msg)["result"]


class ReadTest(unittest.TestCase):
    def test_検索キーワードのエスケープ(self):
        self.assertEqual(_escape_query("a'b\\c"), "a\\'b\\\\c")

    def test_検索は共有ドライブも対象でクエリを注入できない(self):
        s = FakeServices({})
        result = call(s, "drive_search", {"query": "x' or trashed = true or name contains '"})
        self.assertFalse(result["isError"])
        kwargs = s.files_api.list.call_args.kwargs
        self.assertIs(kwargs["supportsAllDrives"], True)
        self.assertIs(kwargs["includeItemsFromAllDrives"], True)
        self.assertIn("x\\' or trashed = true", kwargs["q"])
        self.assertTrue(kwargs["q"].endswith("and trashed = false"))

    def test_フォルダIDに不正な値は渡せない(self):
        s = FakeServices({})
        result = call(s, "drive_list_folder", {"folder_id": "root' in parents or '1"})
        self.assertTrue(result["isError"])
        s.files_api.list.assert_not_called()

    def test_バイナリは読まない(self):
        s = FakeServices({FILE_ID: {**MY_TEXT, "mimeType": "image/png"}})
        result = call(s, "drive_read_file", {"file_id": FILE_ID})
        self.assertTrue(result["isError"])
        s.files_api.get_media.assert_not_called()

    def test_Googleドキュメントはテキストでエクスポート(self):
        s = FakeServices({FILE_ID: {**MY_TEXT, "mimeType": "application/vnd.google-apps.document"}})
        s.files_api.export.return_value.execute.return_value = b"hello"
        result = call(s, "drive_read_file", {"file_id": FILE_ID})
        self.assertFalse(result["isError"])
        self.assertIn("hello", result["content"][0]["text"])
        self.assertEqual(s.files_api.export.call_args.kwargs["mimeType"], "text/plain")


class WriteGuardTest(unittest.TestCase):
    """書き込みツールがガードを通り、拒否時は書き込み API を呼ばないこと。"""

    def assert_rejected(self, s, name, arguments, pattern):
        result = call(s, name, arguments)
        self.assertTrue(result["isError"], result)
        self.assertRegex(result["content"][0]["text"], pattern)
        s.files_api.create.assert_not_called()
        s.files_api.update.assert_not_called()
        s.files_api.copy.assert_not_called()

    def test_共有ドライブのファイルは更新できない(self):
        s = FakeServices({FILE_ID: SHARED_DRIVE_FILE})
        self.assert_rejected(s, "drive_update_text_file", {"file_id": FILE_ID, "content": "x"}, "共有ドライブ")

    def test_共有ドライブのファイルは名前変更できない(self):
        s = FakeServices({FILE_ID: SHARED_DRIVE_FILE})
        self.assert_rejected(s, "drive_rename", {"file_id": FILE_ID, "new_name": "x"}, "共有ドライブ")

    def test_他人のファイルは名前変更できない(self):
        s = FakeServices({FILE_ID: OTHERS_FILE})
        self.assert_rejected(s, "drive_rename", {"file_id": FILE_ID, "new_name": "x"}, "所有者")

    def test_共有ドライブのフォルダには作成できない(self):
        s = FakeServices({FOLDER_ID: {**MY_FOLDER, "driveId": "0ABCDEF"}})
        self.assert_rejected(s, "drive_create_folder", {"name": "x", "parent_id": FOLDER_ID}, "共有ドライブ")
        self.assert_rejected(
            s, "drive_create_text_file", {"name": "x", "content": "y", "parent_id": FOLDER_ID}, "共有ドライブ"
        )

    def test_共有ドライブのフォルダにはコピーできない(self):
        s = FakeServices({FOLDER_ID: {**MY_FOLDER, "driveId": "0ABCDEF"}})
        self.assert_rejected(s, "drive_copy_file", {"file_id": FILE_ID, "parent_id": FOLDER_ID}, "共有ドライブ")

    def test_Googleドキュメントはテキスト上書きできない(self):
        s = FakeServices({FILE_ID: {**MY_TEXT, "mimeType": "application/vnd.google-apps.document"}})
        self.assert_rejected(s, "drive_update_text_file", {"file_id": FILE_ID, "content": "x"}, "テキストファイルではない")


class WriteTest(unittest.TestCase):
    def test_名前変更は名前だけを送り共有ドライブ対応を付けない(self):
        s = FakeServices({FILE_ID: MY_TEXT})
        result = call(s, "drive_rename", {"file_id": FILE_ID, "new_name": "新しい名前"})
        self.assertFalse(result["isError"])
        kwargs = s.files_api.update.call_args.kwargs
        self.assertEqual(kwargs["body"], {"name": "新しい名前"})
        self.assertNotIn("supportsAllDrives", kwargs)
        self.assertNotIn("addParents", kwargs)
        self.assertNotIn("removeParents", kwargs)

    def test_テキスト上書き(self):
        s = FakeServices({FILE_ID: MY_TEXT})
        result = call(s, "drive_update_text_file", {"file_id": FILE_ID, "content": "本文"})
        self.assertFalse(result["isError"])
        kwargs = s.files_api.update.call_args.kwargs
        self.assertNotIn("body", kwargs)
        self.assertNotIn("supportsAllDrives", kwargs)

    def test_フォルダ作成は既定でマイドライブ直下(self):
        s = FakeServices({NEW_ID: {**NEW_FILE, "mimeType": FOLDER_MIME}})
        result = call(s, "drive_create_folder", {"name": "新規"})
        self.assertFalse(result["isError"])
        body = s.files_api.create.call_args.kwargs["body"]
        self.assertEqual(body["parents"], ["root"])
        self.assertNotIn("supportsAllDrives", s.files_api.create.call_args.kwargs)

    def test_自分のフォルダへのテキスト作成(self):
        s = FakeServices({FOLDER_ID: MY_FOLDER, NEW_ID: NEW_FILE})
        result = call(s, "drive_create_text_file", {"name": "a.md", "content": "# a", "mime_type": "text/markdown", "parent_id": FOLDER_ID})
        self.assertFalse(result["isError"])
        self.assertEqual(s.files_api.create.call_args.kwargs["body"]["parents"], [FOLDER_ID])

    def test_共有ドライブのファイルをマイドライブにコピー(self):
        s = FakeServices({NEW_ID: NEW_FILE})
        result = call(s, "drive_copy_file", {"file_id": FILE_ID})
        self.assertFalse(result["isError"])
        kwargs = s.files_api.copy.call_args.kwargs
        self.assertEqual(kwargs["body"]["parents"], ["root"])
        self.assertIs(kwargs["supportsAllDrives"], True)


class ToolListTest(unittest.TestCase):
    def test_削除や共有設定のツールが無い(self):
        names = set(load_tools())
        for word in ("delete", "trash", "permission", "share", "move"):
            self.assertFalse([n for n in names if word in n], word)

    def test_書き込みツールはreadOnlyHintがfalse(self):
        for tool in load_tools().values():
            write = any(
                w in tool.name for w in ("create", "update", "rename", "copy", "write", "append", "add", "replace")
            )
            self.assertEqual(tool.read_only, not write, tool.name)


if __name__ == "__main__":
    unittest.main()
