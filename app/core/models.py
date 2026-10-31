# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""界面层专用的数据模型。

刻意与 mijiaAPI 的原始 dict / 对象结构解耦：上游返回格式变化时只需调整
core.service 适配层，界面层永远只依赖这里的稳定类型。
"""

from dataclasses import dataclass, field


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
class SceneAction:
    """场景里的一条动作，文案由 core.service 翻译好直接展示。

    米家把每条动作存成 `{order, payload_json: {command, delay_time,
    device_name, did, model, value: [{siid, piid, value}]}}`；
    界面层不该再碰 siid/piid，所以这里只保留人话。
    """

    order: int = 0
    delay: int = 0            # 执行前延时（秒），0 表示立即
    did: str = ""             # 动作指向的设备（用于查当前设备名/图标）
    # 设备显示名：优先用设备列表里的当前名称（与设备页一致），
    # 米家场景里存的是**创建场景那一刻**的名字，改名后不会同步
    device_name: str = ""
    text: str = ""            # 如「开关 → 开」「亮度 → 60」
    model: str = ""           # 设备型号，取图标用；空表示没有对应设备


@dataclass
class SceneInfo:
    """米家手动场景（米家 APP → 智能 → 手动控制 中创建的项）。

    home_id 是执行场景时上游 run_scene 所需的家庭 ID；
    home_name 为家庭显示名（卡片副标题用），为空表示归属未知。
    actions 是这条场景会执行的动作明细（详情面板展示），
    接口没给明细时为空列表。
    """

    scene_id: str
    name: str
    home_id: str
    home_name: str = ""
    actions: list[SceneAction] = field(default_factory=list)
    # 自动场景（米家里的"自动化"）：由触发条件驱动，不能手动执行；
    # 手动场景（"手动控制"）才是点一下就跑的
    is_automation: bool = False
    enabled: bool = True


def is_speaker(device: DeviceInfo) -> bool:
    """小爱音箱判定：音频控制栏、语音入口共用同一标准。

    用包含匹配而非限定 xiaomi 前缀，第三方音箱（模型含
    wifispeaker）同样具备音量/静音与文本指令能力。
    """
    return "wifispeaker" in device.model
