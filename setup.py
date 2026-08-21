from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil

from setuptools import Extension, setup
from setuptools.command.build_py import build_py


REPORT_FILES = (
    "initial-opening-ranking.json",
    "selective-opening-deepening.json",
    "published-reply-comparison.json",
)
NATIVE_SOURCE_FILES = (
    "_native_eval.cpp",
    "native_eval.hpp",
    "native_selfplay.cpp",
    "native_selfplay.hpp",
)
NATIVE_MATE_SOURCE_FILES = ("_native_mate.cpp",)


def engine_source_fingerprint(package: Path) -> str:
    digest = hashlib.sha256()
    paths = (
        path
        for pattern in ("*.py", "*.cpp", "*.hpp", "*.h")
        for path in package.rglob(pattern)
    )
    for path in sorted(paths, key=lambda item: item.relative_to(package).as_posix()):
        digest.update(path.relative_to(package).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def native_source_identity(package: Path) -> str:
    """Identity of the exact sources compiled into the optional extension."""

    digest = hashlib.sha256()
    for filename in NATIVE_SOURCE_FILES:
        path = package / filename
        digest.update(filename.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def native_mate_source_identity(package: Path) -> str:
    """Identity of the isolated one-series mate extension sources."""

    digest = hashlib.sha256()
    for filename in NATIVE_MATE_SOURCE_FILES:
        path = package / filename
        digest.update(filename.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


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
        omit_stale_reports = (
            os.environ.get("SPC_OMIT_STALE_OPENING_REPORTS") == "1"
        )
        for filename in REPORT_FILES:
            source = reports / filename
            if not source.is_file():
                raise FileNotFoundError(f"required opening report is missing: {source}")
            payload = json.loads(source.read_text(encoding="utf-8"))
            if payload.get("source_fingerprint") != fingerprint:
                if omit_stale_reports:
                    # Hosted play does not depend on optional theory reports.
                    # Never package stale evidence as though it described the
                    # deployed engine; omit it explicitly instead. Normal
                    # release builds remain fail-closed and require freshly
                    # generated reports.
                    continue
                raise RuntimeError(
                    f"opening report is stale for source {fingerprint}: {source}"
                )
            shutil.copyfile(source, target / filename)


native_compile_args = ["/std:c++20", "/O2"] if os.name == "nt" else [
    "-std=c++20",
    "-O3",
]
native_package = Path(__file__).resolve().parent / "src" / "scottish_progressive"
native_identity = native_source_identity(native_package)
native_mate_identity = native_mate_source_identity(native_package)

setup(
    cmdclass={"build_py": BuildPyWithOpeningReports},
    ext_modules=[
        Extension(
            "scottish_progressive._native_eval",
            sources=[
                "src/scottish_progressive/_native_eval.cpp",
                "src/scottish_progressive/native_selfplay.cpp",
            ],
            depends=[
                "src/scottish_progressive/native_eval.hpp",
                "src/scottish_progressive/native_selfplay.hpp",
            ],
            language="c++",
            optional=True,
            define_macros=[("SPC_NATIVE_SOURCE_IDENTITY", f'"{native_identity}"')],
            extra_compile_args=native_compile_args,
        ),
        Extension(
            "scottish_progressive._native_mate",
            sources=["src/scottish_progressive/_native_mate.cpp"],
            language="c++",
            optional=True,
            define_macros=[
                ("SPC_NATIVE_MATE_SOURCE_IDENTITY", f'"{native_mate_identity}"')
            ],
            extra_compile_args=native_compile_args,
        ),
    ],
)
