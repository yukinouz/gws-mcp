#!/bin/bash
# gws-mcp の OAuth 資格情報（credentials.json / token.json）へのアクセスを Bash から拒否する
# PreToolUse フック：標準入力のツール呼び出し JSON から command を取り出して判定する
cmd=$(jq -r '.tool_input.command // ""')
if printf '%s' "$cmd" | grep -Eq '(^|[^A-Za-z0-9_.-])(credentials|token)\.json|\.config/gws-mcp'; then
  echo "拒否: gws-mcp の資格情報（credentials.json / token.json / ~/.config/gws-mcp）にはアクセスできません。必要な操作はユーザーに依頼してください。" >&2
  exit 2
fi
exit 0
