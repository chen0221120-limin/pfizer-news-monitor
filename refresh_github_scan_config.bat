@echo off
setlocal
cd /d "%~dp0"

if exist "C:\Users\Lenovo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" (
  set "PYTHON_EXE=C:\Users\Lenovo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
) else (
  set "PYTHON_EXE=python"
)

"%PYTHON_EXE%" scripts\refresh_github_scan_config.py
echo.
pause
