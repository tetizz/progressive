"""Replay-only publication contract; no engine imports or searches."""
from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest

from scripts import validate_archive_delivery as delivery

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def candidate(tmp_path):
    shutil.copytree(ROOT / delivery.ARCHIVE, tmp_path / delivery.ARCHIVE)
    shutil.copyfile(ROOT / delivery.MANIFEST, tmp_path / delivery.MANIFEST)
    return tmp_path


def test_exact_original_archive_is_accepted():
    manifest = delivery.validate_payload(ROOT)
    assert manifest["anchor_revision"] == delivery.ANCHOR


@pytest.mark.parametrize("name", ["report.json", "protocol.json", "match-viewer.js", "styles.css"])
def test_any_byte_change_is_rejected(candidate, name):
    path = candidate / delivery.ARCHIVE / name
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="raw bytes"):
        delivery.validate_payload(candidate)


def test_extra_executable_is_rejected(candidate):
    (candidate / delivery.ARCHIVE / "engine.wasm").write_bytes(b"not authorized")
    with pytest.raises(ValueError, match="inventory"):
        delivery.validate_payload(candidate)


@pytest.mark.parametrize("field,value", [("anchor_revision", "0" * 40), ("archive_path", "../escape")])
def test_wrong_anchor_or_traversal_rejected(candidate, field, value):
    manifest = json.loads((candidate / delivery.MANIFEST).read_bytes())
    manifest[field] = value
    (candidate / delivery.MANIFEST).write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        delivery.validate_payload(candidate)


def test_packaging_preserves_entire_anchor_root(candidate, tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_bytes(b"exact old root")
    (site / "engine").mkdir()
    (site / "engine/kernel.wasm").write_bytes(b"exact certified engine")
    proof = tmp_path / "proof.json"
    proof.write_text(json.dumps({"artifact_revision": delivery.ANCHOR, "status": "validated", "product_publishable": True, "release_id": "spc-browser-wasm-release-6ee01f71d6a6fc3f"}))
    before = delivery.inventory(site)
    attestation = tmp_path / "attestation.json"
    delivery.overlay(candidate, site, attestation, "a" * 40, proof)
    record = json.loads(attestation.read_bytes())
    assert record["root_before"] == record["root_after"] == before
    assert record["candidate_revision"] == "a" * 40
    assert (site / "engine/kernel.wasm").read_bytes() == b"exact certified engine"


def test_wrong_anchor_validation_rejected(candidate, tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    proof = tmp_path / "proof.json"
    proof.write_text(json.dumps({"artifact_revision": "0" * 40}))
    with pytest.raises(ValueError, match="engine validation"):
        delivery.overlay(candidate, site, tmp_path / "attestation.json", "a" * 40, proof)


def test_symlink_rejected(candidate, tmp_path):
    link = candidate / delivery.ARCHIVE / "unexpected-link"
    try:
        link.symlink_to(tmp_path)
    except OSError:
        pytest.skip("symlink privilege unavailable")
    with pytest.raises(ValueError, match="symlink"):
        delivery.validate_payload(candidate)


@pytest.mark.parametrize("path", [
    "src/scottish_progressive/search.py", "release/browser-wasm/release-receipt.json",
    "src/scottish_progressive/web/static/app.js", "src/scottish_progressive/web/static/styles.css",
    "archives/actual-100games-20261015/engine.wasm", "../escape",
])
def test_non_archive_change_rejected(path):
    with pytest.raises(ValueError, match="non-archive"):
        delivery.validate_changes({path: "M"})


def test_exact_allowed_change_set():
    delivery.validate_changes({path: "A" for path in delivery.SUPPORT | {f"{delivery.ARCHIVE}/{name}" for name in delivery.ASSETS}})


def test_deletion_rejected():
    with pytest.raises(ValueError, match="deletion"):
        delivery.validate_changes({delivery.MANIFEST: "D"})


def test_workflow_uses_actual_anchor_checkout_and_scoped_environment():
    workflow = (ROOT / ".github/workflows/pages.yml").read_text()
    assert f"ref: {delivery.ANCHOR}" in workflow
    assert f"GITHUB_SHA={delivery.ANCHOR} python scripts/validate_promoted_browser_wasm_release.py" in workflow
    assert "working-directory: anchor" in workflow
    assert '--candidate-sha "$GITHUB_SHA"' in workflow


def test_marker_present_does_not_hijack_future_engine_promotion():
    assert delivery.delivery_mode({delivery.MANIFEST: "A"}, True) == "archive"
    assert delivery.delivery_mode({"release/browser-wasm/release-receipt.json": "M"}, True) == "promoted"
    assert delivery.delivery_mode({"src/scottish_progressive/search.py": "M"}, True) == "promoted"
    assert delivery.delivery_mode({delivery.MANIFEST: "A"}, False) == "promoted"


@pytest.mark.parametrize("name", [delivery.MANIFEST, ".gitattributes", "scripts/validate_archive_delivery.py"])
def test_support_symlinks_rejected(tmp_path, name):
    target = tmp_path / "real-file"
    target.write_bytes(b"payload")
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip("symlink privilege unavailable")
    with pytest.raises(ValueError, match="symlink"):
        delivery.regular_path(tmp_path, name)


def test_support_symlink_parent_rejected(tmp_path):
    target = tmp_path / "real-directory"
    target.mkdir()
    (target / "x.py").write_bytes(b"payload")
    try:
        (tmp_path / "scripts").symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink privilege unavailable")
    with pytest.raises(ValueError, match="symlink"):
        delivery.regular_path(tmp_path, "scripts/x.py")


def test_current_promoted_engine_can_preserve_historical_archive(candidate, tmp_path):
    site = tmp_path / "new-engine-site"
    site.mkdir()
    (site / "index.html").write_bytes(b"new independently promoted root")
    proof = tmp_path / "new-engine-proof.json"
    proof.write_text(json.dumps({"artifact_revision": "b" * 40, "status": "validated", "product_publishable": True, "release_id": "new-engine-release"}))
    attestation = tmp_path / "preservation.json"
    delivery.overlay(candidate, site, attestation, "b" * 40, proof, preserve_current_engine=True)
    record = json.loads(attestation.read_bytes())
    assert record["engine_authority_revision"] == "b" * 40
    assert record["historical_archive_anchor_revision"] == delivery.ANCHOR
    assert record["root_before"] == record["root_after"]


@pytest.mark.parametrize("mode", ["120000", "160000", "040000"])
def test_support_nonregular_git_mode_rejected(mode):
    tracked = {name: "100644" for name in delivery.SUPPORT | {f"{delivery.ARCHIVE}/{name}" for name in delivery.ASSETS}}
    tracked[delivery.MANIFEST] = mode
    with pytest.raises(ValueError, match="Git mode"):
        delivery.validate_modes(tracked)


def test_directory_instead_of_support_file_rejected(tmp_path):
    (tmp_path / ".gitattributes").mkdir()
    with pytest.raises(ValueError, match="nonregular"):
        delivery.regular_path(tmp_path, ".gitattributes")
