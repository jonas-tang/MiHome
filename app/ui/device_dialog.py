# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""设备详情抽屉：与添加功能同款的侧滑抽屉。

普通设备（没有子设备）：顶部沿用工作台（可自定义的功能面板），下方补一块
「可配置参数」，把这个设备剩下的可读写属性全部列出来——以前只有手动添加
进工作台的几项可见，其余属性藏在「编辑功能」里，等于看不见。

多通道设备（多键面板拆出的按键、网关下的子机）：顶部是父设备名称与型号，
中间把各通道排成卡片（一行 4 个，超过就换行），每张卡片左侧是该通道的圆形
开关、右侧是通道名；点卡片本体在下方展开该通道的可配置参数。父设备自己
没有属于它的那一路，卡片上本就不挂电源钮，控制入口全在这里。

开关一律只由圆形电源钮触发：点卡片/参数行的空白处不做任何操作，
避免"想看看信息却把设备关了"。
"""

import logging

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.core.jobs import PRIORITY_CONTROL, JobExecutor
from app.core.models import DeviceDetail, DeviceInfo, PropInfo
from app.core.service import MijiaService
from app.ui.overlay_dialog import OverlayDialog
from app.ui.power_button import PowerButton
from app.ui.prop_widgets import build_prop_section, group_key
from app.ui.si_theme import SiColors
from app.ui.workbench_panel import WorkbenchPanel

logger = logging.getLogger(__name__)

# 通道卡片一行放几个；超过就换行（米家 APP 的多键面板也是这个密度）
_CHANNELS_PER_ROW = 4


def _cn(desc: str) -> str:
    """'Switch Status / 一键' -> '一键'：界面只留中文那一段。"""
    return desc.split("/")[-1].strip()


def _compact(widget: QWidget) -> None:
    """把功能卡片压紧一些，参数列表用。

    这些卡片本来是为工作台那种"一屏几块大卡"的版面设计的：内边距
    16~24、固定高度 76/104。参数列表里一屏要放七八项，同样的留白会
    让列表显得又长又散，滑块那类（标题 + 滑轨）尤其明显。
    """
    widget.setMinimumHeight(0)
    widget.setMaximumHeight(16777215)  # 清掉 setFixedHeight 留下的上限
    lay = widget.layout()
    if lay is not None:
        m = lay.contentsMargins()
        lay.setContentsMargins(min(m.left(), 14), min(m.top(), 8),
                               min(m.right(), 14), min(m.bottom(), 8))
        if lay.spacing() > 6:
            lay.setSpacing(6)
    # 滑轨这类控件自带"占满容器"的尺寸提示（siui 滑块 480px），
    # 交给它自己的紧凑版处理；其余卡片按 sizeHint 定高，不再伸缩
    if hasattr(widget, "set_compact"):
        widget.set_compact(True)
    else:
        widget.setSizePolicy(QSizePolicy.Policy.Preferred,
                             QSizePolicy.Policy.Fixed)


class _PropPanel(QFrame):
    """一块可滚动参数区：渲染属性、回读当前值、写改动。

    通道详情与普通设备的「可配置参数」共用这一套，读写、乐观回显、
    失败回滚的逻辑只写一遍。
    """

    def __init__(self, service: MijiaService, jobs: JobExecutor,
                 title: str = "", parent=None):
        super().__init__(parent)
        self._service = service
        self._jobs = jobs
        self._did: str | None = None
        self._sections: dict[str, QWidget] = {}
        self._read_busy = False
        # 内容重建后要不要把滚动条拉回顶部（延到排版完成后再执行）
        self._reset_scroll_pending = False
        # 取值代次：连续切换通道时，上一次的读请求回来不能把这一批
        # 的"忙碌"标志清掉、也不能往新的一组控件里填旧值
        self._gen = 0

        self.setObjectName("propPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._apply_style()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 12)
        lay.setSpacing(10)

        head = QHBoxLayout()
        head.setSpacing(8)
        self._title = QLabel(title)
        self._title.setFont(QFont("Microsoft YaHei UI", 12, QFont.Weight.DemiBold))
        self._title.setStyleSheet(
            f"color: {SiColors.TEXT_PRIMARY}; background: transparent;")
        self._note = QLabel("")
        self._note.setStyleSheet(
            f"color: {SiColors.WARN_TEXT}; background: transparent; font-size: 9pt;")
        head.addWidget(self._title)
        head.addStretch(1)
        head.addWidget(self._note)
        lay.addLayout(head)
        if not title:
            self._title.hide()

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setStyleSheet("QScrollArea { background: transparent; }")
        host = QWidget()
        host.setStyleSheet("background: transparent;")
        self._body = QVBoxLayout(host)
        self._body.setContentsMargins(0, 0, 0, 0)
        # 行距压到 6px：参数列表一屏要放得下更多项
        self._body.setSpacing(6)
        self._scroll.setWidget(host)
        lay.addWidget(self._scroll, stretch=1)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            f"#propPanel {{ background: {SiColors.CARD};"
            f" border: 1px solid {SiColors.LINE}; border-radius: 12px; }}")

    def retheme(self) -> None:
        self._apply_style()

    # ---------- 内容 ----------

    def set_title(self, title: str, note: str = "") -> None:
        self._title.setText(title)
        self._title.setVisible(bool(title))
        self._note.setText(note)
        self._note.setVisible(bool(note))

    def _clear(self) -> None:
        while self._body.count():
            item = self._body.takeAt(0)
            if widget := item.widget():
                widget.deleteLater()
        self._sections.clear()
        # 换内容即换代次：在途的读结果作废，同时放开忙碌标志
        self._gen += 1
        self._read_busy = False

    def show_hint(self, text: str) -> None:
        self._clear()
        self._did = None
        hint = QLabel(text)
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setWordWrap(True)
        hint.setStyleSheet(
            f"color: {SiColors.TEXT_MUTED}; background: transparent; font-size: 9pt;")
        self._body.addStretch(1)
        self._body.addWidget(hint)
        self._body.addStretch(1)

    def set_props(self, props: list[PropInfo], did: str) -> None:
        """渲染一组属性；did 是这些属性读写该发往的设备。"""
        self._clear()
        self._did = did
        if not props:
            self.show_hint("该设备没有其它可配置的参数")
            return
        for prop in props:
            # 规格描述是"英文 / 中文"两段，界面只留中文（与工作台同款处理）
            display = PropInfo(
                name=prop.name, desc=_cn(prop.desc), type=prop.type,
                readable=prop.readable, writable=prop.writable,
                range=prop.range, value_list=prop.value_list)
            section = build_prop_section(
                display, lambda value, p=prop: self._write_prop(p, value))
            _compact(section)
            self._sections[prop.name] = section
            self._body.addWidget(section)
        self._body.addStretch(1)
        # 回到顶部：重建内容后滚动条可能停在旧位置（或者被自动聚焦的控件
        # 带着滚走），第一张卡片会被顶出视野
        self._reset_scroll_pending = True
        # 排完再按"当前宽度下真正需要的高度"钉住：容器不够高时让外层滚动，
        # 而不是把每张卡片压扁（枚举网格被压扁会丢掉后面的选项）
        QTimer.singleShot(0, self._sync_heights)
        self.reload()

    def _sync_heights(self) -> None:
        """按实际宽度重算每张卡片需要的高度。

        枚举网格用的是流式布局，选项会自动换行：宽度不同，需要的高度
        可能从 1 行（70px）变成 3 行（220px）。只按 sizeHint 定高会把
        宽度尚未确定时的"窄屏结果"冻住——窗帘的「电机控制」就是这么被
        撑到 228px 的（实际在 866px 宽下只要 72px）。
        """
        for section in self._sections.values():
            width = section.width()
            if section.hasHeightForWidth() and width > 0:
                height = section.heightForWidth(width)
            else:
                height = section.sizeHint().height()
            if height > 0 and abs(height - section.minimumHeight()) > 2:
                section.setMinimumHeight(height)
        if getattr(self, "_reset_scroll_pending", False):
            self._reset_scroll_pending = False
            self._scroll.verticalScrollBar().setValue(0)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        # 宽度变化（抽屉缩放/滚动条出现）后流式布局的换行数会变
        QTimer.singleShot(0, self._sync_heights)

    # ---------- 读写 ----------

    def reload(self) -> None:
        did = self._did
        names = [name for name, sec in self._sections.items()
                 if getattr(sec, "prop", None) is not None and sec.prop.readable]
        if did is None or not names or self._read_busy:
            return
        self._read_busy = True
        gen = self._gen
        self._jobs.submit(
            lambda: self._service.read_props(did, names),
            on_success=lambda values, g=gen: self._apply_values(values, g),
            on_error=lambda exc, g=gen: self._on_read_failed(exc, g),
        )

    def _apply_values(self, values: dict, gen: int) -> None:
        if gen != self._gen:
            return  # 这批控件已经被换掉了，结果作废
        self._read_busy = False
        for name, value in (values or {}).items():
            section = self._sections.get(name)
            if section is not None and value is not None:
                section.refresh_value(value)

    def _on_read_failed(self, error: Exception, gen: int) -> None:
        if gen != self._gen:
            return
        self._read_busy = False
        logger.warning("读取设备属性失败: %s", error)

    def _write_prop(self, prop: PropInfo, value) -> None:
        did = self._did
        if did is None:
            return
        section = self._sections.get(prop.name)
        if section is not None:
            # 乐观回显：写下去的值先显示，失败由随后的重新读数纠正
            try:
                section.refresh_value(value)
            except Exception:
                pass
        self._jobs.submit(
            lambda: self._service.write_prop(did, prop.name, value),
            on_success=lambda _: self._after_write(prop),
            on_error=lambda exc, p=prop: self._on_write_failed(p, exc),
            priority=PRIORITY_CONTROL,
        )

    def _after_write(self, prop: PropInfo) -> None:
        # 写完回读一次：云端没接受时把界面纠回真实值
        self.reload()

    def _on_write_failed(self, prop: PropInfo, error: Exception) -> None:
        from app.ui.toast import Toast
        Toast.info(self, f"「{_cn(prop.desc)}」设置失败：{error}", 4000)
        self.reload()


class _ChannelCard(QFrame):
    """一个通道：圆形开关 + 通道名，整卡可点开详情。

    卡片本体只负责"选中看详情"，不碰开关——要开关就点左边的圆钮。
    """

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
        # 父设备规格缓存：面板按键共用它，点不同通道时只拉一次
        self._parent_detail: DeviceDetail | None = None
        self._detail_panel: _PropPanel | None = None
        self._extra_panel: _PropPanel | None = None

        outer = QVBoxLayout(self._panel)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)
        self._outer = outer

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
        # 工作台加载完才知道哪些功能已经在上面，其余的在下方补齐
        self._workbench.detail_loaded.connect(self._on_workbench_detail)

        if self._channels:
            self._workbench.hide()
            outer.addWidget(self._build_channel_grid(), stretch=1)
        else:
            self._title_label.hide()
            self._sub_label.hide()
            outer.addWidget(self._workbench, stretch=1)
            self._extra_panel = _PropPanel(service, jobs, "可配置参数", self._panel)
            self._extra_panel.hide()
            # 参数区占大头：工作台上通常只放少数几个常用功能，
            # 而这里可能有十几项，空间比例反过来更实用
            outer.addWidget(self._extra_panel, stretch=2)

    # ---------- 普通设备：补齐工作台之外的可配置参数 ----------

    def _on_workbench_detail(self, detail: DeviceDetail) -> None:
        if self._extra_panel is None or detail.did != self.device.did:
            return
        # 已经在工作台上的功能不再重复列一遍：单项按键是属性名，
        # 组合卡片形如 group:<组名>，组内成员整组排除
        active = set(self._workbench._active_keys)
        extras = [
            prop for prop in detail.props
            if prop.name not in active
            and f"group:{group_key(prop.name)}" not in active
        ]
        if not extras:
            self._extra_panel.hide()
            return
        # 可操作的排前面，只读的（滤芯寿命、状态这类）垫底
        extras.sort(key=lambda p: (0 if p.writable else 1, p.name))
        if not active:
            # 工作台还空着：只留标题行（"编辑功能"入口仍在），
            # 把高度让给下面的完整参数列表
            self._workbench.set_body_visible(False)
            index = self._outer.indexOf(self._workbench)
            if index >= 0:
                self._outer.setStretch(index, 0)
        self._extra_panel.set_title("可配置参数", f"{len(extras)} 项")
        self._extra_panel.set_props(extras, detail.did)
        self._extra_panel.show()

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
        self._detail_panel = _PropPanel(self._service, self._jobs, "", self._panel)
        self._detail_panel.show_hint("点击上方通道卡片，在此查看并配置该通道的功能")
        lay.addWidget(self._detail_panel, stretch=1)
        return host

    def _on_channel_activated(self, did: str) -> None:
        """选中通道：高亮卡片并把下方换成它的功能。"""
        for cdid, card in self._channel_cards.items():
            card.set_selected(cdid == did)
        self._selected_did = did
        child = next((c for c in self._channels if c.did == did), None)
        if child is None or self._detail_panel is None:
            return
        self._detail_panel.set_title(child.name, "" if child.online else "离线")
        self._detail_panel.show_hint("正在读取该通道的功能…")
        self._jobs.submit(
            lambda: self._channel_props(child),
            on_success=lambda payload, d=did: self._render_channel_detail(d, payload),
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
        if did != self._selected_did or self._detail_panel is None:
            return  # 期间又点了别的通道，丢掉过期结果
        self._detail_panel.set_props(props, target_did)

    def _on_detail_error(self, did: str, error: Exception) -> None:
        if did != self._selected_did or self._detail_panel is None:
            return
        logger.warning("读取通道功能失败: %s", error)
        self._detail_panel.show_hint(f"读取该通道的功能失败：{error}")

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
            # 没有可写开关属性的通道保持空心"未知"态，不假装能控制
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
        # 下方正开着同一个通道时，把它"开关"那一项也同步过来
        if self._selected_did == did and self._detail_panel is not None:
            self._detail_panel.reload()

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
        for panel in (self._detail_panel, self._extra_panel):
            if panel is not None:
                panel.retheme()
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
