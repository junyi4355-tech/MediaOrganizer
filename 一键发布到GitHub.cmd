@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   MediaOrganizer  -  publish to GitHub
echo ============================================
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0pub.ps1" %*
echo.
pause
