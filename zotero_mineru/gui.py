from __future__ import annotations

import logging
import re
import shutil
import sys
import threading
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox,
    QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMenu, QScrollArea,
    QMessageBox, QPushButton, QSpinBox, QSplitter, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QSystemTrayIcon,
    QTabWidget, QTextBrowser, QTextEdit,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import __version__
from .clients import file_url_to_path
from .config import BRIDGE_TRANSLATION_PROTOCOL, Config
from .engine import Engine
from .llm import PROVIDERS
from .markdown import page_count
from .researcher import INDEX_MD, RESEARCH_DIR, Researcher, research_model
from .resources import RELEASE_URL, download_worker
from .store import ACTIVE, Store
from .summarizer import Summarizer
from .translation import GLOSSARY, NO_ENGINE, TOKENS_PER_PAGE, Translator, engine_command, translation_model


class Events(QObject):
    event = Signal(str, object)


def app_icon() -> QIcon:
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#2563eb"))
    painter.drawRoundedRect(2, 2, 60, 60, 14, 14)
    painter.setPen(QColor("white"))
    painter.setFont(QFont("Segoe UI", 19, QFont.Weight.Bold))
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "MD")
    painter.end()
    return QIcon(pixmap)


HELP = """
<h2>Zotero PDF 自动转 Markdown（MinerU）</h2>
<p>本工具用你自己的 MinerU API Token 调用 MinerU 云端解析服务，把 Zotero 分类中论文的 PDF 转为 Markdown，
并作为子附件放回原论文条目。勾选的 PDF 会上传到 MinerU。</p>
<h3>首次使用</h3>
<ol>
<li>打开 Zotero，在 <b>编辑 → 设置 → 高级</b> 中勾选“允许此计算机上的其他应用程序与 Zotero 通信”。</li>
<li>在本软件 <b>设置</b> 页点击“安装/更新 Zotero 插件”，按提示在 Zotero 中安装附带的 XPI（升级本软件后也要重新安装一次）。</li>
<li>填写 MinerU API Token，点击“保存设置”，再点“检查 API 连接”。</li>
<li>回到 <b>文献与任务</b>，在左侧勾选分类（可包含子分类），右侧会列出其中的论文。<b>此时不会转换任何论文。</b></li>
<li>在右侧列表前面的方框中勾选要转换的论文（点表头“全选”可全选/全不选当前显示的论文）。</li>
<li>点击 <b>▶ 开始转换</b> 开关，勾选的论文才开始上传 MinerU 转换；再点一次（■ 停止转换）即关闭。</li>
</ol>
<h3>转换规则</h3>
<ul>
<li>每篇论文只转换一个 PDF：优先原文 PDF，自动跳过“译文/双语”等翻译版本。</li>
<li>论文默认<b>不勾选</b>；只有勾选了、并且打开了“开始转换”开关，才会转换。已有 Markdown 或没有 PDF 的论文方框为灰色。转换完成后自动取消勾选。</li>
<li><b>论文条目下已经有 Markdown 附件的，一律跳过，不会重复转换。</b></li>
<li>文件名为 <b>MD_作者_论文名称.md</b>，作者与标题的写法与 Zotero 给 PDF 命名时一致（如“Liu 等”“Wu和Cervera”）。</li>
<li>图片以 Base64 形式<b>内嵌在 Markdown 文件内部</b>，没有额外的图片文件夹，单个 .md 文件可以随意复制到其他地方。</li>
<li>超过 200 页的 PDF 会自动分段提交给 MinerU，结果按页序合并成一个文件。</li>
</ul>
<h3>Markdown 保存在哪里</h3>
<p><b>存入 Zotero（默认）</b>：Markdown 作为普通附件保存在 Zotero 数据目录的 storage 中，随 Zotero 同步；
在 Zotero 中右键附件 →“在文件管理器中显示”即可找到并复制。可在设置中勾选“同时在输出目录保存一份副本”，所有 MD 会集中出现在一个文件夹里。</p>
<p><b>链接到输出目录</b>：所有 Markdown 集中保存在你指定的文件夹，Zotero 中添加的是链接附件（链接附件不随 Zotero 文件同步，请勿移动该文件夹）。</p>
<h3>转换开关与新论文提醒</h3>
<ul>
<li>开关只负责转换<b>当前勾选</b>的论文；这一批全部转换完成（或失败）后，<b>开关自动关闭</b>。新加入的论文不会被自动转换。</li>
<li>以后回到某个分类（在左侧勾选分类、打开软件或点“立即扫描”）时，如果发现还没有 Markdown 的论文 PDF，会弹窗提醒：
选 <b>确认开启转换（推荐）</b> 会自动勾选这些论文并打开开关；选 <b>暂不转换</b> 则什么都不做，下次回到该分类时再提醒。</li>
<li>开关打开期间再勾选的论文会加入本批转换；取消勾选的会暂停。</li>
<li>网络中断、Zotero 暂时关闭都会自动重试；超过重试次数的显示“失败，待重试”，修复后点击“重试失败”从原阶段继续，不会重复上传已完成的步骤。</li>
<li>关闭主窗口会缩到系统托盘，转换继续；右键托盘图标选择“退出”才会完全退出。重新打开软件时开关默认关闭（设置中可改为“有未完成的转换时自动继续”）。</li>
</ul>
<h3>AI 论文总结（给 AI 助手快速检索用）</h3>
<ul>
<li>在 <b>设置 → AI 论文总结</b> 中选择大模型服务商（DeepSeek、通义千问、小米 MiMo、Claude、ChatGPT、Gemini、Kimi、智谱、
硅基流动、OpenRouter、本机 Ollama 或任意 OpenAI 兼容接口），填写 API Key 和模型，点“测试连接”。</li>
<li>勾选已有 Markdown 的论文，点 <b>生成 AI 总结</b>；也可以勾选“PDF 转换为 Markdown 后自动生成 AI 总结”。</li>
<li>默认 <b>精读全文</b>：整篇论文一次交给模型逐节精读并撰写卡片 → 再次阅读全文逐项核对、补全。输入和输出长度默认不限制；
只有论文超过模型上下文长度时才自动分段精读。
不论选哪家模型，都使用同一套内置提示词、固定的七个部分和统一的文件格式。</li>
<li>每篇生成 <b>SUM_作者_论文名称.md</b>（研究背景、研究目标、研究对象、研究方法、模型与理论、研究内容、研究结论，外加一句话总结和关键词），
作为论文的子附件；可选同时生成 Zotero 笔记。输出目录中的 <b>AI总结索引.md / ai_summaries.jsonl</b> 汇总所有论文，
AI 助手可以先查索引、再读总结卡片、最后只读真正相关论文的 Markdown 全文。</li>
</ul>
<h3>保留排版翻译（双语对照 PDF）</h3>
<ul>
<li>勾选有原文 PDF 的论文，点 <b>生成双语 PDF</b>。生成的 <b>双语_作者_论文名称.pdf</b> 每页左边是译文、右边是原文，作为附件加入论文条目，
在 Zotero 中双击即可阅读、批注。</li>
<li>翻译引擎是开源的 BabelDOC：它直接读取 PDF 中原有的文字，公式、图表、页眉页脚和超链接保持原样，只把正文段落换成译文并按原位置重新排版，
中文字体（思源宋体/黑体）嵌入 PDF，不会出现乱码。版面识别和排版都在本机完成，只有文字发给你选择的大模型。</li>
<li>在 <b>设置 → 保留排版翻译</b> 中可单独选择翻译用的大模型（默认与 AI 总结相同，Key 和接口地址也相同）、同时翻译的请求数、中文字体、
参考文献是否翻译，以及术语表（每行“英文,中文”）。</li>
<li>每篇生成后会自动检查：字体缺字、无法识别的字符和疑似没有翻译的页面；有问题时“双语 PDF”一栏显示“需检查”和具体页码。</li>
<li>扫描版 PDF（页面是图片，没有文字层）暂不支持。翻译中途失败可点“重试失败”，已翻译的段落有缓存，不会重复计费。</li>
</ul>
<h3>研究综述与新文献追踪</h3>
<ul>
<li>在 <b>研究综述与追踪</b> 页勾选要综述的分类（含全部子分类），选择大模型，打开 <b>▶ 开启研究综述与追踪</b> 开关。</li>
<li>最底层的分类：大模型阅读其中论文的 AI 总结卡片（没有卡片的用 Zotero 摘要）和论文之间的引用关系，写出研究对象与关键词、研究地图、研究脉络、
研究发展情况和研究进展，再回读关键论文的原文方法与结论段落核对补全；上一级分类在各子分类综述的基础上汇总整合，一直到最上一级。</li>
<li>结果保存在输出目录的 <b>研究地图</b> 文件夹，按分类层级存放 RES_分类名.md（综述）、MAP_分类名.html（研究地图）和 TRK_分类名.md（新文献追踪），
顶层的 研究地图索引.md 汇总全部分类，供 AI 助手先查索引、再读综述、最后读相关论文。</li>
<li>定期执行：默认每 7 天检查一次，只有论文或总结卡片有变化的分类（及其上级）才重新综述；每 7 天追踪一次新文献。软件需要在托盘中运行，错过的会在下次打开时补做。</li>
<li>新文献追踪：以每个分类的论文为种子，找近半年引用了它们的新论文（OpenAlex，填写 Semantic Scholar Key 后再加上其引用列表）和 Semantic Scholar 的相似论文推荐，
按与本分类论文的语义相似度筛选，再由大模型阅读标题和摘要逐篇评分，只列出达到阈值的论文。</li>
</ul>
<h3>关于 Zotero MCP</h3>
<p>本软件不需要 Zotero MCP：MCP 是给 AI 助手用的接口。本软件直接读取 Zotero 自带的本地 API（与 zotero-mcp 本地模式读取的是同一接口），
写入附件则通过附带的小插件完成，因为 Zotero 本地 API 是只读的。</p>
<p>Token 使用 Windows 当前用户的 DPAPI 加密保存，不写入日志。</p>
"""

PLUGIN_FOR_TRANSLATION = ("翻译本身不需要插件，会照常进行；生成的双语 PDF 在插件更新后自动加入论文条目（不会重新翻译）。"
                          "安装方法：在 Zotero 的“工具 → 插件”中，点右上角齿轮 →“从文件安装插件…”，选择本软件提供的 XPI。")
PAPER_ROLE = Qt.ItemDataRole.UserRole
CHILD_ROLE = Qt.ItemDataRole.UserRole + 1
PROGRESS_ROLE = Qt.ItemDataRole.UserRole + 2  # 0–100 while a summary or translation is running
SUMMARY_COLUMN, TRANSLATION_COLUMN = 6, 7
CHILD_KINDS = {  # like Zotero's item tree: what each child of a paper is
    "pdf": ("📄", "PDF 原文"), "translation": ("📑", "译文/双语 PDF"), "markdown": ("📝", "Markdown 全文"),
    "summary": ("⭐", "AI 总结卡片"),
    "note": ("🗒", "笔记"), "snapshot": ("🌐", "网页快照"), "other": ("📎", "其他附件"), "link": ("🔗", "网页链接"),
}

STATUS_FILTERS = [
    ("全部论文", None),
    ("已勾选 / 进行中", ("已勾选", "排队中", "上传 PDF", "云端解析", "下载结果", "生成 Markdown", "添加到 Zotero", "等待 PDF")),
    ("已完成", ("已完成",)),
    ("已有 Markdown（跳过）", ("已有 Markdown",)),
    ("失败 / 不可用", ("失败", "不可用")),
    ("未勾选 / 暂无 PDF", ("未勾选", "暂无 PDF")),
]


def translation_percent(status: str) -> int | None:
    """The bar for the 双语 PDF column: “翻译中 37%” → 37; “翻译中” alone (engine starting) shows a sliver."""
    match = re.match(r"翻译中(?: (\d+)%)?", status)
    return (int(match[1]) if match[1] else 2) if match else None


class ProgressDelegate(QStyledItemDelegate):
    """Draws a progress bar under the cell text when the cell carries a percentage (PROGRESS_ROLE)."""

    def paint(self, painter, option, index):
        percent = index.data(PROGRESS_ROLE)
        if percent is None:
            return super().paint(painter, option, index)
        option = QStyleOptionViewItem(option)
        self.initStyleOption(option, index)
        text, option.text = option.text, ""
        style = option.widget.style() if option.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, option, painter, option.widget)  # background, selection
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bar = QRectF(option.rect.adjusted(3, 3, -3, -3))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#e2e8f0"))
        painter.drawRoundedRect(bar, 3, 3)
        painter.setBrush(QColor("#93c5fd"))
        painter.drawRoundedRect(QRectF(bar.x(), bar.y(), bar.width() * max(0, min(100, percent)) / 100, bar.height()), 3, 3)
        painter.setPen(QColor("#1e3a8a"))
        painter.drawText(bar, Qt.AlignmentFlag.AlignCenter, text)
        painter.restore()


class Window(QMainWindow):
    def __init__(self, config: Config, *, smoke=False):
        super().__init__()
        self.config = config
        self.smoke = smoke
        self._force_quit = False
        self._engine_installer = None
        self._engine_install_cancel = threading.Event()
        self._tree_populating = False
        self._rows_populating = False
        self._watching = False
        self._resume_sent = False
        # Offer to convert unconverted papers after the next scan the user caused (start-up, choosing a collection,
        # “立即扫描”); routine re-scans never pop up a dialog.
        self._prompt_pending = True
        self._expanded: set[str] = set()
        self._rows: list[dict] = []
        self._collection_signature = None
        self._collections: list[dict] = []
        self._research_signature = None
        self._research: dict = {"rows": [], "running": "", "progress": "", "review_last": 0, "track_last": 0}
        self._estimate_purpose = ""
        self.store = Store(config.home / "jobs-v2.sqlite3")
        self.events = Events()
        self.events.event.connect(self.handle_event)
        self.selection_timer = QTimer(self)
        self.selection_timer.setSingleShot(True)
        self.selection_timer.setInterval(900)
        self.selection_timer.timeout.connect(self.scan)
        self.setWindowTitle(f"Zotero PDF 转 Markdown · MinerU  {__version__}")
        self.setWindowIcon(app_icon())
        self.resize(1320, 860)
        self.setMinimumSize(1020, 680)
        self._build()
        self._load_settings()
        self.engine = Engine(self.store, self.config.data, self.config.token, self.config.pairing["token"],
                             self.config.work_dir, self.events.event.emit)
        self.engine.start()
        self.summarizer = Summarizer(self.store, self.config.data, self.config.llm_keys, self.config.pairing["token"],
                                     self.config.work_dir, self.events.event.emit)
        self.summarizer.start()
        self.researcher = Researcher(self.store, self.config.data, self.config.llm_keys, self.config.scholar_keys,
                                     self.config.pairing["token"], self.events.event.emit)
        self.researcher.start()
        self.translator = Translator(self.store, self.config.data, self.config.llm_keys, self.config.pairing["token"],
                                     self.config.work_dir, self.config.home, self.events.event.emit)
        self.translator.start()
        self._tray()
        if not self.config.token:
            self.tabs.setCurrentWidget(self.settings_page)
        if self.config.load_error:
            self.handle_event("log", self.config.load_error)
        if not self.smoke:
            QTimer.singleShot(1200, self.offer_engine_download)

    # ------------------------------------------------------------------ layout
    def _build(self):
        container = QWidget()
        outer = QVBoxLayout(container)
        outer.setContentsMargins(22, 18, 22, 12)
        title = QLabel("Zotero PDF → Markdown")
        title.setObjectName("title")
        self.connection = QLabel("正在连接 Zotero…")
        self.connection.setObjectName("muted")
        header = QHBoxLayout()
        header.addWidget(title)
        header.addStretch()
        header.addWidget(self.connection)
        outer.addLayout(header)
        description = QLabel("勾选 Zotero 分类，MinerU 自动把论文 PDF 解析为 Markdown（MD_作者_论文名称.md，图片内嵌），并放回原论文条目。")
        description.setObjectName("muted")
        outer.addWidget(description)
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)
        self.tabs.addTab(self._work_tab(), "文献与任务")
        self.tabs.addTab(self._research_tab(), "研究综述与追踪")
        self.settings_page = self._settings_tab()
        self.tabs.addTab(self.settings_page, "设置")
        help_widget = QTextBrowser()
        help_widget.setOpenExternalLinks(True)
        help_widget.setHtml(HELP)
        self.tabs.addTab(help_widget, "使用说明")
        self.setCentralWidget(container)
        self.statusBar().showMessage("就绪")

    def _work_tab(self):
        work = QWidget()
        layout = QVBoxLayout(work)
        toolbar = QHBoxLayout()
        self.scan_button = QPushButton("立即扫描")
        self.scan_button.clicked.connect(lambda: self.scan(prompt=True))
        self.switch = QPushButton("▶ 开始转换")
        self.switch.setObjectName("primary")
        self.switch.setToolTip("打开后才开始转换右侧勾选的论文；再点一次关闭")
        self.switch.clicked.connect(self.toggle_switch)
        retry = QPushButton("重试失败")
        retry.clicked.connect(lambda: self.engine.command("retry"))
        summarize = QPushButton("生成 AI 总结")
        summarize.setToolTip("用设置中的大模型精读勾选论文的 Markdown 全文，生成 SUM_作者_论文名称.md 总结卡片")
        summarize.clicked.connect(self.generate_summaries)
        cancel = QPushButton("停止 AI 总结")
        cancel.setToolTip("取消排队中的 AI 总结，也可以连同正在阅读的一篇一起停止（会先确认，确认后立即生效）")
        cancel.clicked.connect(self.stop_summaries)
        translate = QPushButton("生成双语 PDF")
        translate.setToolTip("用设置中的大模型翻译勾选论文的 PDF，生成保留原排版的双语对照 PDF（左译文、右原文）并加入论文条目")
        translate.clicked.connect(self.generate_translations)
        stop_translate = QPushButton("停止翻译")
        stop_translate.setToolTip("停止正在翻译的论文并取消排队中的翻译")
        stop_translate.clicked.connect(lambda: self.translator.command("cancel"))
        for widget in (self.scan_button, self.switch, retry, summarize, cancel, translate, stop_translate):
            toolbar.addWidget(widget)
        toolbar.addStretch()
        self.monitor_label = QLabel("转换开关：关闭")
        self.monitor_label.setObjectName("monitor")
        toolbar.addWidget(self.monitor_label)
        layout.addLayout(toolbar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 8, 0)
        ll.addWidget(QLabel("① 选择分类（只显示论文，不会自动转换）"))
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("搜索分类名称")
        self.filter.textChanged.connect(self.filter_tree)
        ll.addWidget(self.filter)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.itemChanged.connect(self.tree_changed)
        ll.addWidget(self.tree, 1)
        self.include_sub = QCheckBox("包含子分类")
        self.include_sub.toggled.connect(self.selection_changed)
        ll.addWidget(self.include_sub)
        splitter.addWidget(left)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(6, 0, 0, 0)
        options = QHBoxLayout()
        options.addWidget(QLabel("② 勾选论文，再打开“开始转换”开关，或点“生成 AI 总结”“生成双语 PDF”"))
        options.addStretch()
        options.addWidget(QLabel("显示"))
        self.status_filter = QComboBox()
        for label, _ in STATUS_FILTERS:
            self.status_filter.addItem(label)
        self.status_filter.currentIndexChanged.connect(self.apply_row_filter)
        options.addWidget(self.status_filter)
        open_md = QPushButton("打开 Markdown")
        open_md.clicked.connect(self.open_markdown)
        options.addWidget(open_md)
        show_zotero = QPushButton("在 Zotero 中显示")
        show_zotero.clicked.connect(self.show_in_zotero)
        options.addWidget(show_zotero)
        delete = QPushButton("删除选中项")
        delete.setToolTip("展开论文后选中其中的 PDF、Markdown、AI 总结卡片或笔记，移入 Zotero 回收站")
        delete.clicked.connect(self.delete_selected)
        options.addWidget(delete)
        rl.addLayout(options)
        self.table = QTreeWidget()
        self.table.setColumnCount(10)
        self.table.setHeaderLabels(["全选", "论文标题", "作者", "所属分类", "PDF", "Markdown 文件", "AI 总结", "双语 PDF",
                                    "状态", "进度／说明"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setUniformRowHeights(True)
        self.table.setExpandsOnDoubleClick(False)
        header = self.table.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(9, QHeaderView.ResizeMode.Stretch)
        header.setSectionsClickable(True)
        header.sectionClicked.connect(self.header_clicked)
        self.table.headerItem().setToolTip(0, "点击“全选”勾选/取消当前显示的全部论文；点论文左侧箭头展开其中的 PDF、Markdown、总结和笔记")
        for index, width in enumerate((78, 240, 90, 90, 120, 150, 110, 110, 100, 200)):
            self.table.setColumnWidth(index, width)
        self.progress_delegate = ProgressDelegate(self.table)
        for column in (SUMMARY_COLUMN, TRANSLATION_COLUMN):
            self.table.setItemDelegateForColumn(column, self.progress_delegate)
        # The model says nothing while it writes, so the bar of the paper being summarised moves with the clock.
        self.progress_timer = QTimer(self)
        self.progress_timer.setInterval(1000)
        self.progress_timer.timeout.connect(self.refresh_progress)
        self.progress_timer.start()
        self.table.itemChanged.connect(self.paper_changed)
        self.table.itemDoubleClicked.connect(self.item_double_clicked)
        self.table.itemExpanded.connect(lambda item: self._expanded.add(item.data(0, PAPER_ROLE)))
        self.table.itemCollapsed.connect(lambda item: self._expanded.discard(item.data(0, PAPER_ROLE)))
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.item_menu)
        rl.addWidget(self.table, 1)
        self.summary = QLabel("在左侧勾选分类后，这里会列出其中的论文。")
        rl.addWidget(self.summary)
        splitter.addWidget(right)
        splitter.setSizes([290, 960])
        layout.addWidget(splitter, 1)
        self.logs = QTextEdit()
        self.logs.setReadOnly(True)
        self.logs.document().setMaximumBlockCount(500)
        self.logs.setMaximumHeight(120)
        self.logs.setPlaceholderText("运行日志")
        layout.addWidget(self.logs)
        return work

    def _research_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        bar = QHBoxLayout()
        self.research_switch = QPushButton("▶ 开启研究综述与追踪")
        self.research_switch.setObjectName("primary")
        self.research_switch.setToolTip("打开后立即为勾选的分类生成研究综述并追踪新文献，之后按设置的周期自动执行；再点一次关闭")
        self.research_switch.clicked.connect(self.toggle_research)
        review_now = QPushButton("立即更新综述")
        review_now.setToolTip("检查勾选分类的论文和总结卡片，有变化的分类（及其上级）重新综述")
        review_now.clicked.connect(lambda: self.request_estimate("review"))
        track_now = QPushButton("立即追踪新文献")
        track_now.clicked.connect(self.track_now)
        stop = QPushButton("停止")
        stop.setToolTip("当前一次大模型调用结束后停止；已完成的分类会保留")
        stop.clicked.connect(lambda: self.researcher.cancel())
        open_folder = QPushButton("打开研究地图文件夹")
        open_folder.clicked.connect(lambda: self.open_research_file(""))
        open_index = QPushButton("打开总索引")
        open_index.clicked.connect(lambda: self.open_research_file(INDEX_MD))
        for widget in (self.research_switch, review_now, track_now, stop, open_folder, open_index):
            bar.addWidget(widget)
        bar.addStretch()
        self.research_state = QLabel("研究综述与追踪：关闭")
        self.research_state.setObjectName("monitor")
        bar.addWidget(self.research_state)
        layout.addLayout(bar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 8, 0)
        ll.addWidget(QLabel("① 勾选要综述的分类（含其全部子分类）"))
        self.research_tree = QTreeWidget()
        self.research_tree.setHeaderHidden(True)
        self.research_tree.itemChanged.connect(self.research_roots_changed)
        ll.addWidget(self.research_tree, 1)
        splitter.addWidget(left)

        right = QSplitter(Qt.Orientation.Vertical)
        self.research_table = QTreeWidget()
        self.research_table.setColumnCount(7)
        self.research_table.setHeaderLabels(["分类", "论文", "资料（卡片/摘要/题录）", "综述", "生成时间", "新文献", "说明"])
        self.research_table.setAlternatingRowColors(True)
        self.research_table.setUniformRowHeights(True)
        for index, width in enumerate((260, 56, 150, 80, 130, 110, 300)):
            self.research_table.setColumnWidth(index, width)
        self.research_table.itemDoubleClicked.connect(lambda item, _c: self.open_research_row(item, "review"))
        self.research_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.research_table.customContextMenuRequested.connect(self.research_menu)
        right.addWidget(self.research_table)
        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setFrameShape(QFrame.Shape.NoFrame)
        settings_scroll.setWidget(self._research_settings())
        right.addWidget(settings_scroll)
        right.setSizes([380, 300])
        splitter.addWidget(right)
        splitter.setSizes([290, 960])
        layout.addWidget(splitter, 1)
        self.research_progress = QLabel("勾选分类并打开开关后，这里显示各分类的综述和追踪情况。双击分类打开它的研究综述。")
        self.research_progress.setObjectName("muted")
        self.research_progress.setWordWrap(True)
        layout.addWidget(self.research_progress)
        return page

    def _research_settings(self):
        group = QGroupBox("综述与追踪设置")
        group.setObjectName("researchSettings")
        form = QFormLayout(group)
        self.research_provider = QComboBox()
        self.research_provider.addItem("与 AI 总结相同", "")
        for key, (label, *_rest) in PROVIDERS.items():
            self.research_provider.addItem(label, key)
        self.research_provider.currentIndexChanged.connect(self._research_provider_changed)
        self.research_model = QComboBox()
        self.research_model.setEditable(True)
        self.research_model.setMinimumWidth(260)
        self.research_model.lineEdit().setPlaceholderText("留空：与 AI 总结使用的模型相同")
        model_row = QHBoxLayout()
        model_row.addWidget(self.research_provider)
        model_row.addWidget(self.research_model, 1)
        form.addRow("大模型", model_row)
        key_hint = QLabel("API Key 和接口地址使用“设置 → AI 论文总结”中该服务商的配置。综述需要读入大量资料，建议选用上下文较长的模型。")
        key_hint.setObjectName("muted")
        key_hint.setWordWrap(True)
        form.addRow(key_hint)
        self.research_interval = QSpinBox()
        self.research_interval.setRange(1, 180)
        self.research_interval.setSuffix(" 天")
        self.research_interval.setToolTip("每隔这么多天检查一次；只有论文、总结卡片或子分类综述有变化的分类才重新综述")
        form.addRow("综述检查周期", self.research_interval)
        self.research_reread = QSpinBox()
        self.research_reread.setRange(0, 30)
        self.research_reread.setSuffix(" 篇")
        self.research_reread.setSpecialValueText("不回读原文")
        self.research_reread.setToolTip("写完初稿后，回读研究脉络和继承关系中最关键的几篇论文的 Markdown 全文（方法与结论段落），核对补全综述")
        form.addRow("回读关键论文原文", self.research_reread)
        self.research_html = QCheckBox("同时生成可交互的研究地图网页（MAP_分类名.html：论文时间轴、引用关系和子方向）")
        form.addRow(self.research_html)
        self.tracking_enabled = QCheckBox("追踪新发表的相关论文（Semantic Scholar、OpenAlex，免费）")
        form.addRow(self.tracking_enabled)
        self.tracking_interval = QSpinBox()
        self.tracking_interval.setRange(1, 90)
        self.tracking_interval.setSuffix(" 天")
        form.addRow("追踪周期", self.tracking_interval)
        self.tracking_window = QSpinBox()
        self.tracking_window.setRange(30, 1095)
        self.tracking_window.setSingleStep(30)
        self.tracking_window.setSuffix(" 天内发表")
        form.addRow("追踪时间范围", self.tracking_window)
        self.tracking_budget = QSpinBox()
        self.tracking_budget.setRange(1, 100)
        self.tracking_budget.setSuffix(" 篇")
        form.addRow("每个分类每次最多推荐", self.tracking_budget)
        self.tracking_score = QSpinBox()
        self.tracking_score.setRange(0, 10)
        self.tracking_score.setSuffix(" / 10 分")
        self.tracking_score.setToolTip("大模型按 0–10 分评价候选论文与本分类的相关程度，达到这个分数才列入追踪报告")
        form.addRow("最低相关度", self.tracking_score)
        for spin in (self.research_interval, self.research_reread, self.tracking_interval, self.tracking_window,
                     self.tracking_budget, self.tracking_score):
            spin.setMaximumWidth(200)
        self.s2_key = QLineEdit()
        self.s2_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.s2_key.setPlaceholderText("可选（免费申请）：不填时公共额度常拥挤，填写后更快更稳定，并加上逐篇引用列表")
        s2_row = QHBoxLayout()
        s2_row.addWidget(self.s2_key, 1)
        s2_row.addWidget(self._link("申请", "https://www.semanticscholar.org/product/api#api-key-form"))
        form.addRow("Semantic Scholar Key", s2_row)
        self.openalex_key = QLineEdit()
        self.openalex_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.openalex_key.setPlaceholderText("可选：填写后额外按关键词检索新论文（免费账户每天约 1 美元额度）")
        oa_row = QHBoxLayout()
        oa_row.addWidget(self.openalex_key, 1)
        oa_row.addWidget(self._link("注册", "https://openalex.org/"))
        form.addRow("OpenAlex Key", oa_row)
        save = QPushButton("保存设置")
        save.clicked.connect(lambda: self.save_settings(feedback=True) and self.research_progress.setText("设置已保存。"))
        form.addRow(save)
        hint = QLabel("综述和追踪全部由你选择的大模型完成，费用按服务商计费；开启前会提示预计调用次数和输入 tokens。"
                      "文件保存在输出目录的“研究地图”文件夹，不写入 Zotero。")
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        form.addRow(hint)
        return group

    def _research_provider_changed(self, *_):
        provider = self.research_provider.currentData() or self.llm_provider.currentData()
        current = self.research_model.currentText()
        self.research_model.clear()
        self.research_model.addItems(PROVIDERS.get(provider, ("", "", "", []))[3])
        self.research_model.setCurrentText(current if self.research_provider.currentData() else "")

    def _settings_tab(self):
        settings = QWidget()
        settings.setObjectName("settingsPage")
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        sl = QVBoxLayout(settings)
        sl.setContentsMargins(24, 16, 24, 16)
        cloud = QGroupBox("MinerU 云端解析")
        form = QFormLayout(cloud)
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.token.setPlaceholderText("粘贴你自己的 MinerU API Token")
        token_row = QHBoxLayout()
        token_row.addWidget(self.token, 1)
        show = QCheckBox("显示")
        show.toggled.connect(lambda checked: self.token.setEchoMode(
            QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password))
        token_row.addWidget(show)
        probe = QPushButton("检查 API 连接")
        probe.clicked.connect(self.probe)
        token_row.addWidget(probe)
        form.addRow("API Token", token_row)
        form.addRow("获取 Token", self._link("打开 MinerU API 管理页面（登录后创建 Token）", "https://mineru.net/apiManage/token"))
        self.model = QComboBox()
        self.model.addItem("VLM 高精度模型（推荐）", "vlm")
        self.model.addItem("Pipeline 传统模型", "pipeline")
        form.addRow("解析模型", self.model)
        self.language = QComboBox()
        for label, value in (("中文 / 英文混合（ch）", "ch"), ("英文（en）", "en"), ("繁体中文（chinese_cht）", "chinese_cht"),
                             ("日文（japan）", "japan"), ("韩文（korean）", "korean"), ("拉丁语系（latin）", "latin")):
            self.language.addItem(label, value)
        form.addRow("文档语言", self.language)
        flags = QHBoxLayout()
        self.ocr = QCheckBox("强制 OCR（扫描版 PDF）")
        self.formula = QCheckBox("识别公式")
        self.tables_flag = QCheckBox("识别表格")
        for widget in (self.ocr, self.formula, self.tables_flag):
            flags.addWidget(widget)
        flags.addStretch()
        form.addRow("解析选项", flags)
        disclosure = QLabel("打开“开始转换”开关后，勾选论文的 PDF 才会上传至 MinerU 云端。单个 PDF 上限 200 MB；超过 200 页自动分段解析。")
        disclosure.setWordWrap(True)
        disclosure.setObjectName("muted")
        form.addRow(disclosure)
        sl.addWidget(cloud)

        saving = QGroupBox("Markdown 保存")
        sf = QFormLayout(saving)
        self.storage_mode = QComboBox()
        self.storage_mode.addItem("存入 Zotero 存储（随文库同步，推荐）", "import")
        self.storage_mode.addItem("链接到输出目录（所有 MD 集中在一个文件夹）", "link")
        self.storage_mode.currentIndexChanged.connect(self._storage_mode_changed)
        sf.addRow("保存方式", self.storage_mode)
        self.output = QLineEdit()
        browse = QPushButton("选择目录")
        browse.clicked.connect(self.choose_output)
        open_output = QPushButton("打开")
        open_output.clicked.connect(self.open_output)
        output_row = QHBoxLayout()
        output_row.addWidget(self.output, 1)
        output_row.addWidget(browse)
        output_row.addWidget(open_output)
        sf.addRow("输出目录", output_row)
        self.export_copy = QCheckBox("同时在输出目录保存一份副本（方便整体复制）")
        sf.addRow(self.export_copy)
        hint = QLabel("文件名：MD_作者_论文名称.md（作者、标题写法与 Zotero 给 PDF 命名一致）。图片以 Base64 内嵌在论文原位置，不生成图片文件夹。")
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        sf.addRow(hint)
        sl.addWidget(saving)
        sl.addWidget(self._ai_group())
        sl.addWidget(self._translate_group())

        local = QGroupBox("Zotero 连接")
        lf = QFormLayout(local)
        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        lf.addRow("Zotero 本地端口", self.port)
        self.interval = QSpinBox()
        self.interval.setRange(15, 3600)
        self.interval.setSuffix(" 秒")
        lf.addRow("定时补扫间隔", self.interval)
        self.resume = QCheckBox("下次打开软件时，如有未完成的转换任务则自动继续（默认关闭）")
        lf.addRow(self.resume)
        for spin in (self.port, self.interval):
            spin.setMaximumWidth(180)
        install = QPushButton("安装/更新 Zotero 插件…")
        install.clicked.connect(self.install_bridge)
        install_row = QHBoxLayout()
        install_row.addWidget(install)
        install_row.addWidget(QLabel("（Zotero 本地 API 只读，写入附件需要这个小插件）"))
        install_row.addStretch()
        lf.addRow("桥接插件", install_row)
        sl.addWidget(local)

        controls = QHBoxLayout()
        save = QPushButton("保存设置")
        save.setObjectName("primary")
        save.clicked.connect(lambda: self.save_settings(feedback=True))
        refresh = QPushButton("刷新 Zotero 连接")
        refresh.clicked.connect(self.scan)
        controls.addWidget(save)
        controls.addWidget(refresh)
        controls.addStretch()
        sl.addLayout(controls)
        self.settings_message = QLabel("")
        self.settings_message.setWordWrap(True)
        sl.addWidget(self.settings_message)
        sl.addStretch()
        scroll.setWidget(settings)
        return scroll

    def _ai_group(self):
        group = QGroupBox("AI 论文总结（大语言模型 API）")
        form = QFormLayout(group)
        self._llm_current = None
        self.llm_provider = QComboBox()
        for key, (label, *_rest) in PROVIDERS.items():
            self.llm_provider.addItem(label, key)
        self.llm_provider.currentIndexChanged.connect(self._provider_changed)
        form.addRow("服务商", self.llm_provider)
        self.llm_base = QLineEdit()
        self.llm_base.setPlaceholderText("OpenAI 兼容接口填到 /v1 为止，例如 https://api.example.com/v1")
        reset = QPushButton("恢复默认地址")
        reset.clicked.connect(lambda: self.llm_base.setText(PROVIDERS[self.llm_provider.currentData()][2]))
        base_row = QHBoxLayout()
        base_row.addWidget(self.llm_base, 1)
        base_row.addWidget(reset)
        form.addRow("接口地址", base_row)
        self.llm_key = QLineEdit()
        self.llm_key.setEchoMode(QLineEdit.EchoMode.Password)
        show = QCheckBox("显示")
        show.toggled.connect(lambda checked: self.llm_key.setEchoMode(
            QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password))
        key_row = QHBoxLayout()
        key_row.addWidget(self.llm_key, 1)
        key_row.addWidget(show)
        form.addRow("API Key", key_row)
        self.llm_model = QComboBox()
        self.llm_model.setEditable(True)
        self.llm_model.setMinimumWidth(280)
        fetch = QPushButton("获取模型列表")
        fetch.clicked.connect(lambda: self.save_settings() and self.summarizer.command("models"))
        test = QPushButton("测试连接")
        test.clicked.connect(self.test_llm)
        model_row = QHBoxLayout()
        model_row.addWidget(self.llm_model, 1)
        model_row.addWidget(fetch)
        model_row.addWidget(test)
        form.addRow("模型", model_row)
        self.summary_depth = QComboBox()
        self.summary_depth.addItem("精读全文：完整阅读全文撰写 → 再次阅读全文逐项核对补全（推荐）", "deep")
        self.summary_depth.addItem("快速通读：完整阅读全文，一次写成（省 token）", "quick")
        form.addRow("阅读方式", self.summary_depth)
        self.summary_language = QComboBox()
        self.summary_language.addItem("简体中文", "zh")
        self.summary_language.addItem("English", "en")
        form.addRow("总结语言", self.summary_language)
        self.max_words = QSpinBox()
        self.max_words.setRange(500, 20000)
        self.max_words.setSingleStep(500)
        self.max_words.setSuffix(" 字")
        self.max_words.setMaximumWidth(220)
        self.max_words.setToolTip("一句话总结、关键词和七个部分合计的上限；中文按字计，英文单词和数字按词计（同 Word 字数统计）。超出时自动请模型压缩")
        form.addRow("总结字数上限", self.max_words)
        self.budget = QSpinBox()
        self.budget.setRange(0, 2000000)
        self.budget.setSingleStep(10000)
        self.budget.setSuffix(" tokens")
        self.budget.setSpecialValueText("自动（不限制，整篇读取）")
        self.budget.setToolTip("默认整篇论文一次交给模型；只有模型报告超出上下文长度时才自动分段")
        form.addRow("单次输入上限", self.budget)
        self.max_output = QSpinBox()
        self.max_output.setRange(0, 262144)
        self.max_output.setSingleStep(4096)
        self.max_output.setSuffix(" tokens")
        self.max_output.setSpecialValueText("自动（不限制，按模型上限）")
        self.max_output.setToolTip("默认不限制；按模型自身的输出上限请求，思考型模型额度不够时自动加大")
        form.addRow("最大输出", self.max_output)
        self.part_tokens = QSpinBox()
        self.part_tokens.setRange(4000, 500000)
        self.part_tokens.setSingleStep(2000)
        self.part_tokens.setSuffix(" tokens")
        self.part_tokens.setToolTip("仅当论文超过模型上下文长度时使用：按顺序分段精读，每段的长度")
        form.addRow("超长论文分段长度", self.part_tokens)
        for spin in (self.part_tokens, self.budget, self.max_output):
            spin.setMaximumWidth(220)
        self.summary_auto = QCheckBox("PDF 转换为 Markdown 后自动生成 AI 总结")
        self.summary_note = QCheckBox("同时把总结添加为 Zotero 笔记（在 Zotero 中直接查看，可被搜索和 zotero-mcp 读取）")
        form.addRow(self.summary_auto)
        form.addRow(self.summary_note)
        self.summary_vision = QCheckBox("总结时让模型看论文中的图片（需支持看图的模型，如 qwen3-vl-plus、GPT、Claude、Gemini、glm-4.5v；"
                                        "DeepSeek 等只读文字的模型会自动改为只读文字）")
        self.summary_vision.setToolTip("图片按顺序编号为 [图 N] 与正文对应，缩小到长边 1024 像素后随全文发送；图标、logo 等小图自动跳过。"
                                       "看图会增加输入 token（每张图约 1000 tokens 左右，视模型而定）")
        self.vision_max = QSpinBox()
        self.vision_max.setRange(1, 200)
        self.vision_max.setSuffix(" 张")
        self.vision_max.setMaximumWidth(220)
        self.summary_vision.toggled.connect(self.vision_max.setEnabled)
        form.addRow(self.summary_vision)
        form.addRow("每篇最多看图", self.vision_max)
        hint = QLabel("每篇生成 SUM_作者_论文名称.md 总结卡片（研究背景、研究目标、研究对象、研究方法、模型与理论、研究内容、研究结论，"
                      "默认不超过 3000 字），作为论文条目的子附件，卡片中带有指向 Zotero 条目、论文 PDF 和全文 Markdown 的链接；输出目录中同时维护 AI总结索引.md 和 ai_summaries.jsonl，方便 AI 助手先查索引、再读全文。"
                      "无论选择哪家服务商，都使用同一套内置提示词和阅读流程。API Key 用 Windows 账户加密保存。")
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        form.addRow(hint)
        return group

    def _translate_group(self):
        group = QGroupBox("保留排版翻译（双语对照 PDF：左边译文、右边原文）")
        form = QFormLayout(group)
        self.translate_provider = QComboBox()
        self.translate_provider.addItem("与 AI 总结相同", "")
        for key, (label, *_rest) in PROVIDERS.items():
            self.translate_provider.addItem(label, key)
        self.translate_provider.currentIndexChanged.connect(self._translate_provider_changed)
        self.translate_model = QComboBox()
        self.translate_model.setEditable(True)
        self.translate_model.setMinimumWidth(260)
        self.translate_model.lineEdit().setPlaceholderText("留空：与 AI 总结使用的模型相同")
        model_row = QHBoxLayout()
        model_row.addWidget(self.translate_provider)
        model_row.addWidget(self.translate_model, 1)
        form.addRow("大模型", model_row)
        key_hint = QLabel("API Key 和接口地址使用上面“AI 论文总结”中该服务商的配置。翻译不需要思考型模型，"
                          "选便宜、快速的模型即可（如 deepseek-v4-flash、qwen-flash）；DeepSeek、通义、智谱会自动关闭思考模式。")
        key_hint.setObjectName("muted")
        key_hint.setWordWrap(True)
        form.addRow(key_hint)
        self.translate_qps = QSpinBox()
        self.translate_qps.setRange(1, 32)
        self.translate_qps.setSuffix(" 个")
        self.translate_qps.setMaximumWidth(220)
        self.translate_qps.setToolTip("同时发给大模型的翻译请求数；越大越快，服务商限流（HTTP 429）时调小")
        form.addRow("同时翻译的请求数", self.translate_qps)
        self.translate_font = QComboBox()
        self.translate_font.addItem("宋体风格：思源宋体，接近论文原文的 Times 字体（推荐）", "serif")
        self.translate_font.addItem("黑体风格：思源黑体，与 Doc2X 的效果相近", "sans-serif")
        form.addRow("中文字体", self.translate_font)
        self.translate_refs = QComboBox()
        self.translate_refs.addItem("翻译参考文献（与 Doc2X 相同，人名、期刊缩写、DOI 保持原样）", "translate")
        self.translate_refs.addItem("参考文献保留原文", "keep")
        form.addRow("参考文献", self.translate_refs)
        self.translate_terms = QCheckBox("翻译前先让模型通读全文提取专业术语，全文统一译法（推荐；约多用三成 tokens）")
        form.addRow(self.translate_terms)
        self.translate_mono = QCheckBox("同时生成只有译文的 PDF（译文_作者_论文名称.pdf，也加入论文条目）")
        form.addRow(self.translate_mono)
        glossary = QPushButton("编辑术语表…")
        glossary.clicked.connect(self.edit_glossary)
        glossary_row = QHBoxLayout()
        glossary_row.addWidget(glossary)
        glossary_row.addWidget(QLabel("每行“英文,中文”，例如 phase-field,相场；保存后对之后的翻译生效"))
        glossary_row.addStretch()
        form.addRow("术语表", glossary_row)
        check = QPushButton("检查翻译引擎")
        check.setToolTip("确认翻译引擎、版面识别模型和中文字体可用（首次使用会下载约 300 MB 的模型和字体）")
        check.clicked.connect(self.check_translator)
        self.translate_engine_label = QLabel("找到翻译引擎" if engine_command() else NO_ENGINE)
        self.translate_engine_label.setWordWrap(True)
        self.translate_engine_label.setObjectName("muted")
        engine_row = QHBoxLayout()
        self.download_engine_button = QPushButton("下载翻译资源包")
        self.download_engine_button.setToolTip("从 GitHub 下载 BabelDocWorker，校验后自动解压配置，无需管理员权限")
        self.download_engine_button.clicked.connect(self.install_engine)
        engine_row.addWidget(self.download_engine_button)
        engine_row.addWidget(check)
        engine_row.addWidget(self.translate_engine_label, 1)
        form.addRow("翻译引擎", engine_row)
        hint = QLabel("勾选论文后点“生成双语 PDF”：开源引擎 BabelDOC 在本机识别版面，公式、图表和页面元素保持原样，只把段落换成译文，"
                      "中文字体嵌入 PDF；生成 双语_作者_论文名称.pdf（每页左边译文、右边原文），保存方式与 Markdown 相同。"
                      "只有段落文字发给大模型，费用按服务商计费。扫描版 PDF 暂不支持。")
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        form.addRow(hint)
        return group

    def _translate_provider_changed(self, *_):
        provider = self.translate_provider.currentData() or self.llm_provider.currentData()
        current = self.translate_model.currentText()
        self.translate_model.clear()
        self.translate_model.addItems(PROVIDERS.get(provider, ("", "", "", []))[3])
        self.translate_model.setCurrentText(current if self.translate_provider.currentData() else "")

    def edit_glossary(self):
        path = self.config.home / GLOSSARY
        try:
            if not path.exists():
                path.write_text("source,target\n", encoding="utf-8-sig")  # BOM: Excel opens Chinese correctly
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        except OSError as error:
            self.statusBar().showMessage(str(error), 10000)

    def check_translator(self):
        if not engine_command():
            self.offer_engine_download()
            return
        self.translate_engine_label.setText("正在检查翻译引擎（首次使用会下载版面模型和字体，可能需要几分钟）…")
        self.translator.command("check")

    def offer_engine_download(self):
        if engine_command() or (self._engine_installer and self._engine_installer.is_alive()):
            return
        answer = QMessageBox.question(self, "下载翻译资源包", "保留排版翻译需要 BabelDocWorker 资源包（约 175 MB）。\n"
            "现在从 GitHub 下载并自动解压配置吗？安装成功后不再弹出此提示。\n\n"
            "也可以稍后在“设置 → 保留排版翻译”点击“下载翻译资源包”。\n"
            "首次检查或使用引擎时，还会由 BabelDOC 下载版面模型和字体（约 300 MB）。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes)
        if answer == QMessageBox.StandardButton.Yes:
            self.install_engine()

    def install_engine(self):
        if self._engine_installer and self._engine_installer.is_alive():
            return
        if engine_command():
            self.translate_engine_label.setText("翻译引擎已安装，可以直接生成双语 PDF。")
            return
        self.download_engine_button.setEnabled(False)
        self.translate_engine_label.setText("正在准备下载翻译资源包…")

        def run():
            try:
                download_worker(self.config.home, lambda text: self.events.event.emit("engine_download", text),
                                self._engine_install_cancel)
                self.events.event.emit("engine_download_done", "翻译资源包已安装并自动配置，可以生成双语 PDF。")
            except Exception as error:
                self.events.event.emit("engine_download_done", f"资源包安装未完成：{error}\n"
                    f"可重试下载，或从 {RELEASE_URL} 下载 ZIP，将 BabelDocWorker 文件夹解压到 EXE 旁。")

        self._engine_installer = threading.Thread(target=run, name="translation-engine-installer", daemon=True)
        self._engine_installer.start()

    def generate_translations(self):
        rows = [r for r in self.checked_rows() if r.get("original_pdf") and not r.get("translations")
                and r.get("translation_status", "未翻译") in ("未翻译", "失败，待重试")]
        if not rows:
            QMessageBox.information(self, "生成双语 PDF", "请先在列表前面的方框中勾选有原文 PDF、还没有双语 PDF 的论文。")
            return
        if not self.save_settings():
            return
        provider, model, _ = translation_model(self.config.data)
        if provider != "ollama" and not self.config.llm_keys.get(provider):
            self.tabs.setCurrentWidget(self.settings_page)
            self.settings_message.setText(f"请先在“AI 论文总结”中选择 {PROVIDERS[provider][0]} 填写 API Key 并测试连接"
                                          "（翻译使用该服务商的 Key 和接口地址）。")
            return
        if not engine_command():
            QMessageBox.warning(self, "生成双语 PDF", NO_ENGINE)
            return
        if 0 < self.engine.bridge_info.get("protocol", 0) < BRIDGE_TRANSLATION_PROTOCOL:
            self.offer_plugin_update(self.engine.bridge_info.get("version") or "旧", "把双语 PDF 加入论文条目", "2.3",
                                     PLUGIN_FOR_TRANSLATION)
        pages = 0
        for row in rows:
            path = file_url_to_path(row.get("pdf_href", ""))
            pages += (page_count(path) or 0) if path and path.is_file() else 0
        answer = QMessageBox.question(self, "生成双语 PDF",
            f"将用 {PROVIDERS[provider][0]} · {model} 把 {len(rows)} 篇论文（约 {pages} 页）翻译为保留排版的双语对照 PDF"
            "（每页左边译文、右边原文），作为附件加入论文条目。\n\n"
            f"粗略估计每页约 {TOKENS_PER_PAGE} tokens，共约 {pages * TOKENS_PER_PAGE / 10000:.1f} 万 tokens，费用按服务商计费；"
            "版面识别和排版在本机完成，只有段落文字发给大模型。确定开始吗？")
        if answer == QMessageBox.StandardButton.Yes:
            self.translator.command("enqueue", [(r["key"], r["title"]) for r in rows])

    def _stash_llm(self):
        provider = self._llm_current
        if not provider:
            return
        base = self.llm_base.text().strip()
        self.config.data["llm_bases"][provider] = "" if base == PROVIDERS[provider][2] else base
        self.config.data["llm_models"][provider] = self.llm_model.currentText().strip()
        self.config.llm_keys[provider] = self.llm_key.text().strip()

    def _show_llm(self, provider):
        self._llm_current = provider
        label, _protocol, base, models = PROVIDERS[provider]
        self.llm_base.setText(self.config.data["llm_bases"].get(provider) or base)
        self.llm_model.clear()
        self.llm_model.addItems(models)
        self.llm_model.setCurrentText(self.config.data["llm_models"].get(provider) or (models[0] if models else ""))
        self.llm_key.setText(self.config.llm_keys.get(provider, ""))
        self.llm_key.setPlaceholderText("本机 Ollama 无需 Key" if provider == "ollama" else f"填写你自己的 {label} API Key")

    def _provider_changed(self, *_):
        self._stash_llm()
        self._show_llm(self.llm_provider.currentData())

    def test_llm(self):
        if self.save_settings():
            self.settings_message.setText("正在测试大模型连接…")
            self.summarizer.command("probe")

    def generate_summaries(self):
        busy = ("排队中", "等待中", "等待重试", "待更新插件", "添加到 Zotero")
        rows = [r for r in self.checked_rows()
                if r["markdowns"] and not r.get("summaries") and r.get("summary_status") not in busy]
        if not rows:
            QMessageBox.information(self, "生成 AI 总结", "请先在列表前面的方框中勾选已有 Markdown、还没有 AI 总结的论文。")
            return
        provider = self.llm_provider.currentData()
        if provider != "ollama" and not self.llm_key.text().strip():
            self.tabs.setCurrentWidget(self.settings_page)
            self.settings_message.setText("请先在“AI 论文总结”中填写大模型 API Key 并测试连接。")
            return
        deep = self.summary_depth.currentData() == "deep"
        answer = QMessageBox.question(self, "生成 AI 总结",
            f"将用 {PROVIDERS[provider][0]} · {self.llm_model.currentText()} {'精读' if deep else '通读'} {len(rows)} 篇论文的全文并生成总结卡片。\n\n"
            + ("精读模式每篇完整阅读全文两次（撰写 + 核对，输入约为全文的 2 倍 tokens），" if deep else "")
            + "费用按服务商计费。确定开始吗？")
        if answer == QMessageBox.StandardButton.Yes and self.save_settings():
            self.summarizer.command("enqueue", [(r["key"], r["title"]) for r in rows])

    def stop_summaries(self):
        """Confirm what to stop, then stop it at once (the summary thread may still be waiting for the model)."""
        jobs = self.store.summaries()
        reading = self.summarizer.reading if jobs.get(self.summarizer.reading, {}).get("stage") == "queued" else ""
        queued = sum(1 for key, job in jobs.items() if job["stage"] == "queued" and key != reading)
        if not reading and not queued:
            QMessageBox.information(self, "停止 AI 总结", "当前没有排队或正在阅读的 AI 总结。")
            return
        choice = self._ask_stop(jobs[reading]["title"] if reading else "", queued)
        if choice:
            count = self.summarizer.cancel(current=choice == "all")
            parts = [f"已取消排队中的 {count} 篇 AI 总结"] if count else []
            if reading:
                parts.append("正在阅读的一篇已停止" if choice == "all" else "正在阅读的一篇写完后结束")
            self.statusBar().showMessage("，".join(parts), 20000)

    def _ask_stop(self, title: str, queued: int) -> str:
        """'queue' (drop the queue), 'all' (also the paper being read) or '' (keep going)."""
        lines = ([f"正在阅读：《{title[:60]}》"] if title else []) + ([f"排队中：{queued} 篇"] if queued else [])
        notes = []
        if title and queued:
            notes += ["只取消排队：正在阅读的这篇会写完并加入 Zotero。",
                      "全部停止：这篇也立即停止，不再调用大模型（已发出的这一次请求无法收回，仍会计费）。"]
        elif title:
            notes.append("停止后这篇不再调用大模型（已发出的这一次请求无法收回，仍会计费）。")
        notes.append("已完成的总结不受影响；之后勾选论文、点“生成 AI 总结”即可重新排队。")
        box = QMessageBox(QMessageBox.Icon.Question, "停止 AI 总结", "\n".join(lines), parent=self)
        box.setInformativeText("\n".join(notes))
        choices = []
        if queued:
            choices.append((box.addButton(f"只取消排队（{queued} 篇）" if title else f"取消排队（{queued} 篇）",
                                          QMessageBox.ButtonRole.AcceptRole), "queue"))
        if title:
            choices.append((box.addButton("全部停止" if queued else "停止这一篇", QMessageBox.ButtonRole.DestructiveRole), "all"))
        keep = box.addButton("不停止", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(keep)
        box.setEscapeButton(keep)
        box.exec()
        return next((value for button, value in choices if box.clickedButton() == button), "")

    def _link(self, title, url):
        label = QLabel(f'<a href="{url}">{title}</a>')
        label.setOpenExternalLinks(True)
        return label

    def _load_settings(self):
        self._tree_populating = True
        d = self.config.data
        self.token.setText(self.config.token)
        self.output.setText(d["output_root"])
        self.include_sub.setChecked(d["include_subcollections"])
        self.model.setCurrentIndex(max(0, self.model.findData(d["model"])))
        self.language.setCurrentIndex(max(0, self.language.findData(d["language"])))
        self.ocr.setChecked(d["ocr"])
        self.formula.setChecked(d["formula"])
        self.tables_flag.setChecked(d["table"])
        self.storage_mode.setCurrentIndex(max(0, self.storage_mode.findData(d["storage_mode"])))
        self.export_copy.setChecked(d["export_copy"])
        self.interval.setValue(d["interval"])
        self.port.setValue(d["zotero_port"])
        self.resume.setChecked(d["resume_switch"])
        self.llm_provider.setCurrentIndex(max(0, self.llm_provider.findData(d["llm_provider"])))
        self._show_llm(self.llm_provider.currentData())
        self.summary_depth.setCurrentIndex(max(0, self.summary_depth.findData(d["summary_depth"])))
        self.summary_language.setCurrentIndex(max(0, self.summary_language.findData(d["summary_language"])))
        self.part_tokens.setValue(d["summary_part_tokens"])
        self.max_words.setValue(d["summary_max_words"])
        self.budget.setValue(d["summary_budget"])
        self.max_output.setValue(d["summary_max_output"])
        self.summary_auto.setChecked(d["summary_auto"])
        self.summary_note.setChecked(d["summary_note"])
        self.summary_vision.setChecked(d["summary_vision"])
        self.vision_max.setValue(d["summary_vision_max"])
        self.vision_max.setEnabled(d["summary_vision"])
        self.translate_provider.setCurrentIndex(max(0, self.translate_provider.findData(d["translate_provider"])))
        self._translate_provider_changed()
        self.translate_model.setCurrentText(d["translate_model"])
        self.translate_qps.setValue(d["translate_qps"])
        self.translate_font.setCurrentIndex(max(0, self.translate_font.findData(d["translate_font"])))
        self.translate_refs.setCurrentIndex(1 if d["translate_keep_refs"] else 0)
        self.translate_terms.setChecked(d["translate_auto_terms"])
        self.translate_mono.setChecked(d["translate_mono"])
        self.research_provider.setCurrentIndex(max(0, self.research_provider.findData(d["research_provider"])))
        self._research_provider_changed()
        self.research_model.setCurrentText(d["research_model"])
        self.research_interval.setValue(d["research_interval_days"])
        self.research_reread.setValue(d["research_reread"])
        self.research_html.setChecked(d["research_html"])
        self.tracking_enabled.setChecked(d["tracking_enabled"])
        self.tracking_interval.setValue(d["tracking_interval_days"])
        self.tracking_window.setValue(d["tracking_window_days"])
        self.tracking_budget.setValue(d["tracking_budget"])
        self.tracking_score.setValue(d["tracking_min_score"])
        self.s2_key.setText(self.config.scholar_keys.get("s2", ""))
        self.openalex_key.setText(self.config.scholar_keys.get("openalex", ""))
        self._show_research_switch()
        self._storage_mode_changed()
        self._tree_populating = False

    def _storage_mode_changed(self, *_):
        self.export_copy.setEnabled(self.storage_mode.currentData() == "import")

    # ------------------------------------------------------------------ actions
    def selected_collections(self):
        selected = []

        def walk(item):
            if item.checkState(0) == Qt.CheckState.Checked:
                selected.append(item.data(0, Qt.ItemDataRole.UserRole))
            for n in range(item.childCount()):
                walk(item.child(n))
        for n in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(n))
        return selected

    def save_settings(self, *, feedback=False):
        try:
            output = Path(self.output.text().strip()).expanduser()
            if not output.is_absolute():
                raise ValueError("请选择绝对路径作为输出目录。")
            token = self.token.text().strip().removeprefix("Bearer ").strip()
            if any(ch.isspace() for ch in token):
                raise ValueError("Token 中不能包含空格或换行。")
            self.config.token = token
            self._stash_llm()
            if any(ch.isspace() for ch in self.llm_key.text().strip()):
                raise ValueError("大模型 API Key 中不能包含空格或换行。")
            self.config.data.update({
                "llm_provider": self.llm_provider.currentData(), "summary_depth": self.summary_depth.currentData(),
                "summary_language": self.summary_language.currentData(), "summary_part_tokens": self.part_tokens.value(),
                "summary_budget": self.budget.value(), "summary_max_output": self.max_output.value(),
                "summary_max_words": self.max_words.value(),
                "summary_auto": self.summary_auto.isChecked(), "summary_note": self.summary_note.isChecked(),
                "summary_vision": self.summary_vision.isChecked(), "summary_vision_max": self.vision_max.value()})
            self.config.data.update({
                "translate_provider": self.translate_provider.currentData(),
                "translate_model": self.translate_model.currentText().strip() if self.translate_provider.currentData() else "",
                "translate_qps": self.translate_qps.value(), "translate_font": self.translate_font.currentData(),
                "translate_keep_refs": self.translate_refs.currentData() == "keep",
                "translate_auto_terms": self.translate_terms.isChecked(), "translate_mono": self.translate_mono.isChecked()})
            self.config.data.update({
                "output_root": str(output.resolve()),
                "collections": self.selected_collections() if self.tree.topLevelItemCount() else self.config.data["collections"],
                "include_subcollections": self.include_sub.isChecked(),
                "model": self.model.currentData(), "language": self.language.currentData(),
                "ocr": self.ocr.isChecked(), "formula": self.formula.isChecked(), "table": self.tables_flag.isChecked(),
                "storage_mode": self.storage_mode.currentData(), "export_copy": self.export_copy.isChecked(),
                "interval": self.interval.value(), "zotero_port": self.port.value(), "resume_switch": self.resume.isChecked()})
            for value in (self.s2_key.text().strip(), self.openalex_key.text().strip()):
                if any(ch.isspace() for ch in value):
                    raise ValueError("文献数据库 API Key 中不能包含空格或换行。")
            self.config.scholar_keys.update(s2=self.s2_key.text().strip(), openalex=self.openalex_key.text().strip())
            self.config.data.update({
                "research_roots": self.research_roots() if self.research_tree.topLevelItemCount() else self.config.data["research_roots"],
                "research_provider": self.research_provider.currentData(),
                "research_model": self.research_model.currentText().strip() if self.research_provider.currentData() else "",
                "research_interval_days": self.research_interval.value(), "research_reread": self.research_reread.value(),
                "research_html": self.research_html.isChecked(), "tracking_enabled": self.tracking_enabled.isChecked(),
                "tracking_interval_days": self.tracking_interval.value(), "tracking_window_days": self.tracking_window.value(),
                "tracking_budget": self.tracking_budget.value(), "tracking_min_score": self.tracking_score.value()})
            self.config.save()
            if hasattr(self, "engine"):
                self.engine.command("configure", (self.config.data, self.config.token))
            if hasattr(self, "summarizer"):
                self.summarizer.command("configure", (self.config.data, self.config.llm_keys))
            if hasattr(self, "researcher"):
                self.researcher.command("configure", (self.config.data, self.config.llm_keys, self.config.scholar_keys))
            if hasattr(self, "translator"):
                self.translator.command("configure", (self.config.data, self.config.llm_keys))
            if feedback:
                self.settings_message.setText("设置已保存；MinerU Token 和大模型 API Key 已加密保存。")
            return True
        except (ValueError, OSError, RuntimeError) as error:
            self.settings_message.setText(str(error))
            self.statusBar().showMessage(str(error), 15000)
            return False

    def selection_changed(self, *_):
        if not self._tree_populating and hasattr(self, "engine"):
            self.summary.setText("范围已调整，正在重新读取…")
            self._prompt_pending = True
            self.selection_timer.start()

    def tree_changed(self, *_):
        self.selection_changed()

    def scan(self, *_, prompt=False):
        if prompt:
            self._prompt_pending = True
        if self.save_settings():
            self.engine.command("scan")

    def offer_conversion(self):
        """After the user returns to a collection, ask whether to convert papers that still have no Markdown."""
        if self._watching or self.smoke or not self.config.token:
            return
        papers = [r for r in self._rows if r["pdf_key"] and not r["markdowns"] and r["status"] in ("未勾选", "已勾选")]
        if not papers:
            return
        names = "\n".join("· " + r["title"][:60] for r in papers[:8]) + (f"\n…… 等共 {len(papers)} 篇" if len(papers) > 8 else "")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("发现新增或未转换的论文 PDF")
        box.setText(f"当前分类中有 {len(papers)} 篇论文的 PDF 还没有转换为 Markdown：")
        box.setInformativeText(names + "\n\n是否勾选这些论文并开启转换？全部转换完成后开关会自动关闭。")
        confirm = box.addButton("确认开启转换（推荐）", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("暂不转换", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(confirm)
        box.exec()
        if box.clickedButton() is confirm and self.save_settings():
            keys = [r["key"] for r in papers]
            for row in papers:
                row["selected"] = True
            self.engine.command("choice", (keys, True))
            self.engine.command("start")

    def toggle_switch(self):
        if self._watching:
            self.engine.command("pause")
            self.statusBar().showMessage("正在关闭转换开关，当前网络请求结束后生效。", 20000)
            return
        if not self.token.text().strip():
            self.tabs.setCurrentWidget(self.settings_page)
            self.settings_message.setText("请先填写 MinerU API Token 并保存。")
            return
        busy = ("排队中", "上传 PDF", "云端解析", "下载结果", "生成 Markdown", "添加到 Zotero")
        pending = [r for r in self.checked_rows() if not r["markdowns"] and r["status"] not in busy]
        if not pending and not self.smoke:
            QMessageBox.information(self, "开始转换", "请先在右侧列表前面的方框中勾选要转换的论文。")
            return
        if pending and not self.smoke:
            answer = QMessageBox.question(self, "开始转换",
                f"将把勾选的 {len(pending)} 篇论文上传到 MinerU 解析（会消耗解析额度，每日 1000 页高优先级额度），"
                f"全部完成后开关自动关闭。\n\n确定开始吗？")
            if answer != QMessageBox.StandardButton.Yes:
                return
        if self.save_settings():
            self.engine.command("start")

    def probe(self):
        if self.save_settings():
            self.settings_message.setText("正在检查 MinerU 接口…")
            self.engine.command("probe")

    def _paper_items(self) -> list[QTreeWidgetItem]:
        return [self.table.topLevelItem(n) for n in range(self.table.topLevelItemCount())]

    def _current(self) -> tuple[dict | None, dict | None]:
        """(paper, child) of the selected line; child is None when a paper itself is selected."""
        items = self.table.selectedItems()
        if not items:
            return None, None
        item = items[0]
        top = item.parent() or item
        key = top.data(0, PAPER_ROLE)
        return next((r for r in self._rows if r["key"] == key), None), item.data(0, CHILD_ROLE)

    def selected_rows(self) -> list[dict]:
        paper, _ = self._current()
        return [paper] if paper else []

    @staticmethod
    def _child_path(child: dict) -> Path | None:
        path = file_url_to_path(child.get("href", "")) or (Path(child["path"]) if child.get("path") else None)
        return path if path and path.is_file() else None

    def open_child(self, child: dict):
        path = self._child_path(child) if child["kind"] != "note" else None
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        else:  # notes and missing files: show them in Zotero
            QDesktopServices.openUrl(QUrl("zotero://select/library/items/" + child["key"]))

    def item_double_clicked(self, item, _column):
        child = item.data(0, CHILD_ROLE)
        if child:
            self.open_child(child)
        else:
            self.open_markdown()

    def item_menu(self, position):
        item = self.table.itemAt(position)
        if item is None:
            return
        self.table.setCurrentItem(item)
        child = item.data(0, CHILD_ROLE)
        menu = QMenu(self)
        if child:
            menu.addAction("打开", lambda: self.open_child(child))
            menu.addAction("在 Zotero 中显示", lambda: QDesktopServices.openUrl(
                QUrl("zotero://select/library/items/" + child["key"])))
            menu.addSeparator()
            menu.addAction("删除（移入 Zotero 回收站）…", self.delete_selected)
        else:
            menu.addAction("收起" if item.isExpanded() else "展开 PDF / Markdown / 总结 / 笔记",
                           lambda: item.setExpanded(not item.isExpanded()))
            menu.addAction("打开 Markdown", self.open_markdown)
            menu.addAction("在 Zotero 中显示", self.show_in_zotero)
        menu.exec(self.table.viewport().mapToGlobal(position))

    def delete_selected(self):
        paper, child = self._current()
        if not child:
            QMessageBox.information(self, "删除", "请先点论文左侧的箭头展开，选中其中要删除的 PDF、Markdown、AI 总结卡片或笔记。")
            return
        targets = [child]
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("移入 Zotero 回收站")
        name = child.get("filename") or child.get("title") or child["key"]
        box.setText(f"把《{paper['title'][:60]}》下的“{name}”移入 Zotero 回收站吗？")
        notes = []
        if child["kind"] == "summary":
            notes = [c for c in paper.get("children", []) if c["kind"] == "note"
                     and c.get("title", "").startswith(("AI 论文总结", "AI paper card"))]
        tips = {"markdown": "删除后可以重新转换这篇论文的 PDF。", "summary": "删除后可以重新生成 AI 总结。",
                "translation": "删除后可以重新生成双语 PDF。",
                "pdf": "注意：删除 PDF 后，本软件无法再转换这篇论文（除非重新添加 PDF）。"}
        box.setInformativeText(tips.get(child["kind"], "") + "\n可以在 Zotero 的回收站中恢复。")
        also = None
        if notes:
            also = QCheckBox("同时删除对应的 AI 总结笔记")
            also.setChecked(True)
            box.setCheckBox(also)
        confirm = box.addButton("移入回收站", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is not confirm:
            return
        if also is not None and also.isChecked():
            targets += notes
        self.engine.command("trash", (paper["key"], [
            {"key": c["key"], "kind": c["kind"], "name": c.get("filename") or c.get("title") or c["key"]} for c in targets]))

    def after_trash(self, data):
        paper = next((r for r in self._rows if r["key"] == data["paper"]), None)
        if data.get("error"):
            QMessageBox.warning(self, "删除失败", data["error"])
            return
        if data.get("outdated"):
            self.offer_plugin_update(data["outdated"], "删除附件")
            return
        kinds = set(data.get("kinds", []))
        if not paper or not kinds & {"markdown", "summary"}:
            return
        if "markdown" in kinds:
            if QMessageBox.question(self, "重新转换", f"已删除《{paper['title'][:60]}》的 Markdown。\n\n"
                                                  "现在重新转换这篇论文的 PDF 吗？（会消耗 MinerU 额度）") \
                    == QMessageBox.StandardButton.Yes:
                if self.save_settings():
                    self.engine.command("choice", ([paper["key"]], True))
                    self.engine.command("start")
            return
        if QMessageBox.question(self, "重新生成 AI 总结", f"已删除《{paper['title'][:60]}》的 AI 总结卡片。\n\n"
                                              "现在重新生成 AI 总结吗？") == QMessageBox.StandardButton.Yes:
            if self.save_settings():
                self.summarizer.command("enqueue", [(paper["key"], paper["title"])])

    def choose_output(self):
        directory = QFileDialog.getExistingDirectory(self, "选择 Markdown 输出目录", self.output.text())
        if directory:
            self.output.setText(directory)

    def open_output(self):
        path = Path(self.output.text())
        try:
            path.mkdir(parents=True, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))
        except OSError as error:
            self.statusBar().showMessage(str(error), 10000)

    def _markdown_path(self, row: dict) -> Path | None:
        candidates = []
        if row.get("markdown"):
            candidates.append(Path(row["markdown"]))
        for md in row.get("markdowns", []):
            candidates.append(file_url_to_path(md.get("href", "")) or (Path(md["path"]) if md.get("path") else None))
        return next((p for p in candidates if p and p.is_file()), None)

    def open_markdown(self):
        paper, child = self._current()
        if child:
            self.open_child(child)
            return
        path = self._markdown_path(paper) if paper else None
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        else:
            self.statusBar().showMessage("选中的论文还没有可打开的 Markdown。", 8000)

    def show_in_zotero(self):
        paper, child = self._current()
        if paper:
            QDesktopServices.openUrl(QUrl("zotero://select/library/items/" + (child or paper)["key"]))

    def offer_plugin_update(self, version, purpose="", needed="2.2", detail=""):
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("需要更新 Zotero 插件")
        box.setText(f"Zotero 中安装的是 {version} 版桥接插件，{purpose or '本软件'}需要 {needed} 版。")
        box.setInformativeText((detail or ("更新后即可在本软件中删除论文下的附件和笔记（同步移入 Zotero 回收站）。" if purpose else
                                           "插件过旧时无法添加 Markdown 和 AI 总结卡片（已生成的总结会保留，更新插件后自动补加，"
                                           "不会再次调用大模型）。")) + "\n\n现在更新吗？")
        update = box.addButton("立即安装/更新插件（推荐）", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("稍后", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(update)
        box.exec()
        if box.clickedButton() is update:
            self.install_bridge()

    def install_bridge(self):
        if not self.save_settings():
            return
        base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
        source = base / "resources" / "mineru-zotero-bridge.xpi"
        if not source.is_file():
            QMessageBox.information(self, "插件尚未打包", "请先运行 build.py 生成桥接插件。")
            return
        target = self.config.home / "install" / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        QApplication.clipboard().setText(str(target))
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target.parent)))
        QMessageBox.information(self, "安装/更新 Zotero 桥接插件",
            "插件所在文件夹已打开，XPI 完整路径已复制到剪贴板。\n\n"
            "1. Zotero → 工具 → 插件。\n"
            "2. 点击右上角齿轮 → “从文件安装插件…”。\n"
            "3. 在文件名框中粘贴路径（Ctrl+V），选择 mineru-zotero-bridge.xpi 并安装。\n"
            "4. 返回本软件点击“刷新 Zotero 连接”，右上角显示“插件已连接”即可。\n\n"
            "升级本软件后需要重新安装一次插件（会自动覆盖旧版本）。\n\n" + str(target))

    # ------------------------------------------------------------------ research reviews and tracking
    def research_roots(self) -> list[str]:
        roots = []

        def walk(item, inherited):
            checked = item.checkState(0) == Qt.CheckState.Checked
            if checked and not inherited:
                roots.append(item.data(0, Qt.ItemDataRole.UserRole))
            for n in range(item.childCount()):
                walk(item.child(n), inherited or checked)
        for n in range(self.research_tree.topLevelItemCount()):
            walk(self.research_tree.topLevelItem(n), False)
        return roots

    def _research_ready(self) -> bool:
        if not self.research_roots():
            QMessageBox.information(self, "研究综述", "请先在左侧勾选要综述的分类（会包含它的全部子分类）。")
            return False
        provider = self.research_provider.currentData() or self.llm_provider.currentData()
        self._stash_llm()
        if provider != "ollama" and not self.config.llm_keys.get(provider):
            QMessageBox.information(self, "研究综述", f"请先在“设置 → AI 论文总结”中选择 {PROVIDERS[provider][0]}，填写 API Key 并保存。")
            self.tabs.setCurrentWidget(self.settings_page)
            return False
        return self.save_settings()

    def _show_research_switch(self):
        on = self.config.data["research_enabled"]
        self.research_switch.setText("■ 关闭研究综述与追踪" if on else "▶ 开启研究综述与追踪")
        self.research_switch.setObjectName("danger" if on else "primary")
        self.research_switch.style().unpolish(self.research_switch)
        self.research_switch.style().polish(self.research_switch)
        self._show_research_state()

    def _show_research_state(self):
        state = self._research
        on = self.config.data["research_enabled"]
        when = lambda stamp: datetime.fromtimestamp(stamp).strftime("%m-%d %H:%M") if stamp else "—"  # noqa: E731
        text = ("● 已开启" if on else "已关闭") + f" · 上次综述 {when(state.get('review_last'))} · 上次追踪 {when(state.get('track_last'))}"
        if on and state.get("review_last"):
            text += " · 下次检查 " + when(state["review_last"] + self.config.data["research_interval_days"] * 86400)
        if state.get("running"):
            text = ("● 正在综述…" if state["running"] == "review" else "● 正在追踪新文献…" if state["running"] == "track"
                    else "● 正在估算…") + "  " + text
        self.research_state.setText(text)

    def toggle_research(self):
        if self.config.data["research_enabled"]:
            self.config.data["research_enabled"] = False
            self.researcher.cancel()
            self.save_settings()
            self._show_research_switch()
            self.research_progress.setText("研究综述与追踪已关闭；已生成的文件保留。")
            return
        if self._research_ready():
            self.request_estimate("enable")

    def request_estimate(self, purpose: str):
        if purpose == "review" and not self._research_ready():
            return
        self._estimate_purpose = purpose
        self.research_progress.setText("正在读取分类和论文，估算需要综述的分类与 tokens…")
        self.researcher.command("estimate")

    def after_estimate(self, plan):
        purpose, self._estimate_purpose = self._estimate_purpose, ""
        if not purpose:
            return
        if plan is None:
            self.research_progress.setText("无法读取 Zotero 中的分类，请确认 Zotero 已打开（详见运行日志）。")
            return
        provider, model, _base = research_model(self.config.data)
        changed = plan["collections"]
        tokens = plan["input"]
        detail = (f"范围内共 {plan['scope']} 个分类；其中 {len(changed)} 个需要（重新）综述，预计调用大模型约 {plan['calls']} 次、"
                  f"输入约 {tokens / 10000:.1f} 万 tokens（另有输出），费用按 {PROVIDERS[provider][0]} 计费。"
                  if changed else f"范围内共 {plan['scope']} 个分类，论文和总结卡片都没有变化，不需要重新综述。")
        if purpose == "enable":
            schedule = (f"之后每 {self.config.data['research_interval_days']} 天自动检查一次（只重算有变化的分类）"
                        + (f"，每 {self.config.data['tracking_interval_days']} 天追踪一次新文献" if self.config.data["tracking_enabled"] else "")
                        + "；软件需在托盘中运行。")
            if not self.smoke and QMessageBox.question(self, "开启研究综述与追踪",
                    f"将用 {PROVIDERS[provider][0]} · {model or '默认模型'} 为勾选的分类生成研究综述。\n\n{detail}\n\n{schedule}\n\n确定开启吗？") \
                    != QMessageBox.StandardButton.Yes:
                self.research_progress.setText("未开启。")
                return
            self.config.data["research_enabled"] = True
            self.save_settings()
            self._show_research_switch()
            self.researcher.command("both")
        elif changed:
            if not self.smoke and QMessageBox.question(self, "更新研究综述", detail + "\n\n确定开始吗？") != QMessageBox.StandardButton.Yes:
                self.research_progress.setText("未更新。")
                return
            self.researcher.command("review")
        else:
            self.research_progress.setText(detail)

    def track_now(self):
        if not self._research_ready():
            return
        if not self.smoke and QMessageBox.question(self, "追踪新文献",
                "将按勾选分类中的论文检索最近发表的相关论文（Semantic Scholar、OpenAlex，免费），"
                "并由大模型阅读候选论文的标题和摘要评分（每个分类约调用 1–3 次）。\n\n确定开始吗？") != QMessageBox.StandardButton.Yes:
            return
        self.researcher.command("track")

    def research_root_path(self) -> Path:
        return Path(self.config.data["output_root"]) / RESEARCH_DIR

    def open_research_file(self, name: str):
        target = self.research_root_path() / name if name else self.research_root_path()
        if not target.exists():
            self.research_progress.setText("还没有生成研究综述。" if name else "研究地图文件夹尚未创建，生成综述后会自动创建。")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def open_research_row(self, item, kind: str):
        row = next((r for r in self._research["rows"] if r["key"] == item.data(0, Qt.ItemDataRole.UserRole)), None)
        path = Path((row or {}).get("files", {}).get(kind, "") or "")
        if path.name and path.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        else:
            self.research_progress.setText({"review": "这个分类还没有研究综述。", "map": "这个分类还没有研究地图网页。",
                                            "tracking": "这个分类还没有新文献追踪报告。"}[kind])

    def research_menu(self, position):
        item = self.research_table.itemAt(position)
        if item is None:
            return
        key = item.data(0, Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        menu.addAction("打开研究综述", lambda: self.open_research_row(item, "review"))
        menu.addAction("打开研究地图（网页）", lambda: self.open_research_row(item, "map"))
        menu.addAction("打开新文献追踪", lambda: self.open_research_row(item, "tracking"))
        menu.addAction("在 Zotero 中显示分类", lambda: QDesktopServices.openUrl(QUrl("zotero://select/library/collections/" + key)))
        menu.addSeparator()
        menu.addAction("重新生成这个分类的综述（及其上级）", lambda: self.regenerate(key))
        menu.exec(self.research_table.viewport().mapToGlobal(position))

    def regenerate(self, key: str):
        if self._research_ready():
            self.researcher.command("review", [key])

    def research_roots_changed(self, *_):
        if not self._tree_populating:
            self.config.data["research_roots"] = self.research_roots()
            self.refresh_research_table()

    def populate_research_tree(self, collections):
        signature = tuple((c["key"], c["data"]["name"], c["data"].get("parentCollection")) for c in collections)
        if signature == self._research_signature:
            return
        self._research_signature = signature
        roots = set(self.config.data["research_roots"])
        self._tree_populating = True
        self.research_tree.clear()
        nodes = {}
        for collection in sorted(collections, key=lambda c: c["data"]["name"].casefold()):
            count = collection.get("meta", {}).get("numItems")
            item = QTreeWidgetItem([collection["data"]["name"] + (f"  ({count})" if count else "")])
            item.setData(0, Qt.ItemDataRole.UserRole, collection["key"])
            item.setCheckState(0, Qt.CheckState.Checked if collection["key"] in roots else Qt.CheckState.Unchecked)
            nodes[collection["key"]] = item
        for collection in sorted(collections, key=lambda c: c["data"]["name"].casefold()):
            parent = nodes.get(collection["data"].get("parentCollection"))
            (parent.addChild if parent else self.research_tree.addTopLevelItem)(nodes[collection["key"]])
        for key in roots:
            node = nodes.get(key)
            while node is not None:
                node.setExpanded(True)
                node = node.parent()
        self._tree_populating = False
        self.refresh_research_table()

    def refresh_research_table(self):
        records = {r["key"]: r for r in self._research["rows"]}
        roots = set(self.config.data["research_roots"])
        by_parent: dict[str, list[dict]] = {}
        for c in sorted(self._collections, key=lambda c: c["data"]["name"].casefold()):
            by_parent.setdefault(c["data"].get("parentCollection") or "", []).append(c)
        self.research_table.setUpdatesEnabled(False)
        self.research_table.clear()

        def add(collection, parent):
            record = records.get(collection["key"], {})
            status = {"done": "已生成", "failed": "失败", "running": "生成中"}.get(record.get("status", ""), "未生成")
            coverage = record.get("coverage")
            values = [collection["data"]["name"], str(record.get("papers_total") or collection.get("meta", {}).get("numItems") or ""),
                      f"{coverage['card']} / {coverage['abstract']} / {coverage['meta']}" if coverage else "",
                      status, datetime.fromtimestamp(record["generated"]).strftime("%Y-%m-%d %H:%M") if record.get("generated") else "",
                      (f"{record['new_papers']} 篇（{record['tracked'][5:]}）" if record.get("tracked") else ""),
                      record.get("error") or record.get("tracking_error") or ""]
            item = QTreeWidgetItem(parent, values)
            item.setData(0, Qt.ItemDataRole.UserRole, collection["key"])
            for col, value in enumerate(values):
                item.setToolTip(col, value)
            item.setForeground(3, QColor("#15803d" if status == "已生成" else "#b91c1c" if status == "失败"
                                         else "#1d4ed8" if status == "生成中" else "#94a3b8"))
            item.setExpanded(True)
            for child in by_parent.get(collection["key"], []):
                add(child, item)
        for collection in self._collections:
            if collection["key"] in roots:
                add(collection, self.research_table)
        self.research_table.setUpdatesEnabled(True)

    # ------------------------------------------------------------------ tree and table
    def populate_tree(self, collections):
        signature = tuple((c["key"], c["data"]["name"], c["data"].get("parentCollection")) for c in collections)
        if signature == self._collection_signature:
            return
        self._collection_signature = signature
        selected = set(self.selected_collections() if self.tree.topLevelItemCount() else self.config.data["collections"])
        self._tree_populating = True
        self.tree.clear()
        root = QTreeWidgetItem(self.tree, ["我的文库（全部条目）"])
        root.setData(0, Qt.ItemDataRole.UserRole, "__all__")
        root.setCheckState(0, Qt.CheckState.Checked if "__all__" in selected else Qt.CheckState.Unchecked)
        nodes = {}
        for collection in sorted(collections, key=lambda c: c["data"]["name"].casefold()):
            count = collection.get("meta", {}).get("numItems")
            label = collection["data"]["name"] + (f"  ({count})" if count else "")
            item = QTreeWidgetItem([label])
            item.setData(0, Qt.ItemDataRole.UserRole, collection["key"])
            item.setCheckState(0, Qt.CheckState.Checked if collection["key"] in selected else Qt.CheckState.Unchecked)
            nodes[collection["key"]] = item
        for collection in sorted(collections, key=lambda c: c["data"]["name"].casefold()):
            parent = nodes.get(collection["data"].get("parentCollection"), root)
            parent.addChild(nodes[collection["key"]])
        root.setExpanded(True)
        for key in selected:
            node = nodes.get(key)
            while node is not None:
                node.setExpanded(True)
                node = node.parent()
        self._tree_populating = False
        self.filter_tree(self.filter.text())

    def filter_tree(self, text):
        def visit(item):
            own = text.casefold() in item.text(0).casefold()
            child_matches = [visit(item.child(n)) for n in range(item.childCount())]
            visible = own or any(child_matches)
            item.setHidden(not visible)
            if text and any(child_matches):
                item.setExpanded(True)
            return visible
        for n in range(self.tree.topLevelItemCount()):
            visit(self.tree.topLevelItem(n))

    @staticmethod
    def _checkable(paper: dict) -> bool:
        return bool(Window._actions(paper))

    @staticmethod
    def _actions(paper: dict) -> list[str]:
        """What ticking this paper can lead to: convert, summarise or translate."""
        actions = []
        if not paper["markdowns"] and paper["pdf_key"]:
            actions.append("打开“开始转换”开关时转换")
        if paper["markdowns"] and not paper.get("summaries"):
            actions.append("点“生成 AI 总结”总结")
        if paper.get("original_pdf") and not paper.get("translations"):
            actions.append("点“生成双语 PDF”翻译")
        return actions

    def checked_rows(self) -> list[dict]:
        return [r for r in self._rows if self._checkable(r) and r["selected"]]

    def _show_summary_progress(self, item, paper: dict):
        """The paper the model is reading shows “阅读中 xx%” over a bar; every other paper its plain status."""
        status = paper.get("summary_status", "")
        reading = hasattr(self, "summarizer") and paper["key"] == self.summarizer.reading \
            and status in ("排队中", "等待重试")
        percent = self.summarizer.reading_percent() if reading else None
        item.setText(SUMMARY_COLUMN, f"阅读中 {percent}%" if percent is not None else status)
        item.setData(SUMMARY_COLUMN, PROGRESS_ROLE, percent)

    def refresh_progress(self):
        if not hasattr(self, "summarizer"):
            return
        for item, paper in zip(self._paper_items(), self._rows):
            if paper["key"] == self.summarizer.reading or item.data(SUMMARY_COLUMN, PROGRESS_ROLE) is not None:
                self._show_summary_progress(item, paper)

    def paper_changed(self, item, column):
        if not self._rows_populating and column == 0 and item.parent() is None:
            checked = item.checkState(0) == Qt.CheckState.Checked
            key = item.data(0, PAPER_ROLE)
            for paper in self._rows:
                if paper["key"] == key:
                    paper["selected"] = checked
            self.engine.command("choice", ([key], checked))

    def header_clicked(self, section):
        if section != 0:
            return
        rows = [(item, r) for item, r in zip(self._paper_items(), self._rows) if self._checkable(r) and not item.isHidden()]
        if not rows:
            return
        checked = not all(r["selected"] for _, r in rows)
        self._rows_populating = True
        for item, paper in rows:
            paper["selected"] = checked
            item.setCheckState(0, Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self._rows_populating = False
        self.engine.command("choice", ([r["key"] for _, r in rows], checked))

    def populate_rows(self, data):
        current = self.table.currentItem()
        keep = None
        if current is not None:
            child = current.data(0, CHILD_ROLE)
            keep = ("child", child["key"]) if child else ("paper", current.data(0, PAPER_ROLE))
        self._rows = data["rows"]
        self._rows_populating = True
        self.table.setUpdatesEnabled(False)
        self.table.clear()
        restore = None
        for paper in self._rows:
            top = QTreeWidgetItem(self.table)
            top.setData(0, PAPER_ROLE, paper["key"])
            if self._checkable(paper):
                top.setFlags(top.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                top.setCheckState(0, Qt.CheckState.Checked if paper["selected"] else Qt.CheckState.Unchecked)
                top.setToolTip(0, "勾选后可以：" + "；".join(self._actions(paper)))
            else:
                top.setFlags(top.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                top.setToolTip(0, "已有 Markdown、AI 总结和双语 PDF，无需处理" if paper["markdowns"] else "没有 PDF，无法转换")
            values = (paper["title"], paper["creator"], paper["collections"], paper["pdf_name"] or "—",
                      paper.get("md_display", ""), paper.get("summary_status", ""), paper.get("translation_status", ""),
                      paper["status"], paper["detail"])
            for col, value in enumerate(values, 1):
                top.setText(col, str(value))
                top.setToolTip(col, str(value))
            if not paper["markdowns"] and paper["status"] != "已完成":
                top.setForeground(5, QColor("#94a3b8"))
            summary = paper.get("summary_status", "")
            top.setToolTip(SUMMARY_COLUMN, paper.get("summary_detail") or summary)
            top.setForeground(SUMMARY_COLUMN, QColor(
                "#15803d" if summary in ("已完成", "已有总结") else "#b91c1c" if "失败" in summary
                else "#b45309" if summary in ("等待中", "等待重试", "待更新插件")
                else "#1d4ed8" if summary in ("排队中", "添加到 Zotero") else "#94a3b8"))
            self._show_summary_progress(top, paper)
            translation = paper.get("translation_status", "")
            top.setToolTip(TRANSLATION_COLUMN, paper.get("translation_detail") or translation)
            top.setForeground(TRANSLATION_COLUMN, QColor(
                "#b45309" if "需检查" in translation else "#15803d" if translation == "已有双语"
                else "#b91c1c" if "失败" in translation
                else "#b45309" if translation in ("等待中", "待更新插件")
                else "#1d4ed8" if translation.startswith(("翻译中", "排队中", "添加到 Zotero"))
                else "#94a3b8"))
            top.setData(TRANSLATION_COLUMN, PROGRESS_ROLE, translation_percent(translation))
            status = paper["status"]
            if status.startswith("已完成") or status == "已有 Markdown":
                top.setForeground(8, QColor("#15803d"))
            elif "失败" in status or "不可用" in status:
                top.setForeground(8, QColor("#b91c1c"))
            elif status in ("上传 PDF", "云端解析", "下载结果", "生成 Markdown", "添加到 Zotero", "排队中"):
                top.setForeground(8, QColor("#1d4ed8"))
            for child in paper.get("children", []):
                icon, label = CHILD_KINDS.get(child["kind"], ("📎", "附件"))
                line = QTreeWidgetItem(top)
                line.setFlags(line.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                line.setData(0, CHILD_ROLE, child)
                name = child.get("filename") or child.get("title") or child["key"]
                line.setText(1, f"{icon} {name}")
                line.setToolTip(1, name)
                line.setText(2, label)
                line.setForeground(2, QColor("#64748b"))
                where = "笔记（双击在 Zotero 中打开）" if child["kind"] == "note" else (
                    str(self._child_path(child) or "") or "文件不在本机")
                line.setText(9, where)
                line.setToolTip(9, where)
                if keep == ("child", child["key"]):
                    restore = line
            if paper["key"] in self._expanded:
                top.setExpanded(True)
            if keep == ("paper", paper["key"]):
                restore = top
        if restore is not None:
            self.table.setCurrentItem(restore)
        self.table.setUpdatesEnabled(True)
        self._rows_populating = False
        self.apply_row_filter()
        statuses = [r["status"] for r in self._rows]
        done = sum(s.startswith("已完成") for s in statuses)
        has_md = sum(s == "已有 Markdown" for s in statuses)
        waiting = sum(s in STATUS_FILTERS[1][1] for s in statuses)
        checked = len(self.checked_rows())
        failed = sum("失败" in s for s in statuses)
        bilingual = sum(1 for r in self._rows if r.get("translations"))
        self.summary.setText(f"论文 {len(self._rows)} 篇  ·  已有 Markdown {has_md}  ·  本次已完成 {done}  ·  "
                             f"已勾选 {checked}  ·  进行中/待转换 {waiting}  ·  失败 {failed}  ·  已有双语 PDF {bilingual}")
        if data.get("scanned") and self._prompt_pending:
            self._prompt_pending = False
            QTimer.singleShot(0, self.offer_conversion)

    def apply_row_filter(self, *_):
        wanted = STATUS_FILTERS[self.status_filter.currentIndex()][1]
        for item, paper in zip(self._paper_items(), self._rows):
            item.setHidden(bool(wanted) and not any(paper["status"].startswith(w) or w in paper["status"] for w in wanted))

    # ------------------------------------------------------------------ engine events
    def handle_event(self, kind, data):
        if kind == "collections":
            self._collections = data
            self.populate_tree(data)
            self.populate_research_tree(data)
            if self.config.data["resume_switch"] and self.config.token and not self._resume_sent and not self.smoke:
                self._resume_sent = True
                if any(j["stage"] in ACTIVE for j in self.store.jobs().values()):
                    self._prompt_pending = False
                    self.engine.command("start")
        elif kind == "rows":
            self.populate_rows(data)
        elif kind == "connection":
            self.connection.setText(data)
            self.connection.setToolTip(data)
        elif kind == "watching":
            self._watching = data
            self.switch.setText("■ 停止转换" if data else "▶ 开始转换")
            self.switch.setObjectName("danger" if data else "primary")
            self.switch.style().unpolish(self.switch)
            self.switch.style().polish(self.switch)
            self.monitor_label.setText("● 转换开关：打开" if data else "转换开关：关闭")
        elif kind == "log":
            self.logs.append(datetime.now().strftime("%H:%M:%S") + "  " + str(data))
            logging.info("%s", data)
        elif kind in ("error", "probe"):
            self.settings_message.setText(str(data))
            self.statusBar().showMessage(str(data), 20000)
        elif kind == "busy":
            self.statusBar().showMessage(data or "就绪")
        elif kind == "bridge_outdated" and not self.smoke:
            QTimer.singleShot(0, lambda: self.offer_plugin_update(data))
        elif kind == "trash_done":
            QTimer.singleShot(0, lambda: self.after_trash(data))
        elif kind == "batch_done":
            self.statusBar().showMessage(data["message"], 30000)
            if not self.smoke:
                self.tray.showMessage("Zotero MinerU · 转换完成", data["message"])
        elif kind in ("summaries", "translations"):
            self.engine.command("publish")
        elif kind == "translation_plugin_outdated" and not self.smoke:
            QTimer.singleShot(0, lambda: self.offer_plugin_update(data, "把双语 PDF 加入论文条目", "2.3",
                                                                  PLUGIN_FOR_TRANSLATION))
        elif kind in ("summary_attached", "translation_attached"):
            self.engine.command("scan")  # list the new card / PDF under the paper and pick up its unticking
        elif kind == "translate_check":
            self.translate_engine_label.setText(str(data))
            self.settings_message.setText(str(data))
            self.statusBar().showMessage(str(data), 20000)
        elif kind in ("engine_download", "engine_download_done"):
            self.translate_engine_label.setText(str(data))
            if kind == "engine_download_done":
                self.download_engine_button.setEnabled(True)
                self.statusBar().showMessage(str(data), 20000)
        elif kind == "llm_probe":
            self.settings_message.setText(str(data))
            self.statusBar().showMessage(str(data), 20000)
        elif kind == "llm_models":
            current = self.llm_model.currentText()
            self.llm_model.clear()
            self.llm_model.addItems(data)
            self.llm_model.setCurrentText(current)
        elif kind == "research":
            self._research = data
            self.refresh_research_table()
            self._show_research_state()
        elif kind == "research_progress":
            if data:
                self.research_progress.setText(str(data))
        elif kind == "research_estimate":
            QTimer.singleShot(0, lambda: self.after_estimate(data))

    def _tray(self):
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        self.tray.setToolTip("Zotero MinerU · PDF 转 Markdown")
        menu = QMenu(self)
        show = QAction("显示主窗口", self)
        show.triggered.connect(self.restore)
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self.quit_app)
        menu.addAction(show)
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.restore() if reason in (
            QSystemTrayIcon.ActivationReason.DoubleClick, QSystemTrayIcon.ActivationReason.Trigger) else None)
        if not self.smoke:
            self.tray.show()

    def restore(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event):
        if not self._force_quit and not self.smoke and QSystemTrayIcon.isSystemTrayAvailable():
            self.hide()
            self.tray.showMessage("Zotero MinerU", "软件已缩到托盘，转换开关状态保持。右键托盘图标可退出。")
            event.ignore()
            return
        self._engine_install_cancel.set()
        self.engine.close()
        self.summarizer.close()
        self.researcher.close()
        self.translator.close()
        event.accept()

    def quit_app(self):
        self._force_quit = True
        self._engine_install_cancel.set()
        self.engine.close()
        self.summarizer.close()
        self.researcher.close()
        self.translator.close()
        self.tray.hide()
        self.close()
        QApplication.instance().quit()


STYLE = """
QWidget {font-family:'Microsoft YaHei UI','Microsoft YaHei','Segoe UI';font-size:13px;color:#1e293b;}
QMainWindow {background:#f4f7fb;}
QLabel#title {font-size:25px;font-weight:700;color:#16345c;}
QLabel#muted {color:#64748b;}
QWidget#settingsPage {background:white;}
QGroupBox#researchSettings {background:white;}
QLabel#monitor {font-weight:600;color:#15803d;}
QTabWidget::pane {background:white;border:1px solid #dce4ef;border-radius:7px;}
QTabBar::tab {padding:9px 22px;margin-right:5px;background:#e8eef7;border-radius:5px;}
QTabBar::tab:selected {background:#2563eb;color:white;}
QPushButton {padding:7px 12px;border:1px solid #cbd5e1;border-radius:5px;background:white;}
QPushButton:hover {background:#eff6ff;border-color:#60a5fa;}
QPushButton#primary {background:#2563eb;color:white;border-color:#2563eb;}
QPushButton#danger {background:#dc2626;color:white;border-color:#dc2626;}
QPushButton:disabled {color:#94a3b8;background:#f1f5f9;}
QLineEdit,QComboBox,QSpinBox {padding:5px;border:1px solid #cbd5e1;border-radius:4px;background:white;}
QTreeWidget,QTextEdit {border:1px solid #e2e8f0;border-radius:4px;background:white;}
QTreeWidget {alternate-background-color:#f8fafc;selection-background-color:#dbeafe;selection-color:#1e293b;}
QHeaderView::section {background:#f1f5f9;border:0;border-bottom:1px solid #dce4ef;padding:7px;}
QGroupBox {font-weight:600;border:1px solid #dce4ef;border-radius:6px;margin-top:14px;padding-top:16px;}
QGroupBox::title {subcontrol-origin:margin;left:12px;padding:0 6px;}
"""
