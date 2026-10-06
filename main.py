# -*- coding: utf-8 -*-
"""
ImageHunter —— 以图搜图 · 视频抽帧交叉搜索工具
支持：文件选择 / 拖拽 / Ctrl+V 粘贴（图片、截图、图片链接、视频文件）
引擎：Google Lens + Yandex 双引擎交叉搜索

兼容性：Python 3.8+（含 Windows 7），已在 PySide6 6.1.3 环境验证
"""
from __future__ import annotations

import csv
import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

import requests
from PySide6.QtCore import (QObject, QRunnable, QSize, Qt, QThread,
                            QThreadPool, QUrl, Signal)
from PySide6.QtGui import (QDesktopServices, QImage, QKeySequence, QPixmap,
                           QShortcut)
from PySide6.QtWidgets import (QApplication, QCheckBox, QFileDialog,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QMainWindow, QMenu, QMessageBox, QPushButton,
                               QScrollArea, QSpinBox, QSplitter, QTabWidget,
                               QTableWidget, QTableWidgetItem, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from searcher import (GOOGLE, YANDEX, UA, aggregate, google_lens_search,
                      yandex_search)
from video_frames import (IMAGE_EXTS, VIDEO_EXTS, extract_keyframes,
                          load_image_as_jpeg, probe_video, to_jpeg_bytes)

APP_NAME = "ImageHunter 以图搜图 · 视频抽帧交叉搜索"


def setup_windows7_qt():
    """
    Windows 7 兼容处理：
    1) 8.3 短路径问题：Qt 插件路径含空格或中文时在 Win7 上可能加载失败
    2) 显式指定平台插件目录，避免打包后 "Could not find the Qt platform plugin"
    """
    if not sys.platform.startswith("win"):
        return
    try:
        import PySide6
        from PySide6.QtCore import QCoreApplication
        base = os.path.dirname(PySide6.__file__)
        for sub in ("plugins", "Qt/plugins"):
            cand = os.path.join(base, sub)
            if os.path.isdir(os.path.join(cand, "platforms")):
                os.environ.setdefault("QT_QPA_PLATFORM_PLUGIN_PATH", cand)
                os.environ.setdefault("QT_PLUGIN_PATH", cand)
                QCoreApplication.addLibraryPath(cand)
                break
    except Exception:  # noqa: BLE001
        pass
    # 高分屏（Win7 也支持）
    try:
        from PySide6.QtCore import Qt as _Qt
        from PySide6.QtWidgets import QApplication as _App
        _App.setAttribute(_Qt.AA_EnableHighDpiScaling, True)
        _App.setAttribute(_Qt.AA_UseHighDpiPixmaps, True)
    except Exception:  # noqa: BLE001
        pass


# ======================== 后台搜索线程 ========================

class SearchWorker(QThread):
    sig_status = Signal(str)
    sig_item = Signal(object)          # SearchResult
    sig_page = Signal(str, str)        # engine, page_url
    sig_summary = Signal(list)         # 汇总结果
    sig_done = Signal()

    def __init__(self, jobs, engines, proxy, parent=None):
        """jobs: [(frame_no, jpeg_bytes), ...]  engines: ["Google","Yandex"]"""
        super().__init__(parent)
        self.jobs = jobs
        self.engines = engines
        self.proxy = proxy

    def run(self):
        all_results = []
        try:
            fn_map = {GOOGLE: google_lens_search, YANDEX: yandex_search}
            with ThreadPoolExecutor(max_workers=6) as ex:
                futs = []
                for frame_no, jpeg in self.jobs:
                    for eng in self.engines:
                        futs.append(ex.submit(
                            fn_map[eng], jpeg, self.proxy or None, frame_no,
                            lambda m: self.sig_status.emit(m)))
                for f in futs:
                    oc = f.result()
                    if oc.page_url:
                        self.sig_page.emit(oc.engine, oc.page_url)
                    if oc.error:
                        self.sig_status.emit(f"[{oc.engine}] {oc.error}")
                    for r in oc.results:
                        all_results.append(r)
                        self.sig_item.emit(r)
            self.sig_summary.emit(aggregate(all_results))
            self.sig_status.emit(
                f"搜索完成：共 {len(all_results)} 条结果，"
                f"{len(aggregate(all_results))} 个来源域名")
        except Exception as e:  # noqa: BLE001
            self.sig_status.emit(f"搜索线程异常: {type(e).__name__}: {e}")
        self.sig_done.emit()


# ======================== 缩略图加载 ========================

class _ThumbSignals(QObject):
    loaded = Signal(int, bytes)


class _ThumbJob(QRunnable):
    def __init__(self, key, url, proxy):
        super().__init__()
        self.key, self.url, self.proxy = key, url, proxy
        self.signals = _ThumbSignals()

    def run(self):
        try:
            proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
            r = requests.get(self.url, headers={"User-Agent": UA},
                             timeout=15, proxies=proxies)
            if r.ok and r.content:
                self.signals.loaded.emit(self.key, r.content)
        except Exception:  # noqa: BLE001
            pass


class ThumbManager:
    """后台加载缩略图/原图，完成后回调 GUI；缓存原始字节供放大查看"""

    def __init__(self, proxy_getter, on_loaded):
        self.pool = QThreadPool()
        self.pool.setMaxThreadCount(4)
        self.proxy_getter = proxy_getter
        self.on_loaded = on_loaded
        self._keys = set()
        self.cache = {}          # key -> bytes（原始图片数据）

    def fetch(self, key, url, force=False):
        if not url or (key in self._keys and not force):
            return
        self._keys.add(key)
        job = _ThumbJob(key, url, self.proxy_getter())
        job.signals.loaded.connect(self.on_loaded)
        self.pool.start(job)


# ======================== 拖拽区域 ========================

class DropArea(QLabel):
    sig_file = Signal(str)
    sig_qimage = Signal(QImage)

    def __init__(self):
        super().__init__("拖拽 图片 / 视频 到这里\n或 Ctrl+V 粘贴（截图 / 图片 / 链接）\n或点击下方按钮选择文件")
        self.setAlignment(Qt.AlignCenter)
        self.setAcceptDrops(True)
        self.setMinimumHeight(220)
        self.setWordWrap(True)
        self._orig = None
        self._set_idle_style()

    def _set_idle_style(self):
        self.setStyleSheet(
            "border: 2px dashed #999; border-radius: 10px;"
            "color: #777; font-size: 14px; padding: 12px;")

    def set_preview(self, pix: QPixmap):
        self._orig = pix
        self.setStyleSheet("border: 2px solid #4a90d9; border-radius: 10px;")
        super().setPixmap(pix.scaled(self.size(), Qt.KeepAspectRatio,
                                     Qt.SmoothTransformation))

    def resizeEvent(self, e):
        if getattr(self, "_orig", None) is not None and not self._orig.isNull():
            super().setPixmap(self._orig.scaled(
                self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
        super().resizeEvent(e)

    def dragEnterEvent(self, e):
        md = e.mimeData()
        if md.hasUrls() or md.hasImage():
            e.acceptProposedAction()

    def dropEvent(self, e):
        md = e.mimeData()
        if md.hasUrls():
            for u in md.urls():
                if u.isLocalFile():
                    self.sig_file.emit(u.toLocalFile())
                    return
            # 网络图片链接
            self.sig_file.emit(md.urls()[0].toString())
        elif md.hasImage():
            self.sig_qimage.emit(QImage(md.imageData()))


# ======================== 图片放大查看窗口 ========================

class ImageViewer(QWidget):
    """可缩放、可另存的图片查看窗口（支持滚轮缩放 / 拖拽平移 / 双击复位）"""

    def __init__(self, key, title, thumb_pix: QPixmap, page_url, big_url="",
                 parent=None):
        super().__init__(parent, Qt.Window)
        self.setWindowTitle(f"图片预览 - {title[:60]}")
        self.resize(880, 660)
        self.key = key
        self.page_url = page_url
        self.big_url = big_url
        self._full = None          # 当前显示的 QPixmap
        self._got_big = False      # 是否已加载高清原图
        self._zoom = 1.0
        self._pan = False
        self._last = None
        self._drag_moved = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)

        self.label = QLabel("加载中…")
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setStyleSheet("background:#222; color:#aaa;")
        area = QScrollArea()
        area.setWidgetResizable(False)
        area.setWidget(self.label)
        area.setAlignment(Qt.AlignCenter)
        area.setStyleSheet("background:#222;")
        area.viewport().installEventFilter(self)
        self.label.installEventFilter(self)
        self.area = area
        lay.addWidget(area, 1)

        self.status = QLabel("")
        self.status.setStyleSheet("color:#666;font-size:12px;")
        lay.addWidget(self.status)

        bar = QHBoxLayout()
        b_in = QPushButton("放大 +")
        b_out = QPushButton("缩小 −")
        b_fit = QPushButton("适应窗口")
        b_100 = QPushButton("1:1")
        b_save = QPushButton("另存为…")
        b_open = QPushButton("打开来源网页")
        b_in.clicked.connect(lambda: self._zoom_by(1.25))
        b_out.clicked.connect(lambda: self._zoom_by(0.8))
        b_fit.clicked.connect(self.fit)
        b_100.clicked.connect(lambda: self.set_zoom(1.0))
        b_save.clicked.connect(self.save_as)
        b_open.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(self.page_url)) if self.page_url else None)
        for b in (b_in, b_out, b_fit, b_100):
            bar.addWidget(b)
        bar.addStretch(1)
        bar.addWidget(b_save)
        bar.addWidget(b_open)
        lay.addLayout(bar)

        self.set_pixmap(thumb_pix, is_thumb=True)

    # ---- 图片设置 ----
    def set_pixmap(self, pix: QPixmap, is_thumb=False):
        if pix is None or pix.isNull():
            return
        self._full = pix
        self._got_big = not is_thumb
        if is_thumb:
            self._zoom = 1.0
            self.label.setText("")
            self._render()
            if not self.big_url:
                self.status.setText(
                    f"缩略图 {pix.width()}×{pix.height()}（该结果无更高清原图）")
            self._fit_if_needed()
        else:
            self._render()
            self._fit_if_needed()

    def _fit_if_needed(self):
        """图片小于窗口时按 1:1，否则自适应"""
        if self._full is None:
            return
        vw, vh = self.area.viewport().width(), self.area.viewport().height()
        if self._full.width() <= vw and self._full.height() <= vh:
            self._zoom = 1.0
        else:
            self._zoom = min(vw / self._full.width(), vh / self._full.height())
        self._render()

    def fit(self):
        self._fit_if_needed()

    def set_zoom(self, z):
        self._zoom = max(0.05, min(8.0, z))
        self._render()

    def _zoom_by(self, f):
        self.set_zoom(self._zoom * f)

    def _render(self):
        if self._full is None:
            return
        w = max(1, int(self._full.width() * self._zoom))
        h = max(1, int(self._full.height() * self._zoom))
        mode = Qt.SmoothTransformation if self._zoom < 1 else Qt.FastTransformation
        self.label.setPixmap(self._full.scaled(w, h, Qt.KeepAspectRatio, mode))
        self.label.resize(w, h)
        kind = "原图" if self._got_big else "缩略图"
        self.status.setText(
            f"{kind} {self._full.width()}×{self._full.height()}  |  "
            f"缩放 {int(self._zoom*100)}%  |  滚轮缩放 · 拖拽平移 · 双击复位")

    # ---- 交互：滚轮缩放、拖拽平移、双击复位 ----
    def wheelEvent(self, e):
        self._zoom_by(1.15 if e.angleDelta().y() > 0 else 1 / 1.15)
        e.accept()

    def eventFilter(self, obj, e):
        from PySide6.QtCore import QEvent
        if e.type() == QEvent.MouseButtonPress and e.button() == Qt.LeftButton:
            self._pan, self._last, self._drag_moved = True, e.pos(), False
            obj.setCursor(Qt.ClosedHandCursor)
            return True
        if e.type() == QEvent.MouseMove and self._pan:
            d = e.pos() - self._last
            self._drag_moved = True
            self._last = e.pos()
            h = self.area.horizontalScrollBar()
            v = self.area.verticalScrollBar()
            h.setValue(h.value() - d.x())
            v.setValue(v.value() - d.y())
            return True
        if e.type() == QEvent.MouseButtonRelease and e.button() == Qt.LeftButton:
            self._pan = False
            obj.unsetCursor()
            return True
        if e.type() == QEvent.MouseButtonDblClick:
            self.fit()   # 双击复位
            return True
        if e.type() == QEvent.Resize:
            pass
        return super().eventFilter(obj, e)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.close()
        elif e.key() in (Qt.Key_Plus, Qt.Key_Equal):
            self._zoom_by(1.25)
        elif e.key() == Qt.Key_Minus:
            self._zoom_by(0.8)
        elif e.key() == Qt.Key_0:
            self.fit()
        else:
            super().keyPressEvent(e)

    def save_as(self):
        if self._full is None:
            return
        name = (self.page_url or "image").split("//")[-1].split("/")[0] or "image"
        path, _ = QFileDialog.getSaveFileName(
            self, "另存为", f"{name}.jpg",
            "图片 (*.jpg *.jpeg *.png *.bmp *.webp)")
        if not path:
            return
        if self._full.save(path):
            self.status.setText(f"已保存: {path}")


# ======================== 视频帧卡片 ========================

class FrameCard(QWidget):
    def __init__(self, frame_no, ts, jpeg):
        super().__init__()
        self.frame_no = frame_no
        self.jpeg = jpeg
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        img = QLabel()
        pix = QPixmap()
        pix.loadFromData(jpeg)
        img.setPixmap(pix.scaled(150, 90, Qt.KeepAspectRatio,
                                 Qt.SmoothTransformation))
        img.setAlignment(Qt.AlignCenter)
        self.chk = QCheckBox(f"帧 {frame_no}  ({ts:.1f}s)")
        self.chk.setChecked(True)
        self.chk.setStyleSheet("font-size: 11px;")
        lay.addWidget(img)
        lay.addWidget(self.chk, alignment=Qt.AlignCenter)
        self.setStyleSheet("FrameCard{border:1px solid #ccc;border-radius:6px;}")


# ======================== 主窗口 ========================

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1180, 760)
        self.tmpdir = tempfile.mkdtemp(prefix="imagehunter_")
        self.mode = None                 # 'image' / 'video'
        self.image_jpeg = None           # 图片模式的 jpeg 字节
        self.frame_cards = []            # 视频模式的 FrameCard
        self.page_urls = {}              # engine -> 结果页 URL
        self.worker = None
        self.total_jobs = 1
        self._thumb_key = 0
        self._thumb_targets = {}         # key -> (widget_item, kind)
        self._viewers = {}               # key -> ImageViewer（图片放大窗口）
        self.thumbs = ThumbManager(self._proxy, self._on_thumb)

        self._build_ui()
        QShortcut(QKeySequence.Paste, self, self.handle_paste)

    # ---------- UI ----------
    def _build_ui(self):
        root = QSplitter(Qt.Horizontal)

        # ===== 左侧：输入区 =====
        left = QWidget()
        ll = QVBoxLayout(left)
        self.drop = DropArea()
        self.drop.sig_file.connect(self.load_path_or_url)
        self.drop.sig_qimage.connect(self.load_qimage)
        ll.addWidget(self.drop)

        row = QHBoxLayout()
        b_img = QPushButton("选择图片")
        b_img.clicked.connect(self.pick_image)
        b_vid = QPushButton("选择视频")
        b_vid.clicked.connect(self.pick_video)
        b_paste = QPushButton("粘贴 (Ctrl+V)")
        b_paste.clicked.connect(self.handle_paste)
        row.addWidget(b_img)
        row.addWidget(b_vid)
        row.addWidget(b_paste)
        ll.addLayout(row)

        # 视频帧列表
        self.frame_area = QScrollArea()
        self.frame_area.setWidgetResizable(True)
        self.frame_area.setMinimumHeight(120)
        self.frame_area.setMaximumHeight(190)
        self.frame_box = QWidget()
        self.frame_layout = QHBoxLayout(self.frame_box)
        self.frame_layout.setContentsMargins(2, 2, 2, 2)
        self.frame_layout.addStretch(1)
        self.frame_area.setWidget(self.frame_box)
        self.frame_area.hide()
        ll.addWidget(self.frame_area)

        # 选项
        opt = QHBoxLayout()
        self.chk_google = QCheckBox("Google Lens")
        self.chk_google.setChecked(True)
        self.chk_yandex = QCheckBox("Yandex")
        self.chk_yandex.setChecked(True)
        opt.addWidget(self.chk_google)
        opt.addWidget(self.chk_yandex)
        opt.addWidget(QLabel("抽帧数:"))
        self.spin_frames = QSpinBox()
        self.spin_frames.setRange(1, 12)
        self.spin_frames.setValue(4)
        opt.addWidget(self.spin_frames)
        opt.addStretch(1)
        ll.addLayout(opt)

        self.edit_proxy = QLineEdit()
        self.edit_proxy.setPlaceholderText(
            "代理（可选），如 http://127.0.0.1:7890 —— 国内用 Google Lens 必填")
        ll.addWidget(self.edit_proxy)

        self.btn_search = QPushButton("开始交叉搜索")
        self.btn_search.setMinimumHeight(40)
        self.btn_search.setStyleSheet(
            "QPushButton{background:#4a90d9;color:white;font-size:15px;"
            "border-radius:6px;font-weight:bold;}"
            "QPushButton:disabled{background:#999;}")
        self.btn_search.clicked.connect(self.start_search)
        ll.addWidget(self.btn_search)

        self.lbl_info = QLabel("尚未加载文件")
        self.lbl_info.setWordWrap(True)
        self.lbl_info.setStyleSheet("color:#666;font-size:12px;")
        ll.addWidget(self.lbl_info)
        ll.addStretch(1)
        left.setMaximumWidth(400)
        root.addWidget(left)

        # ===== 右侧：结果区 =====
        right = QWidget()
        rl = QVBoxLayout(right)
        brow = QHBoxLayout()
        self.btn_open_google = QPushButton("在浏览器打开 Google 结果页")
        self.btn_open_google.setEnabled(False)
        self.btn_open_google.clicked.connect(lambda: self._open_page(GOOGLE))
        self.btn_open_yandex = QPushButton("在浏览器打开 Yandex 结果页")
        self.btn_open_yandex.setEnabled(False)
        self.btn_open_yandex.clicked.connect(lambda: self._open_page(YANDEX))
        b_csv = QPushButton("导出 CSV")
        b_csv.clicked.connect(self.export_csv)
        b_clear = QPushButton("清空结果")
        b_clear.clicked.connect(self.clear_results)
        brow.addWidget(self.btn_open_google)
        brow.addWidget(self.btn_open_yandex)
        brow.addStretch(1)
        brow.addWidget(b_csv)
        brow.addWidget(b_clear)
        rl.addLayout(brow)

        self.tabs = QTabWidget()
        # —— 汇总 ——
        self.tree = QTreeWidget()
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels(["来源域名", "命中引擎", "命中帧",
                                   "标题 / 缩略图（双击放大）", "链接"])
        self.tree.setColumnWidth(0, 180)
        self.tree.setColumnWidth(1, 130)
        self.tree.setColumnWidth(2, 60)
        self.tree.setColumnWidth(3, 400)
        self.tree.setIconSize(QSize(60, 60))
        self.tree.itemDoubleClicked.connect(self._tree_open)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        self.tabs.addTab(self.tree, "交叉汇总（按来源域名）")
        # —— 明细 ——
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["引擎", "帧", "标题 / 缩略图（双击放大）",
                                              "域名", "链接"])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 70)
        self.table.setColumnWidth(1, 50)
        self.table.setColumnWidth(3, 160)
        self.table.setColumnWidth(4, 260)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setIconSize(QSize(60, 60))
        self.table.verticalHeader().setDefaultSectionSize(66)
        self.table.itemDoubleClicked.connect(self._table_open)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._table_menu)
        self.tabs.addTab(self.table, "全部结果（按引擎明细）")
        rl.addWidget(self.tabs)
        root.addWidget(right)

        root.setStretchFactor(0, 0)
        root.setStretchFactor(1, 1)
        self.setCentralWidget(root)
        self.statusBar().showMessage("就绪 —— 请拖入 / 粘贴 / 选择 图片或视频")

    # ---------- 工具 ----------
    def _proxy(self):
        return self.edit_proxy.text().strip() or None

    def _open_page(self, engine):
        url = self.page_urls.get(engine)
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def _pix_from_jpeg(self, jpeg):
        p = QPixmap()
        p.loadFromData(jpeg)
        return p

    # ---------- 载入输入 ----------
    def pick_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择图片", "", "图片 (*.jpg *.jpeg *.png *.bmp *.webp *.gif *.tif *.tiff)")
        if path:
            self.load_path_or_url(path)

    def pick_video(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择视频", "",
            "视频 (*.mp4 *.avi *.mov *.mkv *.webm *.flv *.wmv *.m4v *.ts)")
        if path:
            self.load_path_or_url(path)

    def load_qimage(self, qimg: QImage):
        if qimg.isNull():
            self.statusBar().showMessage("剪贴板图像为空")
            return
        path = os.path.join(self.tmpdir, "paste.png")
        qimg.save(path, "PNG")
        self._load_image(path, "剪贴板图片")

    def load_path_or_url(self, s: str):
        if s.startswith("http://") or s.startswith("https://"):
            self._download_image(s)
            return
        ext = os.path.splitext(s)[1].lower()
        try:
            if ext in IMAGE_EXTS:
                self._load_image(s, os.path.basename(s))
            elif ext in VIDEO_EXTS:
                self._load_video(s)
            else:
                QMessageBox.warning(self, "不支持的格式", f"不支持的文件类型: {ext}")
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "加载失败", str(e))

    def _download_image(self, url):
        self.statusBar().showMessage(f"正在下载图片: {url}")
        QApplication.processEvents()
        try:
            proxies = {"http": self._proxy(), "https": self._proxy()} if self._proxy() else None
            r = requests.get(url, headers={"User-Agent": UA}, timeout=25,
                             proxies=proxies)
            r.raise_for_status()
            path = os.path.join(self.tmpdir, "url_image")
            with open(path, "wb") as f:
                f.write(r.content)
            self._load_image(path, url[:60])
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "下载失败", str(e))

    def _load_image(self, path, label):
        self.image_jpeg = load_image_as_jpeg(path)
        self.mode = "image"
        self._clear_frames()
        self.drop.set_preview(self._pix_from_jpeg(self.image_jpeg))
        self.lbl_info.setText(f"已加载图片：{label}\n大小: {len(self.image_jpeg)//1024} KB")
        self.statusBar().showMessage("图片已就绪，点击「开始交叉搜索」")

    def _load_video(self, path):
        info = probe_video(path)
        self.statusBar().showMessage("正在抽帧…")
        QApplication.processEvents()
        n = self.spin_frames.value()
        frames = extract_keyframes(path, n)
        self.mode = "video"
        self.image_jpeg = None
        self._clear_frames()
        for fno, ts, bgr in frames:
            jpeg = to_jpeg_bytes(bgr, max_side=800)
            card = FrameCard(fno, ts, jpeg)
            self.frame_cards.append(card)
            self.frame_layout.insertWidget(self.frame_layout.count() - 1, card)
        self.frame_area.show()
        # 预览第一帧
        self.drop.set_preview(self._pix_from_jpeg(self.frame_cards[0].jpeg))
        self.lbl_info.setText(
            f"已加载视频：{os.path.basename(path)}\n"
            f"时长 {info['duration']:.1f}s · {info['w']}x{info['h']} · "
            f"已抽取 {len(frames)} 帧（勾选要搜索的帧）")
        self.statusBar().showMessage("视频抽帧完成，勾选帧后点击「开始交叉搜索」")

    def _clear_frames(self):
        for c in self.frame_cards:
            c.setParent(None)
            c.deleteLater()
        self.frame_cards = []
        self.frame_area.hide()

    # ---------- 粘贴 ----------
    def handle_paste(self):
        cb = QApplication.clipboard()
        md = cb.mimeData()
        if md.hasImage():
            self.load_qimage(cb.image())
        elif md.hasUrls():
            for u in md.urls():
                if u.isLocalFile():
                    self.load_path_or_url(u.toLocalFile())
                    return
            self.load_path_or_url(md.urls()[0].toString())
        elif md.hasText():
            t = md.text().strip()
            if t.startswith(("http://", "https://")):
                self.load_path_or_url(t)
            elif os.path.exists(t):
                self.load_path_or_url(t)
            else:
                self.statusBar().showMessage("剪贴板内容不是图片 / 文件 / 链接")
        else:
            self.statusBar().showMessage("剪贴板为空")

    # ---------- 搜索 ----------
    def start_search(self):
        engines = []
        if self.chk_google.isChecked():
            engines.append(GOOGLE)
        if self.chk_yandex.isChecked():
            engines.append(YANDEX)
        if not engines:
            QMessageBox.warning(self, "提示", "请至少勾选一个搜索引擎")
            return

        jobs = []
        if self.mode == "image" and self.image_jpeg:
            jobs = [(0, self.image_jpeg)]
        elif self.mode == "video":
            for c in self.frame_cards:
                if c.chk.isChecked():
                    # 重新编码为上传质量
                    jobs.append((c.frame_no, c.jpeg))
            if not jobs:
                QMessageBox.warning(self, "提示", "请至少勾选一帧")
                return
        else:
            QMessageBox.warning(self, "提示", "请先加载图片或视频")
            return

        self.total_jobs = len(jobs)
        self.clear_results()
        self.btn_search.setEnabled(False)
        self.btn_search.setText("搜索中…")
        self.worker = SearchWorker(jobs, engines, self._proxy(), self)
        self.worker.sig_status.connect(lambda m: self.statusBar().showMessage(m))
        self.worker.sig_item.connect(self._add_detail)
        self.worker.sig_page.connect(self._set_page)
        self.worker.sig_summary.connect(self._fill_summary)
        self.worker.sig_done.connect(self._search_done)
        self.worker.start()

    def _set_page(self, engine, url):
        self.page_urls[engine] = url
        if engine == GOOGLE:
            self.btn_open_google.setEnabled(True)
        else:
            self.btn_open_yandex.setEnabled(True)

    def _add_detail(self, r):
        row = self.table.rowCount()
        self.table.insertRow(row)
        it_engine = QTableWidgetItem(r.engine)
        it_frame = QTableWidgetItem(str(r.frame))
        it_title = QTableWidgetItem(r.title)
        it_title.setData(Qt.UserRole, r.url)
        it_title.setToolTip(r.url)
        it_domain = QTableWidgetItem(r.domain)
        it_url = QTableWidgetItem(r.url)
        self.table.setItem(row, 0, it_engine)
        self.table.setItem(row, 1, it_frame)
        self.table.setItem(row, 2, it_title)
        self.table.setItem(row, 3, it_domain)
        self.table.setItem(row, 4, it_url)
        self.table.setRowHeight(row, 62)
        if r.thumb:
            self._thumb_key += 1
            it_title.setData(Qt.UserRole + 1, self._thumb_key)   # 缩略图 key
            self._thumb_targets[self._thumb_key] = (it_title, r.origin, r.thumb)
            self.thumbs.fetch(self._thumb_key, r.thumb)

    def _fill_summary(self, groups):
        self.tree.clear()
        for g in groups:
            engines = " + ".join(sorted(g["engines"]))
            frames = f"{len(g['frames'])}/{self.total_jobs}"
            it = QTreeWidgetItem([g["domain"], engines, frames,
                                  g["title"][:120], g["url"]])
            it.setData(3, Qt.UserRole, g["url"])
            it.setToolTip(4, g["url"])
            self.tree.addTopLevelItem(it)
            if g["thumb"]:
                self._thumb_key += 1
                it.setData(3, Qt.UserRole + 1, self._thumb_key)   # 缩略图 key
                self._thumb_targets[self._thumb_key] = (it, g["origin"], g["thumb"])
                self.thumbs.fetch(self._thumb_key, g["thumb"])

    def _on_thumb(self, key, data):
        self.thumbs.cache[key] = data
        pix = QPixmap()
        if not pix.loadFromData(data) or pix.isNull():
            return
        if key < 0:
            # 负 key = 高清原图数据：填充对应的放大窗口
            v = self._viewers.get(-key)
            if v is not None and v.isVisible() and not v._got_big:
                v.set_pixmap(pix, is_thumb=False)
            return
        tgt = self._thumb_targets.get(key)
        if not tgt:
            return
        item = tgt[0]
        item.setData(Qt.DecorationRole,
                     pix.scaled(60, 60, Qt.KeepAspectRatio,
                                Qt.SmoothTransformation))

    def _search_done(self):
        self.btn_search.setEnabled(True)
        self.btn_search.setText("开始交叉搜索")

    # ---------- 结果操作 ----------
    def _table_open(self, item):
        """双击标题列打开原链接；双击左边缩略图区域或第三列以外的图列 -> 放大图片"""
        col = self.table.currentColumn()
        it = self.table.item(item.row(), 2)
        if col == 2 and it is not None:
            key = it.data(Qt.UserRole + 1)
            # 鼠标落在缩略图区域（最左 68px）时放大，否则打开网页
            if key:
                self.open_viewer(key)
            else:
                QDesktopServices.openUrl(QUrl(it.data(Qt.UserRole)))
        elif it is not None:
            QDesktopServices.openUrl(QUrl(it.data(Qt.UserRole)))

    def _tree_open(self, item, col):
        if col >= 3:
            key = item.data(3, Qt.UserRole + 1)
            if key:
                self.open_viewer(key)
                return
        url = item.data(3, Qt.UserRole)
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def open_viewer(self, key):
        """打开（或激活）图片放大查看窗口，并后台加载高清原图"""
        v = self._viewers.get(key)
        if v is not None and v.isVisible():
            v.raise_()
            v.activateWindow()
            return
        tgt = self._thumb_targets.get(key)
        if not tgt:
            return
        item, origin, thumb = tgt
        # 找到该 key 对应的标题 / 链接
        title, page_url = "", ""
        if isinstance(item, QTableWidgetItem):
            page_url = item.data(Qt.UserRole) or ""
            title = item.text()
        else:  # QTreeWidgetItem
            page_url = item.data(3, Qt.UserRole) or ""
            title = item.text(3)
        data = self.thumbs.cache.get(key)
        pix = QPixmap()
        if data:
            pix.loadFromData(data)
        v = ImageViewer(key, title, pix, page_url, origin, self)
        self._viewers[key] = v
        v.show()
        # 有高清原图 URL：先查缓存，否则后台下载（负 key 与缩略图请求区分）
        if origin:
            big_key = -key
            big_data = self.thumbs.cache.get(big_key)
            if big_data:
                bpix = QPixmap()
                if bpix.loadFromData(big_data) and not bpix.isNull():
                    v.set_pixmap(bpix, is_thumb=False)
            else:
                v.status.setText("正在加载高清原图…")
                self.thumbs.fetch(big_key, origin)

    def _table_menu(self, pos):
        item = self.table.itemAt(pos)
        if not item:
            return
        it_title = self.table.item(item.row(), 2)
        url = it_title.data(Qt.UserRole)
        key = it_title.data(Qt.UserRole + 1)
        menu = QMenu(self)
        a0 = menu.addAction("放大查看图片") if key else None
        if a0:
            menu.addSeparator()
        a1 = menu.addAction("打开链接")
        a2 = menu.addAction("复制链接")
        act = menu.exec(self.table.viewport().mapToGlobal(pos))
        if a0 and act == a0:
            self.open_viewer(key)
        elif act == a1:
            QDesktopServices.openUrl(QUrl(url))
        elif act == a2:
            QApplication.clipboard().setText(url)

    def _tree_menu(self, pos):
        item = self.tree.itemAt(pos)
        if not item:
            return
        url = item.data(3, Qt.UserRole)
        key = item.data(3, Qt.UserRole + 1)
        menu = QMenu(self)
        a0 = menu.addAction("放大查看图片") if key else None
        if a0:
            menu.addSeparator()
        a1 = menu.addAction("打开链接")
        a2 = menu.addAction("复制链接")
        act = menu.exec(self.tree.viewport().mapToGlobal(pos))
        if a0 and act == a0:
            self.open_viewer(key)
        elif act == a1:
            QDesktopServices.openUrl(QUrl(url))
        elif act == a2:
            QApplication.clipboard().setText(url)

    def clear_results(self):
        self.table.setRowCount(0)
        self.tree.clear()
        self._thumb_targets.clear()
        for v in self._viewers.values():
            v.close()
        self._viewers.clear()

    def export_csv(self):
        if self.table.rowCount() == 0:
            QMessageBox.information(self, "提示", "没有可导出的结果")
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出 CSV", "results.csv",
                                              "CSV (*.csv)")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["引擎", "帧", "标题", "域名", "链接"])
            for row in range(self.table.rowCount()):
                w.writerow([self.table.item(row, c).text() if self.table.item(row, c) else ""
                            for c in range(5)])
        self.statusBar().showMessage(f"已导出: {path}")


def main():
    # Windows 7 兼容：必须在 QApplication 之前设置插件路径
    setup_windows7_qt()
    app = QApplication(sys.argv)
    try:
        w = MainWindow()
        w.show()
        sys.exit(app.exec())
    except Exception as e:  # noqa: BLE001
        import traceback
        msg = traceback.format_exc()
        try:
            QMessageBox.critical(None, "启动失败",
                                 f"{type(e).__name__}: {e}\n\n{msg[-1500:]}")
        except Exception:  # noqa: BLE001
            print(msg)
        sys.exit(1)


if __name__ == "__main__":
    main()
