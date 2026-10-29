"""ドキュメント ツール。

- 読み取り: 本文をテキストで取得できる
- 書き込み: 新規作成と、自分が所有するマイドライブのファイルへの追記・一括置換ができる
- できないこと: 削除、共有ドライブや他人のファイルへの書き込み
"""

from gws_mcp.tools import GUARD_CREATED, GUARD_MYDRIVE, Tool
from gws_mcp.tools._common import created, file_id, obj, writable

_DOCUMENT_ID = file_id("ドキュメントの ID")
_TEXT = {"type": "string", "minLength": 1, "maxLength": 200_000}


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


def _span(el: dict) -> str:
    return f"[{el.get('startIndex', 0)}-{el.get('endIndex', 0)})"


def _elements_structure(elements: list, indent: str = "") -> list[str]:
    """本文の構造要素を、位置（startIndex〜endIndex）と段落の種類付きの行にする。

    書式のリクエストは位置で範囲を指定するため。位置は API の値（UTF-16 単位）をそのまま返し、
    LLM が文字数を数えてずれるのを防ぐ。
    """
    lines = []
    for el in elements or []:
        if "paragraph" in el:
            para = el["paragraph"]
            style = (para.get("paragraphStyle") or {}).get("namedStyleType", "NORMAL_TEXT")
            if "bullet" in para:
                style += f" 箇条書き(階層{para['bullet'].get('nestingLevel', 0)})"
            text = "".join((pe.get("textRun") or {}).get("content", "") for pe in para.get("elements", []))
            lines.append(f"{indent}{_span(el)} {style} {text.rstrip(chr(10))}")
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


TOOLS = [
    Tool(
        name="docs_read",
        title="ドキュメントを読む",
        description="ドキュメントの本文をテキストで取得する（表は | 区切り、複数タブはタブごとに見出しを付ける）。"
        "format に structure を指定すると、段落ごとに位置 [startIndex-endIndex)（終わりを含まない）・"
        "段落の種類（HEADING_2 など）・箇条書きかどうかを返す。docs_batch_update で範囲を指定する前に使う。",
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
]
