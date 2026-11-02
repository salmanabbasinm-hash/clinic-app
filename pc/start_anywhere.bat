@echo off
chcp 65001 >nul
REM Same as start_sync.bat but also opens a public https link (needs cloudflared.exe next to this file)
python "%~dp0clinic_sync.py" "C:\Clinic\File1.mdb" --public
pause
