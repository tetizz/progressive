"""Add one immutable replay archive to an independently validated Pages anchor.

This does not authorize engine promotion. The existing engine authority must run
unchanged against its own clean anchor checkout before this overlay is built.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess

ANCHOR = "f10f78bd2ee055b15194f104102ba03639f0d504"
ARCHIVE = "archives/actual-100games-20261015"
MANIFEST = "archive-delivery.json"
DATA_SHA = "906971ecf89916b8e946b5770e876a306b755333e6a30d5ad2050f48db810472"
REPORT_SHA = "f6c01cd5f9d3952515d59decce1ef3c73e78ce00bce6b409eb3b3af441ad9594"
PROTOCOL_SHA = "4138918fafc3df6f8a13e13a77d93675c1cfc5ce1e1c12d38788d684c09f9e8b"
SCOPE = "99 wins and 1 local technical incomplete; 49 complete pairs and 1 incomplete; four played histories; repeated initial positions are not independent superiority evidence; replay only"
SUPPORT = {
    MANIFEST, ".gitattributes", ".github/workflows/pages.yml",
    "scripts/validate_archive_delivery.py", "tests/test_archive_delivery.py",
}
ASSETS = {
    "matches.html", "match-viewer.js", "board-renderer.js", "styles.css",
    "report.json", "protocol.json", "matches/match-viewer-manifest.json",
    f"matches/match.{DATA_SHA}.json",
    *(f"pieces/cburnett/{color}{piece}.svg" for color in "bw" for piece in "BKNPQR"),
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def git(repository: Path, *arguments: str) -> str:
    return subprocess.check_output(["git", "-C", str(repository), *arguments], text=True).strip()


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def regular_path(root: Path, relative: str) -> Path:
    parts = PurePosixPath(relative)
    require(not parts.is_absolute() and parts.as_posix() == relative and ".." not in parts.parts and "\\" not in relative, "unsafe path")
    path = root
    for part in ("", *parts.parts):
        path = path / part if part else path
        require(not path.is_symlink() and not getattr(path, "is_junction", lambda: False)(), f"symlink/junction forbidden: {path}")
        mode = path.lstat().st_mode
        require(stat.S_ISDIR(mode) if path != root / relative else stat.S_ISREG(mode), f"nonregular support/archive path: {path}")
    return path


def inventory(root: Path) -> dict[str, dict[str, object]]:
    require(root.is_dir() and not root.is_symlink(), f"unsafe directory: {root}")
    result = {}
    for path in root.rglob("*"):
        require(not path.is_symlink() and not getattr(path, "is_junction", lambda: False)(), f"symlink/junction forbidden: {path}")
        require(path.is_dir() or path.is_file(), f"special path forbidden: {path}")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = {"bytes": path.stat().st_size, "sha256": digest(path)}
    return result


def validate_payload(repository: Path) -> dict:
    manifest = json.loads(regular_path(repository, MANIFEST).read_bytes())
    require(set(manifest) == {"schema", "anchor_revision", "anchor_release_id", "archive_path", "files", "scope"}, "unexpected delivery manifest fields")
    require(manifest["schema"] == "spc-static-archive-delivery-v1", "wrong schema")
    require(manifest["anchor_revision"] == ANCHOR and manifest["archive_path"] == ARCHIVE, "anchor/path mismatch")
    require(manifest["anchor_release_id"] == "spc-browser-wasm-release-6ee01f71d6a6fc3f", "release mismatch")
    require(manifest["scope"] == SCOPE, "archive claim scope changed")
    require(set(manifest["files"]) == ASSETS, "archive inventory not exact")
    for name in manifest["files"]:
        require(PurePosixPath(name).as_posix() == name and not PurePosixPath(name).is_absolute() and ".." not in PurePosixPath(name).parts and "\\" not in name, "unsafe archive path")
        regular_path(repository, f"{ARCHIVE}/{name}")
    require(inventory(repository / ARCHIVE) == manifest["files"], "archive raw bytes/inventory mismatch")
    root = repository / ARCHIVE
    require(digest(root / "report.json") == REPORT_SHA, "wrong original report")
    require(digest(root / "protocol.json") == PROTOCOL_SHA, "wrong protocol")
    require(digest(root / f"matches/match.{DATA_SHA}.json") == DATA_SHA, "wrong exact replay")
    replay_manifest = json.loads((root / "matches/match-viewer-manifest.json").read_bytes())
    require(replay_manifest["data_sha256"] == DATA_SHA and replay_manifest["receipt_sha256"] == REPORT_SHA, "wrong replay provenance")
    require(replay_manifest["data_file"] == f"match.{DATA_SHA}.json", "unsafe replay filename")
    data = json.loads((root / f"matches/match.{DATA_SHA}.json").read_bytes())
    summary = data["summary"]
    require(summary["scheduled_games"] == 100 and summary["completed_games"] == 99 and summary["incomplete_games"] == 1, "wrong game denominator")
    require(summary["completed_pairs"] == 49 and summary["incomplete_pairs"] == 1 and not summary["strict_protocol_complete"], "wrong pair/completion claim")
    require(data["archive"]["distinct_played_histories"] == 4, "wrong history count")
    last = data["games"][99]
    require(len(data["games"]) == 100 and len(last["frames"]) == 2 and last["frames"][1]["uci"] == "e2e3" and last["result"] == "*", "unplayed Game100 reply or wrong result")
    return manifest


def validate_candidate(repository: Path, anchor: Path, candidate_sha: str) -> dict:
    for name in SUPPORT | {f"{ARCHIVE}/{name}" for name in ASSETS}:
        regular_path(repository, name)
    require(git(repository, "rev-parse", "HEAD") == candidate_sha, "candidate SHA mismatch")
    require(not git(repository, "status", "--porcelain=v1", "--untracked-files=all"), "dirty candidate")
    require(git(anchor, "rev-parse", "HEAD") == ANCHOR, "wrong anchor checkout")
    require(not git(anchor, "status", "--porcelain=v1", "--untracked-files=all"), "dirty anchor")
    manifest = validate_payload(repository)
    raw = subprocess.check_output(["git", "-C", str(repository), "diff", "--name-status", "--no-renames", "-z", ANCHOR, candidate_sha])
    parts = raw.decode().split("\0")[:-1]
    require(len(parts) % 2 == 0, "invalid diff")
    changes = dict(zip(parts[1::2], parts[0::2]))
    validate_changes(changes)
    modes = subprocess.check_output(["git", "-C", str(repository), "ls-tree", "-r", "-z", candidate_sha]).decode().split("\0")
    tracked = {}
    for record in filter(None, modes):
        metadata, name = record.split("\t", 1)
        tracked[name] = metadata.split()[0]
    validate_modes(tracked)
    subprocess.check_call(["git", "-C", str(repository), "merge-base", "--is-ancestor", ANCHOR, candidate_sha])
    require(git(repository, "rev-parse", f"{ANCHOR}:release/browser-wasm") == git(repository, "rev-parse", f"{candidate_sha}:release/browser-wasm"), "release drift")
    expected_attributes = (anchor / ".gitattributes").read_bytes().replace(b"\r\n", b"\n").rstrip(b"\n") + b"\narchive-delivery.json -text -diff\narchives/actual-100games-20261015/** -text -diff\n"
    require((repository / ".gitattributes").read_bytes().replace(b"\r\n", b"\n") == expected_attributes, "unrelated attributes change")
    return manifest


def validate_changes(changes: dict[str, str]) -> None:
    allowed = SUPPORT | {f"{ARCHIVE}/{name}" for name in ASSETS}
    require(bool(changes) and set(changes) <= allowed, "non-archive/engine change")
    require(all(status in {"A", "M"} for status in changes.values()), "deletion/rename forbidden")


def validate_modes(tracked: dict[str, str]) -> None:
    require(all(tracked.get(name) == "100644" for name in SUPPORT | {f"{ARCHIVE}/{name}" for name in ASSETS}), "nonregular Git mode in delivered/support file")


def delivery_mode(changes: dict[str, str], marker_present: bool) -> str:
    """Only exact archive-only changes use the old engine anchor authority.

    A later real engine promotion follows its ordinary authority, even when the
    historical replay marker remains present. That authority must independently
    accept the new artifact before its static historical archive is copied.
    """
    allowed = SUPPORT | {f"{ARCHIVE}/{name}" for name in ASSETS}
    return "archive" if marker_present and changes and set(changes) <= allowed and all(status in {"A", "M"} for status in changes.values()) else "promoted"


def overlay(repository: Path, site: Path, output_attestation: Path, candidate_sha: str, anchor_validation: Path, *, preserve_current_engine: bool = False) -> None:
    manifest = validate_payload(repository)
    proof = json.loads(anchor_validation.read_bytes())
    expected_revision = candidate_sha if preserve_current_engine else ANCHOR
    require(proof.get("artifact_revision") == expected_revision and proof.get("status") == "validated" and proof.get("product_publishable") is True, "engine validation not authoritative")
    if not preserve_current_engine:
        require(proof.get("release_id") == manifest["anchor_release_id"], "anchor validation not authoritative")
    before = inventory(site)
    destination = site / ARCHIVE
    require(not destination.exists(), "archive collision")
    shutil.copytree(repository / ARCHIVE, destination)
    after = inventory(site)
    outside = {name: record for name, record in after.items() if not name.startswith(ARCHIVE + "/")}
    require(before == outside, "root artifact changed outside archive")
    require(inventory(destination) == manifest["files"], "packaged archive mismatch")
    attestation = {
        "schema": "spc-static-archive-ci-attestation-v1", "candidate_revision": candidate_sha,
        "historical_archive_anchor_revision": ANCHOR,
        "engine_authority_revision": expected_revision,
        "engine_validation_sha256": digest(anchor_validation),
        "delivery_manifest_sha256": digest(repository / MANIFEST),
        "validator_sha256": digest(Path(__file__)), "root_before": before,
        "root_after": outside, "archive": manifest["files"],
        "scope": "historical replay copied only after separate engine authority; root bytes unchanged by overlay; hosted behavior not yet verified",
    }
    output_attestation.write_text(json.dumps(attestation, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--anchor", type=Path)
    parser.add_argument("--mode", choices=("classify", "archive", "preserve"), default="archive")
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--site", type=Path)
    parser.add_argument("--anchor-validation", type=Path)
    parser.add_argument("--attestation", type=Path)
    args = parser.parse_args()
    if args.mode == "classify":
        raw = subprocess.check_output(["git", "-C", str(args.repository), "diff", "--name-status", "--no-renames", "-z", ANCHOR, args.candidate_sha]).decode().split("\0")[:-1]
        require(len(raw) % 2 == 0, "invalid classification diff")
        print(delivery_mode(dict(zip(raw[1::2], raw[0::2])), (args.repository / MANIFEST).exists()))
        return
    if args.mode == "archive":
        require(args.anchor is not None, "missing actual anchor checkout")
        validate_candidate(args.repository, args.anchor, args.candidate_sha)
    else:
        require(git(args.repository, "rev-parse", "HEAD") == args.candidate_sha, "candidate SHA mismatch")
        for name in SUPPORT:
            regular_path(args.repository, name)
        validate_payload(args.repository)
    if args.site:
        require(args.anchor_validation is not None and args.attestation is not None, "missing packaging evidence")
        overlay(args.repository, args.site, args.attestation, args.candidate_sha, args.anchor_validation, preserve_current_engine=args.mode == "preserve")
    print("Validated additive replay delivery; no new engine promotion")


if __name__ == "__main__":
    main()
