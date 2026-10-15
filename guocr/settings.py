"""设置对话框：三个功能的快捷键（实时检测冲突）及其他选项。"""
import os

from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QGridLayout, QHBoxLayout, QKeySequenceEdit, QLabel,
                               QMessageBox, QPushButton, QVBoxLayout)

from . import terms, winutil
from .config import ACTION_NAMES, ACTIONS, DEFAULT_HOTKEYS, Config
from .hotkey import is_hotkey_available, parse_hotkey
from .translate import ENGINES

OK_STYLE, BAD_STYLE, WARN_STYLE = "color: #1a7f37;", "color: #cf222e;", "color: #9a6700;"


class SettingsDialog(QDialog):
    """打开期间 App 会暂停自己的全局热键，所以这里的占用检测只反映其他程序。"""

    def __init__(self, cfg: Config, parent=None):
        super().__init__(parent)
        self.setWindowTitle("GUOCR 设置")
        self.cfg = cfg

        grid = QGridLayout()
        grid.addWidget(QLabel("快捷键（留空表示不启用）："), 0, 0, 1, 4)
        self.edits: dict[str, QKeySequenceEdit] = {}
        self.marks: dict[str, QLabel] = {}
        for row, action in enumerate(ACTIONS, start=1):
            edit = QKeySequenceEdit(QKeySequence(getattr(cfg, f"hotkey_{action}")))
            edit.setMaximumSequenceLength(1)
            edit.setClearButtonEnabled(True)
            edit.keySequenceChanged.connect(self._check_all)
            reset = QPushButton("默认")
            reset.clicked.connect(lambda _=False, a=action: self.edits[a].setKeySequence(
                QKeySequence(DEFAULT_HOTKEYS[a])))
            mark = QLabel()
            mark.setMinimumWidth(150)
            grid.addWidget(QLabel(f"{ACTION_NAMES[action]}："), row, 0)
            grid.addWidget(edit, row, 1)
            grid.addWidget(reset, row, 2)
            grid.addWidget(mark, row, 3)
            self.edits[action], self.marks[action] = edit, mark
        grid.setColumnStretch(1, 1)
        tips = QLabel("截图：复制图片到剪贴板　识别文字：复制文字　翻译：英译中，译文覆盖在原位")
        tips.setStyleSheet("color: #666;")

        self.show_result = QCheckBox("识别后弹出结果窗口（关闭则只在托盘气泡提示；翻译总会弹窗）")
        self.show_result.setChecked(cfg.show_result)
        self.keep_breaks = QCheckBox("保留屏幕上的原始换行（默认按段落输出：自动识别分栏/分页和分段，段内折行合并）")
        self.keep_breaks.setChecked(cfg.keep_raw_lines)
        self.engine = QComboBox()
        for key, (label, _) in ENGINES.items():
            self.engine.addItem(label, key)
        self.engine.setCurrentIndex(max(self.engine.findData(cfg.translate_engine), 0))
        engine_row = QHBoxLayout()
        engine_row.addWidget(QLabel("翻译引擎（英译中）："))
        engine_row.addWidget(self.engine, 1)
        # 离线翻译的专业词库
        self.domain_boxes = {}
        gloss_row = QHBoxLayout()
        gloss_row.addWidget(QLabel("离线翻译专业词库："))
        for key, name in terms.DOMAINS.items():
            box = QCheckBox(name)
            box.setChecked(key in cfg.glossary_domains)
            self.domain_boxes[key] = box
            gloss_row.addWidget(box)
        gloss_row.addStretch(1)
        edit_btn = QPushButton("编辑自定义词库…")
        edit_btn.setToolTip("每行一条「英文 = 中文」，优先级高于内置词库；保存后下次翻译即生效")
        edit_btn.clicked.connect(lambda: os.startfile(terms.ensure_user_glossary()))
        gloss_row.addWidget(edit_btn)
        self.autostart = QCheckBox("开机自动启动")
        self.autostart.setChecked(winutil.is_autostart_enabled())

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(grid)
        layout.addWidget(tips)
        layout.addSpacing(6)
        layout.addWidget(self.show_result)
        layout.addWidget(self.keep_breaks)
        layout.addLayout(engine_row)
        layout.addLayout(gloss_row)
        layout.addWidget(self.autostart)
        layout.addSpacing(8)
        layout.addWidget(buttons)
        self.resize(560, self.sizeHint().height())
        self._check_all()

    def _hotkey(self, action: str) -> str:
        return self.edits[action].keySequence().toString(QKeySequence.SequenceFormat.PortableText)

    def _check(self, action: str) -> tuple[str, str]:
        """返回 (级别, 说明)，级别为 ok / warn / bad。"""
        hotkey = self._hotkey(action)
        if not hotkey:
            return "warn", "未启用"
        try:
            combo = parse_hotkey(hotkey)
        except ValueError as e:
            return "bad", f"✗ {e}"
        for other in ACTIONS:
            if other != action and self._hotkey(other):
                try:
                    if parse_hotkey(self._hotkey(other)) == combo:
                        return "bad", f"✗ 与「{ACTION_NAMES[other]}」重复"
                except ValueError:
                    pass
        if not is_hotkey_available(hotkey):
            return "bad", "✗ 已被其他程序占用"
        return "ok", "✓ 可用"

    def _check_all(self) -> dict[str, tuple[str, str]]:
        results = {}
        for action in ACTIONS:
            level, text = results[action] = self._check(action)
            self.marks[action].setText(text)
            self.marks[action].setStyleSheet({"ok": OK_STYLE, "warn": WARN_STYLE, "bad": BAD_STYLE}[level])
        return results

    def accept(self):
        problems = [f"{ACTION_NAMES[a]}：{text.lstrip('✗ ')}" for a, (level, text) in self._check_all().items()
                    if level == "bad"]
        if problems:
            QMessageBox.warning(self, "快捷键有冲突", "以下快捷键无法使用，请更换或清空：\n\n" + "\n".join(problems))
            return
        for action in ACTIONS:
            self.cfg.set_hotkey(action, self._hotkey(action))
        self.cfg.show_result = self.show_result.isChecked()
        self.cfg.keep_raw_lines = self.keep_breaks.isChecked()
        self.cfg.translate_engine = self.engine.currentData()
        self.cfg.glossary_domains = [k for k, box in self.domain_boxes.items() if box.isChecked()]
        try:
            if self.autostart.isChecked() != winutil.is_autostart_enabled():
                winutil.set_autostart(self.autostart.isChecked())
        except OSError as e:
            QMessageBox.warning(self, "开机启动", f"设置开机启动失败：{e}")
        super().accept()
