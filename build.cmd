@echo off
rem Builds dist\Chat Archiver.exe and dist\Chat Archiver Admin.exe.
rem Needs Chat Archiver Admin to be set up on this PC (or build\approval.json).
cd /d "%~dp0"
".venv\Scripts\python.exe" build.py %*
echo.
pause
