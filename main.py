"""启动入口（也是 PyInstaller 的打包入口）。"""
import sys

from guocr.app import main

if __name__ == "__main__":
    sys.exit(main())
