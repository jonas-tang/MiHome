# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""界面层专用的数据模型。

刻意与 mijiaAPI 的原始 dict / 对象结构解耦：上游返回格式变化时只需调整
core.service 适配层，界面层永远只依赖这里的稳定类型。
"""

from dataclasses import dataclass


@dataclass
class DeviceInfo:
    """设备列表条目，只保留界面展示需要的最小字段集。

    spec_type 是云端下发的设备规格 URN（可能为空：手表、第三方牙刷
    等无公开规格的设备），上游 5.x 按它拉取设备规格，取代旧版的 model。
    """

    did: str
    name: str
    model: str
    home_name: str
    room_name: str
    online: bool
    spec_type: str = ""
    # 虚拟子设备归属：多键面板拆出的按键、网关下的子机。
    # 云端经 extra.split.parentId(+moduleId) 或顶层 parent_id 给出，
    # 是唯一可靠的父子依据——设备被改过名也照样对得上（实测 77/77 命中）。
    # 空串表示这台设备自己就是独立的物理设备（或父体本身）。
    parent_did: str = ""
    # 子设备占用的通道（即规格里的 siid），面板按键为 2、3、4…；
    # 云端没给（网关子机）时为 None
    parent_siid: int | None = None
    # 父设备的类型，决定父卡还能不能有开关：
    # - "split"：多键面板拆出来的按键，父体没有属于自己的那一路，
    #   按"第一条 on"去控必然控错（会关掉第一路），所以父体不可控
    # - "proxy"：网关下的子机，网关自身还有独立的 on，照常可控
    parent_kind: str = ""

    @property
    def is_sub_device(self) -> bool:
        """是否为某个物理设备拆出来的子设备/通道。"""
        return bool(self.parent_did)


@dataclass
class PropInfo:
    """单个属性的元数据，控件工厂据此决定生成哪种控件。

    range 来自米家 spec 的 valueRange；value_list 为枚举选项，
    每项含 value/description，可能附带 desc_zh_cn 中文描述。
    """

    name: str
    desc: str
    type: str  # bool / int / uint / float / string
    readable: bool
    writable: bool
    range: tuple | None = None  # (min, max, step)
    value_list: list | None = None


@dataclass
class ActionInfo:
    name: str
    desc: str


@dataclass
class DeviceDetail:
    """点击某台设备后加载的控制面板所需全部信息。"""

    did: str
    name: str
    model: str
    props: list[PropInfo]
    actions: list[ActionInfo]


@dataclass
class SceneInfo:
    """米家手动场景（米家 APP → 智能 → 手动控制 中创建的项）。

    home_id 是执行场景时上游 run_scene 所需的家庭 ID；
    home_name 为家庭显示名（卡片副标题用），为空表示归属未知。
    """

    scene_id: str
    name: str
    home_id: str
    home_name: str = ""


def is_speaker(device: DeviceInfo) -> bool:
    """小爱音箱判定：音频控制栏、语音入口共用同一标准。

    用包含匹配而非限定 xiaomi 前缀，第三方音箱（模型含
    wifispeaker）同样具备音量/静音与文本指令能力。
    """
    return "wifispeaker" in device.model
