@echo off
chcp 65001 >nul
rem Windows 双击启动（第一次会自动创建虚拟环境并安装依赖）
cd /d "%~dp0"
echo.
echo   正在启动… / Starting…
echo.
if exist ".venv\Scripts\python.exe" goto run
set "PY="
where py >nul 2>nul && py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && python -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo   没有找到 Python 3.12 或更高版本，请先安装（安装时勾选 Add Python to PATH）：
  echo   Python 3.12+ not found. Please install it ^(tick "Add Python to PATH"^):
  echo   https://www.python.org/downloads/
  start "" "https://www.python.org/downloads/"
  pause
  exit /b 1
)
echo   正在创建运行环境… / Creating the runtime environment…
%PY% -m venv .venv
if errorlevel 1 (
  echo   创建虚拟环境失败 / Could not create the virtual environment
  pause
  exit /b 1
)
:run
".venv\Scripts\python.exe" run.py %*
if errorlevel 1 pause
