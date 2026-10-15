# 单文件版打包配置：python -m PyInstaller --noconfirm --clean GUOCR_onefile.spec
# 完整版（默认）：一个文件包含全部依赖、OCR 模型、离线翻译大模型（Qwen2.5-3B）、小模型和词库，约 2.2 GB。
# 精简版（环境变量 GUOCR_SLIM=1，见 build_slim.bat）：不内置离线翻译模型，约 200 MB，可作为 GitHub Release 发布；
#   识别和在线翻译开箱即用，离线翻译模型首次需要时自动下载到 %LOCALAPPDATA%/GUOCR/models。
import os

from PyInstaller.utils.hooks import collect_all, collect_dynamic_libs

SLIM = os.environ.get("GUOCR_SLIM") == "1"

datas, binaries, hiddenimports = collect_all("rapidocr")
binaries += collect_dynamic_libs("ctranslate2")
binaries += collect_dynamic_libs("onnxruntime")  # 含 DirectML.dll（显卡识别）
datas += [("guocr/glossary", "guocr/glossary")]
if not SLIM:
    datas += [("models/en_zh", "models/en_zh"),
              ("models/llm/qwen2.5-3b-instruct-q4_k_m.gguf", "models/llm"),
              ("models/llama_bin", "models/llama_bin")]  # llama.cpp Vulkan 版推理程序（自动选显卡/CPU）

hiddenimports += ["win32com", "win32com.client", "pythoncom", "pywintypes"]  # 读取 Word 原文
a = Analysis(["main.py"], datas=datas, binaries=binaries, hiddenimports=hiddenimports,
             excludes=["tkinter", "matplotlib", "IPython", "pytest", "fitz", "pymupdf"])

# 用不到的大文件：OpenCV 视频解码（约 29 MB）、Qt 软件 OpenGL（约 20 MB）
_DROP = ("opencv_videoio_ffmpeg", "opengl32sw")
a.binaries = [b for b in a.binaries if not any(k in b[0].lower() for k in _DROP)]

pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="GUOCR", icon="build/guocr.ico",
          console=False, upx=False, runtime_tmpdir=None)
