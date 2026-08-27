from __future__ import annotations

import argparse
from pathlib import Path
import re
import shutil


INDEX_ASSET_REFERENCES = (
    ("href=\"./styles.css\"", "href=\"./styles.css?v={version}\""),
    ("src=\"./study-safety.js\"", "src=\"./study-safety.js?v={version}\""),
    (
        "src=\"./evaluation-format.js\"",
        "src=\"./evaluation-format.js?v={version}\"",
    ),
    ("src=\"./play-handoff.js\"", "src=\"./play-handoff.js?v={version}\""),
    ("src=\"./play-timeline.js\"", "src=\"./play-timeline.js?v={version}\""),
    (
        "src=\"./browser-prefix-contract.js\"",
        "src=\"./browser-prefix-contract.js?v={version}\"",
    ),
    (
        "src=\"./root-iteration-coordinator.js\"",
        "src=\"./root-iteration-coordinator.js?v={version}\"",
    ),
    (
        "src=\"./browser-root-iteration-client.js\"",
        "src=\"./browser-root-iteration-client.js?v={version}\"",
    ),
    (
        "src=\"./browser-engine-client.js\"",
        "src=\"./browser-engine-client.js?v={version}\"",
    ),
    (
        "src=\"./board-renderer.js\"",
        "src=\"./board-renderer.js?v={version}\"",
    ),
    ("src=\"./app.js\"", "src=\"./app.js?v={version}\""),
    (
        "href=\"./matches.html\"",
        "href=\"./matches.html?v={version}\"",
    ),
)
MATCH_ASSET_REFERENCES = (
    ("href=\"./styles.css\"", "href=\"./styles.css?v={version}\""),
    (
        "src=\"./board-renderer.js\"",
        "src=\"./board-renderer.js?v={version}\"",
    ),
    (
        "src=\"./match-viewer.js\"",
        "src=\"./match-viewer.js?v={version}\"",
    ),
)


def _version_page(path: Path, references: tuple[tuple[str, str], ...], version: str) -> None:
    page = path.read_text(encoding="utf-8")
    for original, versioned in references:
        count = page.count(original)
        if count != 1:
            raise ValueError(
                f"expected one {original!r} reference in {path.name}, found {count}"
            )
        page = page.replace(original, versioned.format(version=version))
    path.write_text(page, encoding="utf-8")


def build_pages_site(source: Path, output: Path, version: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", version):
        raise ValueError("asset version must be URL-safe")
    if output.exists():
        raise FileExistsError(f"output already exists: {output}")

    shutil.copytree(source, output)
    _version_page(output / "index.html", INDEX_ASSET_REFERENCES, version)
    _version_page(output / "matches.html", MATCH_ASSET_REFERENCES, version)


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
