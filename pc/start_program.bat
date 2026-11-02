@echo off
chcp 65001 >nul
REM Opens the clinic as a normal program window on this PC (needs Edge or Chrome).
REM 1) edit the path below to your Access file   2) first time only:  pip install pyodbc
python "%~dp0clinic_sync.py" "C:\Clinic\File1.mdb" --app
pause
