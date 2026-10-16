@echo off
REM ---------------------------------------------------------------------
REM  Rebuild dist\pocket-share.exe from teleport_share.py.
REM
REM  Double-click this. It is the only supported way to produce the app,
REM  so the build flags cannot drift from what is documented in README.md.
REM
REM  Build-time only, never needed to run the app:
REM      pip install pyinstaller pywebview
REM ---------------------------------------------------------------------
setlocal
cd /d "%~dp0"

echo.
echo === pocket-share build ===
echo.

REM ---- 1. sanity: is there anything to build? ----
if not exist "teleport_share.py" (
    echo ERROR: teleport_share.py is missing. Run this from the project folder.
    goto :failed
)
if not exist "qr.py" (
    echo ERROR: qr.py is missing. The app draws its own QR codes with it.
    goto :failed
)
if not exist "logo.ico" (
    echo ERROR: logo.ico is missing. The app serves it as its icon and logo.
    goto :failed
)

REM ---- 2. tools: install what is missing, but never silently upgrade ----
python -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo Installing PyInstaller...
    python -m pip install --quiet pyinstaller || goto :failed
)
python -c "import webview" 2>nul
if errorlevel 1 (
    echo Installing pywebview...
    python -m pip install --quiet pywebview || goto :failed
)

REM ---- 3. stop anything already running, or the new .exe cannot be written ----
taskkill /f /im pocket-share.exe >nul 2>&1
taskkill /f /im msedgewebview2.exe >nul 2>&1

REM ---- 4. build ----
REM  --onefile   a single portable .exe
REM  --noconsole a window, not a console full of logs
REM  --icon      the logo in the taskbar and title bar
REM  --add-data  bundles logo.ico so the built app carries its own artwork
REM  --hidden-import webview  pywebview is imported by name inside a function
python -m PyInstaller --onefile --noconsole --name pocket-share --clean --noconfirm --icon logo.ico --add-data "logo.ico;." --hidden-import webview teleport_share.py
if errorlevel 1 (
    echo.
    echo ERROR: the build failed. See the messages above.
    goto :failed
)

REM ---- 5. report ----
if not exist "dist\pocket-share.exe" (
    echo.
    echo ERROR: the build reported success but produced no .exe.
    goto :failed
)

for %%F in ("dist\pocket-share.exe") do set SIZE=%%~zF
set /a MB=%SIZE%/1048576

echo.
echo === done ===
echo   dist\pocket-share.exe  (%MB% MB)
echo.
echo The .env and logo.ico are bundled inside the .exe, so it can be copied
echo to another machine on its own. SharedFiles and .env are created beside
echo the .exe on first run.
echo.
pause
exit /b 0

:failed
echo.
pause
exit /b 1