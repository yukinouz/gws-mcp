"""ドキュメント ツール。

- 読み取り: 本文をテキストで取得できる。位置と段落の種類付きでも取得できる
- 書き込み: 新規作成と、自分が所有するマイドライブのファイルへの追記・一括置換、
  docs_batch_update による見出し・箇条書き・表・書式・文書内の削除ができる
- できないこと: ファイルの削除、共有ドライブや他人のファイルへの書き込み

docs_batch_update は許可リストの種類だけを受け付ける。プロンプトインジェクションで悪用されても
ドキュメントの外に影響が出ないよう、Google が外部 URL を取りに行く操作と、第三者に通知が届く操作は含めない。
"""

from gws_mcp.tools import GUARD_CREATED, GUARD_MYDRIVE, Tool, ToolError
from gws_mcp.tools._common import (
    BATCH_REJECTED,
    created,
    external_ref_problems,
    file_id,
    obj,
    parse_batch_requests,
    writable,
)

_DOCUMENT_ID = file_id("ドキュメントの ID")
_TEXT = {"type": "string", "minLength": 1, "maxLength": 200_000}

# docs_batch_update で使えるリクエスト。ドキュメントの中で完結する編集だけを含める。
# Google が外部 URL を取りに行くもの（insertInlineImage・replaceImage・insertRichLink）、第三者に通知が届くものや
# 他人の提案を確定させるもの（insertComment などのコメント・insertPerson・acceptSuggestion などの提案）、
# タブごとの削除（deleteTab）は含めない
_BATCH_ALLOWED = frozenset(
    {
        # 文字と段落
        "insertText",
        "replaceAllText",
        "deleteContentRange",
        "updateTextStyle",
        "updateParagraphStyle",
        "createParagraphBullets",
        "deleteParagraphBullets",
        "insertPageBreak",
        "insertSectionBreak",
        "insertDate",
        # 表
        "insertTable",
        "insertTableRow",
        "insertTableColumn",
        "deleteTableRow",
        "deleteTableColumn",
        "updateTableCellStyle",
        "updateTableRowStyle",
        "updateTableColumnProperties",
        "mergeTableCells",
        "unmergeTableCells",
        "pinTableHeaderRows",
        # 文書の構成と見た目
        "updateDocumentStyle",
        "updateNamedStyle",
        "updateSectionStyle",
        "createHeader",
        "createFooter",
        "deleteHeader",
        "deleteFooter",
        "deletePositionedObject",
        "createFootnote",
        "addDocumentTab",
        "updateDocumentTabProperties",
        "createNamedRange",
        "deleteNamedRange",
        "replaceNamedRangeContent",
    }
)
_MAX_BATCH_REQUESTS = 200
_MAX_BATCH_TEXT = 200_000
# Docs で Google が取りに行く URL は uri（画像）だけ。リンクの url は共通の検査で http(s) に限る
_BLOCKED_KEYS = {"uri": "外部 URL の取得（画像など）は使えません"}


def _elements_text(elements: list) -> str:
    """本文の構造要素（段落・表・目次）からテキストを取り出す。"""
    out = []
    for el in elements or []:
        if "paragraph" in el:
            for pe in el["paragraph"].get("elements", []):
                out.append((pe.get("textRun") or {}).get("content", ""))
        elif "table" in el:
            for row in el["table"].get("tableRows", []):
                cells = [_elements_text(c.get("content", [])).strip() for c in row.get("tableCells", [])]
                out.append(" | ".join(cells) + "\n")
        elif "tableOfContents" in el:
            out.append(_elements_text(el["tableOfContents"].get("content", [])))
    return "".join(out)


# 段落の中の文字以外の要素。どれも位置を 1 以上使うため、目印を出して段落の中の位置がずれないようにする
_ELEMENT_LABELS = {
    "inlineObjectElement": "画像",
    "footnoteReference": "脚注",
    "horizontalRule": "横線",
    "pageBreak": "改ページ",
    "columnBreak": "段区切り",
    "autoText": "自動テキスト",
    "person": "人物",
    "richLink": "リッチリンク",
    "dateElement": "日付",
    "equation": "数式",
}


def _span(el: dict) -> str:
    # API は 0 の startIndex を省略して返す
    return f"[{el.get('startIndex', 0)}-{el.get('endIndex', 0)})"


def _element_text(pe: dict) -> str:
    if "textRun" in pe:
        return pe["textRun"].get("content", "").rstrip("\n")
    kind = next((k for k in pe if k not in ("startIndex", "endIndex")), "不明")
    return f"[{_ELEMENT_LABELS.get(kind, kind)}]"


def _paragraph_structure(el: dict, indent: str) -> list[str]:
    """段落を 1 行にする。書式や文字以外の要素が混ざる段落は、要素ごとの位置も続けて出す。

    段落の一部（単語だけ太字など）を指定するとき、要素の境界をそのまま範囲に使えるようにするため。
    """
    para = el["paragraph"]
    style = (para.get("paragraphStyle") or {}).get("namedStyleType", "NORMAL_TEXT")
    if "bullet" in para:
        style += f" 箇条書き(階層{para['bullet'].get('nestingLevel', 0)})"
    elements = para.get("elements", [])
    text = "".join(_element_text(pe) for pe in elements) or "（空行）"
    lines = [f"{indent}{_span(el)} {style} {text}"]
    if len(elements) > 1:
        # 改行だけの要素は空白で終わり、何もない位置と見分けにくいため目印を出す
        lines += [f"{indent}  {_span(pe)} {_element_text(pe) or '（改行）'}" for pe in elements]
    return lines


def _elements_structure(elements: list, indent: str = "") -> list[str]:
    """本文の構造要素を、位置（startIndex〜endIndex）と段落の種類付きの行にする。

    書式のリクエストは位置で範囲を指定するため。位置は API の値（UTF-16 単位）をそのまま返し、
    LLM が文字数を数えてずれるのを防ぐ。
    """
    lines = []
    for el in elements or []:
        if "paragraph" in el:
            lines += _paragraph_structure(el, indent)
        elif "table" in el:
            table = el["table"]
            lines.append(f"{indent}{_span(el)} 表 {table.get('rows', 0)}行×{table.get('columns', 0)}列")
            for r, row in enumerate(table.get("tableRows", [])):
                for c, cell in enumerate(row.get("tableCells", [])):
                    lines.append(f"{indent}  セル({r},{c}) {_span(cell)}")
                    lines.extend(_elements_structure(cell.get("content", []), indent + "    "))
        elif "tableOfContents" in el:
            lines.append(f"{indent}{_span(el)} 目次")
    return lines


def _tab_body(tab: dict, structure: bool) -> str:
    body = ((tab.get("documentTab") or {}).get("body") or {}).get("content", [])
    return "\n".join(_elements_structure(body)) + "\n" if structure else _elements_text(body)


def _tabs_text(tabs: list, structure: bool, depth: int = 0) -> list[str]:
    parts = []
    for tab in tabs or []:
        props = tab.get("tabProperties") or {}
        # 構造表示では、位置の指定（Location.tabId）に使うタブ ID も示す
        tab_id = f"（tabId: {props.get('tabId')}）" if structure else ""
        parts.append(f"{'#' * (depth + 2)} タブ: {props.get('title', '')}{tab_id}\n\n{_tab_body(tab, structure)}")
        parts.extend(_tabs_text(tab.get("childTabs", []), structure, depth + 1))
    return parts


def docs_read(services, args):
    doc = (
        services.get("docs")
        .documents()
        .get(documentId=args["document_id"], includeTabsContent=True)
        .execute()
    )
    tabs = doc.get("tabs", [])
    structure = args.get("format") == "structure"
    # タブが 1 つだけならタブ見出しを付けずに本文だけ返す
    if len(tabs) == 1 and not tabs[0].get("childTabs"):
        text = _tab_body(tabs[0], structure)
    else:
        text = "\n".join(_tabs_text(tabs, structure))
    return f"# {doc.get('title')}\n\n{text}"


def _validate_batch_requests(text: str) -> list:
    """docs_batch_update のリクエストを検査し、1つでも問題があれば API を呼ばずに全体を拒否する。"""
    requests = parse_batch_requests(text, _BATCH_ALLOWED, _MAX_BATCH_REQUESTS)
    problems = []
    for i, req in enumerate(requests):
        kind, body = next(iter(req.items()))
        problems += external_ref_problems(body, f"requests[{i}].{kind}", _BLOCKED_KEYS)
    if problems:
        raise ToolError(BATCH_REJECTED + "問題: " + "、".join(problems))
    return requests


def _insert_at_end(services, document_id: str, text: str):
    return (
        services.get("docs")
        .documents()
        .batchUpdate(
            documentId=document_id,
            body={"requests": [{"insertText": {"text": text, "endOfSegmentLocation": {}}}]},
        )
        .execute()
    )


def docs_create(services, args):
    # Docs API の create はマイドライブ直下に作成する
    doc = services.get("docs").documents().create(body={"title": args["title"]}).execute()
    result = created(services, doc["documentId"])
    if args.get("text"):
        _insert_at_end(services, result["id"], args["text"])
    return {**result, "url": f"https://docs.google.com/document/d/{result['id']}/edit"}


def docs_append_text(services, args):
    meta = writable(services, args["document_id"])
    _insert_at_end(services, meta["id"], args["text"])
    return {"id": meta["id"], "name": meta.get("name"), "appendedChars": len(args["text"])}


def docs_replace_text(services, args):
    meta = writable(services, args["document_id"])
    res = (
        services.get("docs")
        .documents()
        .batchUpdate(
            documentId=meta["id"],
            body={
                "requests": [
                    {
                        "replaceAllText": {
                            "containsText": {"text": args["find"], "matchCase": args.get("match_case", True)},
                            "replaceText": args["replace"],
                        }
                    }
                ]
            },
        )
        .execute()
    )
    changed = (res.get("replies") or [{}])[0].get("replaceAllText", {}).get("occurrencesChanged", 0)
    return {"id": meta["id"], "name": meta.get("name"), "occurrencesChanged": changed}


def docs_batch_update(services, args):
    # 入力の検査は通信が要らないので、ガードより先に行う
    requests = _validate_batch_requests(args["requests"])
    meta = writable(services, args["document_id"])
    res = (
        services.get("docs")
        .documents()
        .batchUpdate(documentId=meta["id"], body={"requests": requests})
        .execute()
    )
    # createHeader や addDocumentTab などで作られた ID を後で使えるよう、replies を返す
    return {"replies": res.get("replies", [])}


TOOLS = [
    Tool(
        name="docs_read",
        title="ドキュメントを読む",
        description="ドキュメントの本文をテキストで取得する（表は | 区切り、複数タブはタブごとに見出しを付ける）。"
        "format に structure を指定すると、本文（ヘッダー・フッター・脚注は含まない）の段落ごとに"
        "位置 [startIndex-endIndex)（終わりを含まない・UTF-16 単位）・段落の種類（HEADING_2 など）・"
        "箇条書きの階層（0 始まり）を返す。書式や画像などが混ざる段落は、要素ごとの位置も続けて返す。"
        "docs_batch_update で範囲を指定する前に使う。",
        input_schema=obj(
            {
                "document_id": _DOCUMENT_ID,
                "format": {
                    "type": "string",
                    "enum": ["text", "structure"],
                    "description": "text（既定・本文のみ）または structure（位置と段落の種類付き）",
                },
            },
            ["document_id"],
        ),
        handler=docs_read,
        read_only=True,
    ),
    Tool(
        name="docs_create",
        title="ドキュメントを作成",
        description="マイドライブ直下にドキュメントを新規作成する。text を指定すると本文として入れる。",
        input_schema=obj(
            {"title": {"type": "string", "minLength": 1, "maxLength": 255}, "text": _TEXT},
            ["title"],
        ),
        handler=docs_create,
        read_only=False,
        guard=GUARD_CREATED,
    ),
    Tool(
        name="docs_append_text",
        title="末尾に追記",
        description="ドキュメント本文の末尾にテキストを追記する。対象は自分が所有するマイドライブのファイルに限る。",
        input_schema=obj({"document_id": _DOCUMENT_ID, "text": _TEXT}, ["document_id", "text"]),
        handler=docs_append_text,
        read_only=False,
        guard=GUARD_MYDRIVE,
    ),
    Tool(
        name="docs_replace_text",
        title="テキストを置換",
        description="ドキュメント内の一致するテキストをすべて置換する。対象は自分が所有するマイドライブのファイルに限る。",
        input_schema=obj(
            {
                "document_id": _DOCUMENT_ID,
                "find": {"type": "string", "minLength": 1, "maxLength": 1000},
                "replace": {"type": "string", "maxLength": 10_000},
                "match_case": {"type": "boolean", "description": "大文字小文字を区別する（既定 true）"},
            },
            ["document_id", "find", "replace"],
        ),
        handler=docs_replace_text,
        read_only=False,
        guard=GUARD_MYDRIVE,
        idempotent=True,
    ),
    Tool(
        name="docs_batch_update",
        title="見出し・リスト・表などの編集",
        description="Docs API の documents.batchUpdate で、見出し・箇条書き・表・文字の書式・挿入・削除などの編集を行う。"
        "対象は自分が所有するマイドライブのファイルに限る。"
        "requests にはリクエストの配列を JSON 文字列で渡す（" + str(_MAX_BATCH_REQUESTS) + " 件まで）。"
        "使えるリクエスト: " + " ".join(sorted(_BATCH_ALLOWED)) + "。"
        "範囲や位置は docs_read の format: structure で得た index で指定する（複数タブなら tabId も指定する）。"
        "リクエストは配列の順に適用され、挿入や削除で後ろの index がずれるため、後ろの位置から順に並べる。"
        "本文やセルの最後の段落の改行は削除できないため、最後の段落を消すときは endIndex から 1 を引く。"
        "見出しは updateParagraphStyle の paragraphStyle.namedStyleType（HEADING_1〜6 など）と fields: namedStyleType、"
        "箇条書きは createParagraphBullets で作る。"
        "リンク（updateTextStyle の textStyle.link）は http:// か https:// の url、または文書内の heading・bookmark・tabId に限る。"
        "画像の挿入・差し替え、リッチリンク、コメント、人物のメンション、提案の承認・却下、タブの削除はできない。"
        "1件でも不正なリクエストがあれば、何も変更せずに全体を拒否する。",
        input_schema=obj(
            {
                "document_id": _DOCUMENT_ID,
                "requests": {
                    "type": "string",
                    "minLength": 2,
                    "maxLength": _MAX_BATCH_TEXT,
                    "description": "リクエストの配列の JSON 文字列"
                    '（例: [{"updateParagraphStyle": {"range": {"startIndex": 1, "endIndex": 6}, '
                    '"paragraphStyle": {"namedStyleType": "HEADING_2"}, "fields": "namedStyleType"}}, '
                    '{"createParagraphBullets": {"range": {"startIndex": 6, "endIndex": 21}, '
                    '"bulletPreset": "BULLET_DISC_CIRCLE_SQUARE"}}]）',
                },
            },
            ["document_id", "requests"],
        ),
        handler=docs_batch_update,
        read_only=False,
        guard=GUARD_MYDRIVE,
    ),
]
