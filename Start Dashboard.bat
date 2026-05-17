@echo off
title Stock Dashboard
cd /d "%~dp0"

echo ============================================
echo   Starting your Stock Dashboard...
echo   A web page will open in your browser.
echo   Keep this black window open while using it.
echo   Close this window to stop the dashboard.
echo ============================================
echo.

python -m streamlit run app.py

echo.
echo Dashboard stopped. You can close this window.
pause
