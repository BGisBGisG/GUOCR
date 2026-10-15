@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv || goto :fail
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt pyinstaller || goto :fail
".venv\Scripts\python.exe" -m guocr.icon build\guocr.ico || goto :fail
rem 下载离线翻译模型（已存在则跳过）
".venv\Scripts\python.exe" -m guocr.translate || goto :fail
".venv\Scripts\pyinstaller.exe" --noconfirm --clean --windowed --name GUOCR ^
    --icon build\guocr.ico --collect-all rapidocr --collect-binaries ctranslate2 --collect-binaries onnxruntime --hidden-import win32com.client --hidden-import pythoncom ^
    --add-data "models\en_zh;models\en_zh" --add-data "guocr\glossary;guocr\glossary" ^
    --add-data "models\llm;models\llm" --add-data "models\llama_bin;models\llama_bin" main.py || goto :fail
rem 删掉用不到的大文件：OpenCV 视频解码（约 29 MB）、Qt 软件 OpenGL（约 20 MB）
del /q "dist\GUOCR\_internal\cv2\opencv_videoio_ffmpeg*.dll" "dist\GUOCR\_internal\PySide6\opengl32sw.dll" 2>nul
start "" /wait "dist\GUOCR\GUOCR.exe" --selftest
if errorlevel 1 (
    echo 自检失败，详见 %APPDATA%\GUOCR\guocr.log
    goto :fail
)
echo.
echo 打包完成：dist\GUOCR\GUOCR.exe（分发时请复制整个 dist\GUOCR 文件夹）
exit /b 0

:fail
echo 打包失败。
exit /b 1
