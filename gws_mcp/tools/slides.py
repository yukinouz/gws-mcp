"""スライド ツール。

- 読み取り: 各スライドのテキストを取得できる。オブジェクト ID・位置・大きさ付きでも取得できる
- 書き込み: 新規作成と、自分が所有するマイドライブのファイルへのスライド追加・一括置換、
  slides_batch_update によるスライド・図形・表・文字の作成・編集・削除ができる
- できないこと: ファイルの削除、共有ドライブや他人のファイルへの書き込み

slides_batch_update は許可リストの種類だけを受け付ける。プロンプトインジェクションで悪用されても
プレゼンテーションの外に影響が出ないよう、Google が外部 URL を取りに行く操作と、第三者に通知が届く操作は含めない。
スプレッドシートのグラフは、参照先が書き込みガードを通る（自分が所有するマイドライブの）ファイルのときだけ取り込む。
共有ドライブや他人のファイルの最新のデータに、リンクし続けるグラフを作らせないため。
"""

import uuid

from gws_mcp.tools import GUARD_CREATED, GUARD_MYDRIVE, Tool, ToolError
from gws_mcp.tools._common import (
    BATCH_REJECTED,
    created,
    external_ref_problems,
    file_id,
    key_name,
    obj,
    parse_batch_requests,
    run_text,
    writable,
)

_PRESENTATION_ID = file_id("プレゼンテーションの ID")

# slides_batch_update で使えるリクエスト。プレゼンテーションの中で完結する編集だけを含める。
# Google が外部 URL を取りに行くもの（createImage・replaceImage・replaceAllShapesWithImage）、
# 別のファイルの中身を取り込む動画（createVideo）、
# 第三者に通知が届くもの（insertComment などのコメント）は含めない
_BATCH_ALLOWED = frozenset(
    {
        # スライド
        "createSlide",
        "duplicateObject",
        "updateSlidesPosition",
        "updateSlideProperties",
        "deleteObject",
        # 図形と線
        "createShape",
        "createLine",
        "updatePageElementTransform",
        "updateShapeProperties",
        "updateLineProperties",
        "updateLineCategory",
        "rerouteLine",
        "groupObjects",
        "ungroupObjects",
        "updatePageElementsZOrder",
        "updatePageElementAltText",
        # 文字と段落
        "insertText",
        "deleteText",
        "replaceAllText",
        "updateTextStyle",
        "updateParagraphStyle",
        "createParagraphBullets",
        "deleteParagraphBullets",
        # 表
        "createTable",
        "insertTableRows",
        "insertTableColumns",
        "deleteTableRow",
        "deleteTableColumn",
        "updateTableCellProperties",
        "updateTableBorderProperties",
        "updateTableColumnProperties",
        "updateTableRowProperties",
        "mergeTableCells",
        "unmergeTableCells",
        # ページの背景と、すでにある画像・動画の見た目
        "updatePageProperties",
        "updateImageProperties",
        "updateVideoProperties",
        # スプレッドシートのグラフ。参照先のファイルを書き込みガードで判定してから取り込む
        "createSheetsChart",
        "replaceAllShapesWithSheetsChart",
        "refreshSheetsChart",
    }
)
# spreadsheetId を直下に指定するグラフの種類
_CHART_KINDS = frozenset({"createSheetsChart", "replaceAllShapesWithSheetsChart"})
_MAX_BATCH_REQUESTS = 200
_MAX_BATCH_TEXT = 200_000
# 外部 URL や別ファイルの ID のキー。許可した種類の中では背景画像（updatePageProperties の
# stretchedPictureFill.contentUrl）に入れられ、Google が挿入時に URL を取りに行く。
# imageUrl は止めた種類にだけあるキーで、API がほかの種類に広げても拾えるよう入れておく。
# spreadsheetId はグラフの種類の直下だけ受け付け（参照先はガードで判定）、それ以外の場所では拒否する
_BLOCKED_KEYS = {
    "contentUrl": "画像の URL は使えません（Google が外部 URL を取りに行くため）",
    "imageUrl": "画像の URL は使えません（Google が外部 URL を取りに行くため）",
    "spreadsheetId": "別のファイル（スプレッドシート）の取り込みは使えません",
}


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


# 1pt あたりの EMU。API の大きさと位置は EMU で返るため、読みやすい pt に直す
_EMU_PER_PT = 12_700
# ページ要素の種類と表示名。図形（shape）と表・グループは中身も出すため別に扱う
_ELEMENT_LABELS = {
    "image": "画像",
    "video": "動画",
    "line": "線",
    "sheetsChart": "グラフ",
    "wordArt": "ワードアート",
    "speakerSpotlight": "スピーカー スポットライト",
}


def _pt(value: float | None, unit: str | None) -> float:
    return (value or 0) if unit == "PT" else (value or 0) / _EMU_PER_PT


def _dimension_pt(dimension: dict | None) -> float:
    """Dimension（magnitude と unit）を pt に直す。"""
    dimension = dimension or {}
    return _pt(dimension.get("magnitude"), dimension.get("unit"))


def _num(value: float) -> str:
    # 0.0 を足して、-0.0 を 0 と出す
    return f"{round(value, 1) + 0.0:.1f}".rstrip("0").rstrip(".")


def _geometry(el: dict, in_group: bool) -> str:
    """見た目の外接矩形の左上の位置と大きさを pt で返す。

    LLM が新しい図形を置く場所や、既存の図形の大きさを決めるため。回転・反転した要素も、
    位置と大きさが同じ矩形を指すよう、変換後の 4 隅から外接矩形を求める。
    グループ内の要素の transform はグループ基準のため、そう明記する。
    """
    t = el.get("transform")
    # API は 0 の値を省略して返す。transform 自体がなければ移動も拡大もない
    if t is None:
        t = {"scaleX": 1, "scaleY": 1}
    sx, sy = t.get("scaleX", 0), t.get("scaleY", 0)
    shx, shy = t.get("shearX", 0), t.get("shearY", 0)
    size = el.get("size") or {}
    w, h = _dimension_pt(size.get("width")), _dimension_pt(size.get("height"))
    x = _pt(t.get("translateX"), t.get("unit")) + min(0, sx * w) + min(0, shx * h)
    y = _pt(t.get("translateY"), t.get("unit")) + min(0, shy * w) + min(0, sy * h)
    base = " グループ基準:" if in_group else ""
    out = f"{base} 位置({_num(x)},{_num(y)}pt)"
    if size:
        out += f" 大きさ({_num(abs(sx * w) + abs(shx * h))}×{_num(abs(sy * h) + abs(shy * w))}pt)"
    if shx or shy or sx < 0 or sy < 0:
        out += " 回転・反転あり"
    return out


def _span(el: dict) -> str:
    # API は 0 の startIndex を省略して返す
    return f"[{el.get('startIndex', 0)}-{el.get('endIndex', 0)})"


def _run_text(te: dict) -> str:
    if "textRun" in te:
        return run_text(te["textRun"].get("content", ""))
    return "[自動テキスト]" if "autoText" in te else ""


def _paragraphs(text: dict | None) -> list[tuple[dict, list]]:
    """テキストの要素を、段落の目印（paragraphMarker）と、その段落の文字の範囲の組に分ける。"""
    paragraphs = []
    for te in (text or {}).get("textElements", []):
        if "paragraphMarker" in te:
            paragraphs.append((te, []))
        elif paragraphs:
            paragraphs[-1][1].append(te)
    return paragraphs


def _text_structure(text: dict | None, indent: str) -> list[str]:
    """図形や表のセルのテキストを、段落ごとの位置付きの行にする。

    文字の範囲（startIndex〜endIndex）は API の値をそのまま返し、LLM が文字数を数えてずれるのを防ぐ。
    書式が混ざる段落は、文字の範囲（textRun）ごとの位置も続けて出す。
    """
    lines = []
    for marker, runs in _paragraphs(text):
        bullet = marker["paragraphMarker"].get("bullet")
        level = f"箇条書き(階層{bullet.get('nestingLevel', 0)}) " if bullet is not None else ""
        body = "".join(_run_text(r) for r in runs) or "（空行）"
        lines.append(f"{indent}{_span(marker)} {level}{body}")
        if len(runs) > 1:
            lines += [f"{indent}  {_span(r)} {_run_text(r) or '（改行）'}" for r in runs]
    return lines


def _head(el: dict, indent: str, in_group: bool, kind: str, detail: str = "") -> str:
    return f"{indent}{kind} {el.get('objectId')}{detail}{_geometry(el, in_group)}"


def _elements_structure(elements: list, indent: str = "", in_group: bool = False) -> list[str]:
    """ページ要素を、オブジェクト ID・種類・位置と大きさ・テキストの位置付きの行にする。

    スライドの編集リクエストはオブジェクト ID で対象を指すため。
    """
    lines = []
    for el in elements or []:
        if "shape" in el:
            shape = el["shape"]
            shape_type = f" {shape['shapeType']}" if shape.get("shapeType") else ""
            placeholder = (shape.get("placeholder") or {}).get("type")
            extra = f" プレースホルダー: {placeholder}" if placeholder else ""
            lines.append(_head(el, indent, in_group, "図形", shape_type) + extra)
            lines.extend(_text_structure(shape.get("text"), indent + "  "))
        elif "table" in el:
            table = el["table"]
            lines.append(_head(el, indent, in_group, "表", f" {table.get('rows', 0)}行×{table.get('columns', 0)}列"))
            for r, row in enumerate(table.get("tableRows", [])):
                for c, cell in enumerate(row.get("tableCells", [])):
                    lines.append(f"{indent}  セル({r},{c})")
                    lines.extend(_text_structure(cell.get("text"), indent + "    "))
        elif "elementGroup" in el:
            lines.append(_head(el, indent, in_group, "グループ"))
            lines.extend(_elements_structure(el["elementGroup"].get("children", []), indent + "  ", True))
        else:
            kind = next((k for k in _ELEMENT_LABELS if k in el), None)
            lines.append(_head(el, indent, in_group, _ELEMENT_LABELS.get(kind, "要素")))
    return lines


def _notes_shape(slide: dict) -> tuple[str | None, dict | None]:
    """スピーカーノートの図形の ID と、その図形を返す。"""
    notes_page = (slide.get("slideProperties") or {}).get("notesPage") or {}
    notes_id = (notes_page.get("notesProperties") or {}).get("speakerNotesObjectId")
    shape = next(
        (el["shape"] for el in notes_page.get("pageElements", []) if el.get("objectId") == notes_id and "shape" in el),
        None,
    )
    return notes_id, shape


def _slides_structure(pres: dict) -> list[str]:
    layouts = {lay.get("objectId"): (lay.get("layoutProperties") or {}) for lay in pres.get("layouts", [])}
    parts = []
    for i, slide in enumerate(pres.get("slides", []), 1):
        layout = layouts.get((slide.get("slideProperties") or {}).get("layoutObjectId"), {}).get("name")
        layout_text = f", レイアウト: {layout}" if layout else ""
        parts.append(f"\n## スライド {i}（objectId: {slide.get('objectId')}{layout_text}）")
        parts.extend(_elements_structure(slide.get("pageElements", [])))
        notes_id, shape = _notes_shape(slide)
        if notes_id:
            # 空のノートにも書き込めるよう、ID は中身がなくても出す
            parts.append(f"（スピーカーノート objectId: {notes_id}）")
            parts.extend(_text_structure((shape or {}).get("text"), "  "))
    if layouts:
        parts.append("\n## レイアウト（createSlide の slideLayoutReference.layoutId に使う）")
    parts.extend(
        f"{lay_id} {props.get('name', '')}「{props.get('displayName', '')}」" for lay_id, props in layouts.items()
    )
    size = pres.get("pageSize") or {}
    if size:
        width, height = _dimension_pt(size.get("width")), _dimension_pt(size.get("height"))
        parts.append(f"\nページの大きさ {_num(width)}×{_num(height)}pt")
    return parts


def slides_read(services, args):
    pres = services.get("slides").presentations().get(presentationId=args["presentation_id"]).execute()
    parts = [f"# {pres.get('title')}"]
    if args.get("format") == "structure":
        return "\n".join(parts + _slides_structure(pres))
    for i, slide in enumerate(pres.get("slides", []), 1):
        parts.append(f"\n## スライド {i}（objectId: {slide.get('objectId')}）")
        parts.extend(_page_texts(slide.get("pageElements", [])))
        _, shape = _notes_shape(slide)
        note = _text_of((shape or {}).get("text")).strip()
        if note:
            parts.append("（スピーカーノート）" + note)
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


def _validate_batch_requests(text: str) -> tuple[list, set, set]:
    """slides_batch_update のリクエストを検査し、1つでも問題があれば API を呼ばずに全体を拒否する。

    リクエストと、グラフの参照先のスプレッドシートの ID、更新するグラフの objectId を返す。
    """
    requests = parse_batch_requests(text, _BATCH_ALLOWED, _MAX_BATCH_REQUESTS)
    problems, spreadsheet_ids, refresh_ids = [], set(), set()
    for i, req in enumerate(requests):
        kind, body = next(iter(req.items()))
        path = f"requests[{i}].{kind}"
        if kind in _CHART_KINDS:
            ids = [v for k, v in body.items() if key_name(k) == "spreadsheetid"]
            if len(ids) != 1 or not isinstance(ids[0], str):
                problems.append(f"{path}.spreadsheetId: 参照先のスプレッドシートの ID を 1 つ指定してください")
            else:
                spreadsheet_ids.add(ids[0])
            # 直下の spreadsheetId はガードで判定するため、拒否するキーの検査から外す
            body = {k: v for k, v in body.items() if key_name(k) != "spreadsheetid"}
        elif kind == "refreshSheetsChart":
            ids = [v for k, v in body.items() if key_name(k) == "objectid"]
            if len(ids) != 1 or not isinstance(ids[0], str):
                problems.append(f"{path}.objectId: 更新するグラフの objectId を 1 つ指定してください")
            else:
                refresh_ids.add(ids[0])
        problems += external_ref_problems(body, path, _BLOCKED_KEYS)
    # 更新するグラフの参照先は適用前のプレゼンテーションで調べるため、同じ batch で ID を付け替えられないようにする
    if refresh_ids and any(next(iter(req)) != "refreshSheetsChart" for req in requests):
        problems.append("refreshSheetsChart は、ほかの種類のリクエストと同じ requests に入れられません")
    if problems:
        raise ToolError(BATCH_REJECTED + "問題: " + "、".join(problems))
    return requests, spreadsheet_ids, refresh_ids


def _chart_sources(elements: list) -> dict:
    """ページ要素（グループの中を含む）から、グラフの objectId と参照先のスプレッドシートの ID を集める。"""
    sources, stack = {}, list(elements or [])
    while stack:
        el = stack.pop()
        if "sheetsChart" in el:
            sources[el.get("objectId")] = el["sheetsChart"].get("spreadsheetId")
        stack.extend((el.get("elementGroup") or {}).get("children", []))
    return sources


def _refresh_sources(services, presentation_id: str, object_ids: set) -> set:
    """更新するグラフの参照先を、プレゼンテーションを読んで調べる。

    refreshSheetsChart は objectId しか受け取らず、手作業で貼った共有ドライブのグラフも更新できてしまうため。
    """
    pres = services.get("slides").presentations().get(presentationId=presentation_id).execute()
    sources = {}
    for page in pres.get("slides", []) + pres.get("layouts", []) + pres.get("masters", []):
        sources.update(_chart_sources(page.get("pageElements", [])))
    missing = sorted(str(i) for i in object_ids if i not in sources)
    if missing:
        raise ToolError(BATCH_REJECTED + "更新するスプレッドシートのグラフが見つかりません: " + "、".join(missing))
    return {sources[i] for i in object_ids}


def _assert_chart_sources(services, spreadsheet_ids: set) -> None:
    """参照先のスプレッドシートが書き込みガードを通るか確かめ、通らなければ batch 全体を拒否する。"""
    for spreadsheet_id in sorted(spreadsheet_ids, key=str):
        try:
            writable(services, spreadsheet_id)
        except ToolError as e:
            # ガードの文の後半（「読み取り専用です」など）は書き込みの理由で、読み込むだけのグラフには当てはまらない
            reason = str(e).split("。")[0] + "。"
            raise ToolError(
                BATCH_REJECTED + "グラフの参照先は、自分が所有するマイドライブのスプレッドシートに限ります: " + reason
            ) from None


def slides_batch_update(services, args):
    # 入力の検査は通信が要らないので、ガードより先に行う
    requests, spreadsheet_ids, refresh_ids = _validate_batch_requests(args["requests"])
    meta = writable(services, args["presentation_id"])
    if refresh_ids:
        spreadsheet_ids |= _refresh_sources(services, meta["id"], refresh_ids)
    _assert_chart_sources(services, spreadsheet_ids)
    res = (
        services.get("slides")
        .presentations()
        .batchUpdate(presentationId=meta["id"], body={"requests": requests})
        .execute()
    )
    # createSlide や duplicateObject などで作られた ID を後で使えるよう、replies を返す
    return {"replies": res.get("replies", [])}


TOOLS = [
    Tool(
        name="slides_read",
        title="スライドを読む",
        description="プレゼンテーションの各スライドのテキスト（図形・表・スピーカーノート）を取得する。"
        "format に structure を指定すると、ページ要素（図形・表・画像・グループなど）ごとに objectId・種類・"
        "外接矩形の位置と大きさ（pt・グループ内の要素はグループ基準）・プレースホルダーの種類と、"
        "テキストの段落ごとの位置 [startIndex-endIndex)"
        "（終わりを含まない）・箇条書きの階層を返す。スピーカーノートの objectId、レイアウトの一覧、ページの大きさも返す。"
        "slides_batch_update で対象を指定する前に使う。",
        input_schema=obj(
            {
                "presentation_id": _PRESENTATION_ID,
                "format": {
                    "type": "string",
                    "enum": ["text", "structure"],
                    "description": "text（既定・テキストのみ）または structure（ID・位置・大きさ付き）",
                },
            },
            ["presentation_id"],
        ),
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
    Tool(
        name="slides_batch_update",
        title="スライド・図形・表などの編集",
        description="Slides API の presentations.batchUpdate で、スライドの追加・複製・並べ替え・削除、図形・線・表の作成、"
        "位置と大きさ、文字・段落・箇条書きの書式、背景色などの編集を行う。対象は自分が所有するマイドライブのファイルに限る。"
        "requests にはリクエストの配列を JSON 文字列で渡す（" + str(_MAX_BATCH_REQUESTS) + " 件まで）。"
        "使えるリクエスト: " + " ".join(sorted(_BATCH_ALLOWED)) + "。"
        "対象の objectId・文字の位置・レイアウトの ID は slides_read の format: structure で調べる。"
        "createSlide・createShape などで objectId（英数字とアンダースコアで 5〜50 文字）を指定すると、"
        "同じ requests の後ろのリクエストでその ID を使える。リクエストは配列の順に適用される。"
        "位置と大きさは unit に PT を指定すると pt で書ける。"
        "リンク（updateTextStyle の style.link、図形・画像・線の properties の link）は http:// か https:// の url、"
        "またはスライド内（pageObjectId・slideIndex・relativeLink）に限る。"
        "スプレッドシートのグラフ（createSheetsChart・replaceAllShapesWithSheetsChart・refreshSheetsChart）は、"
        "参照先が自分の所有するマイドライブのスプレッドシートのときだけ使える（chartId は sheets_get_metadata で調べる）。"
        "refreshSheetsChart はほかの種類と同じ requests に入れられない。"
        "画像・動画の挿入や差し替え、背景画像、コメントはできない。"
        "1件でも不正なリクエストがあれば、何も変更せずに全体を拒否する。",
        input_schema=obj(
            {
                "presentation_id": _PRESENTATION_ID,
                "requests": {
                    "type": "string",
                    "minLength": 2,
                    "maxLength": _MAX_BATCH_TEXT,
                    "description": "リクエストの配列の JSON 文字列"
                    '（例: [{"createSlide": {"objectId": "slide_new1", "slideLayoutReference": '
                    '{"predefinedLayout": "TITLE_AND_BODY"}}}, {"updateTextStyle": {"objectId": "t1", '
                    '"textRange": {"type": "FIXED_RANGE", "startIndex": 0, "endIndex": 4}, '
                    '"style": {"bold": true}, "fields": "bold"}}]）',
                },
            },
            ["presentation_id", "requests"],
        ),
        handler=slides_batch_update,
        read_only=False,
        guard=GUARD_MYDRIVE,
    ),
]
