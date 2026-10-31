# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""场景详情面板：这条场景会执行哪些动作。

主页「场景」标签下点击卡片空白处打开；右侧圆钮仍是直接执行，两者分开，
避免"想看明细却不小心把场景跑了"。动作文案由 core.service 翻译好，
这里只负责排布。
"""

import shiboken6
from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QScrollArea, QVBoxLayout, QWidget)

import qtawesome as qta

from app.core.models import SceneInfo
from app.ui.overlay_dialog import OverlayDialog
from app.ui.si_theme import SiColors, primary_button_qss, secondary_button_qss

_PANEL_W = 520
_PANEL_MIN_H = 300
_PANEL_MAX_H = 620


class SceneDetailDialog(OverlayDialog):
    """场景详情：动作明细 + 执行入口。"""

    run_requested = Signal(str)  # scene_id
    save_requested = Signal(str, str)  # scene_id, 新名称

    def __init__(self, scene: SceneInfo, parent=None, trigger_text: str = ""):
        super().__init__(parent)
        self._scene = scene
        self._trigger_text = trigger_text
        self.setWindowTitle(
            f"{'自动化' if scene.is_automation else '场景'} · {scene.name}")

        panel = self._panel
        panel.setFixedWidth(_PANEL_W)

        lay = QVBoxLayout(panel)
        lay.setContentsMargins(24, 20, 24, 18)
        lay.setSpacing(10)

        title_row = QHBoxLayout()
        title_row.setSpacing(6)
        self._name_edit = QLineEdit(scene.name)
        self._name_edit.setReadOnly(True)
        self._name_edit.setFont(QFont("Microsoft YaHei UI", 15, QFont.Weight.DemiBold))
        self._name_edit.setStyleSheet(
            f"QLineEdit {{ color: {SiColors.TEXT_PRIMARY}; background: transparent;"
            " border: 1px solid transparent; border-radius: 6px; padding: 2px 4px; }"
            f"QLineEdit[readOnly=\"false\"] {{ border-color: {SiColors.LINE};"
            f" background: {SiColors.CARD}; }}")
        self._name_edit.textChanged.connect(lambda _t: self._refresh_save_btn())
        title_row.addWidget(self._name_edit, stretch=1)

        self._edit_btn = QPushButton()
        self._edit_btn.setFixedSize(26, 26)
        self._edit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._edit_btn.setIcon(qta.icon("mdi.pencil", color=SiColors.TEXT_SECONDARY))
        self._edit_btn.setIconSize(QSize(15, 15))
        self._edit_btn.setToolTip("重命名")
        self._edit_btn.setStyleSheet(
            "QPushButton { background: transparent; border: none; border-radius: 13px; }"
            f"QPushButton:hover {{ background: {SiColors.CTRL_BG}; }}")
        self._edit_btn.clicked.connect(self._begin_edit)
        title_row.addWidget(self._edit_btn)
        lay.addLayout(title_row)

        count = len(scene.actions)
        sub_parts = [scene.home_name or "未知家庭"]
        if scene.is_automation:
            sub_parts.append("已启用" if scene.enabled else "已停用")
        else:
            sub_parts.append("手动执行")
        sub_parts.append(f"{count} 个动作" if count else "无动作明细")
        subtitle = QLabel(" · ".join(sub_parts))
        subtitle.setStyleSheet(
            f"color: {SiColors.TEXT_SECONDARY}; background: transparent;"
            " font-size: 9pt;")
        lay.addWidget(subtitle)

        if scene.is_automation:
            trigger = QLabel(
                f"触发条件：{trigger_text}" if trigger_text
                else "触发条件：读取中…（米家未返回）")
            trigger.setWordWrap(True)
            trigger.setStyleSheet(
                f"color: {SiColors.TEXT_SECONDARY}; background: {SiColors.CTRL_BG};"
                " border-radius: 8px; padding: 8px 10px; font-size: 9pt;")
            lay.addWidget(trigger)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.viewport().setAutoFillBackground(False)
        self._scroll.viewport().setStyleSheet("background: transparent;")
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget()
        host.setObjectName("sceneHost")
        self._list = QVBoxLayout(host)
        self._list.setContentsMargins(0, 0, 0, 0)
        self._list.setSpacing(8)
        self._scroll.setWidget(host)
        lay.addWidget(self._scroll, stretch=1)

        if scene.actions:
            for index, action in enumerate(scene.actions, start=1):
                self._list.addWidget(self._make_action_row(index, action))
        else:
            empty = QLabel("米家没有返回这条场景的动作明细，只能看到它归属的家庭。")
            empty.setWordWrap(True)
            empty.setStyleSheet(
                f"color: {SiColors.TEXT_MUTED}; background: transparent;"
                " font-size: 9pt; padding: 12px 0;")
            self._list.addWidget(empty)
        self._list.addStretch(1)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch(1)
        self._save_btn = QPushButton("保存")
        self._save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._save_btn.clicked.connect(self._save_name)
        self._save_btn.setStyleSheet(primary_button_qss("8px 20px"))
        self._save_btn.setEnabled(False)
        btn_row.addWidget(self._save_btn)
        close_btn = QPushButton("关闭")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.reject)
        close_btn.setStyleSheet(secondary_button_qss("8px 20px"))
        btn_row.addWidget(close_btn)
        # 自动化由触发条件驱动，没有"执行"这个动作，不摆按钮
        if not scene.is_automation:
            run_btn = QPushButton("执行场景")
            run_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            run_btn.clicked.connect(self._run)
            run_btn.setStyleSheet(primary_button_qss("8px 20px"))
            btn_row.addWidget(run_btn)
        lay.addLayout(btn_row)

        self._apply_styles()
        panel.setFixedHeight(self._panel_height())

    # ---------- 内容 ----------

    def _make_action_row(self, index: int, action) -> QWidget:
        """一行动作：序号 + 设备名 / 动作描述 + 可选延时标签。"""
        box = QFrame()
        box.setObjectName("sceneRow")
        lay = QHBoxLayout(box)
        lay.setContentsMargins(12, 9, 14, 9)
        lay.setSpacing(10)

        badge = QLabel(str(index))
        badge.setFixedSize(20, 20)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setStyleSheet(
            f"QLabel {{ color: {SiColors.TEXT_SECONDARY};"
            f" background: {SiColors.CTRL_BG}; border-radius: 10px;"
            " font-size: 8pt; }")
        lay.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        name = QLabel(action.device_name or "设备")
        name.setStyleSheet(
            f"color: {SiColors.TEXT_PRIMARY}; background: transparent;"
            " font-size: 10pt; font-weight: 600;")
        text_col.addWidget(name)
        detail = QLabel(action.text or "未知动作")
        detail.setWordWrap(True)
        detail.setStyleSheet(
            f"color: {SiColors.TEXT_SECONDARY}; background: transparent;"
            " font-size: 9pt;")
        text_col.addWidget(detail)
        lay.addLayout(text_col, stretch=1)

        if action.delay:
            delay = QLabel(f"延时 {action.delay} 秒")
            delay.setStyleSheet(
                f"QLabel {{ color: {SiColors.THEME}; background: transparent;"
                " font-size: 8pt; }}")
            lay.addWidget(delay, 0, Qt.AlignmentFlag.AlignTop)
        return box

    def _panel_height(self) -> int:
        """按动作条数估算高度：少了不留白，多了滚。"""
        rows = max(len(self._scene.actions), 1)
        height = 150 + rows * 58
        return max(_PANEL_MIN_H, min(_PANEL_MAX_H, height))

    def _begin_edit(self) -> None:
        self._name_edit.setReadOnly(False)
        self._name_edit.setFocus()
        self._name_edit.selectAll()
        self._refresh_save_btn()

    def _refresh_save_btn(self) -> None:
        changed = self._name_edit.text().strip() != self._scene.name
        self._save_btn.setEnabled(changed)

    def _save_name(self) -> None:
        name = self._name_edit.text().strip()
        if not name or name == self._scene.name:
            return
        scene_id = self._scene.scene_id
        self.accept()
        # 关闭后再写，避免主窗口在模态期间重建网格
        QTimer.singleShot(0, lambda: self.save_requested.emit(scene_id, name))

    def _run(self) -> None:
        scene_id = self._scene.scene_id
        self.accept()
        # 面板关掉之后再执行，避免主窗口在模态期间刷 Toast/重建网格
        QTimer.singleShot(0, lambda: self.run_requested.emit(scene_id))

    # ---------- 样式与定位 ----------

    def _apply_styles(self) -> None:
        self._panel.setStyleSheet(
            f"QFrame#overlayPanel {{ background: {SiColors.WINDOW_BG};"
            f" border: 1px solid {SiColors.LINE}; border-radius: 16px; }}")
        self._scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollBar:vertical { background: transparent; width: 6px;"
            " margin: 0 2px 0 0; }"
            f"QScrollBar::handle:vertical {{ background: {SiColors.SCROLLBAR};"
            " border-radius: 3px; min-height: 30px; }"
            f"QScrollBar::handle:vertical:hover {{ background: {SiColors.SCROLLBAR_HOVER}; }}"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical"
            " { background: none; }")
        # 行卡片的底色必须用显式选择器，裸声明拼在后面会整条规则失效
        self._scroll.widget().setStyleSheet(
            "QWidget#sceneHost { background: transparent; }"
            "QFrame#sceneRow {"
            f" background: {SiColors.CARD}; border: 1px solid {SiColors.LINE};"
            " border-radius: 12px; }")

    def retheme(self) -> None:
        super().retheme()
        self._apply_styles()

    def showEvent(self, event) -> None:  # noqa: N802 (Qt 命名约定)
        super().showEvent(event)
        # 与状态面板一致：基类的淡入会被紧接着的几何调整打断，
        # 先压回全透明，铺满父窗口后再统一淡入
        self.setWindowOpacity(0.0)
        QTimer.singleShot(0, self._fill_and_place)

    def _fill_and_place(self) -> None:
        # 延迟回调可能在面板关闭后才到，先确认底层对象还活着
        if not shiboken6.isValid(self):
            return
        self._fill_parent_window()
        self._place_overlay()
        self._fade_in()

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt 命名约定)
        super().resizeEvent(event)
        self._panel.move(
            (self.width() - self._panel.width()) // 2,
            (self.height() - self._panel.height()) // 2)
