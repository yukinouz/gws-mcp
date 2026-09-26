"""ツール入力の JSON Schema バリデータ（ツール定義で使うキーワードのみ対応）。

対応キーワード: type / properties / required / additionalProperties(false のみ) /
enum / minLength / maxLength / pattern / minimum / maximum / items / minItems / maxItems
未対応のキーワードが含まれるスキーマは、定義ミスとして登録時に弾く（check_schema）。
"""

import re

_SUPPORTED_KEYWORDS = {
    "type",
    "description",
    "properties",
    "required",
    "additionalProperties",
    "enum",
    "default",
    "minLength",
    "maxLength",
    "pattern",
    "minimum",
    "maximum",
    "items",
    "minItems",
    "maxItems",
}

_SCALAR_TYPES = {"string", "integer", "number", "boolean"}
_TYPES = _SCALAR_TYPES | {"object", "array"}


class SchemaError(ValueError):
    """入力がスキーマに合わないときに送出する。"""


def check_schema(schema: dict, path: str = "$") -> None:
    """スキーマ定義自体を検査する（ツール登録時に呼ぶ）。

    - 未対応キーワードを使っていないこと
    - object 型は必ず additionalProperties: false であること（想定外の引数を受け付けない）
    """
    unknown = set(schema) - _SUPPORTED_KEYWORDS
    if unknown:
        raise ValueError(f"{path}: 未対応のスキーマキーワード {sorted(unknown)}")
    t = schema.get("type")
    if isinstance(t, list):
        # 複数型はスカラー値（スプレッドシートのセル値など）にだけ使う
        if not t or not set(t) <= _SCALAR_TYPES:
            raise ValueError(f"{path}: 複数型の type はスカラー型のみ指定できます: {t!r}")
        return
    if t not in _TYPES:
        raise ValueError(f"{path}: type が不正です: {t!r}")
    if t == "object":
        if schema.get("additionalProperties") is not False:
            raise ValueError(f"{path}: object には additionalProperties: false が必須です")
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in props:
                raise ValueError(f"{path}: required の {req!r} が properties にありません")
        for key, sub in props.items():
            check_schema(sub, f"{path}.{key}")
    if t == "array":
        if "items" not in schema:
            raise ValueError(f"{path}: array には items が必須です")
        check_schema(schema["items"], f"{path}[]")


def _type_ok(t: str, value) -> bool:
    # bool は int のサブクラスなので、数値型の判定から明示的に除外する
    if t == "object":
        return isinstance(value, dict)
    if t == "string":
        return isinstance(value, str)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "boolean":
        return isinstance(value, bool)
    if t == "array":
        return isinstance(value, list)
    return False


def validate(schema: dict, value, path: str = "$") -> None:
    """value が schema に合うか検証し、合わなければ SchemaError を送出する。"""
    types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
    t = next((x for x in types if _type_ok(x, value)), None)
    if t is None:
        raise SchemaError(f"{path}: {' / '.join(types)} 型である必要があります")

    if "enum" in schema and value not in schema["enum"]:
        raise SchemaError(f"{path}: 次のいずれかである必要があります: {schema['enum']}")

    if t == "string":
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise SchemaError(f"{path}: {schema['minLength']} 文字以上である必要があります")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise SchemaError(f"{path}: {schema['maxLength']} 文字以下である必要があります")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            raise SchemaError(f"{path}: 形式が不正です")

    elif t in ("integer", "number"):
        if "minimum" in schema and value < schema["minimum"]:
            raise SchemaError(f"{path}: {schema['minimum']} 以上である必要があります")
        if "maximum" in schema and value > schema["maximum"]:
            raise SchemaError(f"{path}: {schema['maximum']} 以下である必要があります")

    elif t == "array":
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise SchemaError(f"{path}: 要素は {schema['minItems']} 個以上必要です")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise SchemaError(f"{path}: 要素は {schema['maxItems']} 個以下である必要があります")
        for i, item in enumerate(value):
            validate(schema["items"], item, f"{path}[{i}]")

    elif t == "object":
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in value:
                raise SchemaError(f"{path}: 必須項目 {req!r} がありません")
        for key, v in value.items():
            if key not in props:
                raise SchemaError(f"{path}: 想定外の項目 {key!r} があります")
            validate(props[key], v, f"{path}.{key}")
