"""“翻译语境”对话框：管理预设，并用自然语言告诉本地大模型语境和词义，模型复述它记住了什么。"""
import html
import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
                               QListWidget, QMessageBox, QPlainTextEdit, QPushButton, QSplitter, QTableWidget,
                               QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget)

from . import context

_PLACEHOLDER = ("用自然语言随便写，长度不限，例如：\n"
                "这是一篇血栓力学的综述。clearance 在这里指血栓被清除，不是间隙；recanalization 译为再通；\n"
                "clot 统一译成血栓；作者名和期刊名保持英文不翻译。\n\n"
                "以后可以随时补充或修改（例如“删掉 clearance 那条”），Ctrl+Enter 发送。")


class _Relay(QObject):
    """后台线程 → 界面线程。"""
    done = Signal(str, str)  # 预设名, 模型复述


class ContextDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("翻译语境")
        self.resize(960, 760)
        self.presets, self.active = context.load()
        self.busy = False
        self.relay = _Relay()
        self.relay.done.connect(self._on_reply)

        # 左：预设列表
        self.list = QListWidget()
        self.list.currentRowChanged.connect(lambda _r: self._show_current())
        new_btn, ren_btn, del_btn = QPushButton("新建"), QPushButton("重命名"), QPushButton("删除")
        new_btn.clicked.connect(self._new)
        ren_btn.clicked.connect(self._rename)
        del_btn.clicked.connect(self._delete)
        btns = QHBoxLayout()
        for b in (new_btn, ren_btn, del_btn):
            btns.addWidget(b)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(QLabel("预设："))
        lv.addWidget(self.list, 1)
        lv.addLayout(btns)

        # 右：对话记录 + 输入
        self.history = QTextBrowser()
        self.history.setFont(QFont("Microsoft YaHei UI", 10))
        self.input = QPlainTextEdit()
        self.input.setPlaceholderText(_PLACEHOLDER)
        self.input.setFont(QFont("Microsoft YaHei UI", 10))
        self.send_btn = QPushButton("发送（Ctrl+Enter）")
        self.send_btn.clicked.connect(self._send)
        QShortcut(QKeySequence("Ctrl+Return"), self.input, activated=self._send)
        QShortcut(QKeySequence("Ctrl+Enter"), self.input, activated=self._send)
        self.status = QLabel()
        self.status.setStyleSheet("color: #666;")
        # 词义表：模型理解得不对时可以直接改
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["词（原文）", "在此语境下的含义 / 译法"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.cellChanged.connect(self._on_table_edited)
        add_row, del_row = QPushButton("新增一行"), QPushButton("删除选中")
        add_row.clicked.connect(self._add_row)
        del_row.clicked.connect(self._del_rows)
        trow = QHBoxLayout()
        trow.addWidget(QLabel("词义表（可直接编辑）："), 1)
        trow.addWidget(add_row)
        trow.addWidget(del_row)
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.addWidget(self.history, 3)
        rv.addLayout(trow)
        rv.addWidget(self.table, 2)
        rv.addWidget(self.input, 1)
        row = QHBoxLayout()
        row.addWidget(self.status, 1)
        row.addWidget(self.send_btn)
        rv.addLayout(row)

        split = QSplitter()
        split.addWidget(left)
        split.addWidget(right)
        split.setSizes([220, 680])

        # 上：翻译时使用哪个预设
        self.use = QComboBox()
        self.use.currentIndexChanged.connect(self._on_use_changed)
        top = QHBoxLayout()
        top.addWidget(QLabel("翻译时使用："))
        top.addWidget(self.use, 1)
        tip = QLabel("设了预设后，翻译会优先用本地大模型（Google 在线翻译无法接收语境说明）。")
        tip.setStyleSheet("color: #666;")

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(tip)
        layout.addWidget(split, 1)
        self._refresh()

    # ---------------------------------------------------------------- 列表
    def _refresh(self, select: str | None = None) -> None:
        cur = select or (self._current().name if self._current() else self.active)
        self.list.blockSignals(True)
        self.list.clear()
        for p in self.presets:
            self.list.addItem(p.name + ("  ✓" if p.name == self.active else ""))
        self.list.blockSignals(False)
        names = [p.name for p in self.presets]
        self.list.setCurrentRow(names.index(cur) if cur in names else (0 if names else -1))
        self.use.blockSignals(True)
        self.use.clear()
        self.use.addItem("不使用预设", "")
        for n in names:
            self.use.addItem(n, n)
        self.use.setCurrentIndex(max(self.use.findData(self.active), 0))
        self.use.blockSignals(False)
        self._show_current()

    def _current(self) -> context.Preset | None:
        row = self.list.currentRow()
        return self.presets[row] if 0 <= row < len(self.presets) else None

    def _show_current(self) -> None:
        p = self._current()
        self.input.setEnabled(p is not None and not self.busy)
        self.send_btn.setEnabled(p is not None and not self.busy)
        self._fill_table(p)
        if p is None:
            self.history.setHtml("<p style='color:#888'>先点左下角“新建”建一个预设。</p>")
            return
        parts = []
        for k, note in enumerate(p.notes):
            parts.append(f"<p><b>你：</b>{html.escape(note).replace(chr(10), '<br>')}</p>")
            if k < len(p.replies):
                parts.append(f"<p style='color:#1a5fb4'><b>模型：</b>{html.escape(p.replies[k]).replace(chr(10), '<br>')}</p>")
        if not parts:
            parts.append("<p style='color:#888'>还没有说明。在下面写下这个预设的语境、词义和要求。</p>")
        self.history.setHtml("".join(parts))
        self.history.verticalScrollBar().setValue(self.history.verticalScrollBar().maximum())

    # ---------------------------------------------------------------- 词义表
    def _fill_table(self, p) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for w, m in (p.terms.items() if p else []):
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.table.setItem(r, 0, QTableWidgetItem(w))
            self.table.setItem(r, 1, QTableWidgetItem(m))
        self.table.setEnabled(p is not None)
        self.table.blockSignals(False)

    def _table_terms(self) -> dict:
        terms = {}
        for r in range(self.table.rowCount()):
            w, m = self.table.item(r, 0), self.table.item(r, 1)
            w, m = (w.text().strip() if w else ""), (m.text().strip() if m else "")
            if w and m:
                terms[w] = m
        return terms

    def _on_table_edited(self, _r: int, _c: int) -> None:
        p = self._current()
        if p is None or self.busy:
            return
        p.terms = self._table_terms()
        context.save(self.presets, self.active)
        self.status.setText("词义表已保存。")

    def _add_row(self) -> None:
        if self._current() is None:
            return
        self.table.blockSignals(True)
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.table.setItem(r, 0, QTableWidgetItem(""))
        self.table.setItem(r, 1, QTableWidgetItem(""))
        self.table.blockSignals(False)
        self.table.editItem(self.table.item(r, 0))

    def _del_rows(self) -> None:
        p = self._current()
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        if p is None or not rows:
            return
        for r in rows:
            self.table.removeRow(r)
        p.terms = self._table_terms()
        context.save(self.presets, self.active)
        self.status.setText("已删除，词义表已保存。")

    def _new(self) -> None:
        name, ok = QInputDialog.getText(self, "新建预设", "预设名称（例如：血栓综述、审稿回复）：")
        name = name.strip()
        if not ok or not name:
            return
        if any(p.name == name for p in self.presets):
            QMessageBox.information(self, "新建预设", "已经有同名的预设了。")
            return
        self.presets.append(context.Preset(name))
        if not self.active:
            self.active = name  # 第一个预设默认就用它
        context.save(self.presets, self.active)
        self._refresh(name)
        self.input.setFocus()

    def _rename(self) -> None:
        p = self._current()
        if p is None:
            return
        name, ok = QInputDialog.getText(self, "重命名", "新名称：", text=p.name)
        name = name.strip()
        if not ok or not name or name == p.name or any(q.name == name for q in self.presets):
            return
        if self.active == p.name:
            self.active = name
        p.name = name
        context.save(self.presets, self.active)
        self._refresh(name)

    def _delete(self) -> None:
        p = self._current()
        if p is None or QMessageBox.question(self, "删除预设", f"删除预设“{p.name}”及其全部说明？") \
                != QMessageBox.StandardButton.Yes:
            return
        self.presets.remove(p)
        if self.active == p.name:
            self.active = ""
        context.save(self.presets, self.active)
        self._refresh()

    def _on_use_changed(self, _i: int) -> None:
        self.active = self.use.currentData() or ""
        context.save(self.presets, self.active)
        self._refresh()

    # ---------------------------------------------------------------- 对话
    def _send(self) -> None:
        p = self._current()
        text = self.input.toPlainText().strip()
        if p is None or not text or self.busy:
            return
        self.busy = True
        self.input.clear()
        self.status.setText("本地模型正在理解……（首次需要加载模型，约十几秒）")
        self.input.setEnabled(False)
        self.send_btn.setEnabled(False)
        self.history.append(f"<p><b>你：</b>{html.escape(text).replace(chr(10), '<br>')}</p>")

        def run(preset=p):
            reply = context.learn(preset, text)
            context.save(self.presets, self.active)
            self.relay.done.emit(preset.name, reply)

        threading.Thread(target=run, daemon=True).start()

    def _on_reply(self, _name: str, _reply: str) -> None:
        self.busy = False
        self.status.setText("已保存。")
        self._show_current()
        self.input.setFocus()
