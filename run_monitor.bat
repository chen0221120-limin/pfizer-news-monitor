@echo off
setlocal
cd /d "%~dp0"

python scripts\pfizer_news_monitor.py ^
  --state .state\pfizer_news_seen.json ^
  --report-prefix gi-oncology-monitor

set EXIT_CODE=%ERRORLEVEL%
echo.
if %EXIT_CODE%==0 (
  echo GI monitor scan finished.
) else (
  echo GI monitor scan failed with exit code %EXIT_CODE%.
)
pause
exit /b %EXIT_CODE%
