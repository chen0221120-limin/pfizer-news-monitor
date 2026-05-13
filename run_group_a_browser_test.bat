@echo off
setlocal
cd /d "%~dp0"

set "BROWSER_DISCOVERY_ENABLED=true"
set "NODE_EXECUTABLE=C:\Users\Lenovo\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
set "NODE_MODULES_DIR=C:\Users\Lenovo\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\node_modules"

"C:\Users\Lenovo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" scripts\pfizer_news_monitor_browser_trial.py ^
  --company-group-count 2 ^
  --company-group-index 1 ^
  --state .state\pfizer_news_seen_group_a.json ^
  --report-prefix gi-oncology-monitor-group-a-browser-test

set EXIT_CODE=%ERRORLEVEL%
echo.
if %EXIT_CODE%==0 (
  echo Group A browser-assisted scan finished.
) else (
  echo Group A browser-assisted scan failed with exit code %EXIT_CODE%.
)
pause
exit /b %EXIT_CODE%
