@echo off
setlocal enabledelayedexpansion
REM ============================================================
REM  AI Gateway Pro - One-click full build
REM  Usage: scripts\build_all.bat            (in cmd)
REM         cmd //c scripts\build_all.bat     (in git bash)
REM  Steps: kill proc -> frontend build -> MSVC -> Cython -> PyInstaller
REM ============================================================
cd /d "%~dp0\.."

set LOG=%CD%\build_log_latest.txt
> "%LOG%" echo === Build started %date% %time% ===

echo ================================================
echo   AI Gateway Pro - Full Build
echo   Frontend ^| Cython ^| PyInstaller
echo ================================================
echo   Log: %LOG%
echo.

REM ---------- 0. Pre-check ----------
echo [0/5] Pre-check...
if not exist ".venv\Scripts\python.exe" (
    echo   [X] .venv\Scripts\python.exe not found
    echo   Run: python -m venv .venv
    exit /b 1
)
where npm >nul 2>&1
if errorlevel 1 (
    echo   [X] npm not found, install Node.js first
    exit /b 1
)
if not exist "web-vue\node_modules" (
    echo   [i] node_modules missing, installing...
    pushd web-vue
    call npm install
    if errorlevel 1 ( echo   [X] npm install failed & popd & exit /b 1 )
    popd
)
echo   [v] Pre-check OK
echo.

REM ---------- 1. Kill processes ----------
echo [1/5] Killing processes that may lock files...
REM Kill any exe launched from this project's dist\ (path match, avoids CJK process name)
powershell -NoProfile -Command "Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -like '*ai-gateway-pro*dist*' } | Stop-Process -Force -ErrorAction SilentlyContinue"
REM Kill anything listening on port 8000 (uvicorn dev server)
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000 " ^| findstr LISTENING 2^>nul') do (
    taskkill /f /pid %%a >nul 2>&1
)
echo   [v] Processes cleared
echo.

REM ---------- 2. Frontend build ----------
echo [2/5] Frontend build (web-vue: npm run build)...
pushd web-vue
call npm run build >> "%LOG%" 2>&1
if errorlevel 1 (
    echo   [X] Frontend build failed, see %LOG%
    type "%LOG%" | findstr /i "error"
    popd
    exit /b 1
)
popd
if not exist "web\index.html" (
    echo   [X] web\index.html not found after build
    exit /b 1
)
echo   [v] Frontend build done
echo.

REM ---------- 3. Load MSVC ----------
echo [3/5] Loading MSVC toolchain (vcvars64)...
set "VCFOUND="
for %%V in (
    "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
    "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat"
    "C:\Program Files\Microsoft Visual Studio\2022\Professional\VC\Auxiliary\Build\vcvars64.bat"
    "C:\Program Files\Microsoft Visual Studio\2022\Enterprise\VC\Auxiliary\Build\vcvars64.bat"
    "C:\Program Files (x86)\Microsoft Visual Studio\2019\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
) do (
    if exist %%~V set "VCFOUND=%%~V"
)
if not defined VCFOUND (
    echo   [X] vcvars64.bat not found, install VS Build Tools with C++ workload
    exit /b 1
)
call "%VCFOUND%" >nul 2>&1
where cl >nul 2>&1
if errorlevel 1 (
    echo   [X] cl.exe not found after vcvars64 - MSVC install issue
    exit /b 1
)
REM Strip mingw gcc from PATH so Cython uses MSVC cl.exe, not gcc
set "MINGW_GCC="
for /f "tokens=*" %%p in ('where gcc 2^>nul') do (
    echo %%p | findstr /i "mingw" >nul && set "MINGW_GCC=%%p"
)
if defined MINGW_GCC echo   [i] Note: mingw gcc detected at !MINGW_GCC! - vcvars64 should take priority
echo   [v] MSVC loaded
echo.

REM ---------- 4. Cython + PyInstaller ----------
echo [4/5] Backend build (Cython 18 modules + PyInstaller)...
echo   --- build_exe.py output goes to %LOG% ---
".venv\Scripts\python.exe" scripts\build_exe.py >> "%LOG%" 2>&1
if errorlevel 1 (
    echo   [X] Backend build failed, see %LOG%
    echo   --- last 40 lines of log ---
    powershell -NoProfile -Command "Get-Content '%LOG%' -Tail 40"
    exit /b 1
)
echo   [v] Backend build done
echo.

REM ---------- 5. Verify output ----------
echo [5/5] Verifying output...
set "EXEPATH="
for %%I in ("dist\*.exe") do set "EXEPATH=%%~I"
if not defined EXEPATH (
    echo   [X] No exe found in dist\
    exit /b 1
)
for %%I in ("%EXEPATH%") do set EXESIZE=%%~zI
set /a EXEMB=!EXESIZE! / 1048576
echo   [v] Output: %EXEPATH%  (!EXEMB! MB)
echo.

echo ================================================
echo   BUILD SUCCESS
echo   Output: %EXEPATH%  (!EXEMB! MB)
echo   Log:    %LOG%
echo ================================================
echo.
echo   [!] dist exe is NOT auto-deployed. Production runs from D:\AI* WangGuan dir.
echo       Deploy now:  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\deploy_gw.ps1
endlocal
