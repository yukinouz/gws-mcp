"""ツールの登録レジストリ。

各サービスのモジュール（drive.py など）は register() でツールを登録する。
ここに登録されたツールだけが MCP クライアントに公開される。
"""

from dataclasses import dataclass
from typing import Callable

from gws_mcp.schema import check_schema


class ToolError(Exception):
    """ツール実行の失敗（isError: true として LLM に返す想定内のエラー）。"""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict
    # handler(services, args) -> str | dict | list
    handler: Callable
    read_only: bool
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
    check_schema(tool.input_schema, tool.name)
    registry[tool.name] = tool


def load_tools() -> dict:
    """公開する全ツールを読み込んで返す。"""
    registry: dict = {}
    # サービスごとのモジュールは各ステップで追加する
    return registry
