"""guard.py（書き込みガード）のテスト。Drive API はモックする。"""

import unittest
from unittest.mock import MagicMock

import httplib2
from googleapiclient.errors import HttpError

from gws_mcp.guard import (
    FOLDER_MIME,
    SHORTCUT_MIME,
    GuardError,
    assert_own_event,
    assert_writable_file,
    assert_writable_parent,
    validate_id,
)
from gws_mcp.tools import ToolError

FILE_ID = "1AbCdEfGhIjKlMnOp"

# マイドライブにある自分のファイル（書き込み可）
MY_FILE = {
    "id": FILE_ID,
    "name": "自分のシート",
    "mimeType": "application/vnd.google-apps.spreadsheet",
    "ownedByMe": True,
    "trashed": False,
}


def fake_drive(meta=None, error=None):
    """files().get().execute() が meta を返す（または error を送出する）モック。"""
    drive = MagicMock()
    execute = drive.files.return_value.get.return_value.execute
    if error is not None:
        execute.side_effect = error
    else:
        execute.return_value = meta
    return drive


def http_error(status: int) -> HttpError:
    return HttpError(httplib2.Response({"status": status}), b"{}")


class AssertWritableFileTest(unittest.TestCase):
    def test_マイドライブの自分のファイルは許可(self):
        drive = fake_drive(MY_FILE)
        self.assertEqual(assert_writable_file(drive, FILE_ID), MY_FILE)

    def test_共有ドライブも対象にして取得する(self):
        # supportsAllDrives が無いと共有ドライブのファイルが 404 になり、判定できない
        drive = fake_drive(MY_FILE)
        assert_writable_file(drive, FILE_ID)
        kwargs = drive.files.return_value.get.call_args.kwargs
        self.assertEqual(kwargs["fileId"], FILE_ID)
        self.assertIs(kwargs["supportsAllDrives"], True)
        for field in ("ownedByMe", "driveId", "trashed", "mimeType"):
            self.assertIn(field, kwargs["fields"])

    def test_共有ドライブのファイルは拒否(self):
        drive = fake_drive({**MY_FILE, "driveId": "0ABCDEF"})
        with self.assertRaisesRegex(GuardError, "共有ドライブ"):
            assert_writable_file(drive, FILE_ID)

    def test_共有ドライブのファイルは所有者判定より先に拒否(self):
        # 共有ドライブのファイルは ownedByMe が常に false だが、理由は共有ドライブとして示す
        drive = fake_drive({**MY_FILE, "driveId": "0ABCDEF", "ownedByMe": False})
        with self.assertRaisesRegex(GuardError, "共有ドライブ"):
            assert_writable_file(drive, FILE_ID)

    def test_driveIdが空文字でも拒否(self):
        drive = fake_drive({**MY_FILE, "driveId": ""})
        with self.assertRaises(GuardError):
            assert_writable_file(drive, FILE_ID)

    def test_他人が所有するファイルは拒否(self):
        drive = fake_drive({**MY_FILE, "ownedByMe": False})
        with self.assertRaisesRegex(GuardError, "所有者"):
            assert_writable_file(drive, FILE_ID)

    def test_ownedByMeが無ければ拒否(self):
        meta = {k: v for k, v in MY_FILE.items() if k != "ownedByMe"}
        with self.assertRaises(GuardError):
            assert_writable_file(fake_drive(meta), FILE_ID)

    def test_ゴミ箱のファイルは拒否(self):
        drive = fake_drive({**MY_FILE, "trashed": True})
        with self.assertRaisesRegex(GuardError, "ゴミ箱"):
            assert_writable_file(drive, FILE_ID)

    def test_trashedが無ければ拒否(self):
        meta = {k: v for k, v in MY_FILE.items() if k != "trashed"}
        with self.assertRaises(GuardError):
            assert_writable_file(fake_drive(meta), FILE_ID)

    def test_ショートカットは拒否(self):
        drive = fake_drive({**MY_FILE, "mimeType": SHORTCUT_MIME})
        with self.assertRaisesRegex(GuardError, "ショートカット"):
            assert_writable_file(drive, FILE_ID)

    def test_見つからないファイルはGuardError(self):
        drive = fake_drive(error=http_error(404))
        with self.assertRaisesRegex(GuardError, "見つからない"):
            assert_writable_file(drive, FILE_ID)

    def test_404以外のAPIエラーはそのまま送出(self):
        drive = fake_drive(error=http_error(500))
        with self.assertRaises(HttpError):
            assert_writable_file(drive, FILE_ID)

    def test_不正なIDはAPIを呼ばずに拒否(self):
        drive = fake_drive(MY_FILE)
        with self.assertRaises(GuardError):
            assert_writable_file(drive, "abc' or name contains '")
        drive.files.assert_not_called()

    def test_GuardErrorはツールエラーとして扱われる(self):
        self.assertTrue(issubclass(GuardError, ToolError))


class AssertWritableParentTest(unittest.TestCase):
    def test_マイドライブ直下は許可(self):
        drive = fake_drive()
        assert_writable_parent(drive, "root")
        drive.files.assert_not_called()

    def test_自分のフォルダは許可(self):
        assert_writable_parent(fake_drive({**MY_FILE, "mimeType": FOLDER_MIME}), FILE_ID)

    def test_フォルダでなければ拒否(self):
        with self.assertRaisesRegex(GuardError, "フォルダではありません"):
            assert_writable_parent(fake_drive(MY_FILE), FILE_ID)

    def test_共有ドライブのフォルダは拒否(self):
        drive = fake_drive({**MY_FILE, "mimeType": FOLDER_MIME, "driveId": "0ABCDEF"})
        with self.assertRaisesRegex(GuardError, "共有ドライブ"):
            assert_writable_parent(drive, FILE_ID)

    def test_他人のフォルダは拒否(self):
        drive = fake_drive({**MY_FILE, "mimeType": FOLDER_MIME, "ownedByMe": False})
        with self.assertRaises(GuardError):
            assert_writable_parent(drive, FILE_ID)


class ValidateIdTest(unittest.TestCase):
    def test_正しい形式(self):
        self.assertEqual(validate_id(FILE_ID), FILE_ID)

    def test_不正な形式(self):
        for bad in ["", "short", "a" * 201, "abc/def/ghij", "abcdefghij\n", 12345678901]:
            with self.subTest(bad=bad), self.assertRaises(GuardError):
                validate_id(bad)


class AssertOwnEventTest(unittest.TestCase):
    def test_自分が主催の予定は許可(self):
        assert_own_event({"id": "e1", "organizer": {"email": "me@example.com", "self": True}})

    def test_他人が主催の予定は拒否(self):
        with self.assertRaisesRegex(GuardError, "主催者"):
            assert_own_event({"id": "e1", "summary": "定例", "organizer": {"email": "other@example.com"}})

    def test_主催者情報が無ければ拒否(self):
        with self.assertRaises(GuardError):
            assert_own_event({"id": "e1"})


if __name__ == "__main__":
    unittest.main()
