from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from scripts import validate_promoted_browser_wasm_release as validator


FIXTURE_MODULE_BYTES = b"export default async function engine() {}\n"
FIXTURE_WASM_BYTES = b"\0asm\x01\0\0\0fixture"


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
            if relative == "browser-engine/single/spc-engine.js":
                path.write_bytes(FIXTURE_MODULE_BYTES)
            elif relative == "browser-engine/single/spc-root-session.wasm":
                path.write_bytes(FIXTURE_WASM_BYTES)
            else:
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
    module.write_bytes(FIXTURE_MODULE_BYTES)
    wasm.write_bytes(FIXTURE_WASM_BYTES)

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
        "safety_reserve_positions": 4_000_000,
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
    raw_trace_attestation = {
        "schema": validator.evidence_producer.RAW_TRACE_ATTESTATION_SCHEMA,
        "horizon_safety_trace_count": 8,
        "horizon_safety_trace_sha256": "6" * 64,
        "horizon_research_trace_count": 3,
        "horizon_research_trace_sha256": "7" * 64,
    }
    selected_d5_witness = {
        "schema": "spc-opera-selected-d5-horizon-certification-v1",
        "fixture_id": validator.evidence_producer.SELECTED_D5_FIXTURE_ID,
        "selected_root_series": "b2b3",
        "candidate_identity": "spc-root-candidate-v1|fixture-b3",
        "owner_worker_id": "root-1",
        "principal_variation_sha256": "8" * 64,
        "selected_series5_semantic_sha256": "9" * 64,
        "known_adverse_series5_semantic_sha256": "a" * 64,
        "known_adverse_present": False,
        "horizon_request_sequence": 10,
        "horizon_status": "exhausted",
        "horizon_call_work_credit": 3_500_000,
        "horizon_work_used": 2_500_000,
        "root_child_request_sequence": 11,
        "root_child_status": "exhausted",
        "root_child_call_work_credit": 1_500_000,
        "root_child_work_used": 1_000_000,
        "safety_work_used": 3_500_000,
        "safety_call_work_credit": 4_000_000,
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
                "selected_root_series": "b2b3",
                "pv_horizon_line_rejections": 2,
                "pv_horizon_native_repairs": 1,
                "pv_horizon_candidate_vetoes": 1,
                "principal_variation_sha256": "8" * 64,
                "selected_fixture_id": (
                    validator.evidence_producer.SELECTED_D5_FIXTURE_ID
                ),
                "known_adverse_excluded": True,
                "selected_horizon_exhaustively_certified": True,
                "selected_root_child_exhaustively_certified": True,
                "raw_trace_attestation": raw_trace_attestation,
                "selected_d5_horizon_certification_witness": selected_d5_witness,
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
    # Most fixtures exercise the generic validator with synthetic identities.
    # The dedicated legacy-v5 regression below tests the real immutable pin.
    monkeypatch.setattr(
        validator,
        "_validate_legacy_v5_release_identity",
        lambda **_kwargs: None,
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
            checked_principal_variation_sha256=checked[
                "principal_variation_sha256"
            ],
            checked_selected_fixture_id=checked["selected_fixture_id"],
            checked_known_adverse_excluded=checked["known_adverse_excluded"],
            checked_selected_horizon_exhaustively_certified=checked[
                "selected_horizon_exhaustively_certified"
            ],
            checked_selected_root_child_exhaustively_certified=checked[
                "selected_root_child_exhaustively_certified"
            ],
            checked_raw_trace_attestation=copy.deepcopy(
                checked["raw_trace_attestation"]
            ),
            checked_selected_d5_horizon_certification_witness=copy.deepcopy(
                checked["selected_d5_horizon_certification_witness"]
            ),
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
    assert fixture["receipt"]["schema"] == "spc-browser-wasm-release-promotion-v2"
    assert fixture["receipt"]["promotion_policy"]["safety_reserve_positions"] == 4_000_000
    for gate in (
        "opera_selected_b3_known_adverse_horizon_excluded",
        "opera_selected_b3_horizon_exhaustively_certified",
        "opera_selected_b3_root_child_exhaustively_certified",
    ):
        assert fixture["receipt"]["gates"][gate] is True
    assert fixture["calls"] == [
        (fixture["release"] / "browser-engine", fixture["source_package"])
    ]


def test_promoted_validator_requires_v6_boundary_ladder_gates() -> None:
    assert validator._expected_gates_for_checked_schema(
        validator.LEGACY_CHECKED_HORIZON_SCHEMA
    ) == validator.EXPECTED_GATES
    assert validator._expected_gates_for_checked_schema(
        validator.CURRENT_CHECKED_HORIZON_SCHEMA
    ) == validator.V6_EXPECTED_GATES
    assert {
        "opera_selected_b3_boundary_ladder_certified",
        "opera_found_stops_boundary_ladder",
        "opera_unknown_fail_closed_observed",
    } <= validator.V6_EXPECTED_GATES
    assert not {
        "opera_selected_b3_boundary_ladder_certified",
        "opera_found_stops_boundary_ladder",
        "opera_unknown_fail_closed_observed",
    } & validator.EXPECTED_GATES


def test_promoted_validator_recognizes_v6_checked_evidence_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _build_fixture(tmp_path, monkeypatch)
    release = fixture["release"]
    receipt = json.loads((release / "release-receipt.json").read_text(encoding="utf-8"))
    record = receipt["evidence_receipts"][-1]
    path = release / record["path"]
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema"] = validator.CURRENT_CHECKED_HORIZON_SCHEMA
    _write_json(path, payload)
    record["schema"] = validator.CURRENT_CHECKED_HORIZON_SCHEMA
    record["sha256"] = validator._sha256_file(path)
    record["bytes"] = path.stat().st_size

    normalized, payloads, _paths = validator._validate_evidence(
        receipt["evidence_receipts"], release=release
    )

    assert normalized[-1]["schema"] == validator.CURRENT_CHECKED_HORIZON_SCHEMA
    assert payloads["opera_checked_horizon"]["schema"] == (
        validator.CURRENT_CHECKED_HORIZON_SCHEMA
    )


def test_legacy_v5_exception_rejects_reissued_artifact_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed_receipt_path = tmp_path / "legacy-v5-release-receipt.json"
    checked = tmp_path / "legacy-v5-checked-receipt.json"
    fixed_receipt = {
        "release_id": validator.LEGACY_V5_RELEASE_ID,
        "source_revision": validator.LEGACY_V5_SOURCE_REVISION,
        "artifact": copy.deepcopy(validator.LEGACY_V5_ARTIFACT),
        "browser_bundle": copy.deepcopy(validator.LEGACY_V5_BROWSER_BUNDLE),
    }
    _write_json(fixed_receipt_path, fixed_receipt)
    _write_json(checked, {"schema": validator.LEGACY_CHECKED_HORIZON_SCHEMA})

    expected_hashes = {
        fixed_receipt_path.resolve(): validator.LEGACY_V5_RELEASE_RECEIPT_SHA256,
        checked.resolve(): validator.LEGACY_CHECKED_HORIZON_SHA256,
    }
    actual_sha256_file = validator._sha256_file

    def legacy_fixture_sha256(path: Path) -> str:
        resolved = path.resolve()
        if resolved in expected_hashes:
            return expected_hashes[resolved]
        return actual_sha256_file(path)

    monkeypatch.setattr(
        validator,
        "_sha256_file",
        legacy_fixture_sha256,
    )

    validator._validate_legacy_v5_release_identity(
        receipt=fixed_receipt,
        receipt_path=fixed_receipt_path,
        checked_receipt_path=checked,
    )

    forged = copy.deepcopy(fixed_receipt)
    forged_source = "f" * 40
    forged["source_revision"] = forged_source
    forged["artifact"]["source_revision"] = forged_source
    forged_path = tmp_path / "release-receipt.json"
    _write_json(forged_path, forged)
    expected_hashes[forged_path.resolve()] = validator.LEGACY_V5_RELEASE_RECEIPT_SHA256

    with pytest.raises(
        validator.PromotedReleaseError,
        match="immutable legacy v5 release identity",
    ):
        validator._validate_legacy_v5_release_identity(
            receipt=forged,
            receipt_path=forged_path,
            checked_receipt_path=checked,
        )


def test_accepts_replacement_release_and_release_only_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _build_fixture(tmp_path, monkeypatch, prior_release=True)

    for path in validator.REUSABLE_IMMUTABLE_RELEASE_PAYLOADS:
        assert _git(
            fixture["repository"],
            "diff",
            "--name-only",
            fixture["source_revision"],
            fixture["artifact_revision"],
            "--",
            path,
        ) == ""
    assert _validate(fixture)["status"] == "validated"


def test_reissue_still_requires_every_release_authority_file_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _build_fixture(tmp_path, monkeypatch, prior_release=True)
    repository = fixture["repository"]
    release = fixture["release"]
    release_files = {
        path.relative_to(release).as_posix()
        for path in release.rglob("*")
        if path.is_file()
    }
    unchanged_authority = "browser-engine/browser-engine-manifest.json"
    for relative in release_files:
        tracked = f"release/browser-wasm/{relative}"
        if (
            tracked in validator.REUSABLE_IMMUTABLE_RELEASE_PAYLOADS
            or relative == unchanged_authority
        ):
            continue
        path = release / relative
        path.write_bytes(path.read_bytes() + b"\nnext release authority\n")
    _git(repository, "add", "release/browser-wasm")
    _git(repository, "commit", "-q", "-m", "reissue fixture")

    with pytest.raises(
        validator.PromotedReleaseError,
        match="promoted release file was not added or replaced",
    ):
        validator._validate_artifact_commit(
            repository=repository,
            release_files=release_files,
            source_revision=fixture["artifact_revision"],
        )


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


def test_native_wasm_receipt_sources_are_forced_to_lf() -> None:
    repository = Path(__file__).resolve().parents[1]
    sources = sorted(validator.evidence_producer.KERNEL_SOURCES)
    completed = subprocess.run(
        ["git", "check-attr", "--cached", "-z", "text", "eol", "--", *sources],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    fields = [item.decode("utf-8") for item in completed.stdout.split(b"\0") if item]
    assert len(fields) == len(sources) * 2 * 3
    resolved = {
        (fields[index], fields[index + 1]): fields[index + 2]
        for index in range(0, len(fields), 3)
    }

    for relative in sources:
        assert resolved[(relative, "text")] == "set"
        assert resolved[(relative, "eol")] == "lf"
        checkout_bytes = (repository / relative).read_bytes()
        revision_bytes = subprocess.run(
            ["git", "show", f"HEAD:{relative}"],
            cwd=repository,
            check=True,
            capture_output=True,
        ).stdout
        assert b"\r" not in checkout_bytes
        assert checkout_bytes == revision_bytes


def test_checked_horizon_browser_sources_are_forced_to_lf() -> None:
    repository = Path(__file__).resolve().parents[1]
    sources = sorted(
        "src/scottish_progressive/web/static/" + filename
        for filename in validator.evidence_producer.CHECKED_HORIZON_STATIC_ASSETS.values()
    )
    completed = subprocess.run(
        ["git", "check-attr", "--cached", "-z", "text", "eol", "--", *sources],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    fields = [item.decode("utf-8") for item in completed.stdout.split(b"\0") if item]
    assert len(fields) == len(sources) * 2 * 3
    resolved = {
        (fields[index], fields[index + 1]): fields[index + 2]
        for index in range(0, len(fields), 3)
    }

    for relative in sources:
        assert resolved[(relative, "text")] == "set"
        assert resolved[(relative, "eol")] == "lf"
        checkout_bytes = (repository / relative).read_bytes()
        revision_bytes = subprocess.run(
            ["git", "show", f"HEAD:{relative}"],
            cwd=repository,
            check=True,
            capture_output=True,
        ).stdout
        assert b"\r" not in checkout_bytes
        assert checkout_bytes == revision_bytes


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
            lambda receipt: receipt["promotion_policy"].update(
                safety_reserve_positions=3_999_999
            ),
            "certified 4,000,000-position budget",
        ),
        (
            lambda receipt: receipt["measured"]["opera_checked_horizon"].update(
                pv_horizon_candidate_vetoes=0
            ),
            "independently satisfy",
        ),
        (
            lambda receipt: receipt["measured"]["opera_checked_horizon"][
                "raw_trace_attestation"
            ].update(horizon_safety_trace_sha256="b" * 64),
            "differ from the validated Opera receipt",
        ),
        (
            lambda receipt: receipt["measured"]["opera_checked_horizon"].update(
                known_adverse_excluded=False
            ),
            "independently satisfy",
        ),
        (
            lambda receipt: receipt["measured"]["opera_checked_horizon"][
                "selected_d5_horizon_certification_witness"
            ].update(horizon_call_work_credit=3_499_999),
            "independently satisfy",
        ),
        (
            lambda receipt: receipt["measured"]["opera_checked_horizon"][
                "selected_d5_horizon_certification_witness"
            ].update(root_child_call_work_credit=1_499_999),
            "independently satisfy",
        ),
        (
            lambda receipt: receipt["gates"].update(
                opera_selected_b3_root_child_exhaustively_certified=False
            ),
            "gate",
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
