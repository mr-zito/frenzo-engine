@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"

rem ---- find Python (python on PATH, the py launcher, or Anaconda/Miniconda) ----
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY where py >nul 2>nul && set "PY=py -3"
if not defined PY if exist "%USERPROFILE%\anaconda3\python.exe" set "PY=%USERPROFILE%\anaconda3\python.exe"
if not defined PY if exist "%USERPROFILE%\miniconda3\python.exe" set "PY=%USERPROFILE%\miniconda3\python.exe"
if not defined PY (
  echo Python was not found. Install it from python.org ^(tick "Add python.exe to PATH"^) and run this again.
  pause
  exit /b
)

rem ---- ffmpeg ----
where ffmpeg >nul 2>nul
if errorlevel 1 (
  if exist "%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe" (
    set "PATH=%LOCALAPPDATA%\Microsoft\WinGet\Links;%PATH%"
  ) else (
    echo Installing ffmpeg...
    winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
    set "PATH=%LOCALAPPDATA%\Microsoft\WinGet\Links;%PATH%"
  )
)

rem ---- Python environment ----
if not exist .venv\Scripts\python.exe (
  echo First run: creating the Python environment ^(a few minutes^)...
  "%PY%" -m venv .venv 2>nul || %PY% -m venv .venv
)
call .venv\Scripts\activate.bat
python -c "import streamlink, yt_dlp, faster_whisper, websocket, anthropic, cv2, flask, ruamel.yaml, nudenet, yt_dlp_ejs" 2>nul || (
  python -m pip install --upgrade pip
  python -m pip install -r requirements.txt
)

if not exist config.yaml (
  copy config.example.yaml config.yaml >nul
  echo.
  echo config.yaml was created. Open it in Notepad, fill in the PASTE_ values, then run this again.
  pause
  exit /b
)
rem ---- "run_windows.bat app": open the Frenzo app (and put a Frenzo shortcut on the Desktop) ----
if /I "%~1"=="app" (
  python -c "import webview, pystray, PyInstaller" 2>nul || python -m pip install -r requirements-app.txt
  python -m frenzo build-exe --if-needed
  python -m frenzo shortcut
  if exist "FrenzoApp\Frenzo\Frenzo.exe" (
    start "" "FrenzoApp\Frenzo\Frenzo.exe"
  ) else (
    start "" ".venv\Scripts\pythonw.exe" -m frenzo app
  )
  exit /b
)
if /I "%~1"=="build-exe" (
  python -c "import webview, pystray, PyInstaller" 2>nul || python -m pip install -r requirements-app.txt
  python -m frenzo build-exe
  python -m frenzo shortcut
  pause
  exit /b
)
python -m frenzo %*
pause
