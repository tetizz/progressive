from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
import tempfile
from typing import Any, Mapping


RECEIPT_FORMAT = "spc-frozen-fullgame-verification-receipt-v1"
SNAPSHOT_FORMAT = "spc-immutable-source-snapshot-v1"
_STREAM_DOMAIN = b"SPC-FROZEN-FULLGAME-STORE-V1\0"


def _canonical(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _snapshot_manifest(path: Path, snapshot_root: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    payload = json.loads(raw.decode("ascii"))
    if not isinstance(payload, dict) or payload.get("format") != SNAPSHOT_FORMAT:
        raise ValueError("unsupported immutable snapshot manifest")
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("immutable snapshot manifest has no file catalog")
    observed: list[dict[str, Any]] = []
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "size"}:
            raise ValueError("immutable snapshot file catalog is malformed")
        relative = Path(str(item["path"]))
        candidate = (snapshot_root / relative).resolve()
        if candidate == snapshot_root or snapshot_root not in candidate.parents:
            raise ValueError("immutable snapshot file escapes its root")
        if not candidate.is_file():
            raise ValueError(f"immutable snapshot file is missing: {relative.as_posix()}")
        observed.append(
            {
                "path": relative.as_posix(),
                "sha256": _sha256(candidate),
                "size": candidate.stat().st_size,
            }
        )
    if observed != files:
        raise ValueError("immutable snapshot files differ from their manifest")
    catalog_digest = hashlib.sha256(_canonical({"files": files})).hexdigest()
    if catalog_digest != payload.get("snapshot_file_manifest_digest"):
        raise ValueError("immutable snapshot file manifest digest is invalid")
    return payload, hashlib.sha256(raw).hexdigest()


def _store_catalog(root: Path) -> dict[str, Any]:
    manifest = root / "manifest.json"
    checkpoint = root / "checkpoint.sqlite3"
    chunks = root / "chunks"
    if not manifest.is_file() or not checkpoint.is_file() or not chunks.is_dir():
        raise ValueError("full-game store is missing manifest, checkpoint, or chunks")
    forbidden = tuple(chunks.glob("*.pending"))
    if forbidden:
        raise ValueError("full-game store has pending chunk files")
    wal = root / "checkpoint.sqlite3-wal"
    paths = (
        manifest,
        checkpoint,
        *((wal,) if wal.is_file() else ()),
        *sorted(chunks.glob("*.spcg")),
    )
    entries: list[dict[str, Any]] = []
    stream = hashlib.sha256(_STREAM_DOMAIN)
    for path in paths:
        relative = path.relative_to(root).as_posix()
        digest = hashlib.sha256()
        size = 0
        encoded = relative.encode("utf-8")
        stream.update(len(encoded).to_bytes(8, "big"))
        stream.update(encoded)
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                size += len(block)
                digest.update(block)
                stream.update(block)
        entries.append({"path": relative, "sha256": digest.hexdigest(), "size": size})
    payload = {"files": entries}
    return {
        "files": entries,
        "catalog_sha256": hashlib.sha256(_canonical(payload)).hexdigest(),
        "stream_sha256": stream.hexdigest(),
        "manifest_sha256": entries[0]["sha256"],
        "checkpoint_sha256": entries[1]["sha256"],
        "checkpoint_wal_sha256": next(
            (
                item["sha256"]
                for item in entries
                if item["path"] == "checkpoint.sqlite3-wal"
            ),
            None,
        ),
        "chunk_count": sum(item["path"].startswith("chunks/") for item in entries),
    }


def _atomic_json(payload: Mapping[str, Any], destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(_canonical(payload) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Verify a historical full-game store under its immutable runtime"
    )
    result.add_argument("store")
    result.add_argument("snapshot_root")
    result.add_argument("output")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    store = Path(args.store).expanduser().resolve()
    snapshot_root = Path(args.snapshot_root).expanduser().resolve()
    snapshot_manifest_path = snapshot_root / "manifest.json"
    snapshot_lib = snapshot_root / "lib"
    output = Path(args.output).expanduser().resolve()
    if output == store or store in output.parents:
        raise ValueError("verification receipt output must be outside the full-game store")
    if output == snapshot_root or snapshot_root in output.parents:
        raise ValueError("verification receipt output must be outside the immutable snapshot")
    snapshot, snapshot_manifest_sha256 = _snapshot_manifest(
        snapshot_manifest_path, snapshot_root
    )
    sys.path.insert(0, str(snapshot_lib))

    from scottish_progressive import evaluation
    from scottish_progressive.fullgame import verify_fullgame_run
    from scottish_progressive.fullgame_identity import FULLGAME_SEMANTIC_FINGERPRINT

    package_root = Path(evaluation.__file__).resolve().parent
    native = evaluation._native_eval
    native_path = None if native is None else Path(str(native.__file__)).resolve()
    if package_root.parent != snapshot_lib or native_path is None or native_path.parent != package_root:
        raise ValueError("frozen verifier imported code outside the immutable snapshot")
    if getattr(native, "SOURCE_IDENTITY", None) != snapshot.get("native_source_identity"):
        raise ValueError("frozen verifier native identity differs from the snapshot")
    if FULLGAME_SEMANTIC_FINGERPRINT != snapshot.get("semantic_fingerprint"):
        raise ValueError("frozen verifier semantic identity differs from the snapshot")

    before = _store_catalog(store)
    verification = verify_fullgame_run(store)
    after = _store_catalog(store)
    if before != after:
        raise ValueError("full-game store changed during frozen verification")
    manifest_raw = (store / "manifest.json").read_bytes()
    manifest = json.loads(manifest_raw.decode("ascii"))
    semantic = manifest.get("semantic_config")
    execution = manifest.get("execution")
    if not isinstance(semantic, dict) or not isinstance(execution, dict):
        raise ValueError("verified full-game manifest is incomplete")
    accepted = int(verification["accepted_unique_games"])
    target = int(execution["target_unique_games"])
    if accepted != target:
        raise ValueError(
            f"frozen full-game store is incomplete: {accepted}/{target} unique games"
        )
    verification_sha256 = hashlib.sha256(_canonical(verification)).hexdigest()
    receipt = {
        "format": RECEIPT_FORMAT,
        "receipt_generator_sha256": _sha256(Path(__file__).resolve()),
        "store_root": str(store),
        "snapshot_root": str(snapshot_root),
        "snapshot_manifest_sha256": snapshot_manifest_sha256,
        "snapshot_file_manifest_digest": snapshot["snapshot_file_manifest_digest"],
        "snapshot_semantic_fingerprint": snapshot["semantic_fingerprint"],
        "snapshot_native_source_identity": snapshot["native_source_identity"],
        "snapshot_native_binary_sha256": snapshot["native_binary_sha256"],
        "runtime": {
            "python_executable": str(Path(sys.executable).resolve()),
            "python_version": platform.python_version(),
            "package_root": str(package_root),
            "native_binary": str(native_path),
            "native_binary_sha256": _sha256(native_path),
            "native_loaded_source_identity": getattr(native, "SOURCE_IDENTITY", None),
        },
        "store": before,
        "simulation_id": manifest["simulation_id"],
        "semantic_config_sha256": hashlib.sha256(_canonical(semantic)).hexdigest(),
        "accepted_unique_games": accepted,
        "target_unique_games": target,
        "verification_result": verification,
        "verification_result_sha256": verification_sha256,
    }
    _atomic_json(receipt, output)
    print(_canonical(receipt).decode("ascii"))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
