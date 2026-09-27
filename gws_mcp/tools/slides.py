"""スライド ツール。

- 読み取り: 各スライドのテキストを取得できる
- 書き込み: 新規作成と、自分が所有するマイドライブのファイルへのスライド追加・一括置換ができる
- できないこと: 削除、共有ドライブや他人のファイルへの書き込み
"""

import uuid

from gws_mcp.tools import GUARD_CREATED, GUARD_MYDRIVE, Tool
from gws_mcp.tools._common import created, file_id, obj, writable

_PRESENTATION_ID = file_id("プレゼンテーションの ID")


def _text_of(text: dict | None) -> str:
    return "".join((te.get("textRun") or {}).get("content", "") for te in (text or {}).get("textElements", []))


def _page_texts(elements: list) -> list[str]:
    """ページ要素（図形・表・グループ）からテキストを取り出す。"""
    out = []
    for el in elements or []:
        if "shape" in el:
            t = _text_of(el["shape"].get("text")).strip()
            if t:
                out.append(t)
        elif "table" in el:
            for row in el["table"].get("tableRows", []):
                cells = [_text_of(c.get("text")).strip() for c in row.get("tableCells", [])]
                out.append(" | ".join(cells))
        elif "elementGroup" in el:
            out.extend(_page_texts(el["elementGroup"].get("children", [])))
    return out


def slides_read(services, args):
    pres = services.get("slides").presentations().get(presentationId=args["presentation_id"]).execute()
    parts = [f"# {pres.get('title')}"]
    for i, slide in enumerate(pres.get("slides", []), 1):
        parts.append(f"\n## スライド {i}（objectId: {slide.get('objectId')}）")
        parts.extend(_page_texts(slide.get("pageElements", [])))
        notes_page = (slide.get("slideProperties") or {}).get("notesPage") or {}
        notes_id = (notes_page.get("notesProperties") or {}).get("speakerNotesObjectId")
        notes = [
            _text_of(el["shape"].get("text")).strip()
            for el in notes_page.get("pageElements", [])
            if el.get("objectId") == notes_id and "shape" in el
        ]
        if any(notes):
            parts.append("（スピーカーノート）" + "\n".join(n for n in notes if n))
    return "\n".join(parts)


def slides_create(services, args):
    # Slides API の create はマイドライブ直下に作成する
    pres = services.get("slides").presentations().create(body={"title": args["title"]}).execute()
    result = created(services, pres["presentationId"])
    return {**result, "url": f"https://docs.google.com/presentation/d/{result['id']}/edit"}


def slides_add_text_slide(services, args):
    meta = writable(services, args["presentation_id"])
    # オブジェクト ID は 5〜50 文字の英数字・アンダースコア
    suffix = uuid.uuid4().hex[:16]
    slide_id, title_id, body_id = f"s_{suffix}", f"t_{suffix}", f"b_{suffix}"
    create = {
        "objectId": slide_id,
        "slideLayoutReference": {"predefinedLayout": "TITLE_AND_BODY"},
        "placeholderIdMappings": [
            {"layoutPlaceholder": {"type": "TITLE"}, "objectId": title_id},
            {"layoutPlaceholder": {"type": "BODY"}, "objectId": body_id},
        ],
    }
    if "insertion_index" in args:
        create["insertionIndex"] = args["insertion_index"]
    requests = [{"createSlide": create}, {"insertText": {"objectId": title_id, "text": args["title"]}}]
    if args.get("body"):
        requests.append({"insertText": {"objectId": body_id, "text": args["body"]}})
    services.get("slides").presentations().batchUpdate(
        presentationId=meta["id"], body={"requests": requests}
    ).execute()
    return {"presentationId": meta["id"], "slideObjectId": slide_id}


def slides_replace_text(services, args):
    meta = writable(services, args["presentation_id"])
    res = (
        services.get("slides")
        .presentations()
        .batchUpdate(
            presentationId=meta["id"],
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
        name="slides_read",
        title="スライドを読む",
        description="プレゼンテーションの各スライドのテキスト（図形・表・スピーカーノート）を取得する。",
        input_schema=obj({"presentation_id": _PRESENTATION_ID}, ["presentation_id"]),
        handler=slides_read,
        read_only=True,
    ),
    Tool(
        name="slides_create",
        title="プレゼンテーションを作成",
        description="マイドライブ直下にプレゼンテーションを新規作成する。",
        input_schema=obj({"title": {"type": "string", "minLength": 1, "maxLength": 255}}, ["title"]),
        handler=slides_create,
        read_only=False,
        guard=GUARD_CREATED,
    ),
    Tool(
        name="slides_add_text_slide",
        title="スライドを追加",
        description=(
            "タイトルと本文のスライドを追加する（テーマの「タイトルと本文」レイアウトを使用）。"
            "対象は自分が所有するマイドライブのファイルに限る。"
        ),
        input_schema=obj(
            {
                "presentation_id": _PRESENTATION_ID,
                "title": {"type": "string", "minLength": 1, "maxLength": 1000},
                "body": {"type": "string", "maxLength": 20_000},
                "insertion_index": {"type": "integer", "minimum": 0, "maximum": 10_000, "description": "挿入位置（省略時は末尾）"},
            },
            ["presentation_id", "title"],
        ),
        handler=slides_add_text_slide,
        read_only=False,
        guard=GUARD_MYDRIVE,
    ),
    Tool(
        name="slides_replace_text",
        title="テキストを置換",
        description="プレゼンテーション内の一致するテキストをすべて置換する。対象は自分が所有するマイドライブのファイルに限る。",
        input_schema=obj(
            {
                "presentation_id": _PRESENTATION_ID,
                "find": {"type": "string", "minLength": 1, "maxLength": 1000},
                "replace": {"type": "string", "maxLength": 10_000},
                "match_case": {"type": "boolean", "description": "大文字小文字を区別する（既定 true）"},
            },
            ["presentation_id", "find", "replace"],
        ),
        handler=slides_replace_text,
        read_only=False,
        guard=GUARD_MYDRIVE,
        idempotent=True,
    ),
]
