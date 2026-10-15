@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv || goto :fail
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt pyinstaller || goto :fail
".venv\Scripts\python.exe" -m guocr.icon build\guocr.ico || goto :fail
".venv\Scripts\python.exe" -m guocr.translate || goto :fail
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --distpath dist_onefile GUOCR_onefile.spec || goto :fail
start "" /wait "dist_onefile\GUOCR.exe" --selftest
if errorlevel 1 (
    echo 自检失败，详见 %APPDATA%\GUOCR\guocr.log
    goto :fail
)
echo.
echo 打包完成：dist_onefile\GUOCR.exe（单个文件，可直接拷到任何 64 位 Windows 上运行）
rem 同步一份到 OneDrive（用户要求每次打包后都更新这里）
set "SYNC_DIR=%USERPROFILE%\OneDrive - University of Georgia\Beikang Gu"
if exist "%SYNC_DIR%\" (
    copy /y "dist_onefile\GUOCR.exe" "%SYNC_DIR%\GUOCR.exe" >nul && echo 已更新：%SYNC_DIR%\GUOCR.exe
)
exit /b 0

:fail
echo 打包失败。
exit /b 1
