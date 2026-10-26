# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""米家风格的设备卡片。

卡片承担两级操作：右侧圆形电源钮直接开关（快速路径），点击卡片本体
打开详情面板（完整控制）。布局为纯文字：左侧设备名与副标题（房间，
可附带温湿度读数），右侧垂直居中的电源钮，无装饰图标。

设备是否支持开关在列表接口里拿不到：电源钮初始隐藏，后台批量探测
确认有可写开关属性后才显示并回填真实状态。
"""

from pathlib import Path

from pathlib import Path

from PySide6.QtCore import QEvent, QRect, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPixmap
from PySide6.QtWidgets import (
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from app.siui.components.container import SiRowCard

from app.core.models import DeviceInfo
from app.ui.power_button import PowerButton
from app.ui.si_theme import SiColors

_POWER_BTN_SIZE = 32
_ICON_SIZE = 32
# 卡片固定尺寸与主窗口默认一排放几列联动（见 MainWindow._default_window_size）。
# 200×92 → 180×83：整体紧凑 10%，默认窗口宽度按 4 列反推
_CARD_FIXED_WIDTH = 180
_CARD_FIXED_HEIGHT = 83

# 离线卡片的灰置配色：背景更暗、文字更灰，hover 不提亮


class DeviceCard(SiRowCard):
    power_clicked = Signal(str)  # did
    open_requested = Signal(str)  # did

    def __init__(self, device: DeviceInfo, parent=None, show_room: bool = True,
                 channel_count: int = 0, power_enabled: bool = True):
        super().__init__(parent, self.LeftToRight)
        self.device = device
        self._busy = False
        self._hovered = False
        self._online = device.online
        # 「全屋」视图按房间分段后，段内每张卡都重复房间名只是噪音，
        # 由调用方传 False 关掉；读数与离线标记仍然照常显示
        self._show_room = show_room
        # 多通道设备合并展示时，父卡代表整台设备：副标题标出通道数
        self._channels = channel_count
        # 父设备本身没有独立的那一路（其规格只有各通道的 on-2..on-N），
        # 给它挂电源钮必然控到第一路，与「按键1」卡片重复——直接不给
        self._power_enabled = power_enabled
        # 设备名按实际宽度缩过字号没有（只在首次显示时算一次）
        self._name_fitted = False
        # 实时读数（None=还没有）；真正写进副标题要等布局给出宽度
        self._metrics_text: str | None = None

        self.style_data.background_color = QColor(
            SiColors.OFFLINE_CARD if not self._online else SiColors.CARD)
        self.style_data.border_radius = 14.0
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        # 固定尺寸：宽度固定，窗口缩放仅改变列数，卡片本身不被拉伸
        self.setFixedSize(_CARD_FIXED_WIDTH, _CARD_FIXED_HEIGHT)
        self.setCursor(Qt.PointingHandCursor)
        self.muteStretchWidget()

        self._power_btn = PowerButton(_POWER_BTN_SIZE, icon_size=24)
        self._power_btn.clicked.connect(lambda: self.power_clicked.emit(device.did))
        # 列表接口不带开关信息，先隐藏电源钮；后台探测确认设备
        # 有可写开关属性后再显示并回填真实状态
        self._power_btn.hide()
        self._power_btn.set_online(self._online)

        name_color = SiColors.TEXT_PRIMARY if self._online else SiColors.OFFLINE_TEXT
        self._name_color = name_color
        self._name_label = QLabel(device.name)
        # 全局 QSS 的 `* { font-size: 10pt }` 优先级高于 QWidget.setFont()，
        # 名称一直就是 10pt（setFont(15) 从来没生效过）。字号统一走样式表，
        # 这里保持与正文一致的 10pt，只把"过长就溢出卡片"的问题修掉
        self._name_label.setStyleSheet(
            f"color: {name_color}; background: transparent;"
            " font-size: 10pt; font-weight: 600;")
        # 允许换行：长名称不再把所在列撑宽，卡片宽度保持整齐
        self._name_label.setWordWrap(True)
        self._name_label.setToolTip(device.name)
        self._name_label.installEventFilter(self)

        self._sub_label = QLabel(self._sub_text())
        self._sub_label.setFont(QFont("Microsoft YaHei UI", 11))
        sub_color = SiColors.TEXT_SECONDARY if self._online else SiColors.OFFLINE_SUB
        self._sub_label.setStyleSheet(
            f"color: {sub_color}; background: transparent;")
        # 离线卡片电源钮不可操作
        if not self._online:
            self._power_btn.setEnabled(False)

        # 设备图标：后台拉取就绪后由 set_icon 填入，未就绪前保持隐藏
        self._icon_label = QLabel()
        self._icon_label.setFixedSize(_ICON_SIZE, _ICON_SIZE)
        self._icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._icon_label.hide()

        # 左侧文字列整体垂直居中，与右侧电源钮同一水平轴
        text_col = QWidget()
        text_col.setAttribute(Qt.WA_TranslucentBackground)
        text_lay = QVBoxLayout(text_col)
        text_lay.setContentsMargins(0, 0, 0, 0)
        text_lay.setSpacing(4)
        text_lay.addStretch(1)
        text_lay.addWidget(self._name_label)
        text_lay.addWidget(self._sub_label)
        text_lay.addStretch(1)

        lay = self.layout()
        lay.setContentsMargins(14, 10, 18, 10)
        lay.addWidget(self._icon_label, alignment=Qt.AlignVCenter)
        lay.addSpacing(10)
        # 文字列吃掉图标与电源钮之间的全部剩余宽度：以前它后面跟着
        # addStretch(1)，文字列只剩 sizeHint 宽（短名字才 26px），
        # 读数"26.4°C 59%"被裁掉一截、长名字也更容易溢出
        lay.addWidget(text_col, stretch=1)
        lay.addWidget(self._power_btn, alignment=Qt.AlignVCenter)

    # ---------- 名称排版 ----------

    def showEvent(self, event) -> None:  # noqa: N802 (Qt 命名约定)
        super().showEvent(event)
        # 卡片是固定尺寸，长名字（"Yeelight Tube Plus2屏幕挂灯"）用 15pt
        # 换行后能撑出三四行、直接盖到卡片外面。宽度此刻已知，按两行上限
        # 逐级降字号，比截断保留更多信息
        if not self._name_fitted:
            self._name_fitted = True
            self._fit_name_font()
        self._sync_sub()

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt 命名约定)
        super().resizeEvent(event)
        # 卡片固定尺寸下只发生一次；滚动条显隐改变视口宽度时也会进来
        self._fit_name_font()
        self._sync_sub()

    def _fit_name_font(self) -> None:
        """名称最多两行，放不下就把尾部截掉加省略号。

        卡片是固定尺寸（200×92），设备名却可能很长
        （"Yeelight Tube Plus2屏幕挂灯"）。以前直接换行会撑到三四行、
        盖到卡片外面；这里按当前宽度算两行能放下多少字，插件的宽度变化
        （电源钮探测出来后才显示）也跟着重算。
        """
        width = self._name_label.width()
        if width <= 0:
            return
        metrics = QFontMetrics(QFont("Microsoft YaHei UI", 10, QFont.Weight.DemiBold))
        # 两行的高度上限，留 2px 余量：boundingRect 的 2 行结果比
        # 2×lineSpacing 略高，卡死等于只允许一行（实测全被截成单行）
        max_height = 2 * metrics.lineSpacing() + 2
        text = self.device.name
        rect = metrics.boundingRect(
            QRect(0, 0, width, 10000), Qt.TextFlag.TextWordWrap, text)
        if rect.height() > max_height:
            # 二分找"两行内放得下的最长前缀"，再补省略号
            low, high = 0, len(text)
            while low < high:
                mid = (low + high + 1) // 2
                probe = metrics.boundingRect(
                    QRect(0, 0, width, 10000), Qt.TextFlag.TextWordWrap, text[:mid] + "…")
                if probe.height() <= max_height:
                    low = mid
                else:
                    high = mid - 1
            text = text[:low].rstrip() + "…"
        if self._name_label.text() != text:
            self._name_label.setText(text)

    def eventFilter(self, obj, event):  # noqa: N802 (Qt 命名约定)
        # 名称区域宽度变化（电源钮出现/窗口换列）时重挑字号
        if obj is self._name_label and event.type() == QEvent.Type.Resize:
            self._fit_name_font()
        return super().eventFilter(obj, event)

    # ---------- 状态渲染 ----------

    def set_icon(self, path: Path | None) -> None:
        """填入设备图标；路径无效或图片损坏时保持隐藏不打断布局。"""
        if path is None or not path.is_file():
            return
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            return
        self._icon_label.setPixmap(pixmap.scaled(
            _ICON_SIZE, _ICON_SIZE, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))
        self._icon_label.show()

    def set_power_state(self, state: bool) -> None:
        # 父设备没有自己的那一路：状态回来也不显示按钮
        if not self._power_enabled:
            return
        # 能设置状态即证明设备具备开关能力，电源钮随之可见
        self._power_btn.set_state(state)
        self._power_btn.show()

    def _sub_text(self, metrics: str | None = None) -> str:
        """副标题文案：房间名 / 通道数 / 离线标记 / 实时读数，取有的拼。

        `_show_room` 为假时不重复房间名（分组视图的段标题已经写了）。
        """
        parts: list[str] = []
        if self._show_room and self.device.room_name:
            parts.append(self.device.room_name)
        if self._channels >= 2:
            parts.append(f"{self._channels} 个通道")
        if not self._online:
            parts.append("离线")
        elif metrics:
            parts.append(metrics)
        return " · ".join(parts)

    def set_metrics(self, text: str | None) -> None:
        """副标题附带实时读数（温湿度等），离线卡片不叠加读数。"""
        # 离线设备不展示温湿度，读数可能是旧缓存
        if not self._online:
            return
        self._metrics_text = text
        self._sync_sub()

    def _sync_sub(self) -> None:
        """按当前宽度拼副标题：放不下就收省略号。

        不在 set_metrics 里直接做的原因：网格重建时标签宽度还是 0，
        只有等到布局完成（resize/show）才知道实际能放多宽。
        """
        width = self._sub_label.width()
        if width <= 0:
            return
        text = self._sub_text(self._metrics_text)
        elided = QFontMetrics(self._sub_label.font()).elidedText(
            text, Qt.TextElideMode.ElideRight, width)
        if elided != text and self._sub_label.text() != elided:
            self._sub_label.setText(elided)

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._power_btn.set_busy(busy)

    # ---------- hover 观感 ----------

    def enterEvent(self, event) -> None:  # noqa: N802 (Qt 命名约定)
        self._hovered = True
        self._apply_card_color()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 (Qt 命名约定)
        self._hovered = False
        self._apply_card_color()
        super().leaveEvent(event)

    def _apply_card_color(self) -> None:
        if not self._online:
            # 离线卡片整体灰置，hover 不提亮（无交互暗示）
            self.style_data.background_color = QColor(SiColors.OFFLINE_CARD)
        else:
            self.style_data.background_color = QColor(
                SiColors.CARD_HOVER if self._hovered else SiColors.CARD
            )
        self.update()

    # ---------- 交互 ----------

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt 命名约定)
        # 电源钮自己消费点击事件，能走到这里的都是卡片空白区
        if event.button() == Qt.LeftButton:
            self.open_requested.emit(self.device.did)
        super().mouseReleaseEvent(event)


