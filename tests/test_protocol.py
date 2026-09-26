"""server.py（MCP / JSON-RPC 処理）のテスト。Google API は呼ばない。"""

import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from gws_mcp.auth import AuthError
from gws_mcp.server import PROTOCOL_VERSION, MCPServer, serve
from gws_mcp.tools import Tool, ToolError, register

ROOT = Path(__file__).resolve().parent.parent

ECHO_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string", "maxLength": 20}},
    "required": ["text"],
    "additionalProperties": False,
}
EMPTY_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}


def _raise(exc):
    def handler(services, args):
        raise exc

    return handler


def make_server() -> MCPServer:
    tools = {}
    register(tools, Tool("echo", "入力をそのまま返す", ECHO_SCHEMA, lambda s, a: {"echo": a["text"]}, read_only=True))
    register(tools, Tool("guarded", "ガード拒否", EMPTY_SCHEMA, _raise(ToolError("拒否しました")), read_only=False))
    register(tools, Tool("noauth", "未認証", EMPTY_SCHEMA, _raise(AuthError("トークンがありません")), read_only=True))
    register(tools, Tool("broken", "内部エラー", EMPTY_SCHEMA, _raise(RuntimeError("秘密の内部情報")), read_only=True))
    register(tools, Tool("huge", "巨大な結果", EMPTY_SCHEMA, lambda s, a: "あ" * 200_000, read_only=True))
    return MCPServer(tools)


def meta(version=PROTOCOL_VERSION) -> dict:
    return {
        "io.modelcontextprotocol/protocolVersion": version,
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "0"},
    }


def req(id_, method, params=None, version=PROTOCOL_VERSION):
    """_meta 付きのリクエストを作る。"""
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": {**(params or {}), "_meta": meta(version)}}


class HandleTest(unittest.TestCase):
    def setUp(self):
        self.server = make_server()

    def call(self, name, arguments=None):
        return self.server.handle(req(1, "tools/call", {"name": name, "arguments": arguments or {}}))

    # ---- server/discover ----

    def test_discover(self):
        r = self.server.handle(req("d", "server/discover"))["result"]
        self.assertEqual(r["resultType"], "complete")
        self.assertEqual(r["supportedVersions"], [PROTOCOL_VERSION])
        self.assertEqual(r["capabilities"], {"tools": {"listChanged": False}})
        self.assertEqual(r["_meta"]["io.modelcontextprotocol/serverInfo"]["name"], "gws-mcp")
        self.assertIn("ttlMs", r)
        self.assertIn(r["cacheScope"], ("public", "private"))

    # ---- _meta の検証 ----

    def test_未対応バージョンは対応版を添えて拒否する(self):
        e = self.server.handle(req(1, "tools/list", version="2025-11-25"))["error"]
        self.assertEqual(e["code"], -32022)
        self.assertEqual(e["data"], {"supported": [PROTOCOL_VERSION], "requested": "2025-11-25"})

    def test_metaが無いリクエストはInvalidParams(self):
        r = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        self.assertEqual(r["error"]["code"], -32602)

    def test_paramsが無いリクエストはInvalidParams(self):
        r = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(r["error"]["code"], -32602)

    def test_clientCapabilitiesが無いリクエストはInvalidParams(self):
        m = req(1, "tools/list")
        del m["params"]["_meta"]["io.modelcontextprotocol/clientCapabilities"]
        self.assertEqual(self.server.handle(m)["error"]["code"], -32602)

    def test_旧方式のinitializeは対応版を示して拒否する(self):
        r = self.server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}}
        )
        self.assertEqual(r["error"]["code"], -32601)
        self.assertIn(PROTOCOL_VERSION, r["error"]["message"])

    # ---- tools/list ----

    def test_tools_list(self):
        r = self.server.handle(req(1, "tools/list"))["result"]
        self.assertEqual([t["name"] for t in r["tools"]], ["broken", "echo", "guarded", "huge", "noauth"])
        self.assertEqual(r["resultType"], "complete")
        self.assertIn("ttlMs", r)
        echo = next(t for t in r["tools"] if t["name"] == "echo")
        self.assertEqual(echo["inputSchema"], ECHO_SCHEMA)
        self.assertTrue(echo["annotations"]["readOnlyHint"])
        self.assertFalse(echo["annotations"]["destructiveHint"])

    # ---- tools/call ----

    def test_正常なツール呼び出し(self):
        r = self.call("echo", {"text": "こんにちは"})["result"]
        self.assertEqual(r["resultType"], "complete")
        self.assertFalse(r["isError"])
        self.assertEqual(json.loads(r["content"][0]["text"]), {"echo": "こんにちは"})

    def test_未知のツールはプロトコルエラー(self):
        self.assertEqual(self.call("delete_everything")["error"]["code"], -32602)

    def test_引数の型違いはツール実行エラー(self):
        r = self.call("echo", {"text": 1})["result"]
        self.assertTrue(r["isError"])
        self.assertIn("引数が不正です", r["content"][0]["text"])

    def test_想定外の引数はツール実行エラーで実行しない(self):
        r = self.call("echo", {"text": "a", "supportsAllDrives": True})["result"]
        self.assertTrue(r["isError"])

    def test_argumentsがオブジェクトでない(self):
        r = self.server.handle(req(1, "tools/call", {"name": "echo", "arguments": ["a"]}))
        self.assertEqual(r["error"]["code"], -32602)

    def test_ToolErrorはisErrorで返す(self):
        r = self.call("guarded")["result"]
        self.assertTrue(r["isError"])
        self.assertEqual(r["content"][0]["text"], "拒否しました")

    def test_AuthErrorはisErrorで返す(self):
        r = self.call("noauth")["result"]
        self.assertTrue(r["isError"])
        self.assertIn("トークン", r["content"][0]["text"])

    def test_予期しない例外の詳細はLLMに返さない(self):
        with _silence_stderr():
            r = self.call("broken")["result"]
        self.assertTrue(r["isError"])
        self.assertNotIn("秘密の内部情報", r["content"][0]["text"])

    def test_巨大な結果は切り詰める(self):
        text = self.call("huge")["result"]["content"][0]["text"]
        self.assertLess(len(text), 110_000)
        self.assertIn("切り詰めました", text)

    # ---- JSON-RPC の異常系 ----

    def test_未知のメソッド(self):
        self.assertEqual(self.server.handle(req(1, "resources/list"))["error"]["code"], -32601)

    def test_pingは新方式に存在しない(self):
        self.assertEqual(self.server.handle(req(1, "ping"))["error"]["code"], -32601)

    def test_通知には応答しない(self):
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "notifications/cancelled"}))
        # 通知でツールを呼んでも実行しない
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "echo"}}))

    def test_クライアントからの応答は無視する(self):
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "id": 1, "result": {}}))

    def test_不正なJSON(self):
        self.assertEqual(self.server.handle_raw("{not json")["error"]["code"], -32700)

    def test_バッチは受け付けない(self):
        self.assertEqual(self.server.handle([req(1, "tools/list")])["error"]["code"], -32600)

    def test_jsonrpcバージョン違い(self):
        m = req(1, "tools/list")
        m["jsonrpc"] = "1.0"
        self.assertEqual(self.server.handle(m)["error"]["code"], -32600)

    def test_idがnull(self):
        self.assertEqual(self.server.handle(req(None, "tools/list"))["error"]["code"], -32600)

    def test_idがbool(self):
        self.assertEqual(self.server.handle(req(True, "tools/list"))["error"]["code"], -32600)

    def test_paramsが配列(self):
        r = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": []})
        self.assertEqual(r["error"]["code"], -32602)


class ServeTest(unittest.TestCase):
    def test_1行1メッセージで応答し空行と通知は読み飛ばす(self):
        lines = [
            json.dumps(req(1, "tools/list")),
            "",
            json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 9}}),
            json.dumps(req(2, "tools/call", {"name": "echo", "arguments": {"text": "日本語"}})),
        ]
        out = io.StringIO()
        serve(make_server(), io.StringIO("\n".join(lines) + "\n"), out)
        responses = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual([r["id"] for r in responses], [1, 2])
        # 日本語はエスケープせずに出力する
        self.assertIn("日本語", out.getvalue())


class SubprocessTest(unittest.TestCase):
    """実際に `python -m gws_mcp` を起動し、stdio 経由でやり取りする。"""

    def test_stdio経由の通信(self):
        messages = [req(1, "server/discover"), req(2, "tools/list")]
        stdin = "\n".join(json.dumps(m) for m in messages) + "\n{broken\n"
        # 実トークンを読まないよう、存在しない設定ディレクトリを指定する
        env = dict(os.environ, GWS_MCP_CONFIG_DIR=str(ROOT / "tests" / "_nonexistent_config"))
        p = subprocess.run(
            [sys.executable, "-m", "gws_mcp"],
            input=stdin, capture_output=True, text=True, cwd=ROOT, env=env, timeout=30,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        responses = [json.loads(line) for line in p.stdout.splitlines()]
        # 標準出力には JSON-RPC 応答だけが出ること（ログは標準エラー）
        self.assertEqual([r.get("id") for r in responses], [1, 2, None])
        self.assertEqual(responses[0]["result"]["supportedVersions"], [PROTOCOL_VERSION])
        self.assertIn("tools", responses[1]["result"])
        self.assertEqual(responses[2]["error"]["code"], -32700)
        self.assertIn("起動しました", p.stderr)


class _silence_stderr:
    """テスト中に意図的に出す内部エラーログを抑止する。"""

    def __enter__(self):
        self._orig = sys.stderr
        sys.stderr = io.StringIO()

    def __exit__(self, *exc):
        sys.stderr = self._orig


if __name__ == "__main__":
    unittest.main()
