"""ツールの登録レジストリ。

各サービスのモジュール（drive.py など）は register() でツールを登録する。
ここに登録されたツールだけが MCP クライアントに公開される。
"""

from dataclasses import dataclass
from typing import Callable

from gws_mcp.schema import check_schema


class ToolError(Exception):
    """ツール実行の失敗（isError: true として LLM に返す想定内のエラー）。"""


# ツールが書き込み前に行う判定の種類（判定の実装は guard.py）

# 読み取り専用。
GUARD_READ_ONLY = "read_only"

# 既存ファイルへの書き込み。対象が次の4条件をすべて満たすときだけ書き込む
# - 共有ドライブにない
# - 自分が所有者
# - ゴミ箱にない
# - ショートカットではない
GUARD_MYDRIVE = "mydrive"

# 作成先フォルダを指定できる新規作成。作成先フォルダを GUARD_MYDRIVE と同じ条件で判定
GUARD_PARENT = "parent"

# 作成先を指定できない新規作成（常にマイドライブ直下に作られる）
GUARD_CREATED = "created"

# 作成先は自分のメインカレンダーに固定
# 更新は自分が主催者の予定だけ
GUARD_OWN_EVENT = "own_event"

_GUARDS = {GUARD_READ_ONLY, GUARD_MYDRIVE, GUARD_PARENT, GUARD_CREATED, GUARD_OWN_EVENT}


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict
    # handler(services, args) -> str | dict | list
    handler: Callable
    read_only: bool
    # 書き込みツールは GUARD_READ_ONLY 以外を必ず宣言する（register で検査）
    guard: str = GUARD_READ_ONLY
    idempotent: bool = False
    title: str | None = None

    def definition(self) -> dict:
        """tools/list で返すツール定義。"""
        d = {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": {
                "readOnlyHint": self.read_only,
                # 削除系ツールは公開しない方針のため、常に false
                "destructiveHint": False,
                "idempotentHint": self.idempotent,
                "openWorldHint": True,
            },
        }
        if self.title:
            d["title"] = self.title
            d["annotations"]["title"] = self.title
        return d


def register(registry: dict, tool: Tool) -> None:
    """スキーマを検査してからレジストリに登録する。"""
    if tool.name in registry:
        raise ValueError(f"ツール名が重複しています: {tool.name}")
    if tool.input_schema.get("type") != "object":
        raise ValueError(f"{tool.name}: inputSchema は object 型である必要があります")
    if tool.guard not in _GUARDS:
        raise ValueError(f"{tool.name}: guard が不正です: {tool.guard!r}")
    if tool.read_only != (tool.guard == GUARD_READ_ONLY):
        raise ValueError(f"{tool.name}: 読み取り専用のツールは guard=read_only、書き込みツールはそれ以外の guard が必須です")
    check_schema(tool.input_schema, tool.name)
    registry[tool.name] = tool


def load_tools() -> dict:
    """公開する全ツールを読み込んで返す。"""
    from gws_mcp.tools import calendar, docs, drive, gmail, sheets, slides

    registry: dict = {}
    for module in (drive, gmail, calendar, sheets, docs, slides):
        for tool in module.TOOLS:
            register(registry, tool)
    return registry
