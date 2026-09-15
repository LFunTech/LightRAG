"""Public WebUI integration documentation stays open and secret-free."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.offline

REPO_ROOT = Path(__file__).resolve().parents[2]
PUBLIC_OBJECT_UPLOAD_DOC_DIR = (
    REPO_ROOT
    / "lightrag_webui"
    / "public"
    / "docs"
    / "third-party-object-upload-integration"
)
PUBLIC_OBJECT_UPLOAD_DOC = (
    PUBLIC_OBJECT_UPLOAD_DOC_DIR
    / "index.html"
)
WEBUI_OBJECT_UPLOAD_ROUTE = "/webui/docs/third-party-object-upload-integration/"


FORBIDDEN_PUBLIC_DOC_TOKENS = (
    ".secrets",
    "test.secrets",
    "S3_ACCESS_KEY_ID",
    "S3_SECRET_ACCESS_KEY",
    "S3_SESSION_TOKEN",
    "MINIO_ROOT_PASSWORD",
    "MINIO_SECRET_KEY",
    "WOODPECKER",
    "HUGEGRAPH_PASSWORD",
    "POSTGRES_PASSWORD",
)

THIRD_PARTY_ENDPOINTS_WITH_DETAIL = (
    "POST /documents/uploads/presign",
    "POST /documents/uploads/complete",
    "POST /documents/upload",
    "POST /documents/text",
    "POST /documents/texts",
    "GET /documents/supported_file_types",
    "GET /documents/track_status/{track_id}",
    "GET /documents/scan/status/{track_id}",
    "GET /documents/pipeline_status",
    "GET /documents/status_counts",
    "POST /documents/paginated",
    "POST /documents/scan",
    "GET /documents/source_conflicts",
    "POST /documents/source_conflicts/repair",
    "POST /documents/reprocess_failed",
    "DELETE /documents/delete_document",
    "DELETE /documents",
    "POST /documents/recovery/force_reset",
    "POST /documents/cancel_pipeline",
    "POST /query",
    "POST /query/stream",
    "POST /query/data",
    "POST /api/chat",
    "POST /api/generate",
    "GET /api/tags",
    "GET /api/version",
    "GET /api/ps",
    "GET /graphs",
    "GET /graph/label/list",
    "GET /graph/label/popular",
    "GET /graph/label/search",
    "GET /graph/entity/exists",
    "POST /graph/entity/create",
    "POST /graph/relation/create",
    "POST /graph/entity/edit",
    "POST /graph/relation/edit",
    "POST /graph/entities/merge",
    "DELETE /graph/entity/delete",
    "DELETE /graph/relation/delete",
    "GET /health",
)


def _public_doc_text() -> str:
    return PUBLIC_OBJECT_UPLOAD_DOC.read_text(encoding="utf-8")


def _css_rule(text: str, selector: str) -> str:
    match = re.search(rf"{re.escape(selector)}\s*\{{(?P<body>.*?)\n    \}}", text, re.DOTALL)
    assert match is not None
    return match.group("body")


def _font_size_px(css_rule: str) -> float:
    match = re.search(r"\bfont-size:\s*(?P<size>\d+(?:\.\d+)?)px;", css_rule)
    assert match is not None
    return float(match.group("size"))


def _sequence_viewbox_width(text: str) -> int:
    match = re.search(
        r'<svg class="sequence-diagram"[^>]*\bviewBox="0 0 (?P<width>\d+) \d+"',
        text,
    )
    assert match is not None
    return int(match.group("width"))


def _participant_center_xs(text: str) -> list[float]:
    participant_xs = [
        float(value)
        for value in re.findall(
            r'<g transform="translate\((?P<x>\d+(?:\.\d+)?) \d+(?:\.\d+)?\)">'
            r'\s*<rect class="participant-box"',
            text,
        )
    ]
    assert len(participant_xs) == 5
    return [x + 77.5 for x in participant_xs]


def _nav_items(text: str) -> list[tuple[str, str]]:
    return re.findall(
        r'<a class="doc-nav-link" href="#(?P<href>[^"]+)">(?P<label>[^<]+)</a>',
        text,
    )


def _sidebar_text(text: str) -> str:
    match = re.search(
        r'<aside class="docs-sidebar">(?P<body>.*?)</aside>',
        text,
        re.DOTALL,
    )
    assert match is not None
    return match.group("body")


def _endpoint_article_match(text: str, endpoint: str) -> re.Match[str]:
    match = re.search(
        rf'<article class="endpoint-detail"(?P<attrs>[^>]*)'
        rf'\bdata-endpoint="{re.escape(endpoint)}"[^>]*>'
        r"(?P<body>.*?)</article>",
        text,
        re.DOTALL,
    )
    assert match is not None, endpoint
    return match


def _endpoint_article_id(text: str, endpoint: str) -> str:
    attrs = _endpoint_article_match(text, endpoint).group("attrs")
    match = re.search(r'\bid="(?P<id>[^"]+)"', attrs)
    assert match is not None, endpoint
    return match.group("id")


def _endpoint_detail(text: str, endpoint: str) -> str:
    return _endpoint_article_match(text, endpoint).group("body")


def test_third_party_object_upload_doc_is_a_webui_public_asset():
    assert PUBLIC_OBJECT_UPLOAD_DOC.exists()
    text = _public_doc_text()

    assert WEBUI_OBJECT_UPLOAD_ROUTE in text
    assert "LightRAG 第三方 API 对接指南" in text
    assert 'id="sequence"' in text
    assert "<svg" in text
    assert "LightRAG object-backed upload sequence diagram" in text
    assert text.index('id="sequence"') < text.index('id="how-to-upload"')
    assert "POST /documents/uploads/presign" in text
    assert "POST /documents/uploads/complete" in text
    assert "GET /documents/track_status/{track_id}" in text
    assert "DELETE /documents/delete_document" in text
    assert "文件数据面直接进入 S3-compatible 对象存储" in text


def test_third_party_api_docs_expose_a_switchable_navigation_tree():
    """Catch the page regressing to one long upload-only article."""
    text = _public_doc_text()
    nav_items = _nav_items(text)

    assert len(nav_items) >= 7
    assert 'class="docs-shell"' in text
    assert 'class="docs-sidebar"' in text
    assert 'aria-label="第三方 API 文档目录"' in text
    assert 'class="doc-content"' in text
    assert '<script>' in text
    assert "setActiveSection" in text

    for section_id, label in nav_items:
        assert f'id="{section_id}"' in text, label

    assert ("sequence", "对象上传流程") in nav_items
    assert ("query-api", "知识检索与问答") in nav_items
    assert ("graph-api", "图谱查询与管理") in nav_items
    assert ("operations-api", "状态、重试与删除") in nav_items


def test_third_party_api_docs_cover_non_upload_integration_apis():
    """Catch third-party docs only explaining object upload while omitting API use."""
    text = _public_doc_text()
    required_tokens = (
        "POST /query",
        "POST /query/stream",
        "POST /query/data",
        "GET /graphs",
        "GET /graph/label/list",
        "POST /graph/entity/create",
        "POST /graph/relation/create",
        "GET /health",
        "GET /documents/pipeline_status",
        "POST /api/chat",
    )

    for token in required_tokens:
        assert token in text

    assert "第三方应用推荐只把图谱写入 API 用作人工修正" in text
    assert "查询 API 不负责上传文件，也不等待 pipeline 处理文档" in text


def test_third_party_api_docs_detail_every_documented_endpoint():
    """Catch endpoint docs drifting away from the agreed standard API template."""
    text = _public_doc_text()
    required_section_headings = (
        "1. 基本信息",
        "2. 请求参数（Request）",
        "3. 响应结果（Response）",
        "4. 状态码与异常说明",
        "5. 代码示例",
    )
    required_detail_labels = (
        "接口名称",
        "请求路径",
        "请求方式",
        "接口状态",
        "请求头",
        "请求参数",
        "参数名",
        "类型",
        "是否必填",
        "字段说明",
        "返回格式",
        "响应字段",
    )

    for endpoint in THIRD_PARTY_ENDPOINTS_WITH_DETAIL:
        method, path = endpoint.split(" ", 1)
        body = _endpoint_detail(text, endpoint)
        plain_body = re.sub(r"<[^>]+>", "", body)

        assert f"<code>{endpoint}</code>" in body
        for heading in required_section_headings:
            assert f"<h4>{heading}</h4>" in body, endpoint
        for label in required_detail_labels:
            assert label in body, endpoint
        assert f"<code>{path}</code>" in body, endpoint
        assert f"<code>{method}</code>" in body, endpoint
        assert 'class="api-param-table"' in body, endpoint
        assert 'class="api-response-table"' in body, endpoint
        assert 'class="api-error-table"' in body, endpoint
        assert "curl" in body, endpoint
        assert body.count("<tr>") >= 8, endpoint
        assert len(plain_body.strip()) >= 280, endpoint


def test_third_party_api_docs_sidebar_links_to_each_endpoint():
    """Catch the nav tree only switching chapters and not indexing APIs."""
    text = _public_doc_text()
    sidebar = _sidebar_text(text)

    assert 'class="doc-nav-children"' in sidebar
    assert 'class="doc-nav-child"' in sidebar
    assert "resolvePanelId" in text
    assert "scrollIntoView" in text

    for endpoint in THIRD_PARTY_ENDPOINTS_WITH_DETAIL:
        endpoint_id = _endpoint_article_id(text, endpoint)
        assert f'href="#{endpoint_id}"' in sidebar, endpoint


def test_third_party_api_docs_use_readable_reference_layout_not_tile_blocks():
    """Catch the API reference regressing into repeated six-cell tile grids."""
    text = _public_doc_text()

    assert 'class="endpoint-meta"' not in text
    assert 'class="endpoint-field"' not in text
    assert 'class="api-doc"' in text
    assert 'class="api-doc-section api-basics"' in text
    assert 'class="api-basics-list"' in text
    assert 'class="api-param-table"' in text
    assert 'class="api-response-table"' in text
    assert 'class="api-error-table"' in text
    assert 'class="api-code-samples"' in text


def test_third_party_api_docs_endpoint_layout_prevents_narrow_overflow():
    """Catch long endpoint paths forcing page-level horizontal scrolling."""
    text = _public_doc_text()
    endpoint_list_rule = _css_rule(text, ".endpoint-list")
    endpoint_detail_rule = _css_rule(text, ".endpoint-detail")
    endpoint_heading_rule = _css_rule(text, ".endpoint-detail h3")

    assert "min-width: 0" in endpoint_list_rule
    assert "min-width: 0" in endpoint_detail_rule
    assert "max-width: 100%" in endpoint_detail_rule
    assert "overflow-wrap: anywhere" in endpoint_heading_rule


def test_third_party_object_upload_checklist_does_not_use_readable_pseudo_marks():
    text = _public_doc_text()

    assert 'content: "✓"' not in text
    assert 'class="check-icon" aria-hidden="true">✓</span>' in text


def test_third_party_object_upload_doc_root_layout_is_full_width():
    """Catch regressions that reintroduce a desktop-width cap on the page."""
    text = _public_doc_text()
    main_rule = _css_rule(text, "main")

    assert re.search(r"\bwidth:\s*100%;", main_rule)
    assert re.search(r"\bmax-width:\s*none;", main_rule)
    assert "width: min(1180px" not in main_rule
    assert "margin: 0 auto" not in main_rule


def test_third_party_api_docs_shell_keeps_full_width_content():
    """Catch the documentation shell shrinking into a narrow single-column page."""
    text = _public_doc_text()
    shell_rule = _css_rule(text, ".docs-shell")
    sidebar_rule = _css_rule(text, ".docs-sidebar")
    content_rule = _css_rule(text, ".doc-content")

    assert "grid-template-columns: minmax(260px, 360px) minmax(0, 1fr)" in shell_rule
    assert "position: sticky" in sidebar_rule
    assert "min-width: 0" in content_rule


def test_third_party_object_upload_doc_grids_adapt_to_narrow_viewports():
    """Catch regressions that force cards wider than the viewport content area."""
    text = _public_doc_text()

    assert "minmax(min(240px, 100%), 1fr)" in _css_rule(text, ".step-grid")
    assert "minmax(min(300px, 100%), 1fr)" in _css_rule(text, ".two-col")


def test_third_party_object_upload_sequence_figure_uses_available_width():
    """Catch the browser's default figure margins shrinking the diagram."""
    text = _public_doc_text()

    assert re.search(r"\bmargin:\s*0;", _css_rule(text, ".diagram-wrap"))


def test_third_party_object_upload_sequence_diagram_uses_available_width():
    """Catch regressions that collapse the diagram into a capped desktop island."""
    text = _public_doc_text()
    sequence_rule = _css_rule(text, ".sequence-diagram")
    viewbox_width = _sequence_viewbox_width(text)

    assert re.search(r"\bwidth:\s*100%;", sequence_rule)
    assert re.search(rf"\bmin-width:\s*{viewbox_width}px;", sequence_rule)
    assert "max-width:" not in sequence_rule
    assert "margin: 0 auto" not in sequence_rule


def test_third_party_object_upload_sequence_diagram_has_wide_lane_canvas():
    """Catch a wide SVG being simulated by scaling a cramped 1180px canvas."""
    text = _public_doc_text()
    viewbox_width = _sequence_viewbox_width(text)
    participant_centers = _participant_center_xs(text)
    lane_gaps = [
        right - left
        for left, right in zip(participant_centers, participant_centers[1:])
    ]

    assert viewbox_width >= 1500
    assert min(lane_gaps) >= 300
    assert 'width="1124"' not in text


def test_third_party_object_upload_sequence_diagram_uses_compact_typography():
    """Catch diagram text growing back to the oversized first version."""
    text = _public_doc_text()
    expected_ranges = {
        ".seq-title": (17.5, 18.5),
        ".seq-subtitle": (10.5, 11.0),
        ".participant-label": (11.5, 12.0),
        ".participant-sub": (9.0, 9.5),
        ".segment-label": (9.0, 9.5),
        ".msg-label": (10.0, 10.5),
        ".msg-note": (7.5, 8.0),
        ".callout-title": (10.0, 10.5),
        ".callout-text": (7.5, 8.0),
    }

    for selector, (minimum, maximum) in expected_ranges.items():
        size = _font_size_px(_css_rule(text, selector))
        assert minimum <= size <= maximum


def test_third_party_object_upload_sequence_labels_use_halo_not_background_boxes():
    """Catch label backgrounds becoming blocks over the sequence diagram."""
    text = _public_doc_text()

    assert re.search(r"\bdisplay:\s*none;", _css_rule(text, ".label-bg"))
    message_label_rule = _css_rule(text, ".msg-label, .msg-note")
    assert "paint-order: stroke fill" in message_label_rule
    assert "stroke: var(--panel)" in message_label_rule


def test_third_party_object_upload_doc_does_not_publish_secret_material():
    public_texts = [
        path.read_text(encoding="utf-8")
        for path in PUBLIC_OBJECT_UPLOAD_DOC_DIR.rglob("*")
        if path.is_file()
    ]
    assert public_texts

    for text in public_texts:
        for token in FORBIDDEN_PUBLIC_DOC_TOKENS:
            assert token not in text

    text = _public_doc_text()
    assert "https://&lt;lightrag-host&gt;" in text
    assert "&lt;your-lightrag-api-key&gt;" in text
