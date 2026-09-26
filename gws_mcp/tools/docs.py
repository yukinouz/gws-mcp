"""ドキュメント ツール。

書き込みは guard を通し、自分が所有するマイドライブのファイルに限る。
"""

from gws_mcp.tools import Tool
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


def _tabs_text(tabs: list, depth: int = 0) -> list[str]:
    parts = []
    for tab in tabs or []:
        title = (tab.get("tabProperties") or {}).get("title", "")
        body = ((tab.get("documentTab") or {}).get("body") or {}).get("content", [])
        parts.append(f"{'#' * (depth + 2)} タブ: {title}\n\n{_elements_text(body)}")
        parts.extend(_tabs_text(tab.get("childTabs", []), depth + 1))
    return parts


def docs_read(services, args):
    doc = (
        services.get("docs")
        .documents()
        .get(documentId=args["document_id"], includeTabsContent=True)
        .execute()
    )
    tabs = doc.get("tabs", [])
    # タブが 1 つだけならタブ見出しを付けずに本文だけ返す
    if len(tabs) == 1 and not tabs[0].get("childTabs"):
        text = _elements_text(tabs[0]["documentTab"]["body"].get("content", []))
    else:
        text = "\n".join(_tabs_text(tabs))
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
        description="ドキュメントの本文をテキストで取得する（表は | 区切り、複数タブはタブごとに見出しを付ける）。",
        input_schema=obj({"document_id": _DOCUMENT_ID}, ["document_id"]),
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
    ),
    Tool(
        name="docs_append_text",
        title="末尾に追記",
        description="ドキュメント本文の末尾にテキストを追記する。対象は自分が所有するマイドライブのファイルに限る。",
        input_schema=obj({"document_id": _DOCUMENT_ID, "text": _TEXT}, ["document_id", "text"]),
        handler=docs_append_text,
        read_only=False,
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
        idempotent=True,
    ),
]
