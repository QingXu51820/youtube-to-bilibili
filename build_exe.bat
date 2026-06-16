@echo off
setlocal enabledelayedexpansion
REM ============================================================
REM  yt2bili.exe build script
REM  Prerequisites: Python 3.12, all deps installed, conda env "yt2bili"
REM  Optional: UPX on PATH for compression
REM ============================================================

echo ============================================================
echo   yt2bili.exe Builder
echo ============================================================
echo.

REM --- Activate conda env (fall back to current env) -----------
call conda activate yt2bili 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo [WARN] conda env 'yt2bili' not found, using current Python
)

REM --- Check Python --------------------------------------------
python --version 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Python not found on PATH
    exit /b 1
)

REM --- Install PyInstaller if missing --------------------------
python -c "import PyInstaller" 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo [INFO] Installing PyInstaller...
    pip install pyinstaller
    if %ERRORLEVEL% NEQ 0 (
        echo [ERROR] Failed to install PyInstaller
        exit /b 1
    )
)

REM --- Check UPX (optional) ------------------------------------
where upx >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo [INFO] UPX found - compression enabled
) else (
    echo [INFO] UPX not found - building without compression (larger EXE)
    echo        Install from https://upx.github.io/ for ~40%% smaller output
)

REM --- Build ---------------------------------------------------
echo.
echo [BUILD] Running PyInstaller...
pyinstaller --clean yt2bili.spec
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Build FAILED
    exit /b 1
)

REM --- Copy runtime files --------------------------------------
echo.
echo [INFO] Copying runtime files to dist\...
if not exist "dist\.env" (
    copy /Y .env.example dist\.env >nul 2>&1
    echo [INFO] Created dist\.env from .env.example
)
if exist client_secret.json copy /Y client_secret.json dist\ >nul 2>&1
if exist urls.example.txt copy /Y urls.example.txt dist\urls.txt >nul 2>&1

REM --- Report --------------------------------------------------
echo.
echo ============================================================
echo   Build SUCCEEDED
echo ============================================================
for %%F in (dist\yt2bili.exe) do (
    set "size=%%~zF"
    set /a "mb=!size!/1048576"
    echo   Output: dist\yt2bili.exe  (~!mb! MB)
)
echo.
echo   Quick start:
echo     dist\yt2bili.exe --help
echo     dist\yt2bili.exe --monitor --refresh-youtube-cookies
echo ============================================================
exit /b 0
