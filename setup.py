from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py


REPORT_FILES = (
    "initial-opening-ranking.json",
    "selective-opening-deepening.json",
    "published-reply-comparison.json",
)


def engine_source_fingerprint(package: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(
        package.rglob("*.py"),
        key=lambda item: item.relative_to(package).as_posix(),
    ):
        digest.update(path.relative_to(package).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


class BuildPyWithOpeningReports(build_py):
    """Inject the authoritative generated reports into wheel package data."""

    def run(self) -> None:
        build_root = Path(self.build_lib).resolve()
        package = (build_root / "scottish_progressive").resolve()
        if package.parent != build_root:
            raise RuntimeError(f"unsafe package build path: {package}")
        if package.is_dir():
            shutil.rmtree(package)
        super().run()
        root = Path(__file__).resolve().parent
        reports = root / "reports"
        target = package / "reports"
        target.mkdir(parents=True, exist_ok=True)
        fingerprint = engine_source_fingerprint(package)
        for filename in REPORT_FILES:
            source = reports / filename
            if not source.is_file():
                raise FileNotFoundError(f"required opening report is missing: {source}")
            payload = json.loads(source.read_text(encoding="utf-8"))
            if payload.get("source_fingerprint") != fingerprint:
                raise RuntimeError(
                    f"opening report is stale for source {fingerprint}: {source}"
                )
            shutil.copyfile(source, target / filename)


setup(cmdclass={"build_py": BuildPyWithOpeningReports})
