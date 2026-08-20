@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Setting up the local Scottish Progressive Chess environment...
  py -m venv .venv || exit /b 1
  ".venv\Scripts\python.exe" -m pip install -e ".[dev]" || exit /b 1
)

if not exist "data" mkdir "data"
if not exist "profiles" mkdir "profiles"

echo.
echo Detected training resources:
".venv\Scripts\python.exe" -m scottish_progressive.cli league resources || goto :failed
echo.
echo Starting or continuing the 10-engine Scottish Progressive league.
echo This intentionally uses the detected CPU and estimated RAM-planning maximum.
echo You can close this window and run this file again to resume its checkpoint.
echo.

".venv\Scripts\python.exe" -m scottish_progressive.cli league run "data\evolution.sqlite3" --continue-latest --champion-output "profiles\champion.json"
if errorlevel 1 goto :failed

echo.
echo Training run finished. The analysis board will use profiles\champion.json.
pause
exit /b 0

:failed
echo.
echo Training stopped with an error. Completed games remain checkpointed.
pause
exit /b 1
