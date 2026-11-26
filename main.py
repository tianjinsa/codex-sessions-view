"""Qt 6 桌面界面。启动入口：python main.py"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import replace
from datetime import date, datetime, time, timezone
from decimal import Decimal
from pathlib import Path
from threading import Event
from time import monotonic
from typing import Callable, Iterable

from PyQt6.QtCore import (
    QDate,
    QEvent,
    QObject,
    QPoint,
    QRect,
    QSize,
    Qt,
    QThread,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QAction,
    QColor,
    QCloseEvent,
    QCursor,
    QFont,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from codex_ledger import (
    ConfigError,
    ModelPrice,
    PriceConfig,
    ScanResult,
    Session,
    Turn,
    Usage,
    UsageRecord,
    aggregate_cost,
    aggregate_cost_breakdown,
    cache_ceiling,
    cache_ceiling_many,
    default_config_path,
    default_sessions_path,
    load_config,
    parse_amount,
    save_config,
    scan_sessions,
)

ACCENT = "#138A72"
INK = "#1E2C32"
MUTED = "#69777D"
BORDER = "#DFE5E7"
SURFACE = "#FFFFFF"
PAGE = "#F3F6F7"


def money(value: Decimal, symbol: str = "$", precision: int = 4) -> str:
    return f"{symbol}{value:,.{precision}f}"


def detailed_money(value: Decimal, symbol: str = "$") -> str:
    text = format(value, ".12f").rstrip("0").rstrip(".")
    return f"{symbol}{text or '0'}"


def tokens(value: int) -> str:
    return f"{value:,}"


def short_model(name: str) -> str:
    return name if name else "未知模型"


def display_time(value: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return value.astimezone().strftime(fmt) if value else "—"


def duration_text(seconds: float | None) -> str:
    """以 HH:MM:SS 简写显示耗时；不足一天也补满小时位，便于按列对齐。"""
    if seconds is None:
        return "—"
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def tooltip_html(blocks: list[tuple[str, str, str]]) -> str:
    """用富文本组装悬停提示。

    blocks 每项是 (标签, 颜色, 正文)：标签与正文分色，块之间用分隔线隔开，
    长文本在固定宽度内自动换行，避免 Qt 提示框拉成一整条。
    """
    parts = [
        "<div style='width:420px; white-space:normal;'>",
    ]
    for index, (label, color, body) in enumerate(blocks):
        if index:
            parts.append("<hr style='border:none; border-top:1px solid #D7E0E2; margin:7px 0;'>")
        body_html = html_escape(body).replace("\n", "<br>")
        parts.append(
            f"<div style='margin:0;'>"
            f"<span style='color:{color}; font-weight:600;'>{html_escape(label)}</span>"
            f"</div>"
            f"<div style='color:#1E2C32; margin-top:2px;'>{body_html}</div>"
        )
    parts.append("</div>")
    return "".join(parts)


def output_item(usage: Usage) -> NumericItem:
    """输出 token 单元格：悬停显示其中有多少是思考（推理）token。"""
    item = NumericItem(tokens(usage.output_tokens), usage.output_tokens)
    blocks = [("输出", "#11745F", f"{tokens(usage.output_tokens)} tokens")]
    if usage.reasoning_output_tokens:
        blocks.append(
            (
                "其中思考（推理）",
                "#9271B5",
                f"{tokens(usage.reasoning_output_tokens)} tokens（{usage.reasoning_rate:.1%}）",
            )
        )
        blocks.append(
            ("其余可见输出", "#4979B8", f"{tokens(usage.visible_output_tokens)} tokens")
        )
        blocks.append(
            ("说明", "#69777D", "思考 token 已包含在输出中，不额外计费。")
        )
    else:
        blocks.append(
            ("说明", "#69777D", "这条记录没有 reasoning 字段，通常表示本次没有产生思考 token。")
        )
    set_hover_html(item, tooltip_html(blocks))
    return item


def cache_rate_text(usage: Usage) -> str:
    """缓存命中数 / 总输入数，不是缓存数 / 总 token 数。"""
    return f"{usage.cache_hit_rate:.1%}" if usage.input_tokens else "—"


def cache_rate_item(usage: Usage, records) -> NumericItem:
    """缓存命中率单元格：悬停说明理论上限与实际差距。"""
    item = NumericItem(cache_rate_text(usage), usage.cache_hit_rate)
    ceiling = cache_ceiling(records)
    if not usage.input_tokens:
        set_hover_html(
            item,
            tooltip_html([("缓存命中率", "#69777D", "本范围内没有输入 token，无法计算命中率。")]),
        )
        return item
    blocks = [
        (
            "实际缓存命中率",
            "#11745F",
            f"{ceiling.actual_rate:.1%}（{tokens(ceiling.actual_cached)} / {tokens(usage.input_tokens)} 输入 tokens）",
        ),
    ]
    if ceiling.requests == 0:
        blocks.append(
            ("理论上限", "#4979B8", "没有可统计的请求。")
        )
    else:
        first = ceiling.input_tokens - ceiling.compared_input_tokens
        first_cached = ceiling.actual_cached - ceiling.compared_cached
        if first > 0:
            blocks.append(
                (
                    "首次请求",
                    "#69777D",
                    f"{tokens(first)} tokens 没有“上一次”可参考，不预估上限，"
                    f"直接视为已达最优（上限 = 实际 {tokens(first_cached)}）。",
                )
            )
        blocks.append(
            (
                "可复用上限：上一轮输入",
                "#4979B8",
                f"{ceiling.prompt_only_rate:.1%}（{tokens(ceiling.ideal_prompt_only)} tokens，"
                f"缺口 {tokens(ceiling.gap_prompt_only)}）\n"
                "上一轮输入必定已在上下文中，前缀不变就该全部命中，这是保守口径。",
            )
        )
        blocks.append(
            (
                "可复用上限：上一轮输入+输出",
                "#9271B5",
                f"{ceiling.with_output_rate:.1%}（{tokens(ceiling.ideal_with_output)} tokens，"
                f"缺口 {tokens(ceiling.gap_with_output)}）\n"
                "上一轮输出也进入了下轮上下文，但能否命中取决于服务端前缀缓存边界，这是乐观口径。",
            )
        )
        rate = ceiling.extra_eviction / ceiling.input_tokens if ceiling.input_tokens else 0.0
        blocks.append(
            (
                "额外缓存失效",
                "#AF5B32" if ceiling.extra_eviction else "#69777D",
                f"{tokens(ceiling.extra_eviction)} tokens（占输入 {rate:.1%}，"
                f"{ceiling.eviction_events} 次请求低于上次命中）\n"
                "只统计本次命中低于上一次命中的下降量；命中不低于上次时不算失效。",
            )
        )
    blocks.append(
        (
            "口径说明",
            "#69777D",
            "一次请求能命中的上限，取决于上一次请求已经产生并留在上下文里的 token。"
            "只算输入时上限是上一次的 input；若缓存覆盖上一轮完整会话，上限是上一次的 "
            "input + output。缓存的实际覆盖范围未公开，所以两种口径都列出，便于判断当前命中率是否偏低。",
        )
    )
    set_hover_html(item, tooltip_html(blocks))
    return item


def filtered_sessions(
    sessions: Iterable[Session],
    start: date | None = None,
    end: date | None = None,
    model: str = "",
    query: str = "",
    *,
    subagent: bool | None = None,
) -> list[Session]:
    result = []
    query = query.strip().casefold()
    for session in sessions:
        if subagent is not None and session.is_subagent != subagent:
            continue
        if query:
            fields = [session.session_id, session.cwd, str(session.path), *session.models]
            if session.subagent:
                agent = session.subagent
                fields.extend([agent.nickname, agent.path, agent.role, agent.parent_thread_id])
            for turn in session.turns:
                fields.extend([turn.user_prompt, *turn.follow_ups])
                fields.extend(message.text for message in turn.agent_messages)
            if not any(query in field.casefold() for field in fields):
                continue
        if start is None and end is None and not model:
            result.append(session)
            continue
        records = []
        for record in session.records:
            if model and record.model != model:
                continue
            if start or end:
                local_date = record.timestamp.astimezone().date()
                if (start and local_date < start) or (end and local_date > end):
                    continue
            records.append(record)
        if not records:
            continue
        visible_lines = {record.line_number for record in records}
        turns = []
        for turn in session.turns:
            turn_records = [record for record in turn.records if record.line_number in visible_lines]
            if turn_records:
                turns.append(replace(turn, records=turn_records, started_at=turn.start))
        result.append(replace(session, records=records, turns=turns))
    return sorted(
        result, key=lambda item: item.end or datetime.min.replace(tzinfo=timezone.utc), reverse=True
    )


class NumericItem(QTableWidgetItem):
    """让 token、百分比和费用列按数值排序，而非字符串排序。"""

    def __init__(self, text: str, value: int | float | Decimal):
        super().__init__(text)
        self.setData(Qt.ItemDataRole.UserRole, value)
        self.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

    def __lt__(self, other: QTableWidgetItem) -> bool:
        mine = self.data(Qt.ItemDataRole.UserRole)
        theirs = other.data(Qt.ItemDataRole.UserRole)
        numeric = (int, float, Decimal)
        if isinstance(mine, numeric) and isinstance(theirs, numeric):
            return mine < theirs
        return super().__lt__(other)


def table_item(text: str, align_right: bool = False, color: str | None = None) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    if align_right:
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    if color:
        item.setForeground(QColor(color))
    return item


def model_names_item(models: list[str]) -> QTableWidgetItem:
    text = "、".join(models)
    item = table_item(text or "—")
    item.setToolTip(html_escape(text))
    return item


class HoverPanel(QFrame):
    """可滚动的悬停浮层。

    QToolTip 是系统弹出窗口，收不到滚轮和鼠标进入事件，内容一长就会被截断。
    这里改用普通窗口：鼠标可以移进去滚动阅读，离开后自动隐藏。
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent, Qt.WindowType.ToolTip)
        self.setObjectName("hoverPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setObjectName("hoverScroll")
        self.label = QLabel()
        self.label.setObjectName("hoverText")
        self.label.setTextFormat(Qt.TextFormat.RichText)
        self.label.setWordWrap(True)
        self.label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.scroll.setWidget(self.label)
        layout.addWidget(self.scroll)
        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.setInterval(220)
        self.hide_timer.timeout.connect(self.hide)
        # 鼠标从表格移到浮层需要穿过中间的间隙，这段时间不能立刻隐藏。
        self.grace_timer = QTimer(self)
        self.grace_timer.setSingleShot(True)
        self.grace_timer.setInterval(700)
        self.grace_timer.timeout.connect(self.hide_if_outside)

    def show_html(self, html: str, anchor: QPoint, available: QRect):
        """在 anchor 附近显示，并限制在 available 内；超长内容出现内部滚动条。"""
        self.label.setText(html)
        width = min(480, max(260, available.width() - 40))
        self.label.setFixedWidth(width - 26)
        self.setFixedWidth(width)
        self.scroll.viewport().setFixedWidth(width - 14)
        self.adjustSize()
        height = min(self.sizeHint().height(), int(available.height() * 0.7), 520)
        self.setFixedHeight(max(90, height))
        x = anchor.x() + 14
        y = anchor.y() + 10
        if x + width > available.right():
            x = max(available.left() + 8, available.right() - width - 8)
        if y + self.height() > available.bottom():
            y = max(available.top() + 8, available.bottom() - self.height() - 8)
        self.move(x, y)
        self.grace_timer.stop()
        self.hide_timer.stop()
        self.raise_()
        self.show()

    def contains_global(self, point: QPoint) -> bool:
        return self.frameGeometry().contains(point)

    def near_global(self, point: QPoint, margin: int = 22) -> bool:
        return self.frameGeometry().adjusted(-margin, -margin, margin, margin).contains(point)

    def hide_if_outside(self):
        if not self.near_global(QCursor.pos()):
            self.hide()

    def enterEvent(self, event):
        self.hide_timer.stop()
        self.grace_timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.grace_timer.start()
        super().leaveEvent(event)

    def wheelEvent(self, event):
        # 浮层是 ToolTip 窗口，滚轮默认仍可能落到下方表格，这里明确交给内部滚动区。
        self.scroll.verticalScrollBar().setValue(
            self.scroll.verticalScrollBar().value() - event.angleDelta().y()
        )
        event.accept()


HOVER_HTML_ROLE = Qt.ItemDataRole.UserRole + 1


def set_hover_html(item: QTableWidgetItem, html: str) -> None:
    """把富文本提示存进自定义数据角色。

    刻意不使用 setToolTip：否则 Qt 会同时弹出原生 tooltip，与可滚动浮层重叠。
    """
    item.setData(HOVER_HTML_ROLE, html)
    item.setToolTip("")


class HoverTable(QTableWidget):
    """鼠标停在带富文本提示的单元格上时，显示可滚动浮层而非系统 tooltip。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.hover_panel: HoverPanel | None = None
        self.hover_timer = QTimer(self)
        self.hover_timer.setSingleShot(True)
        self.hover_timer.setInterval(320)
        self.hover_timer.timeout.connect(self.show_hover_for_cursor)
        self.setMouseTracking(True)

    def viewportEvent(self, event):
        if event.type() == QEvent.Type.MouseMove:
            self.hover_timer.start()
        elif event.type() == QEvent.Type.Leave:
            self.hover_timer.stop()
            self.schedule_hide()
        elif event.type() in (QEvent.Type.Wheel, QEvent.Type.MouseButtonPress):
            self.hover_timer.stop()
            self.hide_hover()
        return super().viewportEvent(event)

    def schedule_hide(self):
        """离开表格时先宽限一会儿，让鼠标有机会移进浮层。"""
        if self.hover_panel and not self.hover_panel.near_global(QCursor.pos()):
            self.hover_panel.grace_timer.start()

    def show_hover_for_cursor(self):
        position = self.viewport().mapFromGlobal(QCursor.pos())
        if not self.viewport().rect().contains(position):
            return
        item = self.itemAt(position)
        html = item.data(HOVER_HTML_ROLE) if item else None
        if not html:
            self.hide_hover()
            return
        if self.hover_panel is None:
            self.hover_panel = HoverPanel(self)
        # 浮层仍在显示且内容相同，说明鼠标停在同一单元格，无需重排。
        if self.hover_panel.isVisible() and self.hover_panel.label.text() == html:
            return
        screen = self.screen() or QApplication.primaryScreen()
        available = screen.availableGeometry() if screen else QRect(0, 0, 1280, 800)
        self.hover_panel.show_html(html, QCursor.pos(), available)

    def hide_hover(self):
        if self.hover_panel:
            self.hover_panel.hide()


class Card(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)


class ClickableCard(Card):
    clicked = pyqtSignal()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class MetricCard(Card):
    def __init__(self, eyebrow: str, accent: str = ACCENT, large: bool = False):
        super().__init__()
        self.accent = accent
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(8)
        label = QLabel(eyebrow)
        label.setObjectName("eyebrow")
        self.value = QLabel("—")
        self.value.setObjectName("heroValue" if large else "metricValue")
        self.value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.caption = QLabel("等待扫描")
        self.caption.setObjectName("muted")
        self.caption.setWordWrap(True)
        layout.addWidget(label)
        layout.addWidget(self.value)
        layout.addWidget(self.caption)

    def update(self, value: str, caption: str):
        self.value.setText(value)
        self.caption.setText(caption)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(QRect(0, 18, 3, 32), QColor(self.accent))


class SpendChart(QWidget):
    """按日期的支出柱状图（matplotlib）。

    金额标签直接画在柱子上方，无需悬停即可读到数值；悬停仍保留精确金额。
    """

    def __init__(self):
        super().__init__()
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
        from matplotlib.figure import Figure

        self.values: list[tuple[str, Decimal]] = []
        self.symbol = "$"
        self.setMinimumHeight(180)
        self.figure = Figure(figsize=(6, 2.4), dpi=100, facecolor="#FFFFFF")
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.canvas.setStyleSheet("background: white;")
        self.axes = self.figure.add_subplot(111)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.addWidget(self.canvas)
        self.apply_fonts()
        self.canvas.mpl_connect("motion_notify_event", self.on_hover)
        self.canvas.setMouseTracking(True)
        self.render()

    def apply_fonts(self):
        """让 matplotlib 使用中文字体，避免日期与提示出现方块。"""
        from matplotlib import rcParams

        rcParams["font.sans-serif"] = [
            "Microsoft YaHei UI",
            "Microsoft YaHei",
            "SimHei",
            "Segoe UI",
            "DejaVu Sans",
        ]
        rcParams["axes.unicode_minus"] = False

    def set_values(self, values: list[tuple[str, Decimal]], symbol: str):
        self.values = values[-14:]
        self.symbol = symbol
        self.render()

    def render(self):
        axes = self.axes
        axes.clear()
        axes.set_facecolor("#FFFFFF")
        for spine in ("top", "right"):
            axes.spines[spine].set_visible(False)
        for spine in ("left", "bottom"):
            axes.spines[spine].set_color("#E3E8EA")
        axes.tick_params(colors="#69777D", labelsize=8.5, length=0)
        axes.yaxis.grid(True, color="#EDF1F2", linewidth=1)
        axes.set_axisbelow(True)

        if not self.values:
            axes.set_xticks([])
            axes.set_yticks([])
            axes.text(
                0.5, 0.5, "所选范围内暂无费用记录",
                ha="center", va="center", color="#8B989D", fontsize=10,
                transform=axes.transAxes,
            )
            self.figure.subplots_adjust(left=0.02, right=0.99, top=0.88, bottom=0.2)
            self.canvas.draw_idle()
            return

        labels = [label for label, _ in self.values]
        amounts = [float(value) for _, value in self.values]
        highest = max(amounts) or 1.0
        positions = range(len(amounts))
        bars = axes.bar(positions, amounts, width=0.52, color=ACCENT, zorder=3)
        # 金额直接标在柱顶，超出高度的柱子也能读到具体数值。
        headroom = highest * 1.18
        axes.set_ylim(0, headroom)
        for bar, amount in zip(bars, amounts):
            axes.annotate(
                money(Decimal(str(round(amount, 6))), self.symbol, precision=3),
                xy=(bar.get_x() + bar.get_width() / 2, bar.get_height()),
                xytext=(0, 4),
                textcoords="offset points",
                ha="center",
                va="bottom",
                fontsize=8,
                color="#32464D",
                clip_on=False,
            )
        axes.set_xticks(list(positions))
        axes.set_xticklabels(labels, rotation=0, ha="center", color="#69777D")
        axes.set_yticks([])
        axes.margins(x=0.02)
        # 底部留足空间给日期标签，否则会被画布边缘裁掉。
        self.figure.subplots_adjust(left=0.025, right=0.99, top=0.86, bottom=0.24)
        self.canvas.draw_idle()

    def on_hover(self, event):
        if event.inaxes is None or event.xdata is None:
            self.canvas.setToolTip("")
            return
        index = int(round(event.xdata))
        if 0 <= index < len(self.values):
            label, value = self.values[index]
            self.canvas.setToolTip(f"{label}\n{detailed_money(value, self.symbol)}")
        else:
            self.canvas.setToolTip("")


class CostBreakdownChart(QWidget):
    """费用构成环形图：非缓存输入、缓存输入、输出各占多少。"""

    COLORS = ("#11745F", "#4FA88F", "#9271B5")

    def __init__(self):
        super().__init__()
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
        from matplotlib.figure import Figure

        self.setMinimumHeight(190)
        self.figure = Figure(figsize=(3.4, 2.0), dpi=100, facecolor="#FFFFFF")
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.canvas.setStyleSheet("background: white;")
        self.axes = self.figure.add_subplot(111)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.canvas)
        self.parts: list[tuple[str, Decimal, float]] = []
        self.symbol = "$"
        self.render()

    def set_parts(self, parts: list[tuple[str, Decimal, float]], symbol: str):
        self.parts, self.symbol = parts, symbol
        self.render()

    def render(self):
        from matplotlib import rcParams

        rcParams["font.sans-serif"] = [
            "Microsoft YaHei UI", "Microsoft YaHei", "SimHei", "Segoe UI", "DejaVu Sans",
        ]
        rcParams["axes.unicode_minus"] = False
        axes = self.axes
        axes.clear()
        total = sum((amount for _, amount, _ in self.parts), Decimal(0))
        if total <= 0:
            axes.set_axis_off()
            axes.text(
                0.5, 0.5, "所选范围内没有已定价的调用",
                ha="center", va="center", color="#8B989D", fontsize=9,
                transform=axes.transAxes,
            )
            self.figure.subplots_adjust(0, 0, 1, 1)
            self.canvas.draw_idle()
            return
        amounts = [float(amount) for _, amount, _ in self.parts]
        labels = [
            f"{name} {ratio:.1%}\n{money(amount, self.symbol, 3)}"
            for (name, amount, ratio) in self.parts
        ]
        wedges, _ = axes.pie(
            amounts,
            colors=self.COLORS,
            startangle=90,
            counterclock=False,
            wedgeprops={"width": 0.42, "edgecolor": "white", "linewidth": 1.5},
        )
        axes.text(
            0, 0, f"合计\n{money(total, self.symbol, 3)}",
            ha="center", va="center", fontsize=9, color="#1E2C32",
        )
        axes.legend(
            wedges, labels,
            loc="center left",
            bbox_to_anchor=(1.0, 0.5),
            frameon=False,
            fontsize=8,
            labelspacing=0.9,
        )
        axes.set_aspect("equal")
        self.figure.subplots_adjust(left=0.0, right=0.62, top=0.98, bottom=0.02)
        self.canvas.draw_idle()


class ScanWorker(QObject):
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)
    progressed = pyqtSignal(int, int)

    def __init__(self, path: Path):
        super().__init__()
        self.path = path
        self._cancelled = Event()
        self._last_progress = 0.0

    def cancel(self):
        self._cancelled.set()

    def run(self):
        try:
            result = scan_sessions(self.path, self.on_progress, cancelled=self._cancelled.is_set)
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.finished.emit(result)

    def on_progress(self, current: int, total: int) -> bool:
        if self._cancelled.is_set():
            return False
        now = monotonic()
        if current in (0, total) or now - self._last_progress >= 0.08:
            self._last_progress = now
            self.progressed.emit(current, total)
        return True


class PriceDialog(QDialog):
    saved = pyqtSignal(object)

    def __init__(self, config: PriceConfig, path: Path, parent=None):
        super().__init__(parent)
        self.config = config
        self.path = path
        self.rows: list[dict[str, QLineEdit]] = []
        self.setWindowTitle("模型价格 · Codex Session Ledger")
        self.resize(920, 650)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 26, 28, 24)
        layout.setSpacing(16)
        heading = QLabel("每一百万 tokens 的价格")
        heading.setObjectName("dialogTitle")
        layout.addWidget(heading)
        note = QLabel(
            "非缓存输入、缓存命中输入和输出分别计价。支持精确模型名，或 gpt-5-* 这类后缀通配。\n"
            "价格由你填写，程序不会联网获取；0 表示按零价格估算。"
        )
        note.setObjectName("muted")
        layout.addWidget(note)
        form = QFormLayout()
        self.currency = QLineEdit(config.currency)
        self.symbol = QLineEdit(config.currency_symbol)
        self.symbol.setMaxLength(5)
        self.symbol.setMaximumWidth(100)
        form.addRow("货币代码", self.currency)
        form.addRow("金额符号", self.symbol)
        layout.addLayout(form)
        header = QHBoxLayout()
        for text, stretch in (("模型 / 通配规则", 3), ("输入", 2), ("缓存输入", 2), ("输出", 2), ("", 0)):
            label = QLabel(text)
            label.setObjectName("eyebrow")
            header.addWidget(label, stretch)
        layout.addLayout(header)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setMinimumHeight(140)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        rows_container = QWidget()
        self.rows_layout = QVBoxLayout(rows_container)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(8)
        scroll.setWidget(rows_container)
        layout.addWidget(scroll, 1)
        add = QPushButton("＋ 添加模型")
        add.setObjectName("secondaryButton")
        add.clicked.connect(lambda: self.add_row())
        layout.addWidget(add, alignment=Qt.AlignmentFlag.AlignLeft)
        config_path = QLabel(f"保存到：{self.path}")
        config_path.setObjectName("muted")
        config_path.setWordWrap(True)
        layout.addWidget(config_path)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        save = buttons.addButton("保存并重新计算", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.rejected.connect(self.reject)
        save.clicked.connect(self.save)
        layout.addWidget(buttons)
        for name, price in config.models.items():
            self.add_row(name, price)
        if not config.models:
            self.add_row()

    def add_row(self, name: str = "", price: ModelPrice | None = None):
        price = price or ModelPrice(Decimal(0), Decimal(0), Decimal(0))
        row = QHBoxLayout()
        widgets: dict[str, QLineEdit] = {}
        for field, value, stretch in (
            ("name", name, 3),
            ("input", format(price.input, "f"), 2),
            ("cached_input", format(price.cached_input, "f"), 2),
            ("output", format(price.output, "f"), 2),
        ):
            editor = QLineEdit(value)
            editor.setPlaceholderText("例如 gpt-5.2" if field == "name" else "0")
            if field != "name":
                editor.setAlignment(Qt.AlignmentFlag.AlignRight)
            row.addWidget(editor, stretch)
            widgets[field] = editor
        remove = QPushButton("×")
        remove.setObjectName("iconButton")
        remove.setToolTip("移除此模型")
        remove.setFixedSize(34, 34)
        row.addWidget(remove)
        container = QWidget()
        container.setLayout(row)
        self.rows_layout.addWidget(container)
        widgets["name"].setFocus() if not name else None
        self.rows.append(widgets)
        remove.clicked.connect(lambda: self.remove_row(container, widgets))

    def remove_row(self, container: QWidget, widgets: dict[str, QLineEdit]):
        if len(self.rows) == 1:
            for editor in widgets.values():
                editor.clear()
            return
        self.rows.remove(widgets)
        self.rows_layout.removeWidget(container)
        container.deleteLater()

    def save(self):
        models: dict[str, ModelPrice] = {}
        try:
            for row in self.rows:
                name = row["name"].text().strip()
                if not name:
                    if any(row[key].text().strip() for key in ("input", "cached_input", "output")):
                        raise ConfigError("已填写价格，但模型名称为空。")
                    continue
                if name in models:
                    raise ConfigError(f"模型 {name} 重复，请合并为一行。")
                if any(char in name for char in "*?[]"):
                    if name.count("*") != 1 or not name.endswith("*") or len(name) < 2:
                        raise ConfigError(f"{name} 的通配规则无效；只支持例如 gpt-5-* 的后缀星号。")
                values = {
                    key: parse_amount(row[key].text() or "0", f"{name} 的{label}")
                    for key, label in (("input", "输入价"), ("cached_input", "缓存价"), ("output", "输出价"))
                }
                models[name] = ModelPrice(**values, note=self.config.models[name].note if name in self.config.models else "")
            config = PriceConfig(
                models=models,
                currency=self.currency.text().strip() or "USD",
                currency_symbol=self.symbol.text().strip() or "$",
            )
            save_config(config, self.path)
        except (ConfigError, OSError) as exc:
            QMessageBox.warning(self, "价格未保存", str(exc))
            return
        self.config = config
        self.saved.emit(config)
        self.accept()


class CostDetailMixin:
    """会话详情与任务详情共用的费用构成视图。"""

    def build_cost_block(self, records, config: PriceConfig, usage: Usage):
        card = QFrame()
        card.setObjectName("card")
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 10)
        title = QLabel("费用构成")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        body = QHBoxLayout()
        body.setSpacing(12)
        chart = CostBreakdownChart()
        parts, unpriced = aggregate_cost_breakdown(records, config)
        chart.set_parts(parts.items(), config.currency_symbol)
        body.addWidget(chart, 3)

        table = QTableWidget(0, 5)
        table.setHorizontalHeaderLabels(["计费项", "tokens", "单价 / 百万", "费用", "占比"])
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, 5):
            table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        table.verticalHeader().setVisible(False)
        table.setAlternatingRowColors(True)
        table.setShowGrid(False)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        models = sorted({record.model for record in records if record.model})
        reference = config.find(models[0]) if len(models) == 1 else None
        price_hint = "按模型计价" if len(models) > 1 else "未配置" if models else "—"
        rows = [
            ("非缓存输入", usage.uncached_input_tokens,
             reference.input if reference else None, parts.uncached_input),
            ("缓存输入", usage.cached_input_tokens,
             reference.cached_input if reference else None, parts.cached_input),
            ("输出", usage.output_tokens,
             reference.output if reference else None, parts.output),
        ]
        for name, token_count, price, amount in rows:
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 0, table_item(name))
            table.setItem(row, 1, NumericItem(tokens(token_count), token_count))
            table.setItem(row, 2, table_item(
                money(price, config.currency_symbol, 4) if price is not None else price_hint,
                align_right=True,
                color="#AF5B32" if price_hint == "未配置" and price is None else None,
            ))
            table.setItem(row, 3, NumericItem(money(amount, config.currency_symbol, 6), amount))
            table.setItem(row, 4, NumericItem(
                f"{parts.ratio(amount):.1%}" if parts.total else "—", parts.ratio(amount),
            ))
        total_row = table.rowCount()
        table.insertRow(total_row)
        table.setItem(total_row, 0, table_item("合计", color=ACCENT))
        table.setItem(total_row, 1, NumericItem(tokens(usage.total_tokens), usage.total_tokens))
        table.setItem(total_row, 2, table_item("—", True))
        table.setItem(total_row, 3, NumericItem(money(parts.total, config.currency_symbol, 6), parts.total))
        table.setItem(total_row, 4, NumericItem("100.0%" if parts.total else "—", Decimal(1)))
        table.setMinimumHeight(158)
        table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        body.addWidget(table, 4)
        layout.addLayout(body, 1)
        if unpriced:
            warn = QLabel(f"另有 {tokens(unpriced)} tokens 未配置价格，未计入以上金额。")
            warn.setObjectName("warning")
            warn.setWordWrap(True)
            layout.addWidget(warn)
        return card


def turn_message_blocks(turn: Turn) -> list[tuple[str, str, str]]:
    blocks = []
    if turn.user_prompt:
        blocks.append(("首条提问", "#11745F", turn.user_prompt))
    for number, follow_up in enumerate(turn.follow_ups, 1):
        blocks.append((f"补充 {number}", "#4979B8", follow_up))
    for message in turn.agent_messages:
        label = "委派任务" if message.starts_turn else "智能体消息"
        blocks.append((f"{label} · {message.author}", "#4979B8", message.text))
    if turn.last_agent_message:
        blocks.append(("最终回复", "#9271B5", turn.last_agent_message))
    return blocks


def text_preview(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


class TurnDialog(QDialog, CostDetailMixin):
    """单个任务轮次的用量与费用构成。"""

    def __init__(self, turn: Turn, config: PriceConfig, parent=None):
        super().__init__(parent)
        self.setWindowTitle("任务用量明细")
        self.resize(920, 640)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 22)
        title = QLabel(turn.summary_text(80))
        title.setObjectName("dialogTitle")
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setWordWrap(True)
        layout.addWidget(title)
        usage = turn.usage
        meta = QLabel(
            f"{display_time(turn.start)}  →  {display_time(turn.completed_at) if turn.completed_at else '—'}"
            f"　耗时 {duration_text(turn.duration_seconds)}\n"
            f"状态：{turn.status_text}　模型：{'、'.join(turn.models) or '未知模型'}　"
            f"API 调用 {len(turn.records)} 次　缓存命中率 {cache_rate_text(usage)}\n"
            f"补充消息 {len(turn.follow_ups)} 条"
            + (f"　并入内部轮 {turn.absorbed_auto_turns} 个" if turn.absorbed_auto_turns else "")
            + (f"\n父轮次 ID {turn.parent_turn_id}" if turn.parent_turn_id else "")
        )
        meta.setObjectName("muted")
        meta.setTextFormat(Qt.TextFormat.PlainText)
        meta.setWordWrap(True)
        layout.addWidget(meta)
        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        tabs.addTab(self.build_cost_block(turn.records, config, usage), "用量与费用")
        messages = "\n\n".join(f"{label}\n{body}" for label, _, body in turn_message_blocks(turn))
        content = QPlainTextEdit(messages or "该轮次暂无可显示的消息。")
        content.setReadOnly(True)
        content.document().setDocumentMargin(16)
        tabs.addTab(content, "任务内容")
        layout.addWidget(tabs, 1)
        buttons = QDialogButtonBox()
        close = buttons.addButton("关闭", QDialogButtonBox.ButtonRole.AcceptRole)
        close.setObjectName("primaryButton")
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)


class SessionDialog(QDialog, CostDetailMixin):
    def __init__(self, session: Session, config: PriceConfig, parent=None, *, parent_title: str = ""):
        super().__init__(parent)
        self.setWindowTitle("会话用量明细")
        self.resize(1180, 770)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 26, 28, 24)
        title = QLabel(session.title)
        title.setObjectName("dialogTitle")
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setWordWrap(True)
        layout.addWidget(title)
        usage = session.usage
        lines = [
            f"{display_time(session.start)}  →  {display_time(session.end)}",
            f"API 调用 {len(session.records)} 次 · 缓存命中率 {cache_rate_text(usage)}"
            f"（{tokens(usage.cached_input_tokens)} / {tokens(usage.input_tokens)} 输入 tokens）",
            f"会话 ID：{session.session_id}",
        ]
        if session.subagent:
            agent = session.subagent
            identity = []
            if agent.path:
                identity.append(f"智能体路径 {agent.path}")
            if agent.role:
                identity.append(f"角色 {agent.role}")
            if agent.depth is not None:
                identity.append(f"层级 {agent.depth}")
            if identity:
                lines.append(" · ".join(identity))
            if agent.parent_thread_id:
                parent_label = f"父会话 ID：{agent.parent_thread_id}"
                if parent_title:
                    parent_label += f" · {text_preview(parent_title, 48)}"
                lines.append(parent_label)
        lines.append(f"文件：{session.path}")
        metadata = QLabel("\n".join(lines))
        metadata.setObjectName("muted")
        metadata.setTextFormat(Qt.TextFormat.PlainText)
        metadata.setWordWrap(True)
        metadata.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(metadata)
        if session.cwd:
            cwd = QLabel(f"工作目录：{session.cwd}")
            cwd.setTextFormat(Qt.TextFormat.PlainText)
            cwd.setWordWrap(True)
            cwd.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.addWidget(cwd)
        layout.addWidget(self.build_cost_block(session.records, config, usage))
        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        if session.turns:
            turn_page = self.build_turn_table(session, config)
        else:
            empty = "日志中暂无可显示的执行轮次。" if session.is_subagent else "日志中未识别到用户发起的任务。"
            turn_page = QLabel(empty)
            turn_page.setAlignment(Qt.AlignmentFlag.AlignCenter)
            turn_page.setObjectName("muted")
        turn_label = "执行轮次" if session.is_subagent else "任务轮次"
        tabs.addTab(turn_page, f"{turn_label}（{session.turn_count}）")
        tabs.addTab(self.build_model_table(session, config), "按模型")
        tabs.setMinimumHeight(240)
        layout.addWidget(tabs, 1)
        hint = QLabel(f"双击{turn_label}可查看消息与用量。用量按当前筛选统计，时间取自原始日志。")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        buttons = QDialogButtonBox()
        close = buttons.addButton("关闭", QDialogButtonBox.ButtonRole.AcceptRole)
        close.setObjectName("primaryButton")
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    def build_turn_table(self, session: Session, config: PriceConfig) -> QTableWidget:
        table = HoverTable(session.turn_count, 12)
        summary_header = "执行摘要" if session.is_subagent else "任务（首条提问）"
        table.setHorizontalHeaderLabels(
            [
                "#", summary_header, "状态", "开始时间", "完成时间", "耗时", "模型",
                "非缓存输入", "缓存输入", "输出", "缓存命中率", "估算费用",
            ]
        )
        header = table.horizontalHeader()
        header.setMinimumSectionSize(38)
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        for column, width in ((1, 215), (6, 108)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
            header.resizeSection(column, width)
        header.moveSection(header.visualIndex(11), 3)
        header.setStretchLastSection(True)
        table.setWordWrap(False)
        table.setTextElideMode(Qt.TextElideMode.ElideRight)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(34)
        table.setAlternatingRowColors(True)
        table.setShowGrid(False)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        for row, turn in enumerate(session.turns):
            usage = turn.usage
            cost, unpriced, _ = aggregate_cost(turn.records, config)
            number = row + 1
            table.setItem(row, 0, NumericItem(str(number), number))
            # 列表里只显示提问摘要，完整内容放在悬停提示中，避免长文本挤压列宽。
            label = turn.summary_text()
            if turn.follow_ups:
                label += f"（+{len(turn.follow_ups)} 条补充）"
            title_item = table_item(label)
            messages = turn_message_blocks(turn)
            blocks = [(name, color, text_preview(body, 600)) for name, color, body in messages[:5]]
            if len(messages) > 5 or any(len(body) > 600 for _, _, body in messages[:5]):
                blocks.append(("完整内容", MUTED, "双击打开任务详情，切换到「任务内容」。"))
            meta = []
            if turn.turn_id:
                meta.append(f"turn_id {turn.turn_id}")
            if turn.parent_turn_id:
                meta.append(f"父轮次 ID {turn.parent_turn_id}")
            meta.append(f"状态 {turn.status_text}")
            if turn.duration_seconds is not None:
                meta.append(f"耗时 {duration_text(turn.duration_seconds)}")
            if turn.absorbed_auto_turns:
                meta.append(f"并入 {turn.absorbed_auto_turns} 个无提问内部轮")
            blocks.append(("任务信息", "#C68A49", " · ".join(meta)))
            set_hover_html(title_item, tooltip_html(blocks))
            table.setItem(row, 1, title_item)
            status_item = table_item(turn.status_text)
            if turn.ended_unexpectedly:
                status_item.setForeground(QColor("#AF5B32"))
            table.setItem(row, 2, status_item)
            table.setItem(row, 3, table_item(display_time(turn.start, "%m-%d %H:%M:%S")))
            table.setItem(row, 4, table_item(display_time(turn.completed_at, "%m-%d %H:%M:%S")))
            duration = turn.duration_seconds
            table.setItem(
                row, 5,
                NumericItem(duration_text(duration) if duration is not None else "—", duration or 0),
            )
            table.setItem(row, 6, model_names_item(turn.models))
            table.setItem(row, 7, NumericItem(tokens(usage.uncached_input_tokens), usage.uncached_input_tokens))
            table.setItem(row, 8, NumericItem(tokens(usage.cached_input_tokens), usage.cached_input_tokens))
            table.setItem(row, 9, output_item(usage))
            table.setItem(row, 10, cache_rate_item(usage, turn.records))
            amount = money(cost, config.currency_symbol) if turn.records else "—"
            cost_item = NumericItem(f"≥ {amount}" if unpriced else amount, cost)
            if unpriced:
                cost_item.setForeground(QColor("#AF5B32"))
                cost_item.setToolTip(f"{tokens(unpriced)} tokens 未配置价格")
            table.setItem(row, 11, cost_item)
            # 行首存轮次对象，双击时用于展示这一轮自己的费用构成。
            table.item(row, 0).setData(Qt.ItemDataRole.UserRole + 2, turn)
        table.doubleClicked.connect(
            lambda index: self.open_turn_detail(table, index, config)
        )
        return table

    def open_turn_detail(self, table: QTableWidget, index, config: PriceConfig):
        item = table.item(index.row(), 0)
        turn = item.data(Qt.ItemDataRole.UserRole + 2) if item else None
        if turn is None:
            return
        TurnDialog(turn, config, self).exec()

    def build_model_table(self, session: Session, config: PriceConfig) -> QTableWidget:
        table = HoverTable(0, 7)
        table.setHorizontalHeaderLabels(["模型", "非缓存输入", "缓存输入", "缓存命中率", "输出", "API 调用", "估算费用"])
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, 7):
            table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        groups: dict[str, list[UsageRecord]] = {}
        for record in session.records:
            groups.setdefault(record.model, []).append(record)
        for model, records in sorted(groups.items()):
            usage = sum((record.usage for record in records), Usage())
            cost, unpriced, _ = aggregate_cost(records, config)
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 0, table_item(short_model(model)))
            table.setItem(row, 1, NumericItem(tokens(usage.uncached_input_tokens), usage.uncached_input_tokens))
            table.setItem(row, 2, NumericItem(tokens(usage.cached_input_tokens), usage.cached_input_tokens))
            table.setItem(row, 3, cache_rate_item(usage, records))
            table.setItem(row, 4, output_item(usage))
            table.setItem(row, 5, NumericItem(tokens(len(records)), len(records)))
            table.setItem(row, 6, table_item(
                money(cost, config.currency_symbol) if not unpriced else "未配置价格",
                align_right=True,
                color="#AF5B32" if unpriced else ACCENT,
            ))
        return table


class LedgerWindow(QMainWindow):
    def __init__(self, root: Path, config_path: Path, config: PriceConfig):
        super().__init__()
        self.root_path = root
        self.config_path = config_path
        self.config = config
        self.sessions: list[Session] = []
        self.filtered: list[Session] = []
        self.current_page = 0
        self.scan_generation = 0
        self.scan_threads: dict[QThread, ScanWorker] = {}
        self.scan_worker: ScanWorker | None = None
        self.scan_report: ScanResult | None = None
        self.scan_error = ""
        self.is_scanning = False
        self.closing = False
        self.setWindowTitle("Codex Session Ledger")
        self.resize(1380, 880)
        self.setMinimumSize(1060, 700)
        self.build_ui()
        self.create_actions()
        QTimer.singleShot(50, self.start_scan_if_needed)

    def build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QHBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        root_layout.addWidget(self.build_sidebar())
        content = QVBoxLayout()
        content.setContentsMargins(32, 26, 32, 20)
        content.setSpacing(18)
        root_layout.addLayout(content, 1)

        header = QHBoxLayout()
        self.heading = QLabel("费用总览")
        self.heading.setObjectName("pageTitle")
        header.addWidget(self.heading)
        header.addStretch()
        self.export_button = QPushButton("导出 CSV")
        self.export_button.setObjectName("secondaryButton")
        self.export_button.clicked.connect(self.export_csv)
        header.addWidget(self.export_button)
        self.refresh_button = QPushButton("重新扫描")
        self.refresh_button.setObjectName("primaryButton")
        self.refresh_button.clicked.connect(self.start_scan)
        header.addWidget(self.refresh_button)
        content.addLayout(header)

        source_row = QHBoxLayout()
        self.source_edit = QLineEdit(str(self.root_path))
        self.source_edit.setClearButtonEnabled(True)
        self.source_edit.setAccessibleName("JSONL 来源路径")
        self.source_edit.setPlaceholderText("选择 Codex sessions 文件夹或一个 .jsonl 文件")
        self.source_edit.returnPressed.connect(self.start_scan)
        source_row.addWidget(self.source_edit, 1)
        browse = QPushButton("选择目录…")
        browse.setObjectName("secondaryButton")
        browse.clicked.connect(self.browse)
        source_row.addWidget(browse)
        browse_file = QPushButton("选择文件…")
        browse_file.setObjectName("secondaryButton")
        browse_file.clicked.connect(self.browse_file)
        source_row.addWidget(browse_file)
        content.addLayout(source_row)

        self.filter_bar = QFrame()
        filter_layout = QHBoxLayout(self.filter_bar)
        filter_layout.setContentsMargins(0, 0, 0, 0)
        filter_layout.setSpacing(10)
        self.period = QComboBox()
        self.period.addItems(["全部时间", "今天", "最近 7 天", "最近 30 天", "自定义范围"])
        self.period.setMinimumWidth(126)
        self.period.currentIndexChanged.connect(self.period_changed)
        filter_layout.addWidget(self.period)
        self.model_filter = QComboBox()
        self.model_filter.setMinimumWidth(155)
        self.model_filter.addItem("全部模型", "")
        self.model_filter.currentIndexChanged.connect(self.apply_filters)
        filter_layout.addWidget(self.model_filter)
        self.session_kind_filter = QComboBox()
        self.session_kind_filter.setAccessibleName("会话类型")
        self.session_kind_filter.addItem("全部会话", None)
        self.session_kind_filter.addItem("主会话", False)
        self.session_kind_filter.addItem("子智能体", True)
        self.session_kind_filter.setMinimumWidth(106)
        self.session_kind_filter.currentIndexChanged.connect(self.apply_filters)
        filter_layout.addWidget(self.session_kind_filter)
        self.search = QLineEdit()
        self.search.setClearButtonEnabled(True)
        self.search.setPlaceholderText("搜索提问、智能体、ID、路径或模型")
        self.search.setMinimumWidth(210)
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(280)
        self.search_timer.timeout.connect(self.apply_filters)
        self.search.textChanged.connect(lambda: self.search_timer.start())
        filter_layout.addWidget(self.search, 1)
        content.addWidget(self.filter_bar)

        self.date_filter_bar = QWidget()
        date_layout = QHBoxLayout(self.date_filter_bar)
        date_layout.setContentsMargins(0, 0, 0, 0)
        date_layout.addWidget(QLabel("日期范围"))
        self.start_date = QDateEdit(QDate.currentDate())
        self.end_date = QDateEdit(QDate.currentDate())
        for editor, name in ((self.start_date, "开始日期"), (self.end_date, "结束日期")):
            editor.setAccessibleName(name)
            editor.setCalendarPopup(True)
            editor.setDisplayFormat("yyyy-MM-dd")
            editor.dateChanged.connect(self.apply_filters)
            date_layout.addWidget(editor)
        date_layout.addStretch()
        self.date_filter_bar.hide()
        content.addWidget(self.date_filter_bar)

        self.empty_state = QLabel()
        self.empty_state.setObjectName("emptyState")
        self.empty_state.setWordWrap(True)
        self.empty_state.hide()
        content.addWidget(self.empty_state)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.build_overview())
        self.stack.addWidget(self.build_sessions_page())
        self.stack.addWidget(self.build_models_page())
        self.stack.addWidget(self.build_days_page())
        self.stack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self.content_scroll = QScrollArea()
        self.content_scroll.setObjectName("contentScroll")
        self.content_scroll.setWidgetResizable(True)
        self.content_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.content_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.content_scroll.setWidget(self.stack)
        content.addWidget(self.content_scroll, 1)
        self.stack.currentChanged.connect(self.update_scroll_policy)
        self.update_scroll_policy()
        footer = QHBoxLayout()
        self.status = QLabel("准备扫描本地会话…")
        self.status.setObjectName("muted")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.scan_details_button = QPushButton("扫描详情")
        self.scan_details_button.setObjectName("textButton")
        self.scan_details_button.clicked.connect(self.show_scan_details)
        self.scan_details_button.hide()
        self.progress = QProgressBar()
        self.progress.setFixedWidth(140)
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        footer.addWidget(self.status, 1)
        footer.addWidget(self.scan_details_button)
        footer.addWidget(self.progress)
        content.addLayout(footer)

    def update_scroll_policy(self):
        """表格页用自身滚动条；总览页内容固定，交给外层滚动区。"""
        is_overview = self.stack.currentIndex() == 0
        self.stack.setMinimumHeight(self.stack.widget(0).minimumSizeHint().height() if is_overview else 0)
        self.content_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded if is_overview else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        if not is_overview:
            self.content_scroll.verticalScrollBar().setValue(0)

    def build_sidebar(self) -> QWidget:
        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(224)
        layout = QVBoxLayout(side)
        layout.setContentsMargins(18, 30, 18, 24)
        layout.setSpacing(8)
        mark = QLabel("C / L")
        mark.setObjectName("brandMark")
        layout.addWidget(mark)
        brand = QLabel("CODEX\nSESSION LEDGER")
        brand.setObjectName("brand")
        layout.addWidget(brand)
        layout.addSpacing(30)
        self.nav = QListWidget()
        self.nav.setObjectName("navigation")
        self.nav.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.nav.setSpacing(4)
        self.nav.setIconSize(QSize(18, 18))
        for text in ("费用总览", "会话明细", "模型统计", "每日统计"):
            item = QListWidgetItem(text)
            item.setSizeHint(QSize(180, 44))
            self.nav.addItem(item)
        self.nav.currentRowChanged.connect(self.change_page)
        self.nav.setCurrentRow(0)
        self.nav.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.nav.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.nav.setMinimumHeight(4 * 44 + 6 * 4 + 12)
        self.nav.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.nav)
        layout.addStretch()
        divider = QFrame()
        divider.setObjectName("sideDivider")
        divider.setFixedHeight(1)
        layout.addWidget(divider)
        price = QPushButton("模型价格设置")
        price.setObjectName("sideButton")
        price.clicked.connect(self.edit_prices)
        layout.addWidget(price)
        formula = QPushButton("计费口径与说明")
        formula.setObjectName("sideButton")
        formula.clicked.connect(self.show_help)
        layout.addWidget(formula)
        return side

    def build_overview(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 2, 0, 0)
        layout.setSpacing(16)
        cards = QHBoxLayout()
        cards.setSpacing(12)
        self.total_card = MetricCard("估算总支出", ACCENT, large=True)
        self.call_card = MetricCard("API 调用", "#4979B8")
        self.token_card = MetricCard("处理 tokens", "#9271B5")
        self.cache_card = MetricCard("缓存命中率", "#C68A49")
        cards.addWidget(self.total_card, 2)
        cards.addWidget(self.call_card, 1)
        cards.addWidget(self.token_card, 1)
        cards.addWidget(self.cache_card, 1)
        layout.addLayout(cards)
        lower = QHBoxLayout()
        lower.setSpacing(16)
        chart_card = Card()
        chart_layout = QVBoxLayout(chart_card)
        chart_layout.setContentsMargins(22, 18, 22, 16)
        title_row = QHBoxLayout()
        title = QLabel("近 14 个活跃日")
        title.setObjectName("sectionTitle")
        title_row.addWidget(title)
        title_row.addStretch()
        unit = QLabel("估算支出")
        unit.setObjectName("muted")
        title_row.addWidget(unit)
        chart_layout.addLayout(title_row)
        self.chart = SpendChart()
        chart_layout.addWidget(self.chart)
        lower.addWidget(chart_card, 3)
        breakdown_card = Card()
        breakdown_layout = QVBoxLayout(breakdown_card)
        breakdown_layout.setContentsMargins(22, 18, 22, 16)
        breakdown_title = QLabel("Token 去向")
        breakdown_title.setObjectName("sectionTitle")
        breakdown_layout.addWidget(breakdown_title)
        token_grid = QGridLayout()
        token_grid.setHorizontalSpacing(20)
        token_grid.setVerticalSpacing(9)
        self.breakdown_values: dict[str, QLabel] = {}
        for row, (key, label) in enumerate(
            (
                ("uncached", "非缓存输入"),
                ("cached", "缓存输入"),
                ("output", "模型输出"),
                ("reasoning", "其中思考"),
            )
        ):
            caption = QLabel(label)
            caption.setObjectName("breakdownKey")
            caption.setFixedHeight(20)
            value = QLabel("—")
            value.setObjectName("breakdownValue")
            value.setFixedHeight(20)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            token_grid.addWidget(caption, row, 0)
            token_grid.addWidget(value, row, 1, Qt.AlignmentFlag.AlignRight)
            self.breakdown_values[key] = value
        breakdown_layout.addLayout(token_grid)
        breakdown_layout.addSpacing(10)
        self.unpriced = QLabel("")
        self.unpriced.setObjectName("warning")
        self.unpriced.setWordWrap(True)
        breakdown_layout.addWidget(self.unpriced)
        breakdown_layout.addStretch()
        prices = QPushButton("配置模型价格  →")
        prices.setObjectName("textButton")
        prices.clicked.connect(self.edit_prices)
        breakdown_layout.addWidget(prices, alignment=Qt.AlignmentFlag.AlignLeft)
        lower.addWidget(breakdown_card, 2)
        layout.addLayout(lower, 1)
        recent = ClickableCard()
        recent.clicked.connect(lambda: self.change_page(1))
        recent_layout = QVBoxLayout(recent)
        recent_layout.setContentsMargins(22, 16, 22, 12)
        recent_title = QLabel("最近会话   ·   查看全部 →")
        recent_title.setObjectName("sectionTitle")
        recent_layout.addWidget(recent_title)
        self.recent_table = self.create_table(
            ["会话 / 工作目录", "开始时间", "模型", "Tokens", "缓存命中率", "估算费用"]
        )
        self.recent_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, 6):
            self.recent_table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.recent_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        self.recent_table.setColumnWidth(2, 116)
        self.recent_table.setSortingEnabled(False)
        self.recent_table.verticalHeader().setDefaultSectionSize(32)
        self.recent_table.setFixedHeight(168)
        self.recent_table.setObjectName("compactTable")
        self.recent_table.doubleClicked.connect(self.open_selected_session)
        recent_layout.addWidget(self.recent_table)
        layout.addWidget(recent)
        return page

    def create_table(self, headers: list[str]) -> QTableWidget:
        table = HoverTable(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(36)
        table.setAlternatingRowColors(True)
        table.setShowGrid(False)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSortingEnabled(True)
        table.horizontalHeader().setSectionsClickable(True)
        table.horizontalHeader().setStretchLastSection(False)
        table.setWordWrap(False)
        table.setTextElideMode(Qt.TextElideMode.ElideRight)
        return table

    def build_sessions_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 2, 0, 0)
        layout.setSpacing(12)
        self.session_table = self.create_table(
            ["开始时间", "会话 / 工作目录", "模型", "任务轮次", "非缓存输入", "缓存输入", "缓存命中率", "输出", "API 调用", "估算费用"]
        )
        header = self.session_table.horizontalHeader()
        header.setMinimumSectionSize(42)
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        for column, width in ((1, 250), (2, 108)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
            header.resizeSection(column, width)
        header.moveSection(header.visualIndex(1), 0)
        header.moveSection(header.visualIndex(9), 1)
        header.setStretchLastSection(True)
        header.setSortIndicator(0, Qt.SortOrder.DescendingOrder)
        self.session_table.doubleClicked.connect(self.open_selected_session)
        layout.addWidget(self.session_table, 1)
        self.session_summary = QLabel("")
        self.session_summary.setObjectName("muted")
        layout.addWidget(self.session_summary)
        return page

    def build_models_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 2, 0, 0)
        layout.setSpacing(12)
        caption = QLabel("同一模型的所有会话合并；未配置价格时仍统计 tokens，但不计入金额。")
        caption.setObjectName("muted")
        layout.addWidget(caption)
        self.model_table = self.create_table(
            ["模型", "输入单价", "缓存单价", "输出单价", "非缓存输入", "缓存输入", "输出", "API 调用", "估算费用"]
        )
        self.model_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, 9):
            self.model_table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.model_table, 1)
        edit = QPushButton("编辑模型价格")
        edit.setObjectName("secondaryButton")
        edit.clicked.connect(self.edit_prices)
        layout.addWidget(edit, alignment=Qt.AlignmentFlag.AlignLeft)
        return page

    def build_days_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 2, 0, 0)
        layout.setSpacing(12)
        caption = QLabel("按本机时区的自然日汇总 API 调用；可结合模型与路径筛选。")
        caption.setObjectName("muted")
        layout.addWidget(caption)
        self.day_table = self.create_table(
            ["日期", "会话数", "非缓存输入", "缓存输入", "输出", "API 调用", "估算费用"]
        )
        for column in range(7):
            self.day_table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.day_table, 1)
        return page

    def create_actions(self):
        for text, shortcut, slot in (
            ("选择目录", QKeySequence.StandardKey.Open, self.browse),
            ("重新扫描", QKeySequence.StandardKey.Refresh, self.start_scan),
            ("模型价格", "Ctrl+,", self.edit_prices),
            ("退出", QKeySequence.StandardKey.Quit, self.close),
        ):
            action = QAction(text, self)
            action.setShortcut(shortcut)
            action.triggered.connect(slot)
            self.addAction(action)

    def browse(self):
        initial = str(self.root_path if self.root_path.is_dir() else self.root_path.parent)
        directory = QFileDialog.getExistingDirectory(self, "选择 Codex sessions 目录", initial)
        if directory:
            self.source_edit.setText(directory)
            self.start_scan()

    def browse_file(self):
        initial = str(self.root_path if self.root_path.is_dir() else self.root_path.parent)
        filename, _ = QFileDialog.getOpenFileName(self, "选择一个 Codex JSONL", initial, "Codex sessions (*.jsonl)")
        if filename:
            self.source_edit.setText(filename)
            self.start_scan()

    def start_scan_if_needed(self):
        if not self.closing:
            self.start_scan()

    def start_scan(self):
        if self.closing:
            return
        self.cancel_scan()
        self.scan_generation += 1
        source = self.source_edit.text().strip()
        if source.startswith('"') and source.endswith('"'):
            source = source[1:-1]
        if not source:
            self.scan_failed("请输入目录路径，或选择一个 .jsonl 文件。")
            self.source_edit.setFocus()
            return
        self.root_path = Path(source)
        self.set_scanning(True)
        thread = QThread(self)
        worker = ScanWorker(self.root_path)
        worker.moveToThread(thread)
        self.scan_threads[thread] = worker
        self.scan_worker = worker
        generation = self.scan_generation
        thread.started.connect(worker.run, Qt.ConnectionType.QueuedConnection)
        worker.progressed.connect(lambda current, total: self.update_progress(current, total, generation))
        worker.finished.connect(lambda result: self.scan_finished(result, generation))
        worker.failed.connect(lambda message: self.scan_failed(message, generation))
        worker.finished.connect(thread.quit, Qt.ConnectionType.DirectConnection)
        worker.failed.connect(thread.quit, Qt.ConnectionType.DirectConnection)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(lambda: self.scan_thread_finished(thread, generation))
        thread.start()

    def cancel_scan(self):
        if self.scan_worker:
            self.scan_worker.cancel()
        self.scan_worker = None

    def scan_thread_finished(self, thread: QThread, generation: int):
        self.scan_threads.pop(thread, None)
        if self.scan_generation == generation:
            self.scan_worker = None
        thread.deleteLater()
        if self.closing and not self.scan_threads:
            self.close()

    def update_progress(self, current: int, total: int, generation: int):
        if self.closing or generation != self.scan_generation:
            return
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(current)
        self.status.setText(f"正在读取 {current} / {total} 个文件…")

    def scan_failed(self, message: str, generation: int | None = None):
        if self.closing or (generation is not None and generation != self.scan_generation):
            return
        self.scan_error = message
        self.set_scanning(False)
        self.status.setText("扫描失败 · 当前显示上次扫描结果" if self.sessions else "扫描失败")
        self.status.setToolTip(html_escape(message))
        self.scan_details_button.show()
        self.update_empty_state()

    def scan_finished(self, result: ScanResult, generation: int | None = None):
        if self.closing or (generation is not None and generation != self.scan_generation):
            return
        self.scan_report = result
        self.scan_error = ""
        self.set_scanning(False)
        self.sessions = result.sessions
        selected = self.model_filter.currentData() or ""
        models = sorted({record.model for session in self.sessions for record in session.records})
        self.model_filter.blockSignals(True)
        self.model_filter.clear()
        self.model_filter.addItem("全部模型", "")
        for model in models:
            self.model_filter.addItem(short_model(model), model)
        index = self.model_filter.findData(selected)
        self.model_filter.setCurrentIndex(max(0, index))
        self.model_filter.blockSignals(False)
        self.apply_filters()
        errors = result.parse_errors
        no_usage = sum(not session.records for session in result.sessions)
        subagents = sum(session.is_subagent for session in result.sessions)
        parts = [f"{result.file_count} 个 JSONL", f"识别到 {len(result.sessions)} 个会话"]
        if subagents:
            parts.append(f"其中 {subagents} 个子智能体")
        if no_usage:
            parts.append(f"{no_usage} 个无用量")
        if result.skipped_files:
            parts.append(f"跳过 {result.skipped_files} 个文件")
        if errors:
            parts.append(f"{len(errors)} 处读取或格式问题")
        self.status.setText(" · ".join(parts))
        self.status.setToolTip(f"本机时区 {datetime.now().astimezone().tzname()}")
        self.scan_details_button.setVisible(bool(result.skipped_files or errors))

    def show_scan_details(self):
        sections = [f"扫描位置\n{self.root_path}"]
        if self.scan_error:
            sections.append(f"读取失败\n{self.scan_error}")
        elif self.scan_report:
            report = self.scan_report
            if report.skipped:
                sections.append(f"跳过的文件（{report.skipped_files}）\n" + "\n".join(report.skipped))
            errors = report.parse_errors
            if errors:
                sections.append(f"读取或格式问题（{len(errors)}）\n" + "\n".join(errors))
        dialog = QDialog(self)
        dialog.setWindowTitle("JSONL 扫描详情")
        dialog.resize(760, 440)
        layout = QVBoxLayout(dialog)
        details = QPlainTextEdit("\n\n".join(sections))
        details.setReadOnly(True)
        layout.addWidget(details)
        buttons = QDialogButtonBox()
        buttons.addButton("关闭", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.accepted.connect(dialog.accept)
        layout.addWidget(buttons)
        dialog.exec()

    def set_scanning(self, scanning: bool):
        self.is_scanning = scanning
        self.refresh_button.setText("正在扫描…" if scanning else "重新扫描")
        self.refresh_button.setEnabled(not scanning)
        self.export_button.setEnabled(not scanning and bool(self.filtered))
        self.progress.setVisible(scanning)
        if scanning:
            self.scan_details_button.hide()
            self.empty_state.hide()
            self.status.setToolTip("")
            self.progress.setRange(0, 0)
            self.status.setText("正在查找 JSONL 文件…")

    def update_empty_state(self):
        message = ""
        if not self.is_scanning and not self.filtered:
            if self.sessions:
                message = "当前筛选没有匹配的会话。可调整日期、模型、会话类型或搜索词。"
            elif self.scan_error:
                message = "请选择可读取的 Codex sessions 目录或 JSONL 文件。"
            elif self.scan_report:
                message = (
                    "识别到 0 个 Codex 会话。打开扫描详情查看各文件的识别结果。"
                    if self.scan_report.file_count
                    else "这里没有 JSONL 文件。请选择 Codex sessions 目录或单个 JSONL 文件。"
                )
        self.empty_state.setText(message)
        self.empty_state.setVisible(bool(message))

    def period_changed(self):
        custom = self.period.currentText() == "自定义范围"
        self.date_filter_bar.setVisible(custom)
        if self.sessions:
            dates = [record.timestamp.astimezone().date() for session in self.sessions for record in session.records]
            if dates:
                self.start_date.setDate(QDate(min(dates)))
                self.end_date.setDate(QDate(max(dates)))
        self.apply_filters()

    def date_range(self) -> tuple[date | None, date | None]:
        period = self.period.currentText()
        today = datetime.now().astimezone().date()
        if period == "全部时间":
            return None, None
        if period == "今天":
            return today, today
        if period == "最近 7 天":
            return today.fromordinal(today.toordinal() - 6), today
        if period == "最近 30 天":
            return today.fromordinal(today.toordinal() - 29), today
        start = self.start_date.date().toPyDate()
        end = self.end_date.date().toPyDate()
        return (min(start, end), max(start, end))

    def apply_filters(self):
        start, end = self.date_range()
        self.filtered = filtered_sessions(
            self.sessions,
            start,
            end,
            self.model_filter.currentData() or "",
            self.search.text(),
            subagent=self.session_kind_filter.currentData(),
        )
        self.refresh_data()

    def refresh_data(self):
        records = [record for session in self.filtered for record in session.records]
        usage = sum((record.usage for record in records), Usage())
        cost, unpriced_tokens, unknown_models = aggregate_cost(records, self.config)
        self.total_card.update(
            money(cost, self.config.currency_symbol) if records else "—",
            f"{self.config.currency} · {len(self.filtered)} 个会话 · 用户价格估算",
        )
        self.call_card.update(tokens(len(records)), "来自新版与旧版用量记录")
        self.token_card.update(tokens(usage.total_tokens), f"输入 {tokens(usage.input_tokens)} + 输出 {tokens(usage.output_tokens)}")
        ceiling = cache_ceiling_many(session.records for session in self.filtered)
        self.cache_card.update(cache_rate_text(usage), f"缓存命中 {tokens(usage.cached_input_tokens)} tokens")
        self.cache_card.setToolTip(
            f"实际 {ceiling.actual_rate:.1%}（{tokens(ceiling.actual_cached)} / {tokens(usage.input_tokens)}）\n"
            f"理论上限·只复用上一次输入：{ceiling.prompt_only_rate:.1%}\n"
            f"理论上限·复用上一次输入+输出：{ceiling.with_output_rate:.1%}\n"
            "每个会话单独配对，跨会话不互相计入。"
        )
        self.breakdown_values["uncached"].setText(tokens(usage.uncached_input_tokens))
        self.breakdown_values["cached"].setText(tokens(usage.cached_input_tokens))
        self.breakdown_values["output"].setText(tokens(usage.output_tokens))
        self.breakdown_values["reasoning"].setText(
            f"{tokens(usage.reasoning_output_tokens)}（{usage.reasoning_rate:.1%}）"
            if usage.output_tokens
            else "—"
        )
        if unknown_models:
            price_warning = (
                f"未定价：{'、'.join(sorted(unknown_models))}\n"
                f"另有 {tokens(unpriced_tokens)} tokens 未计入支出。请配置对应模型。"
            )
        else:
            model_prices = (self.config.find(model) for model in {record.model for record in records})
            has_zero_price = any(price and price.input == price.cached_input == price.output == 0 for price in model_prices)
            price_warning = "当前部分 API 调用按配置单价 0 计算。" if has_zero_price else ""
        self.unpriced.setText(price_warning)
        self.unpriced.setVisible(bool(price_warning))
        self.populate_recent()
        self.populate_sessions()
        self.populate_models()
        self.populate_days()
        self.export_button.setEnabled(not self.is_scanning and bool(self.filtered))
        self.update_empty_state()

    def session_values(self, session: Session):
        usage = session.usage
        cost, unpriced, unknown = aggregate_cost(session.records, self.config)
        return usage, cost, unpriced, unknown

    def session_cost_text(self, cost: Decimal, unpriced: int) -> str:
        if unpriced:
            return f"≥ {money(cost, self.config.currency_symbol)}"
        return money(cost, self.config.currency_symbol)

    def add_session_row(self, table: QTableWidget, row: int, session: Session, compact=False):
        usage, cost, unpriced, _ = self.session_values(session)
        title = session.title
        if title != session.workspace:
            title += f"  ·  {session.workspace}"
        name = table_item(title)
        name.setData(Qt.ItemDataRole.UserRole, session)
        blocks = [
            ("会话", "#11745F", session.title),
            ("工作目录", MUTED, session.cwd or "—"),
            ("会话 ID", MUTED, session.session_id),
            ("JSONL 文件", MUTED, str(session.path)),
        ]
        if session.subagent and session.subagent.parent_thread_id:
            blocks.append(("父会话 ID", MUTED, session.subagent.parent_thread_id))
        if not session.records:
            blocks.append(("用量", MUTED, "日志中暂无用量记录。"))
        set_hover_html(name, tooltip_html(blocks))
        cost_item = NumericItem(self.session_cost_text(cost, unpriced) if session.records else "—", cost)
        if unpriced:
            cost_item.setForeground(QColor("#AF5B32"))
            cost_item.setToolTip(f"{tokens(unpriced)} tokens 未配置价格")
        if compact:
            values = [
                name,
                table_item(display_time(session.start)),
                model_names_item(session.models),
                NumericItem(tokens(usage.total_tokens), usage.total_tokens),
                cache_rate_item(usage, session.records),
                cost_item,
            ]
        else:
            values = [
                table_item(display_time(session.start)),
                name,
                model_names_item(session.models),
                NumericItem(tokens(session.turn_count), session.turn_count),
                NumericItem(tokens(usage.uncached_input_tokens), usage.uncached_input_tokens),
                NumericItem(tokens(usage.cached_input_tokens), usage.cached_input_tokens),
                cache_rate_item(usage, session.records),
                output_item(usage),
                NumericItem(tokens(len(session.records)), len(session.records)),
                cost_item,
            ]
        for column, item in enumerate(values):
            table.setItem(row, column, item)

    def populate_recent(self):
        recent = self.filtered[:4]
        self.recent_table.setRowCount(len(recent))
        for row, session in enumerate(recent):
            self.add_session_row(self.recent_table, row, session, compact=True)

    def populate_sessions(self):
        self.session_table.setSortingEnabled(False)
        self.session_table.setRowCount(len(self.filtered))
        for row, session in enumerate(self.filtered):
            self.add_session_row(self.session_table, row, session)
        self.session_table.setSortingEnabled(True)
        self.session_summary.setText(
            f"显示 {len(self.filtered)} / {len(self.sessions)} 个会话 · "
            f"合计 {sum(session.turn_count for session in self.filtered)} 个任务轮次 · "
            "双击任一行查看任务轮次与模型拆分"
        )

    def group_records(self, key: Callable[[UsageRecord], str]) -> dict[str, list[UsageRecord]]:
        groups: dict[str, list[UsageRecord]] = {}
        for session in self.filtered:
            for record in session.records:
                groups.setdefault(key(record), []).append(record)
        return groups

    def populate_models(self):
        table = self.model_table
        table.setSortingEnabled(False)
        table.setRowCount(0)
        for model, records in sorted(self.group_records(lambda record: record.model).items()):
            usage = sum((record.usage for record in records), Usage())
            price = self.config.find(model)
            cost, unpriced, _ = aggregate_cost(records, self.config)
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 0, table_item(short_model(model), color=None if price else "#AF5B32"))
            prices = [price.input, price.cached_input, price.output] if price else [Decimal(0)] * 3
            for column, value in enumerate(prices, 1):
                table.setItem(row, column, table_item(money(value) if price else "未配置", True))
            for column, value in (
                (4, usage.uncached_input_tokens),
                (5, usage.cached_input_tokens),
                (7, len(records)),
            ):
                table.setItem(row, column, NumericItem(tokens(value), value))
            table.setItem(row, 6, output_item(usage))
            table.setItem(row, 8, NumericItem("未配置价格" if unpriced else money(cost, self.config.currency_symbol), cost))
        table.setSortingEnabled(True)

    def populate_days(self):
        table = self.day_table
        table.setSortingEnabled(False)
        table.setRowCount(0)
        groups = self.group_records(lambda record: record.timestamp.astimezone().strftime("%Y-%m-%d"))
        day_sessions: dict[str, set[str]] = {}
        for session in self.filtered:
            for record in session.records:
                day = record.timestamp.astimezone().strftime("%Y-%m-%d")
                day_sessions.setdefault(day, set()).add(session.session_id)
        for day, records in sorted(groups.items(), reverse=True):
            usage = sum((record.usage for record in records), Usage())
            cost, unpriced, _ = aggregate_cost(records, self.config)
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 0, table_item(day))
            table.setItem(row, 1, NumericItem(tokens(len(day_sessions[day])), len(day_sessions[day])))
            for column, value in (
                (2, usage.uncached_input_tokens),
                (3, usage.cached_input_tokens),
                (5, len(records)),
            ):
                table.setItem(row, column, NumericItem(tokens(value), value))
            table.setItem(row, 4, output_item(usage))
            table.setItem(row, 6, NumericItem(self.session_cost_text(cost, unpriced), cost))
        table.setSortingEnabled(True)
        self.chart.set_values(
            [
                (day[5:], aggregate_cost(records, self.config)[0])
                for day, records in sorted(groups.items())
            ],
            self.config.currency_symbol,
        )

    def open_selected_session(self):
        table = self.session_table if self.current_page == 1 else self.recent_table
        row = table.currentRow()
        item = table.item(row, 1 if self.current_page == 1 else 0)
        if item is None:
            return
        session = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(session, Session):
            parent_title = ""
            if session.subagent and session.subagent.parent_thread_id:
                parent_id = session.subagent.parent_thread_id
                parent_title = next((entry.title for entry in self.sessions if entry.session_id == parent_id), "")
            SessionDialog(session, self.config, self, parent_title=parent_title).exec()

    def edit_prices(self):
        dialog = PriceDialog(self.config, self.config_path, self)
        dialog.saved.connect(self.prices_changed)
        dialog.exec()

    def prices_changed(self, config: PriceConfig):
        self.config = config
        self.refresh_data()
        self.status.setText("价格已保存，已按新单价重新计算")

    def show_help(self):
        QMessageBox.information(
            self,
            "计费口径与说明",
            "数据源\n"
            "逐行读取 Codex sessions 的 JSONL，不修改原日志，不上传对话。\n\n"
            "去重与累加\n"
            "优先使用 token_usage_record.payload.usage（每次 API 调用的增量），按 response_id 去重。"
            "turn_token_usage、thread_token_usage 和 token_count.total_token_usage 都是累计快照，不会重复相加。"
            "旧版只有 token_count 时，使用 last_token_usage，或根据累计值计算差分。\n\n"
            "Token 口径\n"
            "Codex 的 input_tokens 包含 cached_input_tokens。输出 token 已包含 reasoning_output_tokens，"
            "不另外计费。cache_write_input_tokens 目前不参与计算。\n\n"
            "金额口径\n"
            "(输入 − 缓存输入) × 输入价 + 缓存输入 × 缓存价 + 输出 × 输出价，再除以 1,000,000。"
            "价格与货币符号均可配置。结果是 API 标价的本地估算，不等于订阅账单、优惠或实际扣款。\n\n"
            f"配置文件\n{self.config_path}",
        )

    def export_csv(self):
        if not self.filtered:
            QMessageBox.information(self, "没有可导出的数据", "当前筛选范围没有带 token 用量的会话。")
            return
        default_name = f"codex-usage-{datetime.now():%Y%m%d-%H%M}.csv"
        filename, _ = QFileDialog.getSaveFileName(self, "导出当前筛选结果", default_name, "CSV 文件 (*.csv)")
        if not filename:
            return
        try:
            with Path(filename).open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow([
                    "开始时间", "结束时间", "会话ID", "工作目录", "模型", "任务轮次", "输入tokens",
                    "缓存输入tokens", "缓存命中率", "非缓存输入tokens", "输出tokens",
                    "其中思考tokens", "API调用数", "已定价费用", "货币", "未定价tokens", "JSONL文件",
                    "会话类型", "父会话ID", "智能体名称", "智能体路径",
                ])
                for session in self.filtered:
                    usage, cost, unpriced, _ = self.session_values(session)
                    agent = session.subagent
                    writer.writerow([
                        display_time(session.start), display_time(session.end), session.session_id,
                        session.cwd, "; ".join(session.models), session.turn_count, usage.input_tokens,
                        usage.cached_input_tokens, cache_rate_text(usage),
                        usage.uncached_input_tokens, usage.output_tokens,
                        usage.reasoning_output_tokens,
                        len(session.records), format(cost, "f"), self.config.currency,
                        unpriced, str(session.path),
                        "子智能体" if agent else "主会话", agent.parent_thread_id if agent else "",
                        agent.nickname if agent else "", agent.path if agent else "",
                    ])
        except OSError as exc:
            QMessageBox.critical(self, "导出失败", str(exc))
            return
        self.status.setText(f"已导出 {len(self.filtered)} 个会话：{filename}")
        QMessageBox.information(self, "导出完成", f"已导出当前筛选的 {len(self.filtered)} 个会话。\n\n{filename}")

    def change_page(self, index: int):
        # 侧栏先于主区域构建，初始化选中项时尚无 stack。
        if index < 0 or not hasattr(self, "stack"):
            return
        if index != self.nav.currentRow():
            self.nav.setCurrentRow(index)
            return
        self.current_page = index
        self.stack.setCurrentIndex(index)
        self.heading.setText(self.nav.item(index).text())

    def closeEvent(self, event: QCloseEvent):
        self.closing = True
        self.cancel_scan()
        if self.scan_threads:
            event.ignore()
        else:
            event.accept()


STYLE = """
QWidget { color: #1E2C32; font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif; font-size: 13px; }
QMainWindow, QDialog { background: #F3F6F7; }
QWidget#centralwidget { background: #F3F6F7; }
#sidebar { background: #1B2A30; }
#brandMark { color: #7DD9BF; font-family: "Cascadia Mono", monospace; font-size: 20px; font-weight: 700; }
#brand { color: #F2F6F5; font-family: "Cascadia Mono", monospace; font-size: 15px; font-weight: 700; line-height: 1.3; }
#sideDivider { background: #35464C; }
#navigation { background: transparent; border: none; outline: none; padding: 0; }
#navigation::item { color: #AFC0C5; border-radius: 7px; padding: 0 12px; }
#navigation::item:hover { background: #253A41; color: #EAF4F1; }
#navigation::item:selected { background: #2C514A; color: #E8FFF7; border-left: 3px solid #6CD2B3; }
#sideButton { background: transparent; color: #BBD0D4; border: none; border-radius: 6px; padding: 11px; text-align: left; }
#sideButton:hover { background: #253A41; color: white; }
#pageTitle { font-size: 26px; font-weight: 700; }
#dialogTitle { font-size: 22px; font-weight: 700; }
#sectionTitle { font-size: 14px; font-weight: 700; }
#eyebrow { color: #69777D; font-size: 11px; font-weight: 600; }
#heroValue { color: #11745F; font-family: "Cascadia Mono", "Segoe UI", sans-serif; font-size: 29px; font-weight: 700; }
#metricValue { color: #25353C; font-family: "Cascadia Mono", "Segoe UI", sans-serif; font-size: 21px; font-weight: 700; }
#muted { color: #69777D; font-size: 12px; }
#emptyState { color: #3B545B; background: #EDF5F3; border: 1px solid #D2E3DF; border-radius: 7px; padding: 9px 12px; }
#warning { color: #A76036; font-size: 12px; background: #FFF5E9; border-radius: 5px; padding: 9px; }
#breakdownValue { color: #3B4C53; font-family: "Cascadia Mono", "Segoe UI", sans-serif; font-size: 13px; }
#breakdownKey { color: #69777D; font-size: 12px; }
QFrame#card, QFrame#filterBar { background: white; border: 1px solid #DFE5E7; border-radius: 10px; }
QPushButton { min-height: 34px; padding: 0 14px; border-radius: 6px; font-weight: 600; }
QPushButton#primaryButton { color: white; background: #138A72; border: 1px solid #138A72; }
QPushButton#primaryButton:hover { background: #0F7763; }
QPushButton#primaryButton:pressed { background: #0C6554; }
QPushButton#secondaryButton { color: #32464D; background: white; border: 1px solid #D6DFE1; }
QPushButton#secondaryButton:hover { background: #EDF6F3; border-color: #8DBAAC; }
QPushButton#textButton { color: #138A72; background: transparent; border: none; padding: 5px 0; text-align: left; }
QPushButton#textButton:hover { color: #075A48; }
QPushButton#iconButton { color: #7D888C; background: transparent; border: 1px solid #DFE5E7; font-size: 18px; padding: 0; }
QPushButton:disabled { color: #8B989D; background: #D8E2DF; border-color: #D8E2DF; }
QPushButton:focus, QLineEdit:focus, QComboBox:focus, QDateEdit:focus { border: 1px solid #138A72; }
QLineEdit, QComboBox, QDateEdit { background: white; border: 1px solid #D7E0E2; border-radius: 6px; min-height: 34px; padding: 0 10px; selection-background-color: #BCE2D6; }
QPlainTextEdit { background: white; border: 1px solid #DFE5E7; border-radius: 7px; selection-background-color: #BCE2D6; }
QComboBox::drop-down, QDateEdit::drop-down { border: none; width: 25px; }
QComboBox QAbstractItemView { background: white; border: 1px solid #D7E0E2; selection-background-color: #DDF1EB; selection-color: #1E2C32; padding: 4px; }
QTableWidget { background: white; alternate-background-color: #F8FAFA; border: 1px solid #DFE5E7; border-radius: 8px; gridline-color: transparent; selection-background-color: #DFF1EB; selection-color: #1E2C32; }
QTableWidget::item { border-bottom: 1px solid #EDF0F1; padding: 0 8px; }
QTableWidget::item:selected { background: #DFF1EB; }
QHeaderView::section { color: #66767C; background: #F5F8F8; border: none; border-bottom: 1px solid #DFE5E7; padding: 10px 8px; font-size: 11px; font-weight: 600; }
QTableWidget#compactTable { border: none; border-radius: 0; }
QTableWidget#compactTable::item { padding: 0 4px; }
QProgressBar { border: none; background: #DCE5E5; border-radius: 3px; max-height: 6px; }
QProgressBar::chunk { background: #138A72; border-radius: 3px; }
QScrollBar:vertical { background: #EEF2F3; width: 12px; margin: 0; border-radius: 6px; }
QScrollBar::handle:vertical { background: #BFCCCF; border-radius: 5px; min-height: 30px; margin: 2px; }
QScrollBar::handle:vertical:hover { background: #93ABA9; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; width: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QToolTip { color: #1E2C32; background: white; border: 1px solid #A9B9BC; padding: 5px; }
QFrame#hoverPanel { background: #FBFCFC; border: 1px solid #A9B9BC; border-radius: 7px; }
QScrollArea#hoverScroll { background: transparent; border: none; }
QLabel#hoverText { background: transparent; color: #1E2C32; padding: 8px 10px; }
QTabWidget::pane { background: white; border: 1px solid #DFE5E7; border-radius: 8px; top: -1px; }
QTabBar::tab { color: #66767C; background: #EDF1F2; border: 1px solid #DFE5E7; border-bottom: none; border-top-left-radius: 7px; border-top-right-radius: 7px; padding: 8px 18px; margin-right: 3px; font-weight: 600; }
QTabBar::tab:hover { background: #E3ECEA; color: #32464D; }
QTabBar::tab:selected { color: #11745F; background: white; border-color: #DFE5E7; }
QTabBar::tab:selected:hover { background: white; }
QScrollArea { background: transparent; border: none; }
QScrollArea > QWidget > QWidget { background: transparent; }
QScrollArea#contentScroll { background: transparent; border: none; }
QScrollArea#contentScroll > QWidget > QWidget { background: transparent; }
QListWidget#navigation QScrollBar { width: 0; height: 0; margin: 0; }
"""


def apply_light_theme(app: QApplication) -> None:
    """强制浅色调色板，避免跟随 Windows 深色模式。"""
    app.styleHints().setColorScheme(Qt.ColorScheme.Light)
    palette = QPalette()
    white = QColor("#FFFFFF")
    window = QColor("#F3F6F7")
    text = QColor("#1E2C32")
    disabled = QColor("#8B989D")
    palette.setColor(QPalette.ColorRole.Window, window)
    palette.setColor(QPalette.ColorRole.WindowText, text)
    palette.setColor(QPalette.ColorRole.Base, white)
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#F8FAFA"))
    palette.setColor(QPalette.ColorRole.ToolTipBase, white)
    palette.setColor(QPalette.ColorRole.ToolTipText, text)
    palette.setColor(QPalette.ColorRole.Text, text)
    palette.setColor(QPalette.ColorRole.Button, white)
    palette.setColor(QPalette.ColorRole.ButtonText, text)
    palette.setColor(QPalette.ColorRole.BrightText, QColor("#B3261E"))
    palette.setColor(QPalette.ColorRole.Link, QColor(ACCENT))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#DFF1EB"))
    palette.setColor(QPalette.ColorRole.HighlightedText, text)
    palette.setColor(QPalette.ColorRole.PlaceholderText, disabled)
    for role in (
        QPalette.ColorRole.Text,
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.ButtonText,
    ):
        palette.setColor(QPalette.ColorGroup.Disabled, role, disabled)
    app.setPalette(palette)
    if sys.platform == "win32":
        light_title_bars.install(app)


class LightTitleBarFilter(QObject):
    """Windows 下把标题栏强制为浅色，避免白窗体配黑色标题栏。"""

    def install(self, app: QApplication):
        app.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Show and isinstance(watched, QWidget):
            self.apply(watched)
        return False

    @staticmethod
    def apply(widget: QWidget):
        try:
            import ctypes

            disable_dark = ctypes.c_int(0)
            handle = int(widget.winId())
            # 20 是当前 DWMWA_USE_IMMERSIVE_DARK_MODE，19 是旧版本编号。
            for attribute in (20, 19):
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    handle, attribute, ctypes.byref(disable_dark), ctypes.sizeof(disable_dark)
                )
        except (AttributeError, OSError, ValueError):
            # 标题栏颜色只是外观细节，失败不影响功能。
            pass


light_title_bars = LightTitleBarFilter()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Codex Session Ledger · Qt 6 本地 token 费用账本")
    parser.add_argument("--sessions", type=Path, default=default_sessions_path(), help="sessions 目录或单个 JSONL 文件")
    parser.add_argument("--config", type=Path, default=default_config_path(), help="模型价格 JSON 配置")
    args = parser.parse_args(argv)
    app = QApplication(sys.argv[:1])
    app.setApplicationName("Codex Session Ledger")
    app.setOrganizationName("LocalTools")
    # Qt 6.8+ 默认跟随系统配色；本程序只有浅色设计，必须显式锁定，
    # 否则 Windows 深色模式下会混入深色控件和深色标题栏。
    app.setStyle("Fusion")
    apply_light_theme(app)
    app.setStyleSheet(STYLE)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        QMessageBox.critical(None, "价格配置无法读取", str(exc))
        return 2
    window = LedgerWindow(args.sessions, args.config, config)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
