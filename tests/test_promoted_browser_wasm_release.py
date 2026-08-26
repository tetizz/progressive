from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from scripts import validate_promoted_browser_wasm_release as validator


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _record(path: Path, relative: str) -> dict[str, object]:
    return {"path": relative, "sha256": _sha256(path), "bytes": path.stat().st_size}


def _build_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    prior_release: bool = False,
    mock_semantics: bool = True,
) -> dict[str, object]:
    repository = tmp_path / "repository"
    source_package = repository / "src" / "scottish_progressive"
    source_package.mkdir(parents=True)
    (source_package / "__init__.py").write_text("# fixture source\n", encoding="utf-8")
    if prior_release:
        old_release = repository / "release" / "browser-wasm"
        old_release.mkdir(parents=True)
        prior_files = {
            "release-receipt.json",
            *(path for _label, path, _schema in validator.EVIDENCE_SPECS),
            *(spec[0] for spec in validator.CERTIFICATE_SPECS.values()),
            *validator.BROWSER_FILES,
        }
        for relative in prior_files:
            path = old_release / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"prior release: {relative}\n".encode("utf-8"))
        (old_release / "obsolete.txt").write_text("old release\n", encoding="utf-8")
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "tetizz")
    _git(repository, "config", "user.email", "tetizz@users.noreply.github.com")
    _git(repository, "add", ".")
    _git(repository, "commit", "-q", "-m", "certified source")
    source_revision = _git(repository, "rev-parse", "HEAD")

    release = repository / "release" / "browser-wasm"
    if prior_release:
        shutil.rmtree(release)
    release.mkdir(parents=True, exist_ok=True)
    browser = release / "browser-engine"
    (browser / "single").mkdir(parents=True)
    module = browser / "single" / "spc-engine.js"
    wasm = browser / "single" / "spc-root-session.wasm"
    module.write_text("export default async function engine() {}\n", encoding="utf-8")
    wasm.write_bytes(b"\0asm\x01\0\0\0fixture")

    artifact = {
        "source_revision": source_revision,
        "source_fingerprint": "1234567890abcdef",
        "kernel_sha256": "1" * 64,
        "wasm_sha256": _sha256(wasm),
        "module_js_sha256": _sha256(module),
        "artifact_set_sha256": "2" * 64,
        "exception_strategy": "emscripten",
        "wasm_simd": False,
        "allocator": "dlmalloc",
        "runtime_variant": "single",
        "thread_count": 1,
    }
    policy = {
        "maximum_seconds": 60,
        "default_seconds": 30,
        "default_generation_positions": 100_000_000,
        "safety_reserve_positions": 1_000_000,
    }
    certificate_ids = {
        "prefix": "spc-prefix-1111111111111111",
        "root_session": "spc-root-session-2222222222222222",
        "mate": "spc-mate-3333333333333333",
    }
    common_certificate = {
        "status": "certified",
        "certificate_id": "",
        "source_fingerprint": artifact["source_fingerprint"],
        "wasm_sha256": artifact["wasm_sha256"],
        "module_js_sha256": artifact["module_js_sha256"],
    }
    certificate_payloads = {
        "prefix": {**common_certificate, "certificate_id": certificate_ids["prefix"]},
        "root_session": {
            **common_certificate,
            "schema": validator.bundle_builder.ROOT_SESSION_CERTIFICATE_SCHEMA,
            "certificate_id": certificate_ids["root_session"],
            "geometry": {"play_limits": policy},
        },
        "mate": {
            **common_certificate,
            "schema": validator.bundle_builder.MATE_CERTIFICATE_SCHEMA,
            "certificate_id": certificate_ids["mate"],
        },
    }
    certificate_paths = {
        label: release / validator.CERTIFICATE_SPECS[label][0]
        for label in validator.CERTIFICATE_SPECS
    }
    for label, payload in certificate_payloads.items():
        _write_json(certificate_paths[label], payload)
    certificate_records = {
        label: {
            "certificate_id": certificate_ids[label],
            "path": validator.CERTIFICATE_SPECS[label][0],
            "sha256": _sha256(certificate_paths[label]),
        }
        for label in validator.CERTIFICATE_SPECS
    }
    certificate_directory_records = sorted(
        (
            _record(
                certificate_paths[label],
                certificate_paths[label].relative_to(release / "certificates").as_posix(),
            )
            for label in validator.CERTIFICATE_SPECS
        ),
        key=lambda item: str(item["path"]),
    )
    certificate_set_sha256 = _canonical_sha256(certificate_directory_records)

    manifest = {
        "schema": validator.bundle_builder.MANIFEST_SCHEMA,
        "source_fingerprint": artifact["source_fingerprint"],
        "variants": {
            "single": {
                "thread_count": 1,
                "wasm": "spc-root-session.wasm",
                "module_js": "spc-engine.js",
                "wasm_sha256": artifact["wasm_sha256"],
                "module_js_sha256": artifact["module_js_sha256"],
                "kernel_sha256": artifact["kernel_sha256"],
                "support_files": [],
                "prefix_certificate": {"certificate_id": certificate_ids["prefix"]},
                "root_session_certificate": {
                    "certificate_id": certificate_ids["root_session"],
                    "exception_strategy": artifact["exception_strategy"],
                    "wasm_simd": artifact["wasm_simd"],
                    "allocator": artifact["allocator"],
                },
                "mate_certificate": {"certificate_id": certificate_ids["mate"]},
            }
        },
    }
    manifest_path = browser / "browser-engine-manifest.json"
    _write_json(manifest_path, manifest)
    browser_records = [
        _record(browser / relative, relative) for relative in validator.BROWSER_RECORD_PATHS
    ]
    browser_set_sha256 = _canonical_sha256(browser_records)

    toolchain = {"path": "C:/fixture/em++.exe", "sha256": "3" * 64, "version": "fixture"}
    command = ["C:/fixture/em++.exe", "-O3", "-flto"]
    evidence_records: list[dict[str, object]] = []
    for label, relative, schema in validator.EVIDENCE_SPECS:
        payload: dict[str, object] = {"schema": schema}
        if label == "build":
            payload.update({field: artifact[field] for field in validator.ARTIFACT_IDENTITY_FIELDS})
            payload["toolchain"] = toolchain
            payload["command"] = command
        path = release / relative
        _write_json(path, payload)
        evidence_records.append(
            {
                "label": label,
                "path": relative,
                "schema": schema,
                "sha256": _sha256(path),
                "bytes": path.stat().st_size,
            }
        )

    dependency_closure = {
        "schema": "spc-wasm-dependency-closure-v2",
        "target": "ordinary-worker-root-session-prefix-mate",
        "ok": True,
        "source_revision": source_revision,
        "required": ["src/scottish_progressive/native_subtree.cpp"],
        "missing_from_worktree": [],
        "missing_from_clean_checkout": [],
    }
    seed = {
        "artifact": {field: artifact[field] for field in validator.ARTIFACT_IDENTITY_FIELDS},
        "bundle_set_sha256": browser_set_sha256,
        "certificate_set_sha256": certificate_set_sha256,
        "receipts": [
            {"label": record["label"], "sha256": record["sha256"]}
            for record in evidence_records
        ],
        "policy": {"maximum_seconds": 60, "default_seconds": 30},
    }
    receipt = {
        "schema": validator.RELEASE_SCHEMA,
        "status": "promoted",
        "product_publishable": True,
        "release_id": f"spc-browser-wasm-release-{_canonical_sha256(seed)[:16]}",
        "authorization": {
            "authorized_by": "tetizz",
            "transition": "verified-combined-wasm-to-pages-ready",
            "mechanism": "explicit-command-line",
        },
        "source_revision": source_revision,
        "artifact": artifact,
        "toolchain": toolchain,
        "build_command_sha256": _canonical_sha256(command),
        "dependency_closure": dependency_closure,
        "root_tactical_policy": {
            "capability": True,
            "policy": "canonical-boundary-policy-v1",
            "legacy_wire_root_tactical_protection": False,
        },
        "certificates": certificate_records,
        "evidence_receipts": evidence_records,
        "browser_bundle": {
            "path": "browser-engine",
            "files": browser_records,
            "artifact_set_sha256": browser_set_sha256,
        },
        "certificate_set_sha256": certificate_set_sha256,
        "promotion_policy": policy,
        "measured": {
            "root_d5_oracle_signature_sha256": "4" * 64,
            "opera_total_d1_through_d5_seconds": 9.5,
            "completed_depth": 5,
            "width": 32,
            "workers": 8,
            "initial_full_wave": 8,
            "result": {},
            "memory": {},
            "opera_checked_horizon": {
                "elapsed_seconds": 8.5,
                "work": 75_000_000,
                "selected_root_series": "f2f3",
                "pv_horizon_line_rejections": 2,
                "pv_horizon_native_repairs": 2,
                "pv_horizon_candidate_vetoes": 0,
                "local_checkout_asset_set_sha256": "5" * 64,
            },
        },
        "gates": {label: True for label in validator.EXPECTED_GATES},
    }
    receipt_path = release / "release-receipt.json"
    _write_json(receipt_path, receipt)

    for static_relative, promoted_relative in validator.STATIC_MIRRORS.items():
        static = repository / static_relative
        static.parent.mkdir(parents=True, exist_ok=True)
        static.write_bytes((release / promoted_relative).read_bytes())

    _git(repository, "add", ".")
    _git(repository, "commit", "-q", "-m", "promote browser wasm release")
    artifact_revision = _git(repository, "rev-parse", "HEAD")

    calls: list[tuple[Path, Path]] = []

    def validate_existing_bundle(
        bundle_path: Path,
        package_path: Path,
    ) -> dict[str, object]:
        calls.append((bundle_path, package_path))
        return copy.deepcopy(manifest)

    monkeypatch.setattr(
        validator.bundle_builder,
        "validate_existing_bundle",
        validate_existing_bundle,
    )
    monkeypatch.setattr(
        validator.bundle_builder,
        "validate_prefix_certificate",
        lambda *args, **kwargs: ({}, {}, {}),
    )
    monkeypatch.setattr(
        validator.bundle_builder,
        "validate_root_session_certificate",
        lambda *args, **kwargs: ({}, {}, "", "", {}, {}),
    )
    monkeypatch.setattr(
        validator.bundle_builder,
        "validate_mate_certificate",
        lambda *args, **kwargs: ({}, {}, "", ""),
    )
    if mock_semantics:
        measured = receipt["measured"]
        checked = measured["opera_checked_horizon"]
        semantic = validator.SemanticEvidence(
            certificates=copy.deepcopy(certificate_payloads),
            oracle_signature_sha256=measured["root_d5_oracle_signature_sha256"],
            root_config={"max_work": policy["default_generation_positions"]},
            opera_elapsed_seconds=measured["opera_total_d1_through_d5_seconds"],
            opera_result=copy.deepcopy(measured["result"]),
            opera_memory=copy.deepcopy(measured["memory"]),
            safety_reserve_positions=policy["safety_reserve_positions"],
            checked_elapsed_seconds=checked["elapsed_seconds"],
            checked_work=checked["work"],
            checked_selected_root_series=checked["selected_root_series"],
            checked_line_rejections=checked["pv_horizon_line_rejections"],
            checked_native_repairs=checked["pv_horizon_native_repairs"],
            checked_candidate_vetoes=checked["pv_horizon_candidate_vetoes"],
            checked_local_asset_set_sha256=checked[
                "local_checkout_asset_set_sha256"
            ],
        )
        monkeypatch.setattr(
            validator,
            "_validate_semantic_evidence",
            lambda **_kwargs: semantic,
        )
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    return {
        "repository": repository,
        "source_package": source_package,
        "release": release,
        "receipt": receipt,
        "receipt_path": receipt_path,
        "source_revision": source_revision,
        "artifact_revision": artifact_revision,
        "calls": calls,
    }


def _validate(fixture: dict[str, object]) -> dict[str, object]:
    return dict(
        validator.validate_promoted_release(
            release=fixture["release"],
            repository=fixture["repository"],
            source_package=fixture["source_package"],
        )
    )


def _rewrite_receipt(fixture: dict[str, object], mutate) -> None:
    receipt = copy.deepcopy(fixture["receipt"])
    mutate(receipt)
    _write_json(fixture["receipt_path"], receipt)


def test_validates_exact_parent_bound_promoted_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _build_fixture(tmp_path, monkeypatch)

    result = _validate(fixture)

    assert result["status"] == "validated"
    assert result["product_publishable"] is True
    assert result["source_revision"] == fixture["source_revision"]
    assert result["artifact_revision"] == fixture["artifact_revision"]
    assert fixture["calls"] == [
        (fixture["release"] / "browser-engine", fixture["source_package"])
    ]


def test_accepts_replacement_release_and_release_only_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _build_fixture(tmp_path, monkeypatch, prior_release=True)

    assert _validate(fixture)["status"] == "validated"


def test_rejects_schema_only_fabricated_evidence_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # This is the exact shape the old consumer accepted: every staged receipt
    # had the expected schema/path/hash, but none carried producer-valid proof.
    fixture = _build_fixture(tmp_path, monkeypatch, mock_semantics=False)

    with pytest.raises(validator.PromotedReleaseError, match="build evidence"):
        _validate(fixture)


def test_release_tree_is_forced_binary_in_git_attributes() -> None:
    attributes = (
        Path(__file__).resolve().parents[1] / ".gitattributes"
    ).read_text(encoding="utf-8").splitlines()

    assert "release/browser-wasm/** -text -diff" in attributes


def test_rejects_backslash_git_path_aliases() -> None:
    with pytest.raises(validator.PromotedReleaseError, match="backslash"):
        validator._zero_paths(
            b"release/browser-wasm/file.json\0release\\browser-wasm\\file.json\0",
            "fixture Git paths",
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda receipt: receipt["authorization"].update(
                authorized_by="someone-else"
            ),
            "authorization",
        ),
        (lambda receipt: receipt["gates"].update(root_python_parity=False), "gate"),
        (
            lambda receipt: receipt.update(
                release_id="spc-browser-wasm-release-0000000000000000"
            ),
            "release_id",
        ),
        (lambda receipt: receipt.update(source_revision="f" * 40), "source_revision"),
        (
            lambda receipt: receipt["evidence_receipts"][0].update(
                path="../escape.json"
            ),
            "path",
        ),
        (
            lambda receipt: receipt["certificates"]["mate"].update(
                certificate_id="spc-mate-ffffffffffffffff"
            ),
            "ID",
        ),
        (
            lambda receipt: receipt["measured"].update(
                root_d5_oracle_signature_sha256="a" * 64
            ),
            "oracle signature",
        ),
        (
            lambda receipt: receipt["measured"].update(
                opera_total_d1_through_d5_seconds=8.75
            ),
            "Opera measurements",
        ),
        (
            lambda receipt: receipt["measured"].update(result={"fabricated": True}),
            "Opera measurements",
        ),
        (
            lambda receipt: receipt["promotion_policy"].update(default_seconds=29),
            "promotion_policy",
        ),
        (
            lambda receipt: receipt["measured"]["opera_checked_horizon"].update(
                pv_horizon_candidate_vetoes=1
            ),
            "independently satisfy",
        ),
    ],
)
def test_rejects_tampered_release_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation,
    message: str,
) -> None:
    fixture = _build_fixture(tmp_path, monkeypatch)
    _rewrite_receipt(fixture, mutation)

    with pytest.raises(validator.PromotedReleaseError, match=message):
        _validate(fixture)


def test_rejects_changed_evidence_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _build_fixture(tmp_path, monkeypatch)
    evidence = fixture["release"] / validator.EVIDENCE_SPECS[2][1]
    evidence.write_bytes(evidence.read_bytes() + b"\n")

    with pytest.raises(validator.PromotedReleaseError, match="digest or size"):
        _validate(fixture)


def test_rejects_extra_release_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _build_fixture(tmp_path, monkeypatch)
    (fixture["release"] / "extra.txt").write_text("not certified\n", encoding="utf-8")

    with pytest.raises(validator.PromotedReleaseError, match="inventory is not exact"):
        _validate(fixture)


def test_rejects_release_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _build_fixture(tmp_path, monkeypatch)
    release = fixture["release"]
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path == release or original_is_symlink(path),
    )

    with pytest.raises(validator.PromotedReleaseError, match="symlink"):
        _validate(fixture)


def test_rejects_non_release_change_in_artifact_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _build_fixture(tmp_path, monkeypatch)
    unrelated = fixture["repository"] / "README.md"
    unrelated.write_text("unrelated artifact-commit change\n", encoding="utf-8")
    _git(fixture["repository"], "add", "README.md")
    _git(fixture["repository"], "commit", "--amend", "--no-edit", "-q")

    with pytest.raises(validator.PromotedReleaseError, match="non-release change"):
        _validate(fixture)


def test_rejects_static_mirror_that_differs_from_promoted_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _build_fixture(tmp_path, monkeypatch)
    static = fixture["repository"] / next(iter(validator.STATIC_MIRRORS))
    static.write_bytes(static.read_bytes() + b"changed")
    _git(fixture["repository"], "add", static.relative_to(fixture["repository"]).as_posix())
    _git(fixture["repository"], "commit", "--amend", "--no-edit", "-q")

    with pytest.raises(validator.PromotedReleaseError, match="mirror differs"):
        _validate(fixture)


def test_rejects_github_sha_that_is_not_checked_out_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _build_fixture(tmp_path, monkeypatch)
    monkeypatch.setenv("GITHUB_SHA", "f" * 40)

    with pytest.raises(validator.PromotedReleaseError, match="GITHUB_SHA"):
        _validate(fixture)
