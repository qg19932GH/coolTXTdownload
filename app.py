# -*- coding: utf-8 -*-
"""
cool18 小说下载器 - 桌面 GUI (PySide6)

启动:  python app.py    （或双击 run.bat）
"""
import json
import os
import sys
import threading
import traceback
from datetime import datetime

from PySide6.QtCore import (
    Qt, QMimeData, QPoint, QRect, QStandardPaths, QThread, QUrl, Signal)
from PySide6.QtGui import QColor, QFont, QDesktopServices, QDrag, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QDoubleSpinBox, QFileDialog, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget)

import crawler

if getattr(sys, "frozen", False):          # PyInstaller 打包后
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONF_PATH = os.path.join(APP_DIR, "settings.json")

COLS = ["下载", "打开", "卷号", "标题", "发帖时间", "tid", "来源", "状态"]


class DraggableTable(QTableWidget):
    """支持整行上下拖动排序的表格，带拖动视觉反馈：
    - 被拖行半透明高亮；
    - 整行缩略图跟随鼠标；
    - 目标插入位置画蓝色指示线。
    松手时发 rowsMoved(被拖行, 插入到第几行前)。"""
    MIMETYPE = "application/x-cool18-row"
    rowsMoved = Signal(int, int)
    _ACCENT = QColor(30, 120, 220)

    def __init__(self, rows, cols, parent=None):
        super().__init__(rows, cols, parent)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self._drag_row = -1
        self._drop_pos = (-1, 0)      # (行, 0=插该行前/1=插该行后)

    # ---- 拖动反馈用的几何 ----
    def _row_rect(self, row):
        vr = self.visualRect(self.model().index(row, 0))
        return QRect(0, vr.top(), self.viewport().width(), self.rowHeight(row))

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self.viewport())
        if 0 <= self._drag_row < self.rowCount():          # 源行高亮
            p.fillRect(self._row_rect(self._drag_row),
                       QColor(self._ACCENT.red(), self._ACCENT.green(),
                              self._ACCENT.blue(), 46))
        row, edge = self._drop_pos
        if row >= 0:                                        # 插入指示线
            r = self._row_rect(min(row, self.rowCount() - 1)) \
                if self.rowCount() else QRect()
            y = r.top() if edge == 0 else r.bottom() + 1
            if self.rowCount():
                p.fillRect(QRect(0, y - 1, self.viewport().width(), 3),
                           self._ACCENT)
        p.end()

    # ---- 拖出 ----
    def startDrag(self, _actions):
        row = self.currentRow()
        if row < 0:
            return
        self._drag_row = row
        mime = QMimeData()
        mime.setData(self.MIMETYPE, str(row).encode())
        drag = QDrag(self)
        drag.setMimeData(mime)
        r = self._row_rect(row)
        x = self.columnViewportPosition(3)                  # 只截“标题”列
        w = max(self.columnWidth(3), 100)
        pix = self.viewport().grab(QRect(x, r.top(), w, r.height()))
        if not pix.isNull():
            drag.setPixmap(pix)
            drag.setHotSpot(QPoint(pix.width() // 2, pix.height() // 2))
        drag.exec(Qt.MoveAction)
        self._drag_row = -1
        self._drop_pos = (-1, 0)
        self.viewport().update()

    # ---- 拖入反馈 ----
    def dragEnterEvent(self, e):
        if e.mimeData().hasFormat(self.MIMETYPE):
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        if not e.mimeData().hasFormat(self.MIMETYPE):
            e.ignore()
            return
        row = self.rowAt(e.position().toPoint().y())
        if row < 0:
            self._drop_pos = (max(self.rowCount() - 1, 0), 1)
        else:
            r = self._row_rect(row)
            self._drop_pos = (row, 0 if e.position().toPoint().y()
                              < r.top() + r.height() // 2 else 1)
        self.viewport().update()
        e.acceptProposedAction()

    def dragLeaveEvent(self, e):
        self._drop_pos = (-1, 0)
        self.viewport().update()
        super().dragLeaveEvent(e)

    def dropEvent(self, e):
        if not e.mimeData().hasFormat(self.MIMETYPE):
            e.ignore()
            return
        src = int(bytes(e.mimeData().data(self.MIMETYPE)).decode())
        row, edge = self._drop_pos
        if row < 0:
            tgt = self.rowCount()
        else:
            tgt = row if edge == 0 else row + 1
        self._drop_pos = (-1, 0)
        self.viewport().update()
        e.acceptProposedAction()
        self.rowsMoved.emit(src, tgt)


def default_out_dir():
    docs = QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation)
    return os.path.join(docs or os.path.expanduser("~"), "novels")


def load_conf():
    try:
        with open(CONF_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_conf(d):
    try:
        with open(CONF_PATH, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


class Worker(QThread):
    logSig = Signal(str)
    progressSig = Signal(int, int)      # 0,0 => 忙碌不定进度
    analyzeDone = Signal(str, list, dict)
    downloadDone = Signal(str)
    failed = Signal(str)

    def __init__(self, mode, parent_gui):
        super().__init__()
        self.mode = mode
        self.entry = parent_gui.entry_url.text().strip()
        # 用户手动输入书名(name_is_auto=False)才作为关键字；程序自动填的忽略
        self.keyword = parent_gui.effective_keyword()
        self.proxy = parent_gui.entry_proxy.text().strip()
        self.out_dir = parent_gui.edit_out.text().strip()
        self.interval = parent_gui.spin_interval.value()
        # 下载模式：从表格同步勾选状态
        self.cands = parent_gui.candidates
        self.name = parent_gui.novel_name
        if mode == "download":
            parent_gui.sync_checks_from_table()
        self.cancel = threading.Event()

    def run(self):
        try:
            if self.mode == "analyze":
                self.progressSig.emit(0, 0)
                name, cands, meta = crawler.analyze(
                    self.entry, self.proxy, self.keyword,
                    log=self.logSig.emit, cancel=self.cancel)
                self.analyzeDone.emit(name, cands, meta)
            else:
                path = crawler.download_novel(
                    self.name, self.cands, self.out_dir, self.proxy,
                    self.interval, log=self.logSig.emit, cancel=self.cancel,
                    progress=lambda i, n: self.progressSig.emit(i, n),
                    preserve_order=True)      # 按表格行序下载（含手动拖动）
                self.downloadDone.emit(path)
        except Exception as e:  # noqa: BLE001
            if self.cancel.is_set():
                self.logSig.emit("已取消。")
            else:
                self.failed.emit(f"{e}")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"cool18 小说下载器 v{crawler.__version__}")
        self.resize(980, 660)
        self.candidates = []
        self.novel_name = ""
        self.worker = None
        self.manual_order = False      # 用户是否拖动过顺序
        self.meta = {"declared_min": None, "declared_max": None}

        conf = load_conf()
        root = QWidget()
        lay = QVBoxLayout(root)

        # ---- 第1行: 入口 ----
        r1 = QHBoxLayout()
        r1.addWidget(QLabel("小说帖子链接 / 书名:"))
        self.entry_url = QLineEdit(conf.get("entry", ""))
        self.entry_url.setPlaceholderText(
            "粘贴小说任一卷的网址(如 threadview&tid=14587664)，或直接输入书名")
        r1.addWidget(self.entry_url, 1)
        self.btn_analyze = QPushButton("分析书目")
        r1.addWidget(self.btn_analyze)
        lay.addLayout(r1)

        # ---- 第2行: 参数 ----
        r2 = QHBoxLayout()
        r2.addWidget(QLabel("书名:"))
        self.entry_name = QLineEdit()
        self.entry_name.setPlaceholderText("留空则自动从标题识别")
        self.entry_name.setFixedWidth(220)
        # 程序自动填入的书名不算用户指定：换链接分析时不沿用旧书名
        self.name_is_auto = True
        self.entry_name.textEdited.connect(
            lambda: setattr(self, "name_is_auto", False))
        r2.addWidget(self.entry_name)
        r2.addWidget(QLabel("代理:"))
        self.entry_proxy = QLineEdit(conf.get(
            "proxy", crawler.DEFAULT_PROXY))
        self.entry_proxy.setPlaceholderText(
            "socks5h://127.0.0.1:10808 / http://... / 留空直连")
        r2.addWidget(self.entry_proxy, 1)
        r2.addWidget(QLabel("请求间隔(秒):"))
        self.spin_interval = QDoubleSpinBox()
        self.spin_interval.setRange(0, 30)
        self.spin_interval.setSingleStep(0.2)
        self.spin_interval.setValue(float(conf.get("interval", 0.8)))
        r2.addWidget(self.spin_interval)
        lay.addLayout(r2)

        # ---- 第3行: 输出目录 ----
        r3 = QHBoxLayout()
        r3.addWidget(QLabel("保存目录:"))
        self.edit_out = QLineEdit(conf.get("out_dir") or default_out_dir())
        r3.addWidget(self.edit_out, 1)
        self.btn_browse = QPushButton("浏览…")
        self.btn_open = QPushButton("打开目录")
        r3.addWidget(self.btn_browse)
        r3.addWidget(self.btn_open)
        lay.addLayout(r3)

        # ---- 章节完整性核对（扫描结束后显示，勾选变化实时更新） ----
        self.coverage_lbl = QLabel("章节完整性核对：分析书目后在此显示")
        self.coverage_lbl.setWordWrap(True)
        self.coverage_lbl.setMinimumHeight(40)
        self.coverage_lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.coverage_lbl.setStyleSheet(
            "border:1px solid #c8c8c8; border-radius:4px; padding:6px 10px;"
            "background:#f7f7f7;")
        lay.addWidget(self.coverage_lbl)

        # ---- 候选卷表格 ----
        self.table = DraggableTable(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.setColumnWidth(0, 46)   # 下载
        self.table.setColumnWidth(1, 64)   # 打开
        self.table.setColumnWidth(2, 80)   # 卷号
        self.table.setColumnWidth(3, 330)  # 标题
        self.table.setColumnWidth(4, 130)  # 发帖时间
        self.table.setColumnWidth(5, 80)   # tid
        self.table.setColumnWidth(6, 80)   # 来源
        self.table.setColumnWidth(7, 240)  # 状态
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.cellDoubleClicked.connect(
            lambda row, col: col > 1 and self.open_row_url(row))
        self.table.rowsMoved.connect(self.on_rows_moved)
        self.table.itemChanged.connect(self.on_check_changed)
        lay.addWidget(self.table, 1)

        # ---- 操作按钮 ----
        r4 = QHBoxLayout()
        self.btn_all = QPushButton("全选")
        self.btn_none = QPushButton("全不选")
        self.btn_download = QPushButton("开始下载")
        self.btn_download.setEnabled(False)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setEnabled(False)
        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        self.progress.setFixedWidth(220)
        r4.addWidget(self.btn_all)
        r4.addWidget(self.btn_none)
        r4.addWidget(self.btn_download)
        r4.addWidget(self.btn_cancel)
        r4.addWidget(self.progress, 1)
        lay.addLayout(r4)

        # ---- 日志 ----
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(4000)
        f = QFont("Consolas")
        f.setStyleHint(QFont.Monospace)
        self.log.setFont(f)
        lay.addWidget(self.log, 1)

        self.setCentralWidget(root)
        self.statusBar().showMessage("就绪")

        self.btn_analyze.clicked.connect(self.on_analyze)
        self.btn_download.clicked.connect(self.on_download)
        self.btn_cancel.clicked.connect(self.on_cancel)
        self.btn_all.clicked.connect(lambda: self.set_all_checks(True))
        self.btn_none.clicked.connect(lambda: self.set_all_checks(False))
        self.btn_browse.clicked.connect(self.on_browse)
        self.btn_open.clicked.connect(self.on_open_dir)

        self.log_append("欢迎使用。粘贴帖子链接或输入书名，点“分析书目”。")
        self.log_append(f"默认代理: {self.entry_proxy.text()}（可修改，留空为直连）")

    # ------------------------------------------------ 日志 / 状态
    def log_append(self, msg):
        self.log.appendPlainText(f"[{datetime.now():%H:%M:%S}] {msg}")

    def set_busy(self, busy):
        for b in (self.btn_analyze, self.btn_download, self.btn_all,
                  self.btn_none, self.btn_browse):
            b.setEnabled(not busy)
        self.btn_cancel.setEnabled(busy)
        if busy:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 1)
            self.progress.setValue(0)

    def effective_keyword(self) -> str:
        return "" if self.name_is_auto else self.entry_name.text().strip()

    # ------------------------------------------------ 动作
    def on_analyze(self):
        entry = self.entry_url.text().strip()
        if not entry and not self.effective_keyword():
            QMessageBox.warning(self, "提示", "请先粘贴帖子链接或输入书名")
            return
        self.save_settings()
        self._start("analyze")

    def on_download(self):
        if not self.candidates:
            return
        self.save_settings()
        self._start("download")

    def on_cancel(self):
        if self.worker:
            self.worker.cancel.set()
            self.log_append("正在取消…")

    def on_browse(self):
        d = QFileDialog.getExistingDirectory(self, "选择保存目录",
                                             self.edit_out.text())
        if d:
            self.edit_out.setText(d)

    def on_open_dir(self):
        d = self.edit_out.text().strip()
        if d and os.path.isdir(d):
            os.startfile(d)
        else:
            QMessageBox.information(self, "提示", "目录还不存在，先下载一次。")

    def save_settings(self):
        save_conf({"entry": self.entry_url.text().strip(),
                   "proxy": self.entry_proxy.text().strip(),
                   "out_dir": self.edit_out.text().strip(),
                   "interval": self.spin_interval.value()})

    def _start(self, mode):
        self.worker = Worker(mode, self)
        self.worker.logSig.connect(self.log_append)
        self.worker.progressSig.connect(self.on_progress)
        self.worker.analyzeDone.connect(self.on_analyzed)
        self.worker.downloadDone.connect(self.on_downloaded)
        self.worker.failed.connect(self.on_failed)
        self.set_busy(True)
        self.statusBar().showMessage("分析中…" if mode == "analyze" else "下载中…")
        self.worker.start()

    # ------------------------------------------------ 结果回调
    def on_progress(self, i, n):
        if n == 0:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, n)
            self.progress.setValue(i)
            self.progress.setFormat(f"{i}/{n}")

    def on_analyzed(self, name, cands, meta=None):
        self.set_busy(False)
        self.novel_name = name
        self.meta = meta or {"declared_min": None, "declared_max": None}
        cur = self.entry_name.text().strip()
        if (not cur) or self.name_is_auto:
            # 空 或 上一次自动填的 → 用本书名覆盖，并标记为自动
            self.entry_name.setText(name)
            self.name_is_auto = True
        self.candidates = cands
        self.manual_order = False
        self.fill_table()
        self.refresh_coverage()
        n_ok = sum(1 for c in cands if c.checked)
        self.statusBar().showMessage(
            f"《{name}》: 共 {len(cands)} 帖，预选 {n_ok} 卷，确认后点“开始下载”")
        self.btn_download.setEnabled(bool(cands))

    def on_downloaded(self, path):
        self.set_busy(False)
        self.statusBar().showMessage(f"已保存: {path}")
        self.log_append(f"✅ 下载完成: {path}")

    def on_failed(self, msg):
        self.set_busy(False)
        self.statusBar().showMessage("失败")
        self.log_append("❌ " + msg)

    # ------------------------------------------------ 表格
    def fill_table(self):
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.candidates))
        for row, c in enumerate(self.candidates):
            it_chk = QTableWidgetItem()
            it_chk.setFlags((Qt.ItemIsUserCheckable | Qt.ItemIsEnabled))
            it_chk.setCheckState(Qt.Checked if c.checked else Qt.Unchecked)
            self.table.setItem(row, 0, it_chk)
            btn = QPushButton("打开")
            btn.setFlat(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setToolTip("在浏览器打开本卷原帖，核对上下文衔接")
            url = c.href or crawler.thread_post_url(c.tid)
            btn.clicked.connect(
                lambda _=False, u=url: QDesktopServices.openUrl(QUrl(u)))
            self.table.setCellWidget(row, 1, btn)
            color = QColor(0, 0, 0) if c.checked else QColor(150, 150, 150)
            for col, text in ((2, c.range_text()), (3, c.title), (4, c.date),
                              (5, c.tid), (6, c.source), (7, c.status)):
                it = QTableWidgetItem(text)
                it.setForeground(color if col != 7 else QColor(180, 90, 0))
                self.table.setItem(row, col, it)
        self.table.blockSignals(False)

    def open_row_url(self, row):
        if 0 <= row < len(self.candidates):
            c = self.candidates[row]
            url = c.href or crawler.thread_post_url(c.tid)
            QDesktopServices.openUrl(QUrl(url))

    def on_rows_moved(self, src, tgt):
        """拖动排序：把第 src 行插到第 tgt 行前（tgt 可为 rowCount=末尾）。"""
        if not (0 <= src < len(self.candidates)):
            return
        self.sync_checks_from_table()          # 先固化当前勾选
        tgt = max(0, min(tgt, len(self.candidates)))
        moved = self.candidates.pop(src)
        if tgt > src:
            tgt -= 1                           # 移除源行后目标前移一位
        self.candidates.insert(tgt, moved)
        self.fill_table()
        self.manual_order = True
        self.statusBar().showMessage(
            f"已把 {moved.range_text()} 移到第 {tgt + 1} 位（下载按当前行序）")

    def sync_checks_from_table(self):
        for row, c in enumerate(self.candidates):
            if row < self.table.rowCount():
                c.checked = (self.table.item(row, 0).checkState() == Qt.Checked)

    def on_check_changed(self, item):
        """用户勾选/取消某卷时实时更新完整性核对。"""
        if item.column() != 0:
            return
        row = item.row()
        if not (0 <= row < len(self.candidates)):
            return
        checked = item.checkState() == Qt.Checked
        self.candidates[row].checked = checked
        gray = QColor(150, 150, 150)
        black = QColor(0, 0, 0)
        for col in (2, 3, 4, 5, 6):
            it = self.table.item(row, col)
            if it:
                it.setForeground(black if checked else gray)
        self.refresh_coverage()

    def refresh_coverage(self):
        rep = crawler.coverage_report(self.candidates,
                                      self.meta.get("declared_min"),
                                      self.meta.get("declared_max"))
        text = crawler.format_coverage(rep, self.meta.get("declared_max"))
        self.coverage_lbl.setText(text)
        if rep["target_max"] == 0:
            border, fg = "#c8c8c8", "#666"
        elif rep["complete"]:
            border, fg = "#4caf50", "#2e7d32"
        else:
            border, fg = "#e53935", "#c62828"
        self.coverage_lbl.setStyleSheet(
            f"border:1px solid {border}; border-radius:4px;"
            f"padding:6px 10px; background:#f7f7f7; color:{fg};")

    def set_all_checks(self, val):
        self.table.blockSignals(True)
        for row in range(self.table.rowCount()):
            self.table.item(row, 0).setCheckState(
                Qt.Checked if val else Qt.Unchecked)
        self.table.blockSignals(False)
        self.sync_checks_from_table()
        for row, c in enumerate(self.candidates):          # 同步行文字颜色
            for col in (2, 3, 4, 5, 6):
                it = self.table.item(row, col)
                if it:
                    it.setForeground(QColor(0, 0, 0) if c.checked
                                     else QColor(150, 150, 150))
        self.refresh_coverage()


def main():
    app = QApplication(sys.argv)
    app.setFont(QFont("Microsoft YaHei UI", 9))
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
