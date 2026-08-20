from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import chess

from scottish_progressive.model import ENGINE_SOURCE_FINGERPRINT
from scottish_progressive.webapp import REPORT_FILES


ROOT = Path(__file__).resolve().parents[1]


def test_cold_wheel_serves_current_openings_outside_the_repository(
    tmp_path: Path,
) -> None:
    wheel_dir = tmp_path / "wheel"
    install_dir = tmp_path / "installed"
    run_dir = tmp_path / "unrelated-working-directory"
    wheel_dir.mkdir()
    run_dir.mkdir()
    ambient_reports = tmp_path / "reports"
    ambient_reports.mkdir()
    (ambient_reports / REPORT_FILES["initial_ranking"]).write_text(
        json.dumps({"source_fingerprint": "ambient-stale-report"}),
        encoding="utf-8",
    )

    build = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            str(ROOT),
            "--no-deps",
            "--wheel-dir",
            str(wheel_dir),
        ],
        cwd=run_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = list(wheel_dir.glob("scottish_progressive-*.whl"))
    assert len(wheels) == 1, build.stdout + build.stderr
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--target",
            str(install_dir),
            str(wheels[0]),
        ],
        cwd=run_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    installed_reports = install_dir / "scottish_progressive" / "reports"
    assert not (install_dir / "scottish_progressive" / "experiments").exists()
    for filename in REPORT_FILES.values():
        assert (installed_reports / filename).read_bytes() == (
            ROOT / "reports" / filename
        ).read_bytes()

    dependency_paths = [str(Path(chess.__file__).resolve().parent.parent)]
    probe = r"""
import json
import pathlib
import sys
import threading
from urllib.request import urlopen

install_dir = pathlib.Path(sys.argv[1]).resolve()
for item in json.loads(sys.argv[2]):
    sys.path.append(item)
sys.path.insert(0, str(install_dir))

from scottish_progressive import webapp

module_path = pathlib.Path(webapp.__file__).resolve()
reports_path = webapp._default_reports_dir().resolve()
assert module_path.is_relative_to(install_dir), module_path
assert reports_path.is_relative_to(install_dir), reports_path

server = webapp.create_server("127.0.0.1", 0)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
try:
    with urlopen(
        f"http://127.0.0.1:{server.server_port}/api/openings",
        timeout=5,
    ) as response:
        payload = json.loads(response.read())
    with urlopen(
        f"http://127.0.0.1:{server.server_port}/study-safety.js",
        timeout=5,
    ) as response:
        study_asset = response.read().decode("utf-8")
finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)

print(json.dumps({
    "module_path": str(module_path),
    "reports_path": str(reports_path),
    "study_asset_loaded": "confirmSavedPositionReplacement" in study_asset,
    "payload": payload,
}))
"""
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            probe,
            str(install_dir),
            json.dumps(dependency_paths),
        ],
        cwd=run_dir,
        env={**os.environ, "PYTHONPATH": ""},
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(completed.stdout.strip())
    openings = result["payload"]

    assert openings["available"] is True
    assert openings["source_fingerprint"] == ENGINE_SOURCE_FINGERPRINT
    assert set(openings["reports"]) == set(REPORT_FILES)
    assert all(report["current"] for report in openings["reports"].values())
    assert result["study_asset_loaded"] is True
