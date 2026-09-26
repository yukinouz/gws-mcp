"""schema.py（入力バリデータ）のテスト。"""

import unittest

from gws_mcp.schema import SchemaError, check_schema, validate

SCHEMA = {
    "type": "object",
    "properties": {
        "file_id": {"type": "string", "pattern": r"[A-Za-z0-9_-]{10,200}"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        "mode": {"type": "string", "enum": ["RAW", "USER_ENTERED"]},
        "text": {"type": "string", "maxLength": 5},
        "rows": {
            "type": "array",
            "maxItems": 2,
            "items": {"type": "array", "items": {"type": "string"}},
        },
        "flag": {"type": "boolean"},
    },
    "required": ["file_id"],
    "additionalProperties": False,
}

OK_ID = "abcdefghij12345"


class ValidateTest(unittest.TestCase):
    def test_正常な入力は通る(self):
        validate(SCHEMA, {"file_id": OK_ID, "limit": 10, "mode": "RAW", "rows": [["a"]], "flag": True})

    def test_必須項目の欠落(self):
        with self.assertRaises(SchemaError):
            validate(SCHEMA, {})

    def test_想定外の項目(self):
        with self.assertRaises(SchemaError):
            validate(SCHEMA, {"file_id": OK_ID, "extra": 1})

    def test_型違い(self):
        with self.assertRaises(SchemaError):
            validate(SCHEMA, {"file_id": 123})

    def test_boolは整数として扱わない(self):
        with self.assertRaises(SchemaError):
            validate(SCHEMA, {"file_id": OK_ID, "limit": True})

    def test_範囲外(self):
        with self.assertRaises(SchemaError):
            validate(SCHEMA, {"file_id": OK_ID, "limit": 51})

    def test_enum外(self):
        with self.assertRaises(SchemaError):
            validate(SCHEMA, {"file_id": OK_ID, "mode": "FORMULA"})

    def test_文字数超過(self):
        with self.assertRaises(SchemaError):
            validate(SCHEMA, {"file_id": OK_ID, "text": "123456"})

    def test_pattern不一致は全体一致で判定する(self):
        # 部分一致で通ってしまわないこと（クエリ注入対策）
        with self.assertRaises(SchemaError):
            validate(SCHEMA, {"file_id": OK_ID + "' or 1=1"})

    def test_配列の要素数超過(self):
        with self.assertRaises(SchemaError):
            validate(SCHEMA, {"file_id": OK_ID, "rows": [[], [], []]})

    def test_入れ子の配列要素の型違い(self):
        with self.assertRaises(SchemaError):
            validate(SCHEMA, {"file_id": OK_ID, "rows": [["a", 1]]})


class CheckSchemaTest(unittest.TestCase):
    def test_正しいスキーマ(self):
        check_schema(SCHEMA)

    def test_additionalPropertiesの指定漏れを弾く(self):
        with self.assertRaises(ValueError):
            check_schema({"type": "object", "properties": {}})

    def test_未対応キーワードを弾く(self):
        with self.assertRaises(ValueError):
            check_schema({"type": "string", "format": "email"})

    def test_requiredがpropertiesに無い(self):
        with self.assertRaises(ValueError):
            check_schema({"type": "object", "properties": {}, "required": ["x"], "additionalProperties": False})


if __name__ == "__main__":
    unittest.main()
