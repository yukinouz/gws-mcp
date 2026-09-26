"""MCP サーバー本体（JSON-RPC 2.0 over stdio、プロトコル 2026-07-28）。

- 標準入力から改行区切りの JSON メッセージを 1 行ずつ読み、応答を標準出力に 1 行ずつ書く
- 標準出力は MCP 通信専用。ログや誤った print は標準エラー出力へ流す
- プロトコルはステートレス。接続時のハンドシェイクは無く、各リクエストの
  _meta に含まれるプロトコルバージョンをリクエストごとに検証する
- ツール実行の失敗（ガード拒否・API エラー等）はプロトコルエラーではなく
  isError: true のツール結果として返す
"""

import json
import sys
import traceback

from gws_mcp import __version__
from gws_mcp.auth import AuthError, Services
from gws_mcp.schema import SchemaError, validate
from gws_mcp.tools import ToolError, load_tools

# 実装しているプロトコルバージョン。互換性のない仕様変更があったときだけ更新する
PROTOCOL_VERSION = "2026-07-28"

SERVER_INFO = {"name": "gws-mcp", "version": __version__}

INSTRUCTIONS = (
    "Google Workspace MCP サーバー。"
    "書き込みはマイドライブのファイルに限られ、共有ドライブは読み取り専用。"
    "削除操作は提供しない。Gmail は読み取りのみ。"
)

# _meta の予約キー
META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

# server/discover・tools/list のキャッシュ有効期間。ツール一覧はプロセス内で変わらない
CACHE_TTL_MS = 3_600_000

# ツール結果テキストの上限（LLM のコンテキストを圧迫しないため）
MAX_RESULT_CHARS = 100_000
# 1 メッセージの上限文字数（異常に大きな入力を処理しないため）
MAX_LINE_CHARS = 10_000_000

# JSON-RPC エラーコード
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
UNSUPPORTED_PROTOCOL_VERSION = -32022


def log(msg: str) -> None:
    print(f"[gws-mcp] {msg}", file=sys.stderr, flush=True)


class RpcError(Exception):
    def __init__(self, code: int, message: str, data=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _error(id_, code: int, message: str, data=None) -> dict:
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": id_, "error": err}


def _result(id_, result: dict) -> dict:
    # すべての結果に resultType（必須）と serverInfo を付ける
    body = {"resultType": "complete", **result, "_meta": {META_SERVER_INFO: SERVER_INFO}}
    return {"jsonrpc": "2.0", "id": id_, "result": body}


def _to_text(value) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, indent=2)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + f"\n…（{MAX_RESULT_CHARS} 文字で切り詰めました）"
    return text


def _format_http_error(e) -> str:
    """googleapiclient の HttpError を LLM 向けの短いメッセージにする。"""
    status = getattr(getattr(e, "resp", None), "status", "?")
    reason = e._get_reason() if hasattr(e, "_get_reason") else str(e)
    return f"Google API エラー（HTTP {status}）: {reason}"


def _check_request_meta(params: dict) -> None:
    """各リクエストに必須の _meta を検証する。"""
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        raise RpcError(INVALID_PARAMS, "params._meta がありません")
    version = meta.get(META_PROTOCOL_VERSION)
    if not isinstance(version, str):
        raise RpcError(INVALID_PARAMS, f"_meta に {META_PROTOCOL_VERSION} がありません")
    if not isinstance(meta.get(META_CLIENT_CAPABILITIES), dict):
        raise RpcError(INVALID_PARAMS, f"_meta に {META_CLIENT_CAPABILITIES} がありません")
    if version != PROTOCOL_VERSION:
        raise RpcError(
            UNSUPPORTED_PROTOCOL_VERSION,
            "Unsupported protocol version",
            {"supported": [PROTOCOL_VERSION], "requested": version},
        )


class MCPServer:
    def __init__(self, tools: dict, services=None):
        self.tools = tools
        self.services = services
        self._methods = {
            "server/discover": self._discover,
            "tools/list": self._list_tools,
            "tools/call": self._call_tool,
        }

    # ---- メッセージ処理 ----

    def handle_raw(self, line: str) -> dict | None:
        """1 行分の生テキストを処理し、応答（通知なら None）を返す。"""
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return _error(None, PARSE_ERROR, "JSON を解析できません")
        return self.handle(msg)

    def handle(self, msg) -> dict | None:
        if isinstance(msg, list):
            # MCP ではバッチ（複数メッセージを配列でまとめて送る形式）を使わない
            return _error(None, INVALID_REQUEST, "バッチリクエストには対応していません")
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return _error(None, INVALID_REQUEST, "JSON-RPC 2.0 のメッセージではありません")

        if "id" not in msg:
            # 通知（notifications/cancelled 等）。応答は返さない
            # 各リクエストは同期的に処理し終えるので、取り消す対象も無い
            return None

        id_ = msg["id"]
        if id_ is None or isinstance(id_, bool) or not isinstance(id_, (str, int)):
            return _error(None, INVALID_REQUEST, "id は文字列または整数である必要があります")

        method = msg.get("method")
        if not isinstance(method, str):
            # クライアントからの応答（result/error）は想定外なので無視する
            if "result" in msg or "error" in msg:
                return None
            return _error(id_, INVALID_REQUEST, "method がありません")

        try:
            handler = self._methods.get(method)
            if handler is None:
                # 旧方式（initialize）で接続してきたクライアントにも対応版が分かるようにする
                raise RpcError(
                    METHOD_NOT_FOUND,
                    f"未対応のメソッドです: {method}（対応プロトコルバージョン: {PROTOCOL_VERSION}）",
                )
            params = msg.get("params")
            if not isinstance(params, dict):
                raise RpcError(INVALID_PARAMS, "params はオブジェクトである必要があります")
            _check_request_meta(params)
            return _result(id_, handler(params))
        except RpcError as e:
            return _error(id_, e.code, e.message, e.data)
        except Exception:
            log("内部エラー:\n" + traceback.format_exc())
            return _error(id_, INTERNAL_ERROR, "サーバー内部エラー")

    # ---- 各メソッド ----

    def _discover(self, params: dict) -> dict:
        client = params["_meta"].get(META_CLIENT_INFO) or {}
        log(f"server/discover: client={client.get('name')} {client.get('version')}")
        return {
            "supportedVersions": [PROTOCOL_VERSION],
            "capabilities": {"tools": {"listChanged": False}},
            "instructions": INSTRUCTIONS,
            "ttlMs": CACHE_TTL_MS,
            "cacheScope": "private",
        }

    def _list_tools(self, params: dict) -> dict:
        # ツール数が少ないのでページ分割せず一度に返す（cursor は無視）
        tools = [t.definition() for t in sorted(self.tools.values(), key=lambda t: t.name)]
        return {"tools": tools, "ttlMs": CACHE_TTL_MS, "cacheScope": "private"}

    def _call_tool(self, params: dict) -> dict:
        name = params.get("name")
        args = params.get("arguments", {})
        if not isinstance(name, str) or name not in self.tools:
            raise RpcError(INVALID_PARAMS, f"未知のツールです: {name}")
        if not isinstance(args, dict):
            raise RpcError(INVALID_PARAMS, "arguments はオブジェクトである必要があります")
        tool = self.tools[name]
        try:
            validate(tool.input_schema, args, "arguments")
        except SchemaError as e:
            # LLM が引数を直して再試行できるよう、ツール実行エラーとして返す
            return self._tool_error(f"引数が不正です: {e}")

        log(f"tools/call: {name}")
        try:
            result = tool.handler(self.services, args)
            return {"content": [{"type": "text", "text": _to_text(result)}], "isError": False}
        except (ToolError, AuthError) as e:
            return self._tool_error(str(e))
        except Exception as e:
            if _is_http_error(e):
                log(f"{name}: {_format_http_error(e)}")
                return self._tool_error(_format_http_error(e))
            log(f"{name}: 予期しないエラー:\n" + traceback.format_exc())
            return self._tool_error("ツールの実行中に予期しないエラーが発生しました（詳細はサーバーログ）")

    @staticmethod
    def _tool_error(message: str) -> dict:
        return {"content": [{"type": "text", "text": message}], "isError": True}


def _is_http_error(e: Exception) -> bool:
    try:
        from googleapiclient.errors import HttpError
    except ImportError:
        return False
    return isinstance(e, HttpError)


def serve(server: MCPServer, stdin, stdout) -> None:
    """stdin が閉じられるまでメッセージを処理する。"""
    for raw in stdin:
        if len(raw) > MAX_LINE_CHARS:
            response = _error(None, INVALID_REQUEST, "メッセージが大きすぎます")
        else:
            line = raw.strip()
            if not line:
                continue
            response = server.handle_raw(line)
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()


def main() -> None:
    # 標準出力は通信専用にする。以降の print（ライブラリ由来を含む）は標準エラーへ流れる
    protocol_out = sys.stdout
    sys.stdout = sys.stderr
    protocol_out.reconfigure(encoding="utf-8", newline="\n")
    sys.stdin.reconfigure(encoding="utf-8")

    tools = load_tools()
    log(f"起動しました（ツール {len(tools)} 個、プロトコル {PROTOCOL_VERSION}）")
    try:
        serve(MCPServer(tools, Services()), sys.stdin, protocol_out)
    except KeyboardInterrupt:
        pass
    log("終了しました")


if __name__ == "__main__":
    main()
