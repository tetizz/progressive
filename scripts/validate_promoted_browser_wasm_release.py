from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import subprocess
import sys
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import build_browser_wasm_bundle as bundle_builder  # noqa: E402
from scripts import promote_browser_wasm_release as evidence_producer  # noqa: E402


RELEASE_SCHEMA = "spc-browser-wasm-release-promotion-v2"
HEX_16 = re.compile(r"[0-9a-f]{16}")
HEX_64 = re.compile(r"[0-9a-f]{64}")
GIT_REVISION = re.compile(r"[0-9a-f]{40,64}")

ARTIFACT_IDENTITY_FIELDS = (
    "source_revision",
    "source_fingerprint",
    "kernel_sha256",
    "wasm_sha256",
    "module_js_sha256",
    "artifact_set_sha256",
)
ARTIFACT_FIELDS = frozenset(
    (
        *ARTIFACT_IDENTITY_FIELDS,
        "exception_strategy",
        "wasm_simd",
        "allocator",
        "runtime_variant",
        "thread_count",
    )
)

EVIDENCE_SPECS = (
    (
        "build",
        "evidence/root-session-build-receipt.json",
        "spc-root-session-build-receipt-v1",
    ),
    (
        "root_smoke",
        "evidence/root-session-smoke-receipt.json",
        "spc-root-session-wasm-smoke-v1",
    ),
    (
        "root_parity",
        "evidence/root-session-parity-receipt.json",
        "spc-root-d5-oracle-v1",
    ),
    (
        "prefix_parity",
        "evidence/prefix-parity-receipt.json",
        "spc-prefix-parity-receipt-v2",
    ),
    (
        "browser_prefix",
        "evidence/browser-prefix-receipt.json",
        "spc-browser-prefix-contract-receipt-v1",
    ),
    (
        "mate_parity",
        "evidence/mate-parity-receipt.json",
        "spc-mate-wasm-receipt-v2",
    ),
    (
        "opera",
        "evidence/opera-d1-d5-receipt.json",
        "spc-opera-root-session-cdp-receipt-v1",
    ),
    (
        "opera_checked_horizon",
        "evidence/opera-checked-pv-horizon-receipt.json",
        "spc-opera-checked-pv-horizon-receipt-v5",
    ),
)

CERTIFICATE_SPECS = {
    "prefix": (
        "certificates/prefix-certificate.json",
        "spc-prefix-",
        None,
        "prefix_certificate",
    ),
    "root_session": (
        "certificates/root-session-certificate.json",
        "spc-root-session-",
        bundle_builder.ROOT_SESSION_CERTIFICATE_SCHEMA,
        "root_session_certificate",
    ),
    "mate": (
        "certificates/mate-certificate.json",
        "spc-mate-",
        bundle_builder.MATE_CERTIFICATE_SCHEMA,
        "mate_certificate",
    ),
}

BROWSER_FILES = (
    "browser-engine/browser-engine-manifest.json",
    "browser-engine/single/spc-engine.js",
    "browser-engine/single/spc-root-session.wasm",
)
BROWSER_RECORD_PATHS = tuple(path.removeprefix("browser-engine/") for path in BROWSER_FILES)
STATIC_MIRRORS = {
    "src/scottish_progressive/web/static/engine/browser-engine-manifest.json": BROWSER_FILES[0],
    "src/scottish_progressive/web/static/engine/single/spc-engine.js": BROWSER_FILES[1],
    "src/scottish_progressive/web/static/engine/single/spc-root-session.wasm": BROWSER_FILES[2],
}
REUSABLE_IMMUTABLE_RELEASE_PAYLOADS = frozenset(
    {
        "release/browser-wasm/browser-engine/single/spc-engine.js",
        "release/browser-wasm/browser-engine/single/spc-root-session.wasm",
    }
)
ALLOWED_ARTIFACT_TEST = "tests/test_release_engine_gate.py"

EXPECTED_GATES = frozenset(
    {
        "exact_source_and_artifact_identity",
        "clean_tracked_dependency_closure",
        "root_python_parity",
        "persistent_matches_fresh_d5",
        "multiple_opera_schedule_shapes",
        "prefix_python_and_browser_parity",
        "mate_python_and_proof_parity",
        "combined_root_prefix_mate_abi",
        "canonical_root_tactical_boundary_policy",
        "opera_ordinary_worker_proof",
        "memory_envelope_observed",
        "w32_d1_through_d5_under_60_seconds",
        "existing_bundle_revalidated",
        "immutable_copy_by_digest",
        "opera_checked_horizon_raw_trace_attested",
        "opera_checked_horizon_local_assets_bound",
        "opera_selected_b3_known_adverse_horizon_excluded",
        "opera_selected_b3_horizon_exhaustively_certified",
        "opera_selected_b3_root_child_exhaustively_certified",
        "opera_checked_horizon_d5_under_60_seconds",
        "opera_checked_horizon_accounting_balanced",
    }
)

RELEASE_RECEIPT_FIELDS = frozenset(
    {
        "schema",
        "status",
        "product_publishable",
        "release_id",
        "authorization",
        "source_revision",
        "artifact",
        "toolchain",
        "build_command_sha256",
        "dependency_closure",
        "root_tactical_policy",
        "certificates",
        "evidence_receipts",
        "browser_bundle",
        "certificate_set_sha256",
        "promotion_policy",
        "measured",
        "gates",
    }
)


class PromotedReleaseError(ValueError):
    """Raised when tracked release bytes are not exactly deployable."""


@dataclass(frozen=True)
class SemanticEvidence:
    """Producer-derived values that a promoted receipt may only mirror."""

    certificates: Mapping[str, Mapping[str, Any]]
    oracle_signature_sha256: str
    root_config: Mapping[str, Any]
    opera_elapsed_seconds: float
    opera_result: Mapping[str, Any]
    opera_memory: Mapping[str, Any]
    safety_reserve_positions: int
    checked_elapsed_seconds: float
    checked_work: int
    checked_selected_root_series: str
    checked_line_rejections: int
    checked_native_repairs: int
    checked_candidate_vetoes: int
    checked_principal_variation_sha256: str
    checked_selected_fixture_id: str
    checked_known_adverse_excluded: bool
    checked_selected_horizon_exhaustively_certified: bool
    checked_selected_root_child_exhaustively_certified: bool
    checked_raw_trace_attestation: Mapping[str, Any]
    checked_selected_d5_horizon_certification_witness: Mapping[str, Any]
    checked_local_asset_set_sha256: str


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PromotedReleaseError(f"{label} must be a JSON object")
    return value


def _list(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise PromotedReleaseError(f"{label} must be a JSON array")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str] | frozenset[str], label: str) -> None:
    if set(value) != set(expected):
        raise PromotedReleaseError(
            f"{label} has unknown or missing fields: expected {sorted(expected)!r}, "
            f"found {sorted(value)!r}"
        )


def _integer(value: object, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise PromotedReleaseError(f"{label} must be an integer >= {minimum}")
    return value


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PromotedReleaseError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise PromotedReleaseError(f"{label} must be a finite number")
    return result


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reject_json_constant(value: str) -> None:
    raise PromotedReleaseError(f"non-finite JSON number is forbidden: {value}")


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PromotedReleaseError(f"duplicate JSON field is forbidden: {key}")
        result[key] = value
    return result


def _load_json(path: Path, label: str) -> Mapping[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise PromotedReleaseError(f"{label} is missing or is not a regular file: {path}")
    try:
        raw = path.read_bytes()
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PromotedReleaseError(f"could not read {label}: {error}") from error
    return _mapping(payload, label)


def _safe_relative(value: object, label: str) -> PurePosixPath:
    if not isinstance(value, str) or "\\" in value:
        raise PromotedReleaseError(f"{label} must be a canonical POSIX relative path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise PromotedReleaseError(f"{label} must be a canonical POSIX relative path")
    return path


def _scan_release_tree(root: Path) -> tuple[set[str], set[str]]:
    if root.is_symlink() or not root.is_dir():
        raise PromotedReleaseError(f"promoted release is missing or is a symlink: {root}")
    files: set[str] = set()
    directories: set[str] = set()

    def visit(directory: Path, parts: tuple[str, ...]) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as error:
            raise PromotedReleaseError(f"could not inventory promoted release: {error}") from error
        for entry in entries:
            relative_parts = (*parts, entry.name)
            relative = PurePosixPath(*relative_parts).as_posix()
            if entry.is_symlink():
                raise PromotedReleaseError(f"promoted release contains a symlink: {relative}")
            if entry.is_dir(follow_symlinks=False):
                directories.add(relative)
                visit(Path(entry.path), relative_parts)
            elif entry.is_file(follow_symlinks=False):
                files.add(relative)
            else:
                raise PromotedReleaseError(f"promoted release contains a special file: {relative}")

    visit(root, ())
    return files, directories


def _file_record(path: Path, relative: str) -> dict[str, object]:
    return {"path": relative, "sha256": _sha256_file(path), "bytes": path.stat().st_size}


def _validate_record(
    value: object,
    *,
    release: Path,
    expected_path: str,
    label: str,
    base: Path | None = None,
    include_label: str | None = None,
    include_schema: str | None = None,
) -> tuple[dict[str, object], Path]:
    record = _mapping(value, label)
    expected_keys = {"path", "sha256", "bytes"}
    if include_label is not None:
        expected_keys.add("label")
    if include_schema is not None:
        expected_keys.add("schema")
    _exact_keys(record, expected_keys, label)
    relative = _safe_relative(record.get("path"), f"{label} path")
    if relative.as_posix() != expected_path:
        raise PromotedReleaseError(
            f"{label} path must be {expected_path!r}, found {relative.as_posix()!r}"
        )
    if include_label is not None and record.get("label") != include_label:
        raise PromotedReleaseError(f"{label} label must be {include_label!r}")
    if include_schema is not None and record.get("schema") != include_schema:
        raise PromotedReleaseError(f"{label} schema must be {include_schema!r}")
    digest = record.get("sha256")
    if not isinstance(digest, str) or HEX_64.fullmatch(digest) is None:
        raise PromotedReleaseError(f"{label} has an invalid SHA-256")
    size = _integer(record.get("bytes"), f"{label} bytes")
    path = (base or release).joinpath(*relative.parts)
    if path.is_symlink() or not path.is_file():
        raise PromotedReleaseError(f"{label} file is missing or is not regular: {path}")
    actual = _file_record(path, expected_path)
    if actual["sha256"] != digest or actual["bytes"] != size:
        raise PromotedReleaseError(f"{label} digest or size does not match its bytes")
    return actual, path


def _run_git(
    repository: Path,
    *arguments: str,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=repository,
            check=check,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise PromotedReleaseError(f"git {' '.join(arguments)} failed: {error}") from error


def _git_text(repository: Path, *arguments: str) -> str:
    result = _run_git(repository, *arguments)
    try:
        return result.stdout.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise PromotedReleaseError("git returned a non-UTF-8 path or revision") from error


def _zero_paths(raw: bytes, label: str) -> list[str]:
    try:
        paths = [item.decode("utf-8") for item in raw.split(b"\0") if item]
    except UnicodeDecodeError as error:
        raise PromotedReleaseError(f"{label} contains a non-UTF-8 path") from error
    if any("\\" in path for path in paths):
        raise PromotedReleaseError(f"{label} contains a non-canonical backslash path")
    return paths


def _validate_artifact_commit(
    *,
    repository: Path,
    release_files: set[str],
    source_revision: str,
) -> str:
    head = _git_text(repository, "rev-parse", "HEAD")
    if GIT_REVISION.fullmatch(head) is None:
        raise PromotedReleaseError("checked-out HEAD is not a full Git revision")
    github_sha = os.environ.get("GITHUB_SHA", "").strip().lower()
    if github_sha and github_sha != head:
        raise PromotedReleaseError("GITHUB_SHA does not equal the checked-out HEAD")
    lineage = _git_text(repository, "rev-list", "--parents", "-n", "1", head).split()
    if len(lineage) != 2 or lineage[0] != head:
        raise PromotedReleaseError("the promoted artifact commit must have exactly one parent")
    parent = lineage[1]
    if parent != source_revision:
        raise PromotedReleaseError(
            "release source_revision must equal the promoted artifact commit parent"
        )

    tracked_release = set(
        _zero_paths(
            _run_git(
                repository,
                "ls-tree",
                "-r",
                "--name-only",
                "-z",
                head,
                "--",
                "release/browser-wasm",
            ).stdout,
            "tracked promoted release inventory",
        )
    )
    expected_tracked = {f"release/browser-wasm/{path}" for path in release_files}
    if tracked_release != expected_tracked:
        raise PromotedReleaseError(
            "tracked promoted release inventory differs from the validated filesystem"
        )
    tracked_mirrors = set(
        _zero_paths(
            _run_git(
                repository,
                "ls-tree",
                "-r",
                "--name-only",
                "-z",
                head,
                "--",
                *STATIC_MIRRORS,
            ).stdout,
            "tracked static mirror inventory",
        )
    )
    if tracked_mirrors != set(STATIC_MIRRORS):
        raise PromotedReleaseError("the exact three static engine mirrors must be tracked")

    dirty = _git_text(repository, "status", "--porcelain=v1", "--untracked-files=all")
    if dirty:
        raise PromotedReleaseError("deployment checkout is dirty or contains untracked files")

    diff_tokens = _zero_paths(
        _run_git(
            repository,
            "diff",
            "--name-status",
            "--no-renames",
            "-z",
            parent,
            head,
            "--",
        ).stdout,
        "artifact commit diff",
    )
    if len(diff_tokens) % 2:
        raise PromotedReleaseError("artifact commit diff has an invalid name-status shape")
    changes = dict(zip(diff_tokens[1::2], diff_tokens[0::2]))
    release_changes = {
        path: status
        for path, status in changes.items()
        if path.startswith("release/browser-wasm/")
    }
    if not release_changes:
        raise PromotedReleaseError(
            "artifact commit does not add or replace the promoted release"
        )
    for path, status in changes.items():
        if status not in {"A", "M"} and not (
            status == "D" and path.startswith("release/browser-wasm/")
        ):
            raise PromotedReleaseError(
                f"artifact commit contains forbidden {status} change: {path}"
            )
        allowed = (
            path.startswith("release/browser-wasm/")
            or path in STATIC_MIRRORS
            or path == ALLOWED_ARTIFACT_TEST
        )
        if not allowed:
            raise PromotedReleaseError(f"artifact commit contains a non-release change: {path}")
    if not REUSABLE_IMMUTABLE_RELEASE_PAYLOADS <= expected_tracked:
        raise PromotedReleaseError(
            "reusable immutable release payload policy is outside the release inventory"
        )
    required_replacements = expected_tracked - REUSABLE_IMMUTABLE_RELEASE_PAYLOADS
    for path in required_replacements:
        if changes.get(path) not in {"A", "M"}:
            raise PromotedReleaseError(
                "promoted release file was not added or replaced by the artifact "
                f"commit: {path}"
            )
    for path in REUSABLE_IMMUTABLE_RELEASE_PAYLOADS:
        status = changes.get(path)
        if status is None:
            parent_blob = _git_text(repository, "rev-parse", f"{parent}:{path}")
            head_blob = _git_text(repository, "rev-parse", f"{head}:{path}")
            if parent_blob != head_blob:
                raise PromotedReleaseError(
                    "unchanged reusable release payload does not resolve to the "
                    f"same Git blob: {path}"
                )
        elif status not in {"A", "M"}:
            raise PromotedReleaseError(
                f"reusable release payload has forbidden {status} change: {path}"
            )
    return head


def _validate_artifact(value: object, source_revision: str) -> dict[str, Any]:
    artifact = dict(_mapping(value, "release artifact"))
    _exact_keys(artifact, ARTIFACT_FIELDS, "release artifact")
    if artifact.get("source_revision") != source_revision:
        raise PromotedReleaseError("release artifact source_revision differs from the release")
    if not isinstance(artifact.get("source_fingerprint"), str) or HEX_16.fullmatch(
        str(artifact.get("source_fingerprint"))
    ) is None:
        raise PromotedReleaseError("release artifact has an invalid source_fingerprint")
    for field in (
        "kernel_sha256",
        "wasm_sha256",
        "module_js_sha256",
        "artifact_set_sha256",
    ):
        if not isinstance(artifact.get(field), str) or HEX_64.fullmatch(
            str(artifact.get(field))
        ) is None:
            raise PromotedReleaseError(f"release artifact has an invalid {field}")
    if artifact.get("exception_strategy") not in {"emscripten", "wasm"}:
        raise PromotedReleaseError("release artifact has an unsupported exception strategy")
    if not isinstance(artifact.get("wasm_simd"), bool):
        raise PromotedReleaseError("release artifact wasm_simd must be boolean")
    if artifact.get("allocator") not in {"dlmalloc", "emmalloc"}:
        raise PromotedReleaseError("release artifact has an unsupported allocator")
    if artifact.get("runtime_variant") != "single" or artifact.get("thread_count") != 1:
        raise PromotedReleaseError("only the single-thread browser artifact may deploy")
    return artifact


def _validate_evidence(
    value: object,
    *,
    release: Path,
) -> tuple[
    list[dict[str, object]],
    dict[str, Mapping[str, Any]],
    dict[str, Path],
]:
    records = _list(value, "release evidence_receipts")
    if len(records) != len(EVIDENCE_SPECS):
        raise PromotedReleaseError("release must contain exactly eight evidence receipts")
    normalized: list[dict[str, object]] = []
    payloads: dict[str, Mapping[str, Any]] = {}
    paths: dict[str, Path] = {}
    for index, (label, path, schema) in enumerate(EVIDENCE_SPECS):
        actual, receipt_path = _validate_record(
            records[index],
            release=release,
            expected_path=path,
            label=f"evidence receipt {index}",
            include_label=label,
            include_schema=schema,
        )
        payload = _load_json(receipt_path, f"{label} evidence receipt")
        if payload.get("schema") != schema:
            raise PromotedReleaseError(f"{label} evidence payload has the wrong schema")
        normalized.append(
            {
                "label": label,
                "path": path,
                "schema": schema,
                **{key: actual[key] for key in ("sha256", "bytes")},
            }
        )
        payloads[label] = payload
        paths[label] = receipt_path
    return normalized, payloads, paths


def _portable_parts(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or "\0" in value:
        raise PromotedReleaseError(f"{label} must be a non-empty path string")
    return tuple(part for part in value.replace("\\", "/").split("/") if part)


def _portable_name(value: object, label: str) -> str:
    parts = _portable_parts(value, label)
    if not parts:
        raise PromotedReleaseError(f"{label} has no filename")
    # PureWindowsPath also treats backslashes as separators on the Linux Pages runner.
    return PureWindowsPath(str(value)).name


def _producer_receipt(
    label: str,
    path: Path,
    payload: Mapping[str, Any],
) -> evidence_producer.Receipt:
    raw = path.read_bytes()
    return evidence_producer.Receipt(
        label=label,
        path=path.resolve(),
        raw=raw,
        payload=payload,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def _validate_deployed_build_evidence(
    *,
    receipt: evidence_producer.Receipt,
    repository: Path,
    source_package: Path,
    release: Path,
    artifact: Mapping[str, Any],
    dependency_closure: Mapping[str, Any],
) -> evidence_producer.BuildEvidence:
    """Revalidate the portable part of the producer's build contract.

    The original compiler executable and absolute build directory deliberately are
    not shipped to GitHub Pages. Everything that can be replayed from the artifact
    commit is checked here: parent source bytes, kernel closure, artifact bytes,
    ABI, runtime lane, memory limits, and the canonical compiler invocation shape.
    """

    build = receipt.payload
    if (
        build.get("schema") != evidence_producer.BUILD_SCHEMA
        or build.get("status") != "built-not-certified"
        or build.get("product_publishable") is not False
        or build.get("certificate_id") is not None
    ):
        raise PromotedReleaseError("build evidence is not exact pre-certification evidence")

    identity = evidence_producer._artifact_identity(build)
    expected_identity = {field: artifact[field] for field in ARTIFACT_IDENTITY_FIELDS}
    if identity != expected_identity:
        raise PromotedReleaseError("build evidence identity differs from the release artifact")
    runtime_identity = evidence_producer._runtime_identity(build)
    if any(runtime_identity[field] != artifact[field] for field in runtime_identity):
        raise PromotedReleaseError("build runtime identity differs from the release artifact")
    memory, full_memory = evidence_producer._validate_memory(build)

    if build.get("source_fingerprint") != bundle_builder.engine_source_fingerprint(
        source_package
    ):
        raise PromotedReleaseError(
            "build source fingerprint does not match the artifact parent source"
        )
    source_records = _list(build.get("source_inputs"), "build source inputs")
    expected_source_paths = sorted(evidence_producer.KERNEL_SOURCES)
    if len(source_records) != len(expected_source_paths):
        raise PromotedReleaseError("build source inputs are not the canonical kernel closure")
    normalized_sources: list[dict[str, object]] = []
    for index, expected_path in enumerate(expected_source_paths):
        actual, _ = _validate_record(
            source_records[index],
            release=release,
            expected_path=expected_path,
            label=f"build source input {index}",
            base=repository,
        )
        normalized_sources.append(actual)
    if _canonical_sha256(normalized_sources) != identity["kernel_sha256"]:
        raise PromotedReleaseError("build kernel source-set digest is invalid")
    if dependency_closure.get("required") != list(evidence_producer.CLOSURE_SOURCES):
        raise PromotedReleaseError("release dependency closure is not the canonical source set")

    if (
        build.get("runtime_variant") != "single"
        or build.get("thread_count") != 1
        or build.get("pthreads") is not False
        or build.get("support_files") != []
    ):
        raise PromotedReleaseError("build evidence is not the single ordinary-Worker lane")
    expected_runtime_requirements = {
        "ordinary_module_worker": True,
        "pthreads": False,
        "cross_origin_isolated": False,
        "native_wasm_exception_handling": (
            runtime_identity["exception_strategy"] == "wasm"
        ),
        "wasm_simd": runtime_identity["wasm_simd"],
    }
    runtime_requirements = dict(
        _mapping(build.get("runtime_requirements"), "build runtime requirements")
    )
    if runtime_requirements != expected_runtime_requirements:
        raise PromotedReleaseError("build runtime requirements are internally inconsistent")
    abi = _mapping(build.get("abi"), "build ABI")
    expected_abi = {
        "root_session_version": 2,
        "prefix_kernel_version": 1,
        "series_mate_version": 1,
        "exports": list(evidence_producer.EXPORTED_FUNCTIONS),
        "reply_mate_safety": False,
        "canonical_root_tactical_policy": "canonical-boundary-policy-v1",
        "legacy_root_tactical_protection": False,
    }
    if dict(abi) != expected_abi:
        raise PromotedReleaseError("build evidence does not carry the exact combined ABI")

    artifact_records = _list(build.get("artifacts"), "build artifact records")
    if len(artifact_records) != 2:
        raise PromotedReleaseError("build evidence must describe exactly module and WASM")
    normalized_artifacts: list[dict[str, object]] = []
    for index, value in enumerate(artifact_records):
        item = _mapping(value, f"build artifact record {index}")
        _exact_keys(item, {"path", "sha256", "bytes"}, f"build artifact record {index}")
        relative = _safe_relative(item.get("path"), f"build artifact record {index} path")
        digest = item.get("sha256")
        if not isinstance(digest, str) or HEX_64.fullmatch(digest) is None:
            raise PromotedReleaseError(f"build artifact record {index} has an invalid SHA-256")
        normalized_artifacts.append(
            {
                "path": relative.as_posix(),
                "sha256": digest,
                "bytes": _integer(item.get("bytes"), f"build artifact record {index} bytes"),
            }
        )
    if normalized_artifacts != sorted(
        normalized_artifacts, key=lambda item: str(item["path"])
    ):
        raise PromotedReleaseError("build artifact records are not canonical")
    if _canonical_sha256(normalized_artifacts) != identity["artifact_set_sha256"]:
        raise PromotedReleaseError("build artifact-set digest is invalid")
    wasm_records = [
        record
        for record in normalized_artifacts
        if record["sha256"] == identity["wasm_sha256"]
        and str(record["path"]).endswith(".wasm")
    ]
    module_records = [
        record
        for record in normalized_artifacts
        if record["sha256"] == identity["module_js_sha256"]
        and str(record["path"]).endswith((".js", ".mjs"))
    ]
    if len(wasm_records) != 1 or len(module_records) != 1:
        raise PromotedReleaseError("build artifact records do not identify one module and WASM")
    deployed_module = release / BROWSER_FILES[1]
    deployed_wasm = release / BROWSER_FILES[2]
    if (
        module_records[0]["bytes"] != deployed_module.stat().st_size
        or wasm_records[0]["bytes"] != deployed_wasm.stat().st_size
        or _sha256_file(deployed_module) != identity["module_js_sha256"]
        or _sha256_file(deployed_wasm) != identity["wasm_sha256"]
    ):
        raise PromotedReleaseError("build artifact records differ from deployed browser bytes")

    toolchain_raw = _mapping(build.get("toolchain"), "build toolchain")
    _exact_keys(toolchain_raw, {"path", "sha256", "version"}, "build toolchain")
    compiler_path = toolchain_raw.get("path")
    compiler_digest = toolchain_raw.get("sha256")
    compiler_version = toolchain_raw.get("version")
    _portable_parts(compiler_path, "build toolchain path")
    if not isinstance(compiler_digest, str) or HEX_64.fullmatch(compiler_digest) is None:
        raise PromotedReleaseError("build toolchain has an invalid executable digest")
    if not isinstance(compiler_version, str) or not compiler_version.strip():
        raise PromotedReleaseError("build toolchain has no compiler version")

    command = _list(build.get("command"), "build command")
    if not command or any(not isinstance(item, str) or not item for item in command):
        raise PromotedReleaseError("build command must be a non-empty string array")
    compiled_names = [
        "_native_eval.cpp",
        "native_subtree.cpp",
        "native_subtree_wasm.cpp",
        "native_root_session_wasm.cpp",
        "_native_mate.cpp",
    ]
    if len(command) < 12 or command[0] != compiler_path:
        raise PromotedReleaseError("build command compiler differs from its toolchain")
    for index, filename in enumerate(compiled_names, start=1):
        parts = _portable_parts(command[index], f"build command source {filename}")
        if parts[-3:] != ("src", "scottish_progressive", filename):
            raise PromotedReleaseError("build command compiled-source closure is not canonical")
    include_parts = _portable_parts(command[7], "build command include path")
    if command[6] != "-I" or include_parts[-2:] != ("src", "scottish_progressive"):
        raise PromotedReleaseError("build command include path is not canonical")
    expected_exception_flag = (
        "-fwasm-exceptions"
        if runtime_identity["exception_strategy"] == "wasm"
        else "-fexceptions"
    )
    expected_flags = [
        "-std=c++20",
        "-O3",
        "-flto",
        expected_exception_flag,
    ]
    if runtime_identity["wasm_simd"]:
        expected_flags.append("-msimd128")
    expected_flags.extend(
        [
            "-DSPC_NATIVE_CORE_ONLY=1",
            "-DSPC_NATIVE_MATE_CORE_ONLY=1",
            "-sALLOW_MEMORY_GROWTH=1",
            f"-sINITIAL_MEMORY={full_memory['initial_bytes']}",
            f"-sMAXIMUM_MEMORY={full_memory['maximum_bytes']}",
            f"-sSTACK_SIZE={full_memory['stack_bytes']}",
            "-sABORTING_MALLOC=0",
            f"-sMALLOC={runtime_identity['allocator']}",
            "-sUSE_PTHREADS=0",
            "-sWASM_WORKERS=0",
            "-sENVIRONMENT=worker,node",
            "-sMODULARIZE=1",
            "-sEXPORT_ES6=1",
            "-sFILESYSTEM=0",
            "-sDYNAMIC_EXECUTION=0",
            f"-sEXPORTED_FUNCTIONS={','.join(evidence_producer.EXPORTED_FUNCTIONS)}",
            "-sEXPORTED_RUNTIME_METHODS=UTF8ToString,stringToNewUTF8,HEAPU8",
        ]
    )
    if command[8:-2] != expected_flags or command[-2] != "-o":
        raise PromotedReleaseError("build command is not the canonical builder invocation")
    if _portable_name(command[-1], "build command output") != _portable_name(
        module_records[0]["path"], "build module record"
    ):
        raise PromotedReleaseError("build command output differs from its module record")

    engine = {
        "engine_version": evidence_producer._text(
            build.get("engine_version"), "engine version"
        ),
        "ruleset_version": evidence_producer._text(
            build.get("ruleset_version"), "ruleset version"
        ),
        "profile_id": evidence_producer._text(build.get("profile_id"), "profile id"),
    }
    session_geometry = _mapping(build.get("session_geometry"), "build session geometry")
    _exact_keys(
        session_geometry,
        {
            "desktop_series_cache_capacity",
            "root_contract_tt_capacity",
            "root_contract_eval_capacity",
        },
        "build session geometry",
    )
    for key, value in session_geometry.items():
        _integer(value, f"build session geometry {key}", 1)

    return evidence_producer.BuildEvidence(
        receipt=receipt,
        identity=identity,
        runtime_identity=runtime_identity,
        runtime_requirements=runtime_requirements,
        memory=memory,
        full_memory=full_memory,
        engine=engine,
        toolchain={
            "path": str(compiler_path),
            "sha256": str(compiler_digest),
            "version": str(compiler_version),
        },
        # The producer validators use these names to bind the Opera URL. Their
        # bytes are independently bound to the promoted browser files above.
        wasm=receipt.path.parent / str(wasm_records[0]["path"]),
        module_js=receipt.path.parent / str(module_records[0]["path"]),
        source_fingerprint=str(identity["source_fingerprint"]),
        dependency_closure=dict(dependency_closure),
    )


def _validate_semantic_evidence(
    *,
    repository: Path,
    source_package: Path,
    release: Path,
    artifact: Mapping[str, Any],
    dependency_closure: Mapping[str, Any],
    evidence_payloads: Mapping[str, Mapping[str, Any]],
    evidence_paths: Mapping[str, Path],
    certificate_payloads: Mapping[str, Mapping[str, Any]],
    maximum_seconds: float,
    default_seconds: float,
) -> SemanticEvidence:
    try:
        receipts = {
            label: _producer_receipt(label, evidence_paths[label], evidence_payloads[label])
            for label, _path, _schema in EVIDENCE_SPECS
        }
        build = _validate_deployed_build_evidence(
            receipt=receipts["build"],
            repository=repository,
            source_package=source_package,
            release=release,
            artifact=artifact,
            dependency_closure=dependency_closure,
        )
        root_contract, prefix_contract, checked_horizon_proof = (
            evidence_producer._validate_root_smoke(receipts["root_smoke"], build)
        )
        (
            root_cases,
            root_config,
            canonical_d5,
            oracle_rival_bounds,
            oracle_retained_manifest,
            oracle_signature,
        ) = evidence_producer._validate_root_parity(
            receipts["root_parity"], build, root_contract
        )
        (
            opera_config,
            opera_elapsed,
            opera_result,
            opera_memory,
            safety_reserve,
        ) = evidence_producer._validate_opera(
            receipts["opera"],
            build,
            expected_config=root_config,
            oracle_selected=canonical_d5,
            oracle_rival_bounds=oracle_rival_bounds,
            oracle_retained_manifest_sha256=oracle_retained_manifest,
            oracle_signature_sha256=oracle_signature,
        )
        if opera_config != root_config:
            raise PromotedReleaseError("Opera config differs from the root oracle")
        prefix_cases = evidence_producer._validate_prefix_parity(
            receipts["prefix_parity"], build
        )
        evidence_producer._validate_browser_prefix(receipts["browser_prefix"], build)
        mate_cases = evidence_producer._validate_mate_parity(
            receipts["mate_parity"], build
        )
        validated = evidence_producer.ValidatedEvidence(
            build=build,
            receipts={
                label: receipt
                for label, receipt in receipts.items()
                if label != "opera_checked_horizon"
            },
            root_contract=root_contract,
            checked_horizon_proof_research=checked_horizon_proof,
            prefix_contract=prefix_contract,
            oracle_signature_sha256=oracle_signature,
            root_config=root_config,
            root_differential_cases=root_cases,
            prefix_differential_cases=prefix_cases,
            mate_differential_cases=mate_cases,
            opera_elapsed_seconds=opera_elapsed,
            opera_result=opera_result,
            opera_memory=opera_memory,
            safety_reserve_positions=safety_reserve,
        )
        expected_certificates = evidence_producer.build_certificates(
            validated,
            maximum_seconds=maximum_seconds,
            default_seconds=default_seconds,
        )
        for label, expected in expected_certificates.items():
            if certificate_payloads.get(label) != expected:
                raise PromotedReleaseError(
                    f"{label} certificate is not derived from the seven core evidence receipts"
                )
        checked = evidence_producer.validate_opera_checked_horizon_receipt(
            receipt_path=evidence_paths["opera_checked_horizon"],
            evidence=validated,
            certificates=expected_certificates,
            repository=repository,
            source_package=source_package,
            candidate_bundle=release / "browser-engine",
        )
    except PromotedReleaseError:
        raise
    except (
        FileNotFoundError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
        evidence_producer.ReleaseGateError,
    ) as error:
        raise PromotedReleaseError(f"semantic evidence validation failed: {error}") from error

    return SemanticEvidence(
        certificates=expected_certificates,
        oracle_signature_sha256=oracle_signature,
        root_config=root_config,
        opera_elapsed_seconds=opera_elapsed,
        opera_result=opera_result,
        opera_memory=opera_memory,
        safety_reserve_positions=safety_reserve,
        checked_elapsed_seconds=checked.elapsed_seconds,
        checked_work=checked.work,
        checked_selected_root_series=checked.selected_root_series,
        checked_line_rejections=checked.line_rejections,
        checked_native_repairs=checked.native_repairs,
        checked_candidate_vetoes=checked.candidate_vetoes,
        checked_principal_variation_sha256=checked.principal_variation_sha256,
        checked_selected_fixture_id=checked.selected_fixture_id,
        checked_known_adverse_excluded=checked.known_adverse_excluded,
        checked_selected_horizon_exhaustively_certified=(
            checked.selected_horizon_exhaustively_certified
        ),
        checked_selected_root_child_exhaustively_certified=(
            checked.selected_root_child_exhaustively_certified
        ),
        checked_raw_trace_attestation=dict(checked.raw_trace_attestation),
        checked_selected_d5_horizon_certification_witness=dict(
            checked.selected_d5_horizon_certification_witness
        ),
        checked_local_asset_set_sha256=checked.local_checkout_asset_set_sha256,
    )


def _validate_certificates(
    value: object,
    *,
    release: Path,
    artifact: Mapping[str, Any],
) -> tuple[dict[str, dict[str, str]], dict[str, Mapping[str, Any]], str]:
    records = _mapping(value, "release certificates")
    _exact_keys(records, set(CERTIFICATE_SPECS), "release certificates")
    normalized: dict[str, dict[str, str]] = {}
    payloads: dict[str, Mapping[str, Any]] = {}
    directory_records: list[dict[str, object]] = []
    for label, (path, id_prefix, schema, _manifest_key) in CERTIFICATE_SPECS.items():
        record = _mapping(records[label], f"{label} certificate record")
        _exact_keys(record, {"certificate_id", "path", "sha256"}, f"{label} certificate record")
        relative = _safe_relative(record.get("path"), f"{label} certificate path")
        if relative.as_posix() != path:
            raise PromotedReleaseError(f"{label} certificate path must be {path!r}")
        certificate_id = record.get("certificate_id")
        if (
            not isinstance(certificate_id, str)
            or re.fullmatch(re.escape(id_prefix) + r"[0-9a-f]{16}", certificate_id) is None
        ):
            raise PromotedReleaseError(f"{label} certificate has an invalid certificate_id")
        digest = record.get("sha256")
        if not isinstance(digest, str) or HEX_64.fullmatch(digest) is None:
            raise PromotedReleaseError(f"{label} certificate has an invalid SHA-256")
        certificate_path = release.joinpath(*relative.parts)
        payload = _load_json(certificate_path, f"{label} certificate")
        if payload.get("certificate_id") != certificate_id:
            raise PromotedReleaseError(f"{label} certificate file ID differs from its record")
        if schema is None:
            if "schema" in payload:
                raise PromotedReleaseError(
                    "prefix certificate must use its schema-less v1 envelope"
                )
        elif payload.get("schema") != schema:
            raise PromotedReleaseError(f"{label} certificate payload has the wrong schema")
        if _sha256_file(certificate_path) != digest:
            raise PromotedReleaseError(f"{label} certificate hash differs from its record")
        if payload.get("source_fingerprint") != artifact["source_fingerprint"]:
            raise PromotedReleaseError(f"{label} certificate source fingerprint differs")
        if payload.get("wasm_sha256") != artifact["wasm_sha256"]:
            raise PromotedReleaseError(f"{label} certificate WASM hash differs")
        if payload.get("module_js_sha256") != artifact["module_js_sha256"]:
            raise PromotedReleaseError(f"{label} certificate module hash differs")
        normalized[label] = {
            "certificate_id": certificate_id,
            "path": path,
            "sha256": digest,
        }
        payloads[label] = payload
        directory_relative = relative.relative_to("certificates").as_posix()
        directory_records.append(_file_record(certificate_path, directory_relative))

    validation_arguments = {
        "source_fingerprint": artifact["source_fingerprint"],
        "wasm_sha256": artifact["wasm_sha256"],
        "module_js_sha256": artifact["module_js_sha256"],
        "runtime_variant": "single",
        "thread_count": 1,
        "support_files": [],
    }
    try:
        bundle_builder.validate_prefix_certificate(
            payloads["prefix"], **validation_arguments
        )
        bundle_builder.validate_root_session_certificate(
            payloads["root_session"], **validation_arguments
        )
        bundle_builder.validate_mate_certificate(
            payloads["mate"], **validation_arguments
        )
    except (KeyError, TypeError, ValueError) as error:
        raise PromotedReleaseError(f"standalone certificate validation failed: {error}") from error
    directory_records.sort(key=lambda item: str(item["path"]))
    return normalized, payloads, _canonical_sha256(directory_records)


def _validate_browser_bundle(
    value: object,
    *,
    release: Path,
    source_package: Path,
    artifact: Mapping[str, Any],
    certificates: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Any], str]:
    browser = _mapping(value, "release browser_bundle")
    _exact_keys(browser, {"path", "files", "artifact_set_sha256"}, "release browser_bundle")
    if browser.get("path") != "browser-engine":
        raise PromotedReleaseError("browser bundle path must be 'browser-engine'")
    records = _list(browser.get("files"), "browser bundle files")
    if len(records) != len(BROWSER_RECORD_PATHS):
        raise PromotedReleaseError("browser bundle must contain exactly three files")
    normalized: list[dict[str, object]] = []
    for index, expected in enumerate(BROWSER_RECORD_PATHS):
        actual, _ = _validate_record(
            records[index],
            release=release,
            expected_path=expected,
            label=f"browser bundle file {index}",
            base=release / "browser-engine",
        )
        normalized.append({"path": expected, "sha256": actual["sha256"], "bytes": actual["bytes"]})
    artifact_set_sha256 = browser.get("artifact_set_sha256")
    if not isinstance(artifact_set_sha256, str) or HEX_64.fullmatch(artifact_set_sha256) is None:
        raise PromotedReleaseError("browser bundle has an invalid artifact-set SHA-256")
    calculated_set = _canonical_sha256(normalized)
    if artifact_set_sha256 != calculated_set:
        raise PromotedReleaseError("browser bundle artifact-set digest is invalid")

    bundle_path = release / "browser-engine"
    try:
        manifest = _mapping(
            bundle_builder.validate_existing_bundle(bundle_path, source_package),
            "validated browser engine manifest",
        )
    except (FileNotFoundError, KeyError, TypeError, ValueError) as error:
        raise PromotedReleaseError(f"existing browser bundle validation failed: {error}") from error
    if manifest.get("schema") != bundle_builder.MANIFEST_SCHEMA:
        raise PromotedReleaseError("browser engine manifest schema is invalid")
    if manifest.get("source_fingerprint") != artifact["source_fingerprint"]:
        raise PromotedReleaseError("browser engine manifest source fingerprint differs")
    variants = _mapping(manifest.get("variants"), "browser engine variants")
    if set(variants) != {"single"}:
        raise PromotedReleaseError("browser engine manifest must contain only the single lane")
    variant = _mapping(variants["single"], "single browser engine variant")
    expected_variant = {
        "thread_count": 1,
        "wasm": "spc-root-session.wasm",
        "module_js": "spc-engine.js",
        "wasm_sha256": artifact["wasm_sha256"],
        "module_js_sha256": artifact["module_js_sha256"],
        "kernel_sha256": artifact["kernel_sha256"],
        "support_files": [],
    }
    for key, expected in expected_variant.items():
        if variant.get(key) != expected:
            raise PromotedReleaseError(
                f"browser engine variant {key} differs from the release artifact"
            )
    if "value_model_activation" in variant:
        raise PromotedReleaseError(
            "the promoted three-file browser bundle may not activate an extra model"
        )
    for label, (_path, _prefix, _schema, manifest_key) in CERTIFICATE_SPECS.items():
        embedded = _mapping(variant.get(manifest_key), f"embedded {label} certificate")
        if embedded.get("certificate_id") != certificates[label]["certificate_id"]:
            raise PromotedReleaseError(
                f"embedded {label} certificate ID differs from the release record"
            )
    root = _mapping(variant.get("root_session_certificate"), "embedded root certificate")
    for field in ("exception_strategy", "wasm_simd", "allocator"):
        if root.get(field) != artifact[field]:
            raise PromotedReleaseError(
                f"embedded root certificate {field} differs from the artifact"
            )
    return (
        {
            "path": "browser-engine",
            "files": normalized,
            "artifact_set_sha256": calculated_set,
        },
        calculated_set,
    )


def _validate_static_mirrors(repository: Path, release: Path) -> None:
    for static_relative, promoted_relative in STATIC_MIRRORS.items():
        static = repository.joinpath(*PurePosixPath(static_relative).parts)
        promoted = release.joinpath(*PurePosixPath(promoted_relative).parts)
        if static.is_symlink() or not static.is_file():
            raise PromotedReleaseError(
                "static browser engine mirror is missing or a symlink: "
                f"{static_relative}"
            )
        if static.read_bytes() != promoted.read_bytes():
            raise PromotedReleaseError(
                f"static browser engine mirror differs from promoted bytes: {static_relative}"
            )


def validate_promoted_release(
    *,
    release: Path,
    repository: Path,
    source_package: Path,
) -> Mapping[str, Any]:
    repository = repository.resolve(strict=True)
    expected_release = repository / "release" / "browser-wasm"
    expected_source = repository / "src" / "scottish_progressive"
    if Path(os.path.abspath(release)) != expected_release:
        raise PromotedReleaseError("release must be the repository release/browser-wasm directory")
    if Path(os.path.abspath(source_package)) != expected_source:
        raise PromotedReleaseError("source package must be repository src/scottish_progressive")
    if source_package.is_symlink() or not source_package.is_dir():
        raise PromotedReleaseError("source package is missing or is a symlink")

    release_files, release_directories = _scan_release_tree(expected_release)
    expected_files = {
        "release-receipt.json",
        *(path for _label, path, _schema in EVIDENCE_SPECS),
        *(spec[0] for spec in CERTIFICATE_SPECS.values()),
        *BROWSER_FILES,
    }
    expected_directories = {
        "evidence",
        "certificates",
        "browser-engine",
        "browser-engine/single",
    }
    if release_files != expected_files or release_directories != expected_directories:
        raise PromotedReleaseError(
            "promoted release inventory is not exact: "
            f"expected files {sorted(expected_files)!r} and directories "
            f"{sorted(expected_directories)!r}; found files {sorted(release_files)!r} "
            f"and directories {sorted(release_directories)!r}"
        )

    receipt = _load_json(expected_release / "release-receipt.json", "release receipt")
    _exact_keys(receipt, RELEASE_RECEIPT_FIELDS, "release receipt")
    if receipt.get("schema") != RELEASE_SCHEMA:
        raise PromotedReleaseError("release receipt schema is invalid")
    if (
        receipt.get("status") != "promoted"
        or receipt.get("product_publishable") is not True
    ):
        raise PromotedReleaseError("release receipt is not promoted and product-publishable")
    authorization = _mapping(receipt.get("authorization"), "release authorization")
    expected_authorization = {
        "authorized_by": "tetizz",
        "transition": "verified-combined-wasm-to-pages-ready",
        "mechanism": "explicit-command-line",
    }
    if dict(authorization) != expected_authorization:
        raise PromotedReleaseError("release authorization is not the exact tetizz promotion")
    source_revision = receipt.get("source_revision")
    if (
        not isinstance(source_revision, str)
        or GIT_REVISION.fullmatch(source_revision) is None
    ):
        raise PromotedReleaseError("release source_revision is not a full Git revision")

    artifact = _validate_artifact(receipt.get("artifact"), source_revision)
    gates = _mapping(receipt.get("gates"), "release gates")
    _exact_keys(gates, EXPECTED_GATES, "release gates")
    if any(value is not True for value in gates.values()):
        raise PromotedReleaseError("every expected release gate must be exactly true")
    if receipt.get("root_tactical_policy") != {
        "capability": True,
        "policy": "canonical-boundary-policy-v1",
        "legacy_wire_root_tactical_protection": False,
    }:
        raise PromotedReleaseError("release root tactical policy is not canonical")

    evidence_records, evidence_payloads, evidence_paths = _validate_evidence(
        receipt.get("evidence_receipts"), release=expected_release
    )
    build = evidence_payloads["build"]
    for field in ARTIFACT_IDENTITY_FIELDS:
        if build.get(field) != artifact[field]:
            raise PromotedReleaseError(
                f"build evidence {field} differs from the release artifact"
            )
    if receipt.get("toolchain") != build.get("toolchain"):
        raise PromotedReleaseError("release toolchain differs from build evidence")
    command = build.get("command")
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(item, str) or not item for item in command)
    ):
        raise PromotedReleaseError("build evidence command must be a non-empty string array")
    if receipt.get("build_command_sha256") != _canonical_sha256(command):
        raise PromotedReleaseError("release build-command digest differs from build evidence")

    closure = _mapping(receipt.get("dependency_closure"), "release dependency closure")
    _exact_keys(
        closure,
        {
            "schema",
            "target",
            "ok",
            "source_revision",
            "required",
            "missing_from_worktree",
            "missing_from_clean_checkout",
        },
        "release dependency closure",
    )
    if (
        closure.get("schema") != "spc-wasm-dependency-closure-v2"
        or closure.get("target") != "ordinary-worker-root-session-prefix-mate"
        or closure.get("ok") is not True
        or closure.get("source_revision") != source_revision
        or closure.get("missing_from_worktree") != []
        or closure.get("missing_from_clean_checkout") != []
        or not isinstance(closure.get("required"), list)
        or not closure.get("required")
    ):
        raise PromotedReleaseError("release dependency closure is not clean and source-bound")

    certificate_records, certificate_payloads, certificate_set_sha256 = _validate_certificates(
        receipt.get("certificates"), release=expected_release, artifact=artifact
    )
    if receipt.get("certificate_set_sha256") != certificate_set_sha256:
        raise PromotedReleaseError("release certificate-set digest is invalid")
    browser_record, browser_set_sha256 = _validate_browser_bundle(
        receipt.get("browser_bundle"),
        release=expected_release,
        source_package=source_package,
        artifact=artifact,
        certificates=certificate_records,
    )

    policy = _mapping(receipt.get("promotion_policy"), "release promotion_policy")
    _exact_keys(
        policy,
        {
            "maximum_seconds",
            "default_seconds",
            "default_generation_positions",
            "safety_reserve_positions",
        },
        "release promotion_policy",
    )
    maximum_seconds = _finite_number(policy.get("maximum_seconds"), "maximum_seconds")
    default_seconds = _finite_number(policy.get("default_seconds"), "default_seconds")
    if not 0 < default_seconds <= maximum_seconds <= 60:
        raise PromotedReleaseError("release seconds must satisfy 0 < default <= maximum <= 60")
    default_generation_positions = _integer(
        policy.get("default_generation_positions"),
        "default_generation_positions",
        1,
    )
    safety_reserve_positions = _integer(
        policy.get("safety_reserve_positions"),
        "safety_reserve_positions",
        1,
    )
    if safety_reserve_positions != evidence_producer.CERTIFIED_SAFETY_RESERVE_POSITIONS:
        raise PromotedReleaseError(
            "release safety reserve is not the certified 4,000,000-position budget"
        )

    semantic = _validate_semantic_evidence(
        repository=repository,
        source_package=source_package,
        release=expected_release,
        artifact=artifact,
        dependency_closure=closure,
        evidence_payloads=evidence_payloads,
        evidence_paths=evidence_paths,
        certificate_payloads=certificate_payloads,
        maximum_seconds=maximum_seconds,
        default_seconds=default_seconds,
    )
    for label, expected_certificate in semantic.certificates.items():
        if certificate_records[label]["certificate_id"] != expected_certificate.get(
            "certificate_id"
        ):
            raise PromotedReleaseError(
                f"{label} certificate ID is not derived from the core receipt hashes"
            )
    root_geometry = _mapping(
        certificate_payloads["root_session"].get("geometry"),
        "root certificate geometry",
    )
    if _mapping(
        root_geometry.get("play_limits"), "root certificate play limits"
    ) != policy:
        raise PromotedReleaseError(
            "release promotion_policy differs from the derived root certificate"
        )
    if (
        semantic.root_config.get("max_work") != default_generation_positions
        or semantic.safety_reserve_positions != safety_reserve_positions
    ):
        raise PromotedReleaseError(
            "release work policy differs from the validated root and Opera evidence"
        )

    measured = _mapping(receipt.get("measured"), "release measured evidence")
    _exact_keys(
        measured,
        {
            "root_d5_oracle_signature_sha256",
            "opera_total_d1_through_d5_seconds",
            "completed_depth",
            "width",
            "workers",
            "initial_full_wave",
            "result",
            "memory",
            "opera_checked_horizon",
        },
        "release measured evidence",
    )
    oracle_signature = measured.get("root_d5_oracle_signature_sha256")
    if not isinstance(oracle_signature, str) or HEX_64.fullmatch(
        oracle_signature
    ) is None:
        raise PromotedReleaseError("root D5 oracle signature must be a lowercase SHA-256")
    if oracle_signature != semantic.oracle_signature_sha256:
        raise PromotedReleaseError(
            "release root D5 oracle signature differs from its validated evidence"
        )
    opera_elapsed = _finite_number(
        measured.get("opera_total_d1_through_d5_seconds"),
        "Opera D1-D5 elapsed seconds",
    )
    if (
        measured.get("completed_depth") != 5
        or measured.get("width") != 32
        or measured.get("workers") != 8
        or measured.get("initial_full_wave") != 8
        or not 0 < opera_elapsed < 60
    ):
        raise PromotedReleaseError(
            "release measured evidence does not prove the required D1-D5 geometry"
        )
    measured_result = _mapping(measured.get("result"), "release measured result")
    measured_memory = _mapping(measured.get("memory"), "release measured memory")
    if (
        opera_elapsed != semantic.opera_elapsed_seconds
        or measured_result != semantic.opera_result
        or measured_memory != semantic.opera_memory
    ):
        raise PromotedReleaseError(
            "release Opera measurements differ from the validated core evidence"
        )
    checked_horizon = _mapping(
        measured.get("opera_checked_horizon"),
        "release measured Opera checked horizon",
    )
    _exact_keys(
        checked_horizon,
        {
            "elapsed_seconds",
            "work",
            "selected_root_series",
            "pv_horizon_line_rejections",
            "pv_horizon_native_repairs",
            "pv_horizon_candidate_vetoes",
            "principal_variation_sha256",
            "selected_fixture_id",
            "known_adverse_excluded",
            "selected_horizon_exhaustively_certified",
            "selected_root_child_exhaustively_certified",
            "raw_trace_attestation",
            "selected_d5_horizon_certification_witness",
            "local_checkout_asset_set_sha256",
        },
        "release measured Opera checked horizon",
    )
    checked_elapsed = _finite_number(
        checked_horizon.get("elapsed_seconds"),
        "Opera checked-horizon elapsed seconds",
    )
    checked_work = _integer(
        checked_horizon.get("work"),
        "Opera checked-horizon work",
        1,
    )
    selected_root_series = checked_horizon.get("selected_root_series")
    if (
        not isinstance(selected_root_series, str)
        or re.fullmatch(
            r"[a-h][1-8][a-h][1-8][nbrq]?(?:/[a-h][1-8][a-h][1-8][nbrq]?)*",
            selected_root_series,
        )
        is None
        or selected_root_series != "b2b3"
    ):
        raise PromotedReleaseError("Opera checked horizon has invalid root-series notation")
    line_rejections = _integer(
        checked_horizon.get("pv_horizon_line_rejections"),
        "Opera checked-horizon line rejections",
        2,
    )
    native_repairs = _integer(
        checked_horizon.get("pv_horizon_native_repairs"),
        "Opera checked-horizon native repairs",
        1,
    )
    candidate_vetoes = _integer(
        checked_horizon.get("pv_horizon_candidate_vetoes"),
        "Opera checked-horizon candidate vetoes",
    )
    local_asset_set = checked_horizon.get("local_checkout_asset_set_sha256")
    principal_variation_sha256 = checked_horizon.get("principal_variation_sha256")
    selected_fixture_id = checked_horizon.get("selected_fixture_id")
    known_adverse_excluded = checked_horizon.get("known_adverse_excluded")
    selected_horizon_certified = checked_horizon.get(
        "selected_horizon_exhaustively_certified"
    )
    selected_root_child_certified = checked_horizon.get(
        "selected_root_child_exhaustively_certified"
    )
    raw_trace_attestation = _mapping(
        checked_horizon.get("raw_trace_attestation"),
        "release measured Opera raw trace attestation",
    )
    _exact_keys(
        raw_trace_attestation,
        {
            "schema",
            "horizon_safety_trace_count",
            "horizon_safety_trace_sha256",
            "horizon_research_trace_count",
            "horizon_research_trace_sha256",
        },
        "release measured Opera raw trace attestation",
    )
    raw_safety_count = _integer(
        raw_trace_attestation.get("horizon_safety_trace_count"),
        "Opera raw safety trace count",
        1,
    )
    raw_research_count = _integer(
        raw_trace_attestation.get("horizon_research_trace_count"),
        "Opera raw research trace count",
        1,
    )
    raw_safety_sha256 = raw_trace_attestation.get("horizon_safety_trace_sha256")
    raw_research_sha256 = raw_trace_attestation.get("horizon_research_trace_sha256")
    selected_witness = _mapping(
        checked_horizon.get("selected_d5_horizon_certification_witness"),
        "release measured selected D5 horizon certification witness",
    )
    _exact_keys(
        selected_witness,
        {
            "schema",
            "fixture_id",
            "selected_root_series",
            "candidate_identity",
            "owner_worker_id",
            "principal_variation_sha256",
            "selected_series5_semantic_sha256",
            "known_adverse_series5_semantic_sha256",
            "known_adverse_present",
            "horizon_request_sequence",
            "horizon_status",
            "horizon_call_work_credit",
            "horizon_work_used",
            "root_child_request_sequence",
            "root_child_status",
            "root_child_call_work_credit",
            "root_child_work_used",
            "safety_work_used",
            "safety_call_work_credit",
        },
        "release measured selected D5 horizon certification witness",
    )
    horizon_sequence = _integer(
        selected_witness.get("horizon_request_sequence"),
        "selected D5 horizon request sequence",
        1,
    )
    root_child_sequence = _integer(
        selected_witness.get("root_child_request_sequence"),
        "selected D5 root-child request sequence",
        1,
    )
    horizon_credit = _integer(
        selected_witness.get("horizon_call_work_credit"),
        "selected D5 horizon work credit",
        1,
    )
    horizon_work = _integer(
        selected_witness.get("horizon_work_used"),
        "selected D5 horizon work",
        1,
    )
    root_child_credit = _integer(
        selected_witness.get("root_child_call_work_credit"),
        "selected D5 root-child work credit",
        1,
    )
    root_child_work = _integer(
        selected_witness.get("root_child_work_used"),
        "selected D5 root-child work",
        1,
    )
    safety_work = _integer(
        selected_witness.get("safety_work_used"),
        "selected D5 total safety work",
        1,
    )
    if (
        not 0 < checked_elapsed < 60
        or checked_work > default_generation_positions
        or line_rejections != 2
        or native_repairs != 1
        or candidate_vetoes != 1
        or native_repairs + candidate_vetoes != line_rejections
        or not isinstance(principal_variation_sha256, str)
        or HEX_64.fullmatch(principal_variation_sha256) is None
        or selected_fixture_id != evidence_producer.SELECTED_D5_FIXTURE_ID
        or known_adverse_excluded is not True
        or selected_horizon_certified is not True
        or selected_root_child_certified is not True
        or raw_trace_attestation.get("schema")
        != evidence_producer.RAW_TRACE_ATTESTATION_SCHEMA
        or raw_safety_count < line_rejections
        or raw_research_count < native_repairs
        or not isinstance(raw_safety_sha256, str)
        or HEX_64.fullmatch(raw_safety_sha256) is None
        or not isinstance(raw_research_sha256, str)
        or HEX_64.fullmatch(raw_research_sha256) is None
        or selected_witness.get("schema")
        != evidence_producer.SELECTED_D5_HORIZON_CERTIFICATION_SCHEMA
        or selected_witness.get("fixture_id") != selected_fixture_id
        or selected_witness.get("selected_root_series") != selected_root_series
        or not isinstance(selected_witness.get("candidate_identity"), str)
        or not selected_witness.get("candidate_identity")
        or not isinstance(selected_witness.get("owner_worker_id"), str)
        or not selected_witness.get("owner_worker_id")
        or selected_witness.get("principal_variation_sha256")
        != principal_variation_sha256
        or not isinstance(selected_witness.get("selected_series5_semantic_sha256"), str)
        or HEX_64.fullmatch(selected_witness["selected_series5_semantic_sha256"])
        is None
        or not isinstance(
            selected_witness.get("known_adverse_series5_semantic_sha256"), str
        )
        or HEX_64.fullmatch(
            selected_witness["known_adverse_series5_semantic_sha256"]
        )
        is None
        or selected_witness.get("known_adverse_present") is not False
        or root_child_sequence <= horizon_sequence
        or selected_witness.get("horizon_status") != "exhausted"
        or horizon_credit != evidence_producer.PV_HORIZON_MATE_WORK_LIMIT
        or horizon_work > horizon_credit
        or selected_witness.get("root_child_status") != "exhausted"
        or root_child_credit
        != evidence_producer.CERTIFIED_SAFETY_RESERVE_POSITIONS - horizon_work
        or root_child_work > root_child_credit
        or safety_work != horizon_work + root_child_work
        or selected_witness.get("safety_call_work_credit")
        != evidence_producer.CERTIFIED_SAFETY_RESERVE_POSITIONS
        or safety_work > evidence_producer.CERTIFIED_SAFETY_RESERVE_POSITIONS
        or not isinstance(local_asset_set, str)
        or HEX_64.fullmatch(local_asset_set) is None
    ):
        raise PromotedReleaseError(
            "Opera checked-horizon measurements do not independently satisfy the gates"
        )
    semantic_checked = {
        "elapsed_seconds": semantic.checked_elapsed_seconds,
        "work": semantic.checked_work,
        "selected_root_series": semantic.checked_selected_root_series,
        "pv_horizon_line_rejections": semantic.checked_line_rejections,
        "pv_horizon_native_repairs": semantic.checked_native_repairs,
        "pv_horizon_candidate_vetoes": semantic.checked_candidate_vetoes,
        "principal_variation_sha256": semantic.checked_principal_variation_sha256,
        "selected_fixture_id": semantic.checked_selected_fixture_id,
        "known_adverse_excluded": semantic.checked_known_adverse_excluded,
        "selected_horizon_exhaustively_certified": (
            semantic.checked_selected_horizon_exhaustively_certified
        ),
        "selected_root_child_exhaustively_certified": (
            semantic.checked_selected_root_child_exhaustively_certified
        ),
        "raw_trace_attestation": dict(semantic.checked_raw_trace_attestation),
        "selected_d5_horizon_certification_witness": dict(
            semantic.checked_selected_d5_horizon_certification_witness
        ),
        "local_checkout_asset_set_sha256": semantic.checked_local_asset_set_sha256,
    }
    if checked_horizon != semantic_checked:
        raise PromotedReleaseError(
            "release checked-horizon measurements differ from the validated Opera receipt"
        )

    seed = {
        "artifact": {field: artifact[field] for field in ARTIFACT_IDENTITY_FIELDS},
        "bundle_set_sha256": browser_set_sha256,
        "certificate_set_sha256": certificate_set_sha256,
        "receipts": [
            {"label": record["label"], "sha256": record["sha256"]}
            for record in evidence_records
        ],
        "policy": {
            "maximum_seconds": policy["maximum_seconds"],
            "default_seconds": policy["default_seconds"],
        },
    }
    expected_release_id = f"spc-browser-wasm-release-{_canonical_sha256(seed)[:16]}"
    if receipt.get("release_id") != expected_release_id:
        raise PromotedReleaseError("release_id does not match the canonical promotion seed")

    head = _validate_artifact_commit(
        repository=repository,
        release_files=release_files,
        source_revision=source_revision,
    )
    _validate_static_mirrors(repository, expected_release)
    return {
        "schema": "spc-promoted-browser-wasm-deployment-validation-v1",
        "status": "validated",
        "product_publishable": True,
        "release_id": expected_release_id,
        "source_revision": source_revision,
        "artifact_revision": head,
        "browser_bundle": browser_record,
        "certificates": certificate_records,
        "certificate_payloads_validated": sorted(certificate_payloads),
        "evidence_receipts_validated": [record["label"] for record in evidence_records],
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fail closed unless tracked release/browser-wasm is an exact, parent-source-bound, "
            "tetizz-authorized browser WASM promotion."
        )
    )
    parser.add_argument("--repository", type=Path, default=ROOT)
    parser.add_argument("--release", type=Path, default=ROOT / "release" / "browser-wasm")
    parser.add_argument(
        "--source-package",
        type=Path,
        default=ROOT / "src" / "scottish_progressive",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_args(argv)
    try:
        result = validate_promoted_release(
            release=arguments.release,
            repository=arguments.repository,
            source_package=arguments.source_package,
        )
    except (FileNotFoundError, OSError, PromotedReleaseError, ValueError) as error:
        print(f"promoted release validation failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
