---
name: commit
description: "Conventional Commits 規約に従ってコミットを作成する"
---

## 実行条件

ユーザーから明示的にコミット作成を依頼された場合のみ、コミットを実行する。

# Conventional Commits

## コミットメッセージ形式

```
type(scope): description

[optional body]

[optional footer]
```

## Type 一覧

| Type     | 用途                                                 |
| -------- | ---------------------------------------------------- |
| feat     | 新機能追加                                           |
| fix      | バグ修正                                             |
| docs     | ドキュメントのみ変更                                 |
| style    | コードの意味に影響しない変更（空白、フォーマット等） |
| refactor | バグ修正でも機能追加でもないコード変更               |
| test     | テスト追加・修正                                     |
| chore    | ビルドプロセスや補助ツールの変更                     |
| ci       | CI設定の変更                                         |
| perf     | パフォーマンス改善                                   |
| build    | ビルドシステムや外部依存関係の変更                   |

## コミットルール

1. **日本語で書く**
2. **Staged された変更のみをコミット対象とする**
3. **Subject line は72文字以内**
4. **Body は本文のルールを参照**
5. **Footer は任意**
6. **Breaking change がある場合:** type の後に `!` を付ける（例: `feat(reserve)!: change API response format`）

## コミット本文(Body)のルール

- 変更が極めて単純な場合のみ本文を省略してよい
- 主な変更内容を簡潔に説明する
