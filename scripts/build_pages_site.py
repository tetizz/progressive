from __future__ import annotations

import argparse
from pathlib import Path
import re
import shutil


ASSET_REFERENCES = (
    ("href=\"./styles.css\"", "href=\"./styles.css?v={version}\""),
    ("src=\"./study-safety.js\"", "src=\"./study-safety.js?v={version}\""),
    (
        "src=\"./evaluation-format.js\"",
        "src=\"./evaluation-format.js?v={version}\"",
    ),
    ("src=\"./play-handoff.js\"", "src=\"./play-handoff.js?v={version}\""),
    ("src=\"./play-timeline.js\"", "src=\"./play-timeline.js?v={version}\""),
    ("src=\"./app.js\"", "src=\"./app.js?v={version}\""),
)


def build_pages_site(source: Path, output: Path, version: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", version):
        raise ValueError("asset version must be URL-safe")
    if output.exists():
        raise FileExistsError(f"output already exists: {output}")

    shutil.copytree(source, output)
    index_path = output / "index.html"
    index = index_path.read_text(encoding="utf-8")
    for original, versioned in ASSET_REFERENCES:
        count = index.count(original)
        if count != 1:
            raise ValueError(f"expected one {original!r} reference, found {count}")
        index = index.replace(original, versioned.format(version=version))
    index_path.write_text(index, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a GitHub Pages artifact with commit-addressed assets."
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    arguments = parser.parse_args()
    build_pages_site(arguments.source, arguments.output, arguments.version)


if __name__ == "__main__":
    main()
