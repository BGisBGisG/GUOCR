@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 精简版：不内置离线翻译模型（约 200 MB，可发布到 GitHub Release），离线模型首次使用时自动下载
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv || goto :fail
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt pyinstaller || goto :fail
".venv\Scripts\python.exe" -m guocr.icon build\guocr.ico || goto :fail
set GUOCR_SLIM=1
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --distpath dist_slim --workpath build\slim GUOCR_onefile.spec || goto :fail
set GUOCR_SLIM=
start "" /wait "dist_slim\GUOCR.exe" --selftest
if errorlevel 1 (
    echo 自检失败，详见 %APPDATA%\GUOCR\guocr.log
    goto :fail
)
echo.
echo 打包完成：dist_slim\GUOCR.exe（精简版，不含离线翻译模型）
exit /b 0

:fail
echo 打包失败。
exit /b 1
