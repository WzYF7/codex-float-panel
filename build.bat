@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul || (echo [x] 未找到 python，请先安装 Python 3.10+ & pause & exit /b 1)
python -m pip install --quiet --upgrade pyinstaller || (echo [x] 安装 pyinstaller 失败 & pause & exit /b 1)
python -m PyInstaller --noconfirm --clean --onefile --noconsole ^
  --name CodexFloat --icon "src\icon.ico" --paths "src" --hidden-import nav_core ^
  "src\float_nav.py"
echo.
echo [√] 构建完成：dist\CodexFloat.exe
pause
