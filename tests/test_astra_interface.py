from __future__ import annotations

from collections import Counter
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "scottish_progressive" / "web" / "static"


class _IdCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        del tag
        self.ids.extend(value for name, value in attrs if name == "id" and value)


def test_redesigned_pages_keep_unique_dom_ids() -> None:
    for name in ("index.html", "matches.html"):
        parser = _IdCollector()
        parser.feed((STATIC / name).read_text(encoding="utf-8"))
        duplicates = [
            element_id
            for element_id, count in Counter(parser.ids).items()
            if count > 1
        ]
        assert duplicates == [], f"duplicate IDs in {name}: {duplicates}"


def test_play_surface_explains_the_variant_and_engine_evidence() -> None:
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    matches = (STATIC / "matches.html").read_text(encoding="utf-8")

    assert 'id="series-runway"' in index
    assert index.count('data-series-step="') == 4
    assert 'aria-current="step"' in index
    assert '<details class="engine-proof-details">' in index
    assert '<dl class="engine-proof" aria-label="Engine provenance">' in index
    assert 'id="engine-status" role="status" aria-live="polite" aria-atomic="true"' in index
    assert 'id="analysis-error" hidden role="alert" aria-atomic="true"' in index
    assert 'aria-describedby="saved-dialog-description"' in index
    assert 'id="undo-move" type="button" aria-label="Undo move"' in index
    assert 'id="analyze-button" type="button" aria-label="Pause automatic analysis"' in index
    assert 'id="replay-start" type="button" aria-label="Jump to match start"' in matches


def test_workspace_mode_is_reloadable_and_analysis_errors_are_retryable() -> None:
    app = (STATIC / "app.js").read_text(encoding="utf-8")

    assert 'function syncWorkspaceRoute(mode)' in app
    assert 'url.searchParams.set("workspace", "analyze")' in app
    assert 'window.history.replaceState(null, "", next)' in app
    assert 'syncWorkspaceRoute("analyze")' in app
    assert 'syncWorkspaceRoute("play")' in app
    assert 'failed ? "Retry" : "Pause"' in app
    assert 'if (!dom.analysis_error.hidden && !state.analysisPaused)' in app
    assert 'setAttribute("aria-label", `${action} automatic analysis`)' in app


def test_busy_boundary_repaints_interaction_state_after_async_work() -> None:
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    start = app.index("function setBoardBusy")
    end = app.index("function analysisPositionKey", start)
    busy_boundary = app[start:end]

    assert "renderBoard();" in busy_boundary
    assert "renderPlaySurface();" in busy_boundary


def test_drag_updates_do_not_rebuild_the_board_per_pointer_frame() -> None:
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    styles = (STATIC / "styles.css").read_text(encoding="utf-8")
    start = app.index("function onPointerMove")
    end = app.index("function onPointerUp", start)
    handler = app[start:end]

    assert handler.count("renderBoard();") == 1
    assert "if (!state.drag.moved)" in handler
    assert 'style.setProperty("--drag-x"' in handler
    assert 'style.setProperty("--drag-y"' in handler
    assert "translate3d(var(--drag-x" in styles
    assert ".drag-piece.is-visible { will-change: transform; }" in styles


def test_large_study_tree_uses_one_child_index_per_render() -> None:
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    start = app.index("function appendTreeBranch")
    end = app.index("function pathToTreeNode", start)
    render_block = app[start:end]

    assert "function indexedTreeChildren()" in app
    assert "treeChildren(parentId)" not in render_block
    assert "childIndex.get(parentId)" in render_block
    assert "appendTreeBranch(null, group, 2, indexedTreeChildren())" in render_block


def test_match_replay_cache_key_is_bound_to_verified_content() -> None:
    viewer = (STATIC / "match-viewer.js").read_text(encoding="utf-8")
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")

    assert 'dataUrl.searchParams.set("sha256", manifest.data_sha256)' in viewer
    assert "src/scottish_progressive/web/static/matches/*.json text eol=lf" in attributes


def test_visual_system_is_local_responsive_and_motion_safe() -> None:
    styles = (STATIC / "styles.css").read_text(encoding="utf-8")

    assert '"Segoe UI Variable Text"' in styles
    assert "@keyframes workspace-in" in styles
    assert "@media (prefers-reduced-motion: reduce)" in styles
    assert "animation-duration: .001ms !important" in styles
    assert "calc(100vw - 28px)" in styles
    assert "calc(100vw - 24px)" in styles
    assert "url(" not in styles
