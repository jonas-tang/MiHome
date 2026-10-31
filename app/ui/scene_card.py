# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""米家手动场景卡片（主页「场景」选项卡）。

视觉与设备卡片一致：左侧场景名与家庭副标题，右侧圆形运行按钮。
点击右侧运行按钮执行场景（与米家 APP 交互一致，无二次确认）；
执行中按钮转忙碌灰置。
"""

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

import qtawesome as qta
import shiboken6

from app.core.models import SceneInfo
from app.siui.components.container import SiRowCard
from app.ui.device_card import _CARD_FIXED_HEIGHT, _CARD_FIXED_WIDTH
from app.ui.power_button import PowerButton
from app.ui.si_theme import SiColors

# 与设备卡片的电源按钮同尺寸（PowerButton 32×32），两种卡片视觉一致
_RUN_BTN_SIZE = 32
# 自动化卡片右侧的启停开关：直接复用设备卡片的 PowerButton，
# 尺寸/配色/忙碌态与设备开关完全一致
_POWER_BTN_SIZE = 32
_POWER_ICON_SIZE = 24


class SceneCard(SiRowCard):
    """场景卡片：点击右侧运行按钮触发 executed 信号。

    卡片空白处的点击不执行场景，而是发 open_requested 让主窗口打开
    详情（场景会执行哪些动作）。两个热点分开，避免"想看明细却把场景
    跑了一遍"——场景涉及的门锁、晾衣架这类动作误触成本很高。
    """

    executed = Signal(str)  # scene_id
    open_requested = Signal(str)  # scene_id
    enabled_toggled = Signal(str, bool)  # scene_id, 期望的新状态

    def __init__(self, scene: SceneInfo, parent=None):
        super().__init__(parent, self.LeftToRight)
        self.scene = scene
        self._hovered = False
        self._busy = False

        self.style_data.background_color = QColor(SiColors.CARD)
        self.style_data.border_radius = 14.0
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        # 与设备卡片同尺寸：网格列数计算直接复用主窗口的换算
        self.setFixedSize(_CARD_FIXED_WIDTH, _CARD_FIXED_HEIGHT)
        self.muteStretchWidget()

        # 自动化由触发条件驱动、不能手动执行，右侧不放松运行按钮，
        # 改成启用状态——否则用户会以为点一下就能跑
        self._run_btn: QPushButton | None = None
        self._power: PowerButton | None = None
        if scene.is_automation:
            # 与设备卡片同款电源按钮：开=米家绿、关=中灰，点击即乐观翻转
            self._power = PowerButton(_POWER_BTN_SIZE, icon_size=_POWER_ICON_SIZE)
            self._power.set_state(scene.enabled)
            self._power.setToolTip("启用 / 停用这条自动化")
            self._power.clicked.connect(self._on_power_clicked)
        else:
            self._run_btn = QPushButton()
            self._run_btn.setObjectName("sceneRunBtn")
            self._run_btn.setFixedSize(_RUN_BTN_SIZE, _RUN_BTN_SIZE)
            self._run_btn.setIconSize(QSize(22, 22))
            self._run_btn.setCursor(Qt.PointingHandCursor)
            self._run_btn.clicked.connect(lambda: self.executed.emit(scene.scene_id))
            self._apply_run_btn()

        self._name_label = QLabel(scene.name)
        self._name_label.setFont(QFont("Microsoft YaHei UI", 15, QFont.Weight.DemiBold))
        # 允许换行：长场景名不再把所在列撑宽
        self._name_label.setWordWrap(True)
        self._name_label.setStyleSheet(
            f"color: {SiColors.TEXT_PRIMARY}; background: transparent;")

        # 自动化：家庭由段标题表达（按家庭分区），卡片里只写动作数，
        # 避免 180 宽卡片把"理想之地 · 自动化"截成"理想之地 · 自动…"
        if scene.is_automation:
            _count = len(scene.actions)
            _sub = f"{_count} 个动作" if _count else "无动作明细"
        else:
            _sub = f"{scene.home_name} · 手动场景" if scene.home_name else "手动场景"
        self._sub_label = QLabel(_sub)
        self._sub_label.setFont(QFont("Microsoft YaHei UI", 11))
        self._sub_label.setStyleSheet(
            f"color: {SiColors.TEXT_SECONDARY}; background: transparent;")

        # 左侧文字列整体垂直居中，与右侧运行按钮同一水平轴
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
        lay.setContentsMargins(18, 10, 18, 10)
        lay.addWidget(text_col)
        lay.addStretch(1)
        if self._power is not None:
            lay.addWidget(self._power, alignment=Qt.AlignVCenter)
        if self._run_btn is not None:
            lay.addWidget(self._run_btn, alignment=Qt.AlignVCenter)

    def _on_power_clicked(self) -> None:
        if self._power is None:
            return
        # 乐观翻转：点下立刻变色，写失败由主窗口拨回
        new_state = self._power.state() is not True
        self._power.set_state(new_state)
        self.enabled_toggled.emit(self.scene.scene_id, new_state)

    def set_enabled(self, enabled: bool) -> None:
        """外部（主窗口）同步开关状态。"""
        if self._power is not None:
            self._power.set_state(enabled)

    def set_busy(self, busy: bool) -> None:
        if self._power is not None:      # 自动化：忙碌态交给电源按钮
            self._power.set_busy(busy)
            return
        if self._run_btn is None:
            return
        self._busy = busy
        self._run_btn.setEnabled(not busy)
        self._apply_run_btn()

    def _apply_run_btn(self) -> None:
        if self._run_btn is None:
            return
        radius = _RUN_BTN_SIZE // 2
        if self._busy:
            self._run_btn.setIcon(qta.icon('mdi.play', color=SiColors.ICON_DIM))
            self._run_btn.setStyleSheet(
                f"QPushButton#sceneRunBtn {{ background: {SiColors.SURFACE_PRESSED};"
                f" border: none; border-radius: {radius}px; }}"
            )
            return
        self._run_btn.setIcon(qta.icon('mdi.play', color=SiColors.WHITE))
        self._run_btn.setStyleSheet(
            f"QPushButton#sceneRunBtn {{ background: {SiColors.THEME}; border: none;"
            f" border-radius: {radius}px; }}"
            f"QPushButton#sceneRunBtn:hover {{ background: {SiColors.THEME_HOVER}; }}"
            f"QPushButton#sceneRunBtn:pressed {{ background: {SiColors.THEME_DIM}; }}"
        )

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
        self.style_data.background_color = QColor(
            SiColors.CARD_HOVER if self._hovered else SiColors.CARD
        )
        self.update()

    # 卡片空白处点击 → 打开详情；运行按钮是子控件，点击不会冒泡到这里，
    # 所以两个热点天然分开，不用额外的命中判断
    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt 命名约定)
        # 网格重建（切家庭/房间、主题切换）时旧卡片已被销毁，Qt 仍可能把
        # 在途的鼠标事件投递过来；此时访问 self 会抛 shiboken 的
        # "Internal C++ object already deleted"，刷满日志
        if not shiboken6.isValid(self):
            return
        if (event.button() == Qt.MouseButton.LeftButton
                and self.rect().contains(event.position().toPoint())):
            self.open_requested.emit(self.scene.scene_id)
        super().mouseReleaseEvent(event)
