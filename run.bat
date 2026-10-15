@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo 首次运行：正在创建虚拟环境并安装依赖，请稍候……
    python -m venv .venv || goto :fail
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :fail
)
if not exist "models\llm\qwen2.5-3b-instruct-q4_k_m.gguf" (
    echo 正在下载离线翻译模型（约 2 GB，只需一次）……
    ".venv\Scripts\python.exe" -m guocr.translate
)
start "" ".venv\Scripts\pythonw.exe" main.py
exit /b 0

:fail
echo 安装依赖失败，请检查 Python 与网络。
pause
exit /b 1
