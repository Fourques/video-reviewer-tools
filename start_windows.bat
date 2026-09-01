@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo 错误：找不到 FFmpeg。请先安装 FFmpeg 并加入 PATH。
  pause
  exit /b 2
)
where py >nul 2>nul
if not errorlevel 1 (
  py -3 start.py %*
  goto finished
)
where python >nul 2>nul
if errorlevel 1 (
  echo 错误：找不到 Python 3。请先安装 Python 3.10 或更高版本。
  pause
  exit /b 2
)
python start.py %*
:finished
if errorlevel 1 pause
endlocal
