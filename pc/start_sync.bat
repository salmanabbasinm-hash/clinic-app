@echo off
chcp 65001 >nul
REM 1) edit the path below to your Access file
REM 2) first time only:  pip install pyodbc
python "%~dp0clinic_sync.py" "C:\Clinic\File1.mdb"
pause
