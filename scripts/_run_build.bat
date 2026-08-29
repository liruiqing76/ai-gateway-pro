@echo off
call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat" >nul 2>&1
echo [%time%] VCARS_LOADED >> build_log_bat.txt
"D:\zmzc-code\ai-gateway-pro\.venv\Scripts\python.exe" scripts\build_exe.py >> build_log_bat.txt 2>&1
echo [%time%] BUILD_EXIT=%errorlevel% >> build_log_bat.txt
