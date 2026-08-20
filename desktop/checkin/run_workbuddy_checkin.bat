@echo off
cd /d "%~dp0"
set PYTHONPATH=
set PYTHONNOUSERSITE=1
C:\Python311\python.exe checkin_launcher.py run workbuddy
pause