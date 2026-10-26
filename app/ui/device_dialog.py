# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""设备详情抽屉：与添加功能同款的侧滑抽屉。

普通设备直接复用 WorkbenchPanel（可自定义的功能工作台）。

多通道设备（多键面板拆出的按键、网关下的子机）改用另一套版面：
顶部是父设备名称与型号，中间把各通道排成卡片（一行 4 个，超过就换行），
每张卡片左侧是该通道的圆形开关、右侧是通道名；点卡片本体在下方展开
该通道的可配置功能。父设备自己没有属于它的那一路，卡片上本就不挂
电源钮，控制入口全在这里。
"""

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.core.jobs import PRIORITY_CONTROL, JobExecutor
from app.core.models import DeviceInfo, DeviceDetail, PropInfo
from app.core.service import MijiaService
from app.ui.overlay_dialog import OverlayDialog
from app.ui.power_button import PowerButton
from app.ui.prop_widgets import build_prop_section
from app.ui.si_theme import SiColors
from app.ui.workbench_panel import WorkbenchPanel

logger = logging.getLogger(__name__)

# 通道卡片一行放几个；超过就换行（米家 APP 的多键面板也是这个密度）
_CHANNELS_PER_ROW = 4


class _ChannelCard(QFrame):
    """一个通道：圆形开关 + 通道名，整卡可点开详情。"""

    activated = Signal(str)   # 点卡片本体：选中，下方展示该通道的功能
    toggled = Signal(str)     # 点圆钮：直接开关

    def __init__(self, child: DeviceInfo, parent=None):
        super().__init__(parent)
        self.child = child
        self._selected = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        # 自绘背景需要这个属性，否则 QSS 里的圆角底色不生效
        self.setObjectName("channelCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # 高度固定：通道名换行也在两行内截断（见下方 wordWrap + elide）
        self.setMinimumHeight(84)

        self._btn = PowerButton(38, icon_size=26)
        self._btn.set_online(child.online)
        self._btn.setToolTip(f"{child.name} 开关")
        self._btn.clicked.connect(lambda: self.toggled.emit(child.did))

        name = QLabel(child.name)
        name.setFont(QFont("Microsoft YaHei UI", 11))
        name.setWordWrap(True)
        name.setMinimumWidth(0)
        # 通道名可能很长（"按键1-德力西电气四位单零火智能开关"），
        # 让它在卡片内换行而不是把卡片撑宽
        name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._name_label = name

        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(12)
        lay.addWidget(self._btn, alignment=Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(name, stretch=1)
        self._apply_style()

    # ---- 外观 ----

    def _apply_style(self) -> None:
        if not self.child.online:
            bg, border = SiColors.OFFLINE_CARD, SiColors.LINE
            fg = SiColors.OFFLINE_TEXT
        else:
            bg = SiColors.CARD_HOVER if self._selected else SiColors.CARD
            border = SiColors.THEME if self._selected else SiColors.LINE
            fg = SiColors.TEXT_PRIMARY
        self.setStyleSheet(
            f"#channelCard {{ background: {bg}; border: 1px solid {border};"
            " border-radius: 12px; }")
        self._name_label.setStyleSheet(
            f"color: {fg}; background: transparent;")

    def set_selected(self, selected: bool) -> None:
        if self._selected == selected:
            return
        self._selected = selected
        self._apply_style()

    def set_state(self, state: bool | None) -> None:
        self._btn.set_state(state)

    def set_busy(self, busy: bool) -> None:
        self._btn.set_busy(busy)

    def state(self) -> bool | None:
        return self._btn.state()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt 命名约定)
        # 圆钮自己会吃掉点击事件，走到这里的都是点在卡片空白/名字上
        if event.button() == Qt.MouseButton.LeftButton:
            self.activated.emit(self.child.did)
        super().mouseReleaseEvent(event)


class DeviceDetailDialog(OverlayDialog):
    def __init__(self, service: MijiaService, jobs: JobExecutor,
                 device: DeviceInfo, parent=None,
                 channels: list[DeviceInfo] | None = None):
        super().__init__(parent)
        self.device = device
        self._service = service
        self._jobs = jobs
        # 子通道（合并展示开启时由主窗口传入）；为空表示普通设备
        self._channels = list(channels or [])
        self._channel_cards: dict[str, _ChannelCard] = {}
        self._selected_did: str | None = None
        # 通道详情：
        # - 面板按键共用父设备规格，按 "-<siid>" 后缀只取它那一路的属性
        # - 网关子机有自己的规格（空调），直接读子设备本身的属性
        self._detail_did: str | None = None
        self._detail_sections: dict[str, QWidget] = {}
        self._detail_values_busy = False
        self._parent_detail: DeviceDetail | None = None

        outer = QVBoxLayout(self._panel)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)

        # ---- 顶部：设备名 / 型号 + 关闭 ----
        header = QHBoxLayout()
        header.setSpacing(12)
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        self._title_label = QLabel(device.name)
        self._title_label.setFont(QFont("Microsoft YaHei UI", 15, QFont.Weight.DemiBold))
        self._title_label.setStyleSheet(
            f"color: {SiColors.TEXT_PRIMARY}; background: transparent;")
        title_box.addWidget(self._title_label)
        sub = " · ".join(p for p in (device.room_name, device.model) if p)
        self._sub_label = QLabel(sub)
        self._sub_label.setStyleSheet(
            f"color: {SiColors.TEXT_SECONDARY}; background: transparent;"
            " font-size: 8pt;")
        title_box.addWidget(self._sub_label)
        header.addLayout(title_box, stretch=1)
        header.addWidget(self._make_close_button(), alignment=Qt.AlignmentFlag.AlignTop)
        outer.addLayout(header)

        # ---- 工作台（普通设备的主视图；多通道设备只用它挂信号，不显示）----
        self._workbench = WorkbenchPanel(service, jobs, self._panel)

        if self._channels:
            self._workbench.hide()
            outer.addWidget(self._build_channel_grid(), stretch=1)
        else:
            self._title_label.hide()
            self._sub_label.hide()
            outer.addWidget(self._workbench, stretch=1)

    # ---------- 多通道版面 ----------

    def _build_channel_grid(self) -> QWidget:
        """通道卡片网格 + 下方通道详情。"""
        host = QWidget()
        host.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(host)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)

        grid_host = QWidget()
        grid_host.setStyleSheet("background: transparent;")
        grid = QGridLayout(grid_host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(10)
        for index, child in enumerate(self._channels):
            card = _ChannelCard(child)
            card.activated.connect(self._on_channel_activated)
            card.toggled.connect(self._on_channel_clicked)
            self._channel_cards[child.did] = card
            grid.addWidget(card, index // _CHANNELS_PER_ROW,
                           index % _CHANNELS_PER_ROW)
        # 不足一行时补空列拉伸，保证 4 列的卡片宽度一致
        for col in range(_CHANNELS_PER_ROW):
            grid.setColumnStretch(col, 1)
        lay.addWidget(grid_host)

        # 下方：选中通道的功能区（可滚动，空调这类子机功能很多）
        self._detail_box = QFrame()
        self._detail_box.setObjectName("channelDetail")
        self._detail_box.setStyleSheet(
            f"#channelDetail {{ background: {SiColors.CARD};"
            f" border: 1px solid {SiColors.LINE}; border-radius: 12px; }}")
        box_lay = QVBoxLayout(self._detail_box)
        box_lay.setContentsMargins(0, 0, 0, 0)
        box_lay.setSpacing(0)

        self._detail_scroll = QScrollArea()
        self._detail_scroll.setWidgetResizable(True)
        self._detail_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._detail_scroll.setStyleSheet("QScrollArea { background: transparent; }")
        body = QWidget()
        body.setStyleSheet("background: transparent;")
        self._detail_layout = QVBoxLayout(body)
        self._detail_layout.setContentsMargins(16, 14, 16, 14)
        self._detail_layout.setSpacing(10)
        self._detail_scroll.setWidget(body)
        box_lay.addWidget(self._detail_scroll)

        lay.addWidget(self._detail_box, stretch=1)
        self._show_detail_hint()
        return host

    def _clear_detail(self) -> None:
        while self._detail_layout.count():
            item = self._detail_layout.takeAt(0)
            if widget := item.widget():
                widget.deleteLater()
            elif sub := item.layout():
                self._clear_layout(sub)
        self._detail_sections.clear()

    @staticmethod
    def _clear_layout(layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            if widget := item.widget():
                widget.deleteLater()
            elif sub := item.layout():
                DeviceDetailDialog._clear_layout(sub)

    def _show_detail_hint(self, text: str = "点击上方通道卡片，在此查看并配置该通道的功能") -> None:
        self._clear_detail()
        hint = QLabel(text)
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setWordWrap(True)
        hint.setStyleSheet(
            f"color: {SiColors.TEXT_MUTED}; background: transparent;"
            " font-size: 9pt;")
        self._detail_layout.addStretch(1)
        self._detail_layout.addWidget(hint)
        self._detail_layout.addStretch(1)
        self._detail_did = None

    def _on_channel_activated(self, did: str) -> None:
        """选中通道：高亮卡片并把下方换成它的功能。"""
        for cdid, card in self._channel_cards.items():
            card.set_selected(cdid == did)
        self._selected_did = did
        child = next((c for c in self._channels if c.did == did), None)
        if child is None:
            return
        self._show_detail_hint("正在读取该通道的功能…")
        self._jobs.submit(
            lambda: self._channel_props(child),
            on_success=lambda props, d=did: self._render_channel_detail(d, props),
            on_error=lambda exc, d=did: self._on_detail_error(d, exc),
        )

    def _channel_props(self, child: DeviceInfo) -> tuple[str, list[PropInfo]]:
        """该通道要展示的属性，以及这些属性该往哪个 did 读写。

        面板按键与父设备共用同一份规格，只保留名字以 "-<siid>" 结尾的
        那一组（on-2 / mode-2 …）；网关子机有自己的规格，整份都算它的。
        """
        if child.parent_kind == "split" and child.parent_siid:
            detail = self._parent_detail or self._service.device_detail(child.parent_did)
            self._parent_detail = detail
            suffix = f"-{child.parent_siid}"
            props = [p for p in detail.props if p.name.endswith(suffix)]
            return child.parent_did, props
        detail = self._service.device_detail(child.did)
        return child.did, detail.props

    def _render_channel_detail(self, did: str, payload) -> None:
        target_did, props = payload
        if did != self._selected_did:
            return  # 期间又点了别的通道，丢掉过期结果
        self._clear_detail()
        self._detail_did = target_did
        child = next(c for c in self._channels if c.did == did)

        head = QHBoxLayout()
        head.setSpacing(8)
        name = QLabel(child.name)
        name.setFont(QFont("Microsoft YaHei UI", 12, QFont.Weight.DemiBold))
        name.setStyleSheet(
            f"color: {SiColors.TEXT_PRIMARY}; background: transparent;")
        head.addWidget(name)
        head.addStretch(1)
        state = QLabel("离线" if not child.online else "")
        state.setStyleSheet(
            f"color: {SiColors.WARN_TEXT}; background: transparent; font-size: 9pt;")
        head.addWidget(state)
        self._detail_layout.addLayout(head)

        if not props:
            empty = QLabel("该通道没有可配置的功能")
            empty.setStyleSheet(
                f"color: {SiColors.TEXT_MUTED}; background: transparent; font-size: 9pt;")
            self._detail_layout.addWidget(empty)
            return

        for prop in props:
            # 规格描述是"英文 / 中文"两段，界面只留中文（与工作台同款处理）
            cn_prop = PropInfo(
                name=prop.name, desc=prop.desc.split("/")[-1].strip(),
                type=prop.type, readable=prop.readable, writable=prop.writable,
                range=prop.range, value_list=prop.value_list)
            section = build_prop_section(
                cn_prop, lambda value, p=prop: self._write_detail_prop(p, value))
            self._detail_sections[prop.name] = section
            self._detail_layout.addWidget(section)
        self._detail_layout.addStretch(1)
        self._load_detail_values()

    def _on_detail_error(self, did: str, error: Exception) -> None:
        if did != self._selected_did:
            return
        logger.warning("读取通道功能失败: %s", error)
        self._show_detail_hint(f"读取该通道的功能失败：{error}")

    def _load_detail_values(self) -> None:
        did = self._detail_did
        names = [name for name, sec in self._detail_sections.items()
                 if getattr(sec, "prop", None) is not None and sec.prop.readable]
        if did is None or not names or self._detail_values_busy:
            return
        self._detail_values_busy = True
        self._jobs.submit(
            lambda: self._service.read_props(did, names),
            on_success=self._apply_detail_values,
            on_error=lambda exc: logger.warning("读取通道属性失败: %s", exc),
        )

    def _apply_detail_values(self, values: dict) -> None:
        self._detail_values_busy = False
        for name, value in (values or {}).items():
            section = self._detail_sections.get(name)
            if section is not None and value is not None:
                section.refresh_value(value)

    def _write_detail_prop(self, prop: PropInfo, value) -> None:
        did = self._detail_did
        if did is None:
            return
        section = self._detail_sections.get(prop.name)
        if section is not None:
            # 乐观回显：写下去的值先显示，失败由下面的重新读数纠正
            try:
                section.refresh_value(value)
            except Exception:
                pass
        self._jobs.submit(
            lambda: self._service.write_prop(did, prop.name, value),
            on_success=lambda _: self._after_detail_write(prop),
            on_error=lambda exc: self._on_write_failed(prop, exc),
            priority=PRIORITY_CONTROL,
        )

    def _after_detail_write(self, prop: PropInfo) -> None:
        # 通道开关可能就是在卡片圆钮上点的那一个，写成功后两边一起刷新
        if prop.name.startswith("on"):
            self._load_channel_states()
        self._load_detail_values()

    def _on_write_failed(self, prop: PropInfo, error: Exception) -> None:
        from app.ui.toast import Toast
        Toast.info(self, f"「{prop.desc.split('/')[-1].strip()}」设置失败：{error}", 4000)
        self._load_detail_values()

    # ---------- 通道开关 ----------

    def _load_channel_states(self) -> None:
        """批量读一次各通道的开关状态；离线通道不发请求。"""
        dids = [c.did for c in self._channels if c.online]
        if not dids:
            return
        self._jobs.submit(
            lambda: self._service.power_states(dids),
            on_success=self._apply_channel_states,
            on_error=lambda exc: logger.warning("读取通道开关失败: %s", exc),
        )

    def _apply_channel_states(self, states: dict[str, bool | None]) -> None:
        for did, state in states.items():
            card = self._channel_cards.get(did)
            if card is None:
                continue
            card.set_busy(False)
            if state is None:
                # 该通道没有可写开关属性：留着圆钮只会误导
                card.set_state(None)
            else:
                card.set_state(state)

    def _on_channel_clicked(self, did: str) -> None:
        card = self._channel_cards.get(did)
        if card is None or card.state() is None:
            return
        target = not bool(card.state())
        # 乐观翻转：点下即变色，失败再回滚（与主界面卡片同一套观感）
        card.set_state(target)
        card.set_busy(True)
        self._jobs.submit(
            lambda: (self._service.set_power_state(did, target), target)[1],
            on_success=lambda state, d=did: self._on_channel_done(d, state),
            on_error=lambda err, d=did, prev=not target: self._on_channel_failed(d, err, prev),
            priority=PRIORITY_CONTROL,
        )

    def _on_channel_done(self, did: str, state: bool) -> None:
        card = self._channel_cards.get(did)
        if card is None:
            return
        card.set_busy(False)
        card.set_state(state)
        # 下方详情正开着同一个通道时，把它的开关区块也同步过来
        if self._selected_did == did:
            self._load_detail_values()

    def _on_channel_failed(self, did: str, error: Exception, previous: bool) -> None:
        card = self._channel_cards.get(did)
        if card is None:
            return
        card.set_busy(False)
        card.set_state(previous)
        from app.ui.toast import Toast
        name = next((c.name for c in self._channels if c.did == did), "该通道")
        Toast.info(self, f"「{name}」操作失败：{error}", 4000)

    # ---------- 生命周期 ----------

    @property
    def panel(self) -> WorkbenchPanel:
        return self._workbench

    def load(self) -> None:
        # 多通道设备只用通道版面：工作台保持隐藏，也不去拉父设备的详情
        if not self._channels:
            self._workbench.show_device(
                self.device.did, online=self.device.online, device=self.device)
            return
        self._load_channel_states()
        # 预取父设备规格：点通道时才不用再等一次网络
        self._jobs.submit(
            lambda: self._service.device_detail(self.device.did),
            on_success=self._cache_parent_detail,
            on_error=lambda exc: logger.warning("预热父设备规格失败: %s", exc),
        )

    def _cache_parent_detail(self, detail: DeviceDetail) -> None:
        self._parent_detail = detail

    def retheme(self) -> None:
        """主题切换：面板底色 + 卡片配色重设。"""
        super().retheme()
        for card in self._channel_cards.values():
            card._apply_style()
        self._workbench.retheme()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._place_overlay()
        # 居中面板：宽度 900，窗口过窄时自适应收缩
        pw = 900
        pw = min(pw, max(320, self.width() - 40))
        ph = min(640, self.height() - 40)
        x = (self.width() - pw) // 2
        y = (self.height() - ph) // 2
        self._panel.setGeometry(x, y, pw, ph)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if self._fill_parent_window():
            self.raise_()
            self._fade_in()
            return
        # 托盘独立弹出：铺满可用屏幕（遮罩盖住全屏，避免只罩一小块、
        # 周围露出桌面壁纸的突兀观感），面板由 resizeEvent 居中放置
        from PySide6.QtGui import QGuiApplication
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            self.setGeometry(screen.availableGeometry())
        self.raise_()
        self._fade_in()
