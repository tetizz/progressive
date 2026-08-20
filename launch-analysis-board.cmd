@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Setting up the local Scottish Progressive Chess environment...
  py -m venv .venv || exit /b 1
  ".venv\Scripts\python.exe" -m pip install -e ".[dev]" || exit /b 1
)

if exist "profiles\champion.json" (
  ".venv\Scripts\python.exe" -m scottish_progressive.cli web --engine-profile "profiles\champion.json"
) else (
  ".venv\Scripts\python.exe" -m scottish_progressive.cli web
)
