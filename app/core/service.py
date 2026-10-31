# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""mijiaAPI 适配层——全项目唯一允许 import mijiaAPI 的模块。

上游库只从 PyPI 安装升级（版本锁 5.x，见 pyproject.toml），接口一旦变化
只需要修改这里；界面层与线程层完全不感知第三方类型的存亡。

依赖的上游半公开方法说明：扫码登录拆成 _get_qr_login_data 与
_complete_qr_login 两步使用，是因为上游的 login() 会把二维码打印到
终端，图形界面拿不到；官方 MCP server 也采用同样的两步组合。

设备规格自上游 5.x 起按 spec_type（云端下发的规格 URN）获取，
取代旧版按 model 在 home.miot-spec.com 抓页解析的方式。
"""

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import mijiaAPI as _mijia_api_pkg
from mijiaAPI import (
    DeviceNotFoundError,
    GetDeviceInfoError,
    get_device_info,
    mijiaAPI,
    mijiaDevice,
)
from mijiaAPI.devices import DevAction, DevProp
from mijiaAPI.miutils import generate_enc_params, gen_nonce, get_signed_nonce

from . import icon_store, product_name_store, spec_zh
from .models import (ActionInfo, DeviceDetail, DeviceInfo, PropInfo, SceneAction,
                     SceneInfo)
from .net_guard import install_default_timeout

# 上游多处 HTTP 调用没有 timeout（登录取二维码、刷新 token、设备列表），
# 网络不通时会无限等待、界面永远转圈。在所有网络调用发生前补上默认超时。
install_default_timeout()

logger = logging.getLogger(__name__)

# 单次批量属性读取的设备数上限，防止请求体过大被网关拒绝。
# 32 台在线设备时：15 → 3 次请求，20 → 2 次请求；每次往返约 150~600ms，
# 少一次就少几百毫秒的显示延迟。
_BATCH_SIZE = 20
# 虚拟子设备的 did 后缀：<父did>.sN，N 为该路对应的 siid
_SUB_DEVICE_SUFFIX = re.compile(r"\.s(\d+)$")
# 网关代理子设备的 did 形态：proxy.<父did>.<序号>（如空调网关下的每台内机）。
# 与 .sN 不同，这类子机没有"第几路"的概念，本身有完整的 on 属性
_PROXY_DID = re.compile(r"^proxy\.(\d+)\.\d+$")


def parse_parent_link(did: str, extra: dict | None = None,
                      parent_id: str | None = None) -> tuple[str, int | None, str]:
    """推出设备归属的父设备 did、通道号与父设备类型。

    以云端字段为准（改名也认得出）：
    - 多键面板拆出的按键：extra.split.parentId + extra.split.moduleId
      → 类型 "split"，父体自己没有那一路上，不可控
    - 网关下的子机：顶层 parent_id → 类型 "proxy"，父体自身仍可控
    云端没给时按 did 形态兜底，保证旧接口/旧缓存也能归类。

    返回 ("", None, "") 表示这台设备不属于任何父设备。
    """
    split = {}
    if isinstance(extra, dict):
        maybe = extra.get("split")
        if isinstance(maybe, dict):
            split = maybe
    pid = str(split.get("parentId") or "").strip()
    if pid:
        siid = split.get("moduleId")
        try:
            siid = int(siid) if siid is not None else None
        except (TypeError, ValueError):
            siid = None
        return pid, siid, "split"
    pid = str(parent_id or "").strip()
    if pid:
        return pid, None, "proxy"
    match = _SUB_DEVICE_SUFFIX.search(did)
    if match:
        return did[:match.start()], int(match.group(1)), "split"
    match = _PROXY_DID.match(did)
    if match:
        return match.group(1), None, "proxy"
    return "", None, ""
# spec 并发拉取线程数；目标是 miot-spec.org 的独立请求，无会话竞争
_SPEC_WORKERS = 8
# 产品页中文名的并发数：同一站点的独立页面请求，8 路实测稳定
_NAME_WORKERS = 8


class ServiceError(Exception):
    """携带用户可直接阅读的中文信息，界面层直接展示 message 即可。"""


def _wrap_error(exc: Exception, context: str) -> ServiceError:
    # 上游异常的 args 结构各不相同，原样拼接足够定位问题又不至于暴露噪音；
    # 调用方统一用 raise ... from exc 保持原始堆栈链
    detail = "; ".join(str(a) for a in exc.args)
    return ServiceError(f"{context}: {detail}")


class MijiaService:
    def __init__(self):
        # 认证文件沿用上游默认位置 ~/.config/mijia-api/auth.json，
        # 这样 CLI 里扫过的码在 GUI 直接生效，反之亦然
        self._api = self._init_api()
        self._device_cache: dict[str, mijiaDevice] = {}
        # did -> (model, spec_type, name) 索引，批量读状态与共享设备组装时
        # 避免反复拉设备列表；自有与共享设备都在其中
        self._device_index: dict[str, tuple[str, str, str]] = {}
        # spec_type -> spec 内存缓存，轮询命中后不再读文件/打网络
        self._spec_cache: dict[str, dict | None] = {}
        # 产品页中文名缓存：model -> 中文名或 None（已确认无中文名，不再重查）。
        # 启动即从磁盘加载：这份数据只有米家产品页一个来源，抓到就不会变，
        # 落盘后第二次启动不用再等二十多个型号依次抓一遍
        self._product_page_names: dict[str, str | None] = product_name_store.load()
        # model -> 图标 URL 或 None（拉取失败，会话内不再重试）；
        # 启动即从磁盘加载，此后只增不删（见 icon_store）
        self._icon_cache: dict[str, str | None] = icon_store.load_urls()
        # 家庭名 -> 家庭 ID 映射，场景列表按家庭过滤用；懒加载缓存
        self._home_ids: dict[str, str] | None = None
        # 多键面板的父体 did 集合，由 list_devices 填充。这类设备自己
        # 没有独立负载——规格里只有各通道的 on-2..on-N，按"第一条 on"
        # 去控就会误控第一路；网关父体不在此列（它有属于自己的 on）
        self._virtual_parents: set[str] = set()

    def _init_api(self) -> mijiaAPI:
        """构造上游客户端；认证文件损坏时隔离坏文件并降级为未登录。

        上游构造函数同步读取并解析 auth.json 且写入非原子，进程被杀
        可能留下半截文件；不在这里接住，异常会穿透到 QApplication
        之前，用户只会看到进程无声退出。
        """
        try:
            return mijiaAPI()
        except (json.JSONDecodeError, KeyError, TypeError, OSError) as exc:
            auth_path = Path.home() / ".config" / "mijia-api" / "auth.json"
            try:
                auth_path.replace(auth_path.with_name("auth.json.corrupt"))
            except OSError:
                pass
            logger.warning("认证文件损坏已隔离为 auth.json.corrupt，请重新扫码登录: %s", exc)
            # 文件已移除，上游按「无认证文件」处理，available 恒为 False
            return mijiaAPI()

    @staticmethod
    def api_version() -> str:
        """上游 mijiaAPI 的实际安装版本（关于页展示）。

        运行时从包元信息读取而非从依赖清单抄写，包升级/降级后
        界面展示自动跟随；取不到时返回空串由调用方决定是否展示。
        """
        return getattr(_mijia_api_pkg, "__version__", "")

    # ---------- 登录 ----------

    def login_status(self) -> bool:
        try:
            return self._api.available
        except Exception:
            return False

    def qr_login_begin(self) -> dict | None:
        """获取扫码登录数据。

        返回 None 表示本地凭据已自动刷新、无需扫码；
        否则返回含 loginUrl 的 dict，交由界面渲染二维码。
        """
        try:
            data = self._api._get_qr_login_data()
        except Exception as exc:
            # 不只捕 LoginError：断网时 requests 连接异常也要转成
            # 界面可直接展示的中文信息
            raise _wrap_error(exc, "获取登录二维码失败") from exc
        if data.get("refreshed"):
            return None
        return data

    def qr_login_wait(self, login_data: dict) -> None:
        """长轮询等待用户扫码，阻塞可达两分钟，必须放在后台线程调用。"""
        try:
            self._api._complete_qr_login(login_data)
        except Exception as exc:
            raise _wrap_error(exc, "扫码登录未完成") from exc

    # ---------- 设备列表 ----------

    def list_devices(self) -> list[DeviceInfo]:
        """拉取全部家庭和共享设备并补齐房间归属。

        设备信息本身不含房间字段，参照 CLI 的实现思路：
        遍历每个家庭的 roomlist[].dids[] 反查出 did -> (家庭, 房间)。
        """
        try:
            homes = self._api.get_homes_list()
            devices = self._api.get_devices_list() + self._api.get_shared_devices_list()
        except Exception as exc:
            raise _wrap_error(exc, "获取设备列表失败") from exc

        location: dict[str, tuple[str, str]] = {}
        for home in homes:
            for room in home.get("roomlist", []):
                for did in room.get("dids", []) or []:
                    location[str(did)] = (home["name"], room["name"])

        result = []
        split_parents: set[str] = set()
        for d in devices:
            did = str(d["did"])
            home_name, room_name = location.get(did, ("未知", "未知"))
            parent_did, parent_siid, parent_kind = parse_parent_link(
                did, d.get("extra"), d.get("parent_id"))
            if parent_kind == "split":
                split_parents.add(parent_did)
            result.append(DeviceInfo(
                did=did,
                name=d.get("name", did),
                model=d.get("model", ""),
                home_name=home_name,
                room_name=room_name,
                online=bool(d.get("isOnline", False)),
                # 云端对无公开规格的设备（手表、第三方牙刷等）不下发该字段
                spec_type=d.get("spec_type") or "",
                parent_did=parent_did,
                parent_siid=parent_siid,
                parent_kind=parent_kind,
            ))
        # 只把"多键面板的父体"记为不可控：这类设备规格里只有各通道的
        # on-2..on-N，按第一条 on 写下去就是控第一路。网关父体自己有 on，
        # 照常可控，不能一起禁掉
        self._virtual_parents = split_parents
        return sorted(result, key=lambda x: (x.home_name, x.room_name, x.name))

    # ---------- 设备控制 ----------

    def device_detail(self, did: str) -> DeviceDetail:
        dev = self._get_device(did)
        # prop_list 对属性名里的 '-' 额外注册了一份 '_' 别名键，
        # 两者指向同一对象，按对象身份去重避免面板出现重复控件
        seen: set[int] = set()
        props: list[PropInfo] = []
        for prop in dev.prop_list.values():
            if id(prop) in seen:
                continue
            seen.add(id(prop))
            props.append(PropInfo(
                name=prop.name,
                desc=prop.desc or prop.name,
                type=prop.type,
                readable="r" in prop.rw,
                writable="w" in prop.rw,
                range=tuple(prop.range) if prop.range else None,
                value_list=prop.value_list,
            ))
        actions = [
            ActionInfo(name=a.name, desc=a.desc or a.name)
            for a in dev.action_list.values()
        ]
        return DeviceDetail(did=dev.did, name=dev.name, model=dev.model,
                            props=props, actions=actions)

    def read_prop(self, did: str, name: str):
        dev = self._get_device(did)
        try:
            return dev.get(name)
        except Exception as exc:
            raise _wrap_error(exc, f"读取属性 {name} 失败") from exc

    def read_props(self, did: str, names: list[str]) -> dict[str, Any | None]:
        """批量读取同一台设备的多个属性，一次请求替代逐个轮询。

        逐个读取时上游每个属性固定 sleep 0.5 秒，详情面板十几项
        属性要等近十秒；合并为批量请求后整面板一次往返即可完成。
        """
        dev = self._get_device(did)
        queries: list[dict] = []
        key_to_name: dict[tuple, str] = {}
        for name in names:
            prop = dev.prop_list.get(name)
            if prop is None or "r" not in prop.rw:
                continue
            method = prop.method.copy()
            method["did"] = dev.did
            queries.append(method)
            key_to_name[(method["siid"], method["piid"])] = name

        result: dict[str, Any | None] = {name: None for name in key_to_name.values()}
        for start in range(0, len(queries), _BATCH_SIZE):
            batch = queries[start:start + _BATCH_SIZE]
            try:
                rets = self._api.get_devices_prop(batch)
            except Exception as exc:
                raise _wrap_error(exc, "批量读取属性失败") from exc
            for item in rets:
                key = (item.get("siid"), item.get("piid"))
                name = key_to_name.get(key)
                if name is None:
                    continue
                result[name] = item["value"] if item.get("code") == 0 else None
        return result

    def write_prop(self, did: str, name: str, value) -> None:
        dev = self._get_device(did)
        try:
            dev.set(name, value)
        except Exception as exc:
            raise _wrap_error(exc, f"设置属性 {name} 失败") from exc

    def run_action(self, did: str, name: str, params=None) -> None:
        dev = self._get_device(did)
        try:
            if params is not None:
                # 小爱类文本指令需走 _in 通道（见 mijiaAPI __main__.py:523
                # wifispeaker.run_action('execute-text-directive', _in=[prompt, quiet])）
                if name in ("execute-text-directive", "play-text", "play-music", "play-radio"):
                    # 统一按 _in 传递，兼容单字符串与列表
                    in_val = params if isinstance(params, (list, tuple)) else [params]
                    # execute-text-directive 为 [文本, 是否静默](0/1)，缺省按非静默补齐
                    if name == "execute-text-directive" and len(in_val) == 1:
                        in_val = [in_val[0], 0]
                    dev.run_action(name, _in=in_val)
                elif isinstance(params, (list, tuple)):
                    dev.run_action(name, params)
                else:
                    dev.run_action(name, [params])
            else:
                # 文本类动作无参必报 -704220025，这里直接引导上层弹输入
                if name in ("execute-text-directive", "play-text"):
                    raise ServiceError(f"动作 {name} 需要文本参数")
                dev.run_action(name)
        except ServiceError:
            raise
        except Exception as exc:
            raise _wrap_error(exc, f"执行动作 {name} 失败") from exc

    def _get_device(self, did: str) -> mijiaDevice:
        # 构造 mijiaDevice 本身要发两次网络请求（设备列表 + spec 拉取），
        # 按 did 缓存实例，面板切换时才不会反复打接口
        if did not in self._device_cache:
            if did not in self._device_index:
                self._refresh_device_index()
            entry = self._device_index.get(did)
            if entry is not None and not entry[1]:
                # 云端未下发 spec_type（手表、第三方牙刷等无公开规格的
                # 设备），上游 5.x 会拿空值拉规格抛 ValueError；这里提前
                # 拦截给出用户可读提示，仅对确实存在于列表的无规格设备生效
                raise ServiceError(
                    "该设备无公开的功能规格（常见于仅蓝牙连接的产品），"
                    "无法提供控制面板")
            try:
                self._device_cache[did] = mijiaDevice(self._api, did=did)
            except DeviceNotFoundError:
                # 上游构造只查自有设备列表（不含共享设备），共享设备
                # 必然在此失败；按上游 __init__ 的字段手工组装，后续
                # get/set/run_action 与自有设备走完全相同的代码路径
                self._device_cache[did] = self._build_shared_device(did)
            except GetDeviceInfoError as exc:
                # 无公开功能规格的设备（常见于仅蓝牙连接的产品）：
                # 没有属性/动作可控制，给出用户可读的明确提示
                raise ServiceError(
                    "该设备无公开的功能规格（常见于仅蓝牙连接的产品），"
                    "无法提供控制面板") from exc
            except Exception as exc:
                raise _wrap_error(exc, "加载设备信息失败") from exc
            self._patch_device_zh(self._device_cache[did])
        return self._device_cache[did]

    @staticmethod
    def _patch_device_zh(dev: mijiaDevice) -> None:
        """给 mijiaDevice 的属性/动作描述补齐中文兜底。

        上游构造从磁盘缓存读原始 spec，部分品类（音箱、温湿度计等）
        缺中文 comment 会退化为纯英文；缓存文件不归适配层管、无法在
        源头补，只能在对象组装后按名补一条，与 _fetch_spec 的内存
        dict 路径保持同源逻辑（见 spec_zh）。
        """
        seen: set[int] = set()
        for prop in dev.prop_list.values():
            if id(prop) in seen:
                continue  # 连字符与下划线两个键指向同一对象，去重防双写
            seen.add(id(prop))
            prop.desc = spec_zh.prop_desc_zh(prop.name, prop.desc)
        for act in dev.action_list.values():
            act.desc = spec_zh.action_desc_zh(act.name, act.desc)

    def _build_shared_device(self, did: str) -> mijiaDevice:
        """为共享设备手工组装 mijiaDevice（上游构造器不支持共享设备）。"""
        if did not in self._device_index:
            self._refresh_device_index()
        model, spec_type, name = self._device_index.get(did, ("", "", ""))
        if not model:
            raise ServiceError(f"未找到设备 {did}")
        if not spec_type:
            raise ServiceError(
                "该设备无公开的功能规格（常见于仅蓝牙连接的产品），"
                "无法提供控制面板")
        try:
            dev_info = get_device_info(
                spec_type, cache_path=self._api.auth_data_path.parent)
        except GetDeviceInfoError as exc:
            raise ServiceError(
                "该设备无公开的功能规格（常见于仅蓝牙连接的产品），"
                "无法提供控制面板") from exc
        except Exception as exc:
            raise _wrap_error(exc, "加载设备信息失败") from exc

        dev = mijiaDevice.__new__(mijiaDevice)
        dev.api = self._api
        dev.did = did
        dev.model = model
        dev.name = name or dev_info.get("name", did)
        dev.sleep_time = 0.5
        # prop_list/action_list 必须最后赋值：上游 __setattr__ 在
        # prop_list 存在后会拦截同名属性写入并转发为设备控制
        prop_list: dict[str, DevProp] = {}
        for prop in dev_info.get("properties", []):
            prop_obj = DevProp(prop)
            prop_list[prop["name"]] = prop_obj
            if "-" in prop["name"]:
                prop_list[prop["name"].replace("-", "_")] = prop_obj
        dev.prop_list = prop_list
        dev.action_list = {
            act["name"]: DevAction(act) for act in dev_info.get("actions", [])
        }
        return dev

    # ---------- 开关状态（卡片快速控制用） ----------

    def power_state(self, did: str) -> bool | None:
        """读取单台设备开关状态；无可写开关属性的设备返回 None。

        走 power_states 的 spec 批量路径：model/spec 全部命中缓存时
        只剩一次属性请求，避免为单个开关构造 mijiaDevice（两次请求）。
        """
        return self.power_states([did]).get(did)

    def toggle_power(self, did: str, current: bool | None = None) -> bool:
        """切换到开关的反面，返回新状态。

        `current` 是调用方已知的当前状态（界面层的开关记忆）：给了就
        直接写反值，**省掉一次读往返**——米家云端单次往返 0.3~0.8 秒，
        少一次就能让点击明显更跟手。

        不给 `current` 时退回"先读后写"，读与写串在同一任务里完成
        （调用方经单线程队列提交），否则两次轮询之间会出现读后写的竞态。
        """
        if current is None:
            current = self.power_state(did)
        if current is None:
            raise ServiceError("设备不支持开关控制或已离线")
        new_state = not current
        self.set_power_state(did, new_state)
        return new_state

    @staticmethod
    def _parent_did(did: str) -> str | None:
        """虚拟子设备 did → 父设备 did；不是子设备返回 None。

        `1234567890.s3` → `1234567890`；普通 did 原样返回 None。
        """
        m = _SUB_DEVICE_SUFFIX.search(str(did))
        return str(did)[:m.start()] if m else None

    def set_power_state(self, did: str, state: bool) -> None:
        """写入开关状态：spec method 直接构造批量写请求。

        不经 mijiaDevice.set——那条路要构造设备对象并逐属性写入，
        首次点击要等两三次网络往返；直写批量接口一次请求完成。
        子设备的请求实际发往父 did（见 `_switch_target`），且不再试探
        子 did：那样每次都会白等约 6 秒才拿到拒绝。

        多通道设备的父体在这里直接拒绝：它没有自己的那一路，按第一条
        on 写下去就是去控第一路（见 power_states 里的同款判断）。
        """
        if did in self._virtual_parents:
            raise ServiceError(
                "这是多通道设备的父设备，本身没有独立开关；请在详情页控制具体通道")
        info = self._specs_for([did]).get(did)
        method = self._find_on_method(info, did) if info else None
        if method is None:
            raise ServiceError("设备不支持开关控制")
        self._write_power(self._switch_target(did), method, state)

    def _write_power(self, did: str, method: dict, state: bool) -> None:
        try:
            rets = self._api.set_devices_prop(
                [{"did": did, **method, "value": state}]
            )
        except Exception as exc:
            raise _wrap_error(exc, "设置开关状态失败") from exc
        # 写接口的 code 语义与读不同：上游把 code∈(0,1) 的响应统一
        # 改写为「成功」（apis.py set_devices_prop 出口），其余一律失败
        items = rets if isinstance(rets, list) else [rets]
        for item in items:
            if not isinstance(item, dict):
                continue
            code = item.get("code")
            if code in (0, 1):
                continue
            raise ServiceError(
                f"设置失败（{item.get('message', '未知错误')}，错误码 {code}）"
            )

    def power_states(self, dids: list[str]) -> dict[str, bool | None]:
        """批量读取多台设备的开关状态。

        三段式压缩网络开销：did->model 映射增量补齐（一次设备列表）、
        spec 线程池并发拉取（本地文件缓存后零开销）、属性按批合并
        读取。全部命中缓存时整批设备只剩一两次属性请求。

        虚拟子设备（`<父did>.sN`）的请求目标见 `_switch_target`：
        实测米家云端**只认父 did + 该路 siid**，子 did 一律返回
        -704083036，所以学到之后直接用父 did，不再每次白跑一次失败请求。
        """
        spec_map = self._specs_for(dids)

        queries: list[dict] = []
        owners: list[str] = []          # 与 queries 一一对应：发起请求的设备 did
        result: dict[str, bool | None] = {}
        method_of: dict[str, dict] = {}
        target_of: dict[str, str] = {}
        for did in dids:
            if did in self._virtual_parents:
                # 父体本身没有独立负载：规格里只有 on-2..on-N（那是各按键
                # 的），以前回退到第一条 on 会去控第一路，和「按键1」那张
                # 卡片重复——用户点父设备的开关，关掉的却是第一路。
                result[did] = None
                continue
            info = spec_map.get(did)
            method = self._find_on_method(info, did) if info else None
            if method is None:
                # spec 缺失或无开关属性，直接记为无能力。
                # 这是常态（温度计、门磁、摄像头），按 debug 记：
                # 按 WARNING 会在 debug.log 里造出一堆假故障
                result[did] = None
                entry = self._device_index.get(did, ("", "", ""))
                self._log_switch_failure(
                    did, None, None,
                    "规格无 on 属性或规格未取到（spec_type=%r）"
                    % (entry[1] if len(entry) > 1 else ""),
                    level=logging.DEBUG)
                continue
            method_of[did] = method
            target = self._switch_target(did)
            target_of[did] = target
            queries.append({"did": target, **method})
            owners.append(did)

        failed: list[str] = []
        for start in range(0, len(queries), _BATCH_SIZE):
            batch = queries[start:start + _BATCH_SIZE]
            batch_owners = owners[start:start + _BATCH_SIZE]
            try:
                rets = self._api.get_devices_prop(batch)
            except Exception as exc:
                raise _wrap_error(exc, "批量读取设备状态失败") from exc
            # 响应按 (did, siid) 回填：同一父设备的多路共用一个 did，
            # 只按 did 匹配会把三路结果全算到最后一路
            by_key = {(str(item.get("did")), item.get("siid")): item
                      for item in rets if isinstance(item, dict)}
            for index, owner in enumerate(batch_owners):
                method = method_of[owner]
                item = by_key.get((target_of[owner], method.get("siid")))
                if item is None and index < len(rets) and isinstance(rets[index], dict):
                    item = rets[index]          # 响应缺 siid 时按顺序对齐
                if item is not None and item.get("code") == 0:
                    result[owner] = bool(item["value"])
                else:
                    # 读失败（离线 / 该路被拒）不能记成 None——None 的语义是
                    # "确认没有开关能力"，一旦落进记忆，这台设备就再也不会
                    # 被轮询（force=True 会跳过 None），按钮永久消失。
                    # 这里只把它列进待兜底清单，兜底仍失败就不写进结果。
                    failed.append(owner)

        # 读不到的逐台留一次线索（不按轮询刷屏）
        for did in failed:
            if result.get(did) is None:
                self._log_switch_failure(
                    did, method_of.get(did), None, "读取失败")
        return result

    # ---------- 虚拟子设备的请求目标 ----------

    def _switch_target(self, did: str) -> str:
        """这条开关请求实际发给谁：子设备一律发给父设备。

        实测（2026-10-18，真实账号）：云端对子 did（`<父did>.sN`）的读取
        **不是快速报错，而是拖到 ~6 秒才返回 -704083036**；而父 did + 该路
        siid 只要 ~50ms。所以"先试子 did、失败再退回父 did"的做法会让
        每次启动后的首轮轮询白等 6 秒——这是子设备状态显示延迟的主因。

        既然父 did 才是唯一可用入口，就直接走父 did，不再试探。
        若将来某厂商云端只认子 did，只需把这里改成返回 `did`。
        """
        parent = self._parent_did(did)
        return parent if parent is not None else did

    def _log_switch_failure(self, did: str, method: dict | None,
                            item: dict | None, stage: str,
                            level: int = logging.WARNING) -> None:
        """同一台设备只记一次失败详情，避免 5 秒轮询把日志刷满。

        `level` 供"本来就没有开关属性"用：温度计、门磁这类设备不是失败，
        按 WARNING 记会让 debug.log 里全是假故障，真正的问题反而被淹掉。
        """
        logged = getattr(self, "_switch_diag_logged", None)
        if logged is None:
            logged = set()
            self._switch_diag_logged = logged
        if did in logged:
            return
        logged.add(did)
        entry = self._device_index.get(did, ("", "", ""))
        method = method or {}
        code = item.get("code") if isinstance(item, dict) else None
        message = item.get("message") if isinstance(item, dict) else None
        logger.log(
            level,
            "开关状态读取失败 did=%s name=%r model=%r spec_type=%r "
            "siid=%s piid=%s 阶段=%s code=%s message=%s",
            did, entry[2] if len(entry) > 2 else "", entry[0] if entry else "",
            entry[1] if len(entry) > 1 else "",
            method.get("siid"), method.get("piid"), stage, code, message)

    def _fetch_spec(self, spec_type: str, cache_dir) -> dict | None:
        if not spec_type:
            return None
        try:
            info = get_device_info(spec_type, cache_path=cache_dir)
        except (json.JSONDecodeError, UnicodeDecodeError):
            # 上游缓存文件损坏（多见于并发写坏）。不删掉重拉的话，
            # 该规格的能力信息将永久缺失，开关与温湿度控件随之消失；
            # 缓存文件按 spec_type 命名，冒号替换为下划线（与上游一致）
            try:
                (Path(cache_dir)
                 / f"{spec_type.replace(':', '_')}.json").unlink(missing_ok=True)
            except OSError:
                return None
            try:
                info = get_device_info(spec_type, cache_path=cache_dir)
            except Exception:
                return None
        except Exception:
            # 个别规格在 miot-spec.org 不存在（如第三方牙刷），按无能力处理
            return None
        # 部分品类（音箱、温湿度计等）官方数据缺中文 comment，
        # 按属性/动作名兜底补齐，避免界面退化为纯英文（见 spec_zh）
        spec_zh.apply_fallback(info)
        return info

    @staticmethod
    def _gang_from_did(did: str) -> int | None:
        """从虚拟子设备的 did 里取出它对应的通道号（siid）。

        多键面板在云端会派生出虚拟设备，did 形如 `<父did>.s2`、`.s3`、
        `.s4`——后缀的 N 就是这一路在规格里对应的 siid。单键设备与父
        设备本身没有后缀，返回 None（按第一条 on 属性处理）。
        """
        m = _SUB_DEVICE_SUFFIX.search(str(did))
        return int(m.group(1)) if m else None

    @classmethod
    def _find_on_method(cls, info: dict, did: str | None = None) -> dict | None:
        """挑出该设备可写的 on 属性，返回其 method(siid/piid)。

        多键面板的规格里有 `on-2`(siid 2)、`on-3`(siid 3)、`on-4`(siid 4)
        等多路属性。**必须按 did 后缀选对应的那一路**：三个虚拟设备都
        取第一条的话，后两路永远在控制第一路（用户表现为"虚拟设备点不
        动/开关错灯"）。
        """
        props = [
            prop for prop in info.get("properties", [])
            if prop.get("type") == "bool" and "w" in prop.get("rw", "")
            and (prop.get("name") == "on"
                 or prop["name"].replace("_", "-").startswith("on-"))
        ]
        if not props:
            return None
        gang = cls._gang_from_did(did) if did else None
        if gang is not None:
            for prop in props:
                if prop.get("method", {}).get("siid") == gang:
                    return prop["method"]
            # 后缀编号与 siid 对不上时按顺序兜底：s2→第 1 路、s3→第 2 路…
            index = gang - 2
            if 0 <= index < len(props):
                return props[index]["method"]
        return props[0]["method"]

    # ---------- 环境读数（卡片副标题展示用） ----------

    def read_metrics(self, dids: list[str]) -> dict[str, str | None]:
        """批量读取温湿度并拼成卡片副标题文案。

        匹配 SI 标准属性：temperature 与 relative-humidity（个别型号
        也叫 humidity），读不到的设备返回 None（副标题维持纯房间名）。
        多通道设备的同名属性取第一个通道。
        只采集"环境温度"：插座报的是内部器件温度、净水器报的是进水温度，
        机型与量程两层筛掉（见 is_ambient_source / _ambient_range_ok）。
        """
        spec_map = self._specs_for(dids)
        queries: list[dict] = []
        key_to_metric: dict[tuple, tuple[str, str]] = {}  # (did,siid,piid) -> (did, metric)
        result: dict[str, str | None] = {}
        for did in dids:
            info = spec_map.get(did)
            found: dict[str, dict] = {}
            if info and not is_ambient_source(
                    info.get("spec_type"), info.get("name"), did):
                result[did] = None
                continue
            if info:
                for prop in info.get("properties", []):
                    name = prop.get("name")
                    metric = (
                        "temperature" if name == "temperature"
                        else "humidity" if name in ("relative-humidity", "humidity")
                        else None
                    )
                    if metric == "temperature" and not _ambient_range_ok(prop.get("range")):
                        continue
                    if metric and "r" in prop.get("rw", "") and metric not in found:
                        found[metric] = prop["method"]
            if not found:
                result[did] = None
                continue
            for metric, method in found.items():
                queries.append({"did": did, **method})
                key_to_metric[(did, method["siid"], method["piid"])] = (did, metric)

        temps: dict[str, float] = {}
        hums: dict[str, float] = {}
        for start in range(0, len(queries), _BATCH_SIZE):
            batch = queries[start:start + _BATCH_SIZE]
            try:
                rets = self._api.get_devices_prop(batch)
            except Exception:
                # 读数失败只影响副标题展示，不值得打断主流程
                return {did: None for did in dids}
            for item in rets:
                entry = key_to_metric.get(
                    (str(item.get("did")), item.get("siid"), item.get("piid"))
                )
                if entry is None or item.get("code") != 0:
                    continue
                did, metric = entry
                try:
                    value = float(item["value"])
                except (KeyError, TypeError, ValueError):
                    continue
                if metric == "temperature":
                    temps[did] = value
                else:
                    hums[did] = value

        for did in dids:
            result[did] = format_metrics_text(temps.get(did), hums.get(did))
        return result

    def consumable_items(self) -> list[dict]:
        """耗材明细：设备名、耗材描述、当前值、不足阈值与更新时间。

        不足判定与米家 App 一致：details.value 低于 details.inadeq.val。
        接口异常返回空列表，顶栏隐藏该项即可，不影响主流程。

        注意 `details` 有两种形态：单耗材设备是一个对象，多耗材设备
        （如门锁的电池仓、净水器的多级滤芯）是**数组**。早先只按对象处理，
        遇到数组就是 `'list' object has no attribute 'get'`，异常一路抛到
        后台任务把整轮耗材刷新打断，顶栏的耗材胶囊也跟着不更新。
        """
        try:
            raw_items = self._api.get_consumable_items()
        except Exception:
            logger.debug("读取耗材列表失败", exc_info=True)
            return []
        result: list[dict] = []
        for item in raw_items or []:
            if not isinstance(item, dict) or item.get("is_ignore"):
                continue
            details = item.get("details")
            if isinstance(details, dict):
                details = [details]
            if not isinstance(details, list):
                continue
            for detail in details:
                if not isinstance(detail, dict):
                    continue
                try:
                    value = float(detail.get("value"))
                except (TypeError, ValueError):
                    continue
                try:
                    threshold = float(json.loads(detail.get("inadeq") or "{}").get("val"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    threshold = None
                try:
                    updated = int(detail.get("update_time") or 0)
                except (TypeError, ValueError):
                    updated = 0
                result.append({
                    "did": str(item.get("did") or ""),
                    "name": str(item.get("name") or ""),
                    "model": str(item.get("model") or ""),
                    "description": str(detail.get("description") or ""),
                    "type": str(detail.get("type_name") or ""),
                    "value": value,
                    "threshold": threshold,
                    "low": threshold is not None and value < threshold,
                    "updated": updated,
                })
        return result

    def consumable_low_count(self) -> tuple[int, int]:
        """耗材统计：返回 (耗材项数, 不足项数)。"""
        items = self.consumable_items()
        return len(items), sum(1 for item in items if item["low"])

    def _specs_for(self, dids: list[str]) -> dict[str, dict | None]:
        """did -> spec 映射，spec_type 增量补齐 + spec 并发拉取。

        与 power_states 共用设备索引、内存 spec 缓存与上游文件缓存，
        轮询同时刷开关和读数时不会产生额外请求。同一规格多台设备只拉
        一次：上游把 spec 缓存在按规格命名的同一个文件里，并发拉取
        同规格会互相覆盖写坏缓存。
        """
        if any(d not in self._device_index for d in dids):
            self._refresh_device_index()

        result: dict[str, dict | None] = {}
        to_fetch: list[str] = []
        for did in dids:
            spec_type = self._device_index.get(did, ("", "", ""))[1]
            if not spec_type:
                result[did] = None
            elif spec_type in self._spec_cache:
                result[did] = self._spec_cache[spec_type]
            else:
                to_fetch.append(spec_type)
        # 保序去重：同上循环里内存缓存未命中时会把同规格多台设备重复
        # 入列，并发线程拉同一规格会同时写同一个缓存文件互相覆盖
        to_fetch = list(dict.fromkeys(to_fetch))

        if to_fetch:
            cache_dir = self._api.auth_data_path.parent
            with ThreadPoolExecutor(max_workers=min(_SPEC_WORKERS, len(to_fetch))) as pool:
                fetched = pool.map(
                    lambda st: self._fetch_spec(st, cache_dir), to_fetch
                )
                for spec_type, spec in zip(to_fetch, fetched):
                    self._spec_cache[spec_type] = spec
            for did in dids:
                if did not in result:
                    result[did] = self._spec_cache.get(
                        self._device_index.get(did, ("", "", ""))[1]
                    )
        return result

    def _refresh_device_index(self) -> None:
        """重建 did -> (model, spec_type, name) 索引，自有与共享设备合并。

        采用 upsert 而非整体清空重建：列表接口偶发返回空（尤其共享
        列表）时，旧条目仍在才能继续组装共享设备；已移除设备的残留
        条目无害，UI 不会再给出入口。
        """
        try:
            all_devices = (
                self._api.get_devices_list()
                + self._api.get_shared_devices_list()
            )
        except Exception as exc:
            raise _wrap_error(exc, "获取设备列表失败") from exc
        for d in all_devices:
            self._device_index[str(d["did"])] = (
                d.get("model", ""), d.get("spec_type") or "", d.get("name", ""))

    # ---------- 场景（手动控制） ----------

    def _home_ids_map(self) -> dict[str, str]:
        """家庭名 -> 家庭 ID 映射，懒加载缓存。

        场景接口要家庭 ID 而界面层只有显示名；家庭结构变更概率
        极低，一次拉取后长期复用。
        """
        if self._home_ids is None:
            try:
                homes = self._api.get_homes_list()
            except Exception as exc:
                raise _wrap_error(exc, "获取家庭列表失败") from exc
            self._home_ids = {
                str(h.get("name") or ""): str(h.get("id") or "") for h in homes}
        return self._home_ids

    def scene_list(self, home_name: str | None = None) -> list[SceneInfo]:
        """获取手动场景列表；home_name 为 None 时返回全部家庭的场景。"""
        return self._scene_infos(home_name, "manual_scene_info_list", False)

    def automation_list(self, home_name: str | None = None) -> list[SceneInfo]:
        """获取自动场景（米家里的「自动化」）。

        和手动场景同一个接口返回，只是列表字段不同（auto_scene_info_list）；
        自动化由触发条件驱动、不能手动执行，所以卡片上不放运行按钮。
        """
        return self._scene_infos(home_name, "auto_scene_info_list", True)

    def _scene_infos(self, home_name: str | None, key: str,
                     is_automation: bool) -> list[SceneInfo]:
        home_id: str | None = None
        if home_name is not None:
            home_id = self._home_ids_map().get(home_name)
            if home_id is None:
                # 界面家庭名在映射里不存在（如「未知」），视为无场景
                return []
        try:
            raw = self._raw_scene_lists(home_id)
        except Exception as exc:
            raise _wrap_error(exc, "获取场景列表失败") from exc
        items = raw.get(key) or []
        id_to_name = {v: k for k, v in self._home_ids_map().items()}
        return [
            SceneInfo(
                scene_id=str(s.get("scene_id") or ""),
                name=str(s.get("name") or ("未命名自动化" if is_automation else "未命名场景")),
                # 上游返回的 home_id 与请求一致，兜底用请求时的家庭 ID
                home_id=str(s.get("home_id") or home_id or ""),
                # 家庭显示名反查映射（「全部」路径下跨家庭场景各自归属）
                home_name=id_to_name.get(
                    str(s.get("home_id") or home_id or ""), ""),
                actions=self._scene_actions(s),
                is_automation=is_automation,
                enabled=bool(s.get("enable", True)),
            )
            for s in items
        ]

    def _raw_scene_lists(self, home_id: str | None) -> dict:
        """拉原始场景返回（手动/自动/语音三个列表都在里面）。

        上游库只暴露了手动场景，但同一个接口用 get_type=2 请求时，
        `auto_scene_info_list`（自动化）和 `voice_scene_info_list`（语音）
        一并返回，早先被丢掉了。home_id 为 None 时逐家庭请求后合并，
        并给每条补 home_id（上游只在单家庭请求时带）。
        """
        if home_id is not None:
            return self._request_scene_list(home_id)
        merged: dict[str, list] = {
            "manual_scene_info_list": [], "auto_scene_info_list": [],
            "voice_scene_info_list": [],
        }
        for hid in self._home_ids_map().values():
            ret = self._request_scene_list(hid)
            for key in merged:
                for item in ret.get(key) or []:
                    if isinstance(item, dict):
                        item.setdefault("home_id", str(hid))
                        merged[key].append(item)
        return merged

    def _request_scene_list(self, home_id: str) -> dict:
        return self._api._request(
            "/appgateway/miot/appsceneservice/AppSceneService/GetSimpleSceneList",
            {"app_version": 12, "get_type": 2, "home_id": str(home_id),
             "owner_uid": self._api._get_home_owner(home_id)}) or {}

    def _raw_scene_info(self, scene_id: str, home_id: str) -> dict:
        """场景/自动化的完整定义（含触发条件）。

        列表接口不带 trigger，只有 GetSceneInfo 有；这条路已验证可用。
        """
        return self._api._request(
            "/appgateway/miot/appsceneservice/AppSceneService/GetSceneInfo",
            {"app_version": 12, "scene_id": str(scene_id), "home_id": str(home_id),
             "owner_uid": self._api._get_home_owner(home_id)}) or {}

    # ---------- 场景写操作（启停 / 改名） ----------

    def _scene_edit_payload(self, scene: SceneInfo) -> dict:
        """拿 Edit 接口需要的整份场景定义。

        实测（2026-10-31）：`AppSceneService/Edit` 必须回传**完整对象**
        （含 scene_action / scene_trigger / timewindow…），只传 scene_id +
        要改的字段会返回 `{"code":-8,"msg":"invalid params"}`。
        所以每次先读一次完整定义再改字段回写。
        """
        info = self._raw_scene_info(scene.scene_id, scene.home_id)
        if not info.get("scene_id"):
            raise ServiceError(f"未取到场景「{scene.name}」的定义，无法修改")
        data = dict(info)
        data.update({
            "app_version": 12,
            "scene_id": str(scene.scene_id),
            "home_id": str(scene.home_id),
            "owner_uid": self._api._get_home_owner(scene.home_id),
        })
        return data

    def _scene_edit(self, scene: SceneInfo, **changes) -> None:
        data = self._scene_edit_payload(scene)
        data.update(changes)
        try:
            self._api._request(
                "/appgateway/miot/appsceneservice/AppSceneService/Edit", data)
        except Exception as exc:
            raise _wrap_error(exc, f"修改「{scene.name}」失败") from exc

    def set_scene_enabled(self, scene: SceneInfo, enabled: bool) -> None:
        """启用/停用场景或自动化。"""
        self._scene_edit(scene, enable=bool(enabled))
        scene.enabled = bool(enabled)

    def rename_scene(self, scene: SceneInfo, name: str) -> str:
        """改名；返回去掉首尾空白后的新名称。"""
        cleaned = (name or "").strip()
        if not cleaned:
            raise ServiceError("名称不能为空")
        if cleaned == scene.name:
            return cleaned
        self._scene_edit(scene, name=cleaned)
        scene.name = cleaned
        return cleaned

    def scene_trigger_text(self, scene: SceneInfo) -> str:
        """自动化的触发条件（如「Ai 智能三键开关 · 中键打开时」）。

        列表接口不返回 trigger，只有 GetSceneInfo 有；每条场景一次请求，
        调用方应缓存（见主窗口的 _trigger_cache）。
        """
        try:
            info = self._raw_scene_info(scene.scene_id, scene.home_id)
        except Exception:
            logger.debug("读取场景触发条件失败 scene_id=%s", scene.scene_id, exc_info=True)
            return ""
        triggers = (info.get("scene_trigger") or {}).get("triggers") or []
        parts = []
        for trig in triggers:
            if not isinstance(trig, dict):
                continue
            src = str(trig.get("src") or "")
            name = str(trig.get("name") or "").strip()
            if src == "user":
                parts.append("手动触发")
                continue
            device_name = ""
            try:
                extra = json.loads(trig.get("extra") or "{}")
                device_name = str(extra.get("device_name") or "")
                # 触发条件里同样存的是旧名字，能按 did 纠正就纠正
                did = str(extra.get("did") or "")
                if did:
                    device_name = self._device_name_map().get(did) or device_name
            except (TypeError, ValueError, json.JSONDecodeError):
                device_name = ""
            if device_name and name:
                parts.append(f"{device_name} · {name}")
            elif name:
                parts.append(name)
            elif device_name:
                parts.append(device_name)
            elif trig.get("key"):
                parts.append(str(trig["key"]))
        return "、".join(parts)

    # ---------- 场景动作明细 ----------

    def _device_name_map(self) -> dict[str, str]:
        """did -> 当前显示名，口径与设备页完全一致。

        设备页显示的名字来自两步本地化（见主窗口 _localize_names）：
        1. `localized_product_names`：spec 里的中文产品名；
        2. `cached_product_page_names`：产品页抓到的名称（覆盖前者）。
        场景动作里存的 device_name 是**建场景那一刻**的英文原名，改名后
        不会同步，所以这里重建一份与设备页同源的映射来纠正。
        """
        cached = getattr(self, "_name_map_cache", None)
        if cached is not None:
            return cached
        devices = self._all_devices_cache()
        names = {d.did: d.name for d in devices}
        try:
            names.update(self.localized_product_names(list(names), names))
            ascii_devices = [d for d in devices if d.name.isascii() and d.model]
            model_to_did = {d.model: d.did for d in ascii_devices}
            if model_to_did:
                for model, product_name in self.cached_product_page_names(
                        sorted(model_to_did)).items():
                    did = model_to_did.get(model)
                    if did:
                        names[did] = product_name
        except Exception:
            logger.debug("构建设备显示名映射失败，退回云端名称", exc_info=True)
        self._name_map_cache = names
        return names

    def _scene_actions(self, scene: dict) -> list[SceneAction]:
        """把一条场景的 scene_action.actions 翻成可直接展示的动作列表。

        米家列表接口（GetSimpleSceneList）本身就把动作明细带回来了，
        早期只取了 id/name 丢掉了这部分。结构形如：

            {"order": 1, "payload_json": {
                "command": "set_properties", "delay_time": 0,
                "device_name": "大门", "did": "...", "model": "wfxx.motor.ycmkq",
                "value": [{"did": "...", "siid": 3, "piid": 2, "value": 2}]}}

        翻译失败的条目保留原始 command 文案——宁可难看，也不能让用户
        以为这条场景什么都没做。
        """
        raw_actions = (scene.get("scene_action") or {}).get("actions") or []
        result: list[SceneAction] = []
        for raw in raw_actions:
            if not isinstance(raw, dict):
                continue
            payload = raw.get("payload_json") or {}
            if not isinstance(payload, dict):
                payload = {}
            try:
                delay = int(payload.get("delay_time") or 0)
            except (TypeError, ValueError):
                delay = 0
            model = str(payload.get("model") or "")
            did = str(payload.get("did") or "")
            # 通知类动作没有设备，单独给个名字，否则详情里显示成"设备"
            is_notify = not str(payload.get("command") or "") and (
                payload.get("title") or payload.get("desc"))
            result.append(SceneAction(
                order=int(raw.get("order") or 0),
                delay=delay,
                did=did,
                # 米家把设备名**冗余存在动作里**，改名后不同步：
                # 设备列表里的当前名字才是设备页显示的那个
                device_name=("通知" if is_notify else
                             (self._device_name_map().get(did)
                              or str(payload.get("device_name") or ""))),
                text=self._scene_action_text(payload, did, model),
                model=model,
            ))
        result.sort(key=lambda a: a.order)
        return result

    def _scene_action_text(self, payload: dict, did: str, model: str) -> str:
        """单条动作的中文描述；识别不了就退回原始 command。

        米家里有**两种**动作写法，都会出现在手动场景中：

        1. 新式 `set_properties` + `value: [{siid, piid, value}]`（多数设备）
        2. 旧式命令名 + 字符串值，如 `{"command": "set_power", "value": "on"}`、
           `{"command": "set_mode", "value": "cooling"}`（老款空调 zhimi 系）

        第 2 种如果不翻译，详情页里就是一行 `set_spd_level`，等于没说。
        这里把命令名映射回规格属性，再复用同一套值翻译。
        """
        command = str(payload.get("command") or "")
        if not command:
            title = str(payload.get("title") or "").strip()
            desc = str(payload.get("desc") or "").strip()
            if title or desc:
                body = f"{title}：{desc}" if title and desc else (title or desc)
                return f"发送通知「{body}」"
            return "未知动作"
        if command == "set_properties":
            parts = []
            for entry in payload.get("value") or []:
                if not isinstance(entry, dict):
                    continue
                siid = entry.get("siid")
                piid = entry.get("piid")
                value = entry.get("value")
                name, value_text = self._scene_prop_text(did, model, siid, piid, value)
                # value_text 为空 = 只写属性（一次性命令），只显示名称，
                # 不能拼成 "开/关 → "
                if name and value_text:
                    parts.append(f"{name} → {value_text}")
                elif name:
                    parts.append(name)
                else:
                    parts.append(value_text or "未知动作")
            return "、".join(parts) if parts else "设置属性"
        if command in ("action", "run_action", "execute_action"):
            # 这类动作的 value 是一个对象（不是数组）：
            # {"aiid": 1, "did": "...", "in": [], "siid": 2}
            # 早先只从 payload 顶层取 siid/aiid，取不到就退化成
            # "action → {'aiid': 1, ...}" 这种原始 dict，等于没解析
            value = payload.get("value")
            target_did = did
            siid = payload.get("siid")
            aiid = payload.get("aiid")
            if isinstance(value, dict):
                siid = value.get("siid", siid)
                aiid = value.get("aiid", aiid)
                target_did = str(value.get("did") or did)
            name = self._scene_action_name(target_did, model, siid, aiid)
            # toggle 类动作直接读作「开/关」，与米家 App 一致，
            # 不再包一层「执行「…」」
            if name in ("开关状态切换", "切换", "开/关"):
                return "开/关"
            return f"执行「{name}」" if name else "执行设备动作"

        name, value_text = self._scene_command_text(did, model, command,
                                                    payload.get("value"))
        if value_text:
            return f"{name} → {value_text}"
        return name or command or "未知动作"

    def _scene_command_text(self, did: str, model: str, command: str,
                            value) -> tuple[str, str]:
        """旧式命令名（set_power/set_mode…）→ (中文属性名, 值文本)。"""
        zh = _SCENE_COMMAND_ZH.get(command)
        prop = None
        for prop_name in _SCENE_COMMAND_PROPS.get(command, ()):
            prop = self._scene_prop_by_name(did, model, prop_name)
            if prop is not None:
                if not zh:
                    desc = str(prop.get("description") or "")
                    zh = spec_zh.prop_desc_zh(str(prop.get("name") or ""), desc)
                    zh = zh.split(" / ")[-1] if " / " in zh else zh
                break
        if zh is None:
            # 不认识的命令名：去掉 set_ 前缀、下划线换空格，
            # 至少比直接把 set_xxx_yyy 摔给用户好读
            zh = command[4:].replace("_", " ") if command.startswith("set_") else command
        if value is None or value == "":
            return zh, ""
        if prop is not None:
            return zh, self._enum_value_text(prop, value)
        text = str(value).strip()
        return zh, _SCENE_VALUE_ZH.get(text.lower(), text)

    def _scene_prop_by_name(self, did: str, model: str, name: str) -> dict | None:
        """按属性名找规格定义（规格里可能带数字后缀，如 fan-level-2）。"""
        info = self._scene_spec(did, model)
        if not info:
            return None
        for prop in info.get("properties", []):
            prop_name = str(prop.get("name") or "")
            if prop_name == name or prop_name.startswith(name + "-"):
                return prop
        return None

    def _enum_value_text(self, prop: dict, value) -> str:
        """属性值 → 可读文本：bool 开关、枚举描述、数值补单位。"""
        prop_type = str(prop.get("type") or "")
        text = str(value).strip()
        low = text.lower()
        if prop_type == "bool" or low in ("on", "off", "true", "false"):
            return "开" if low in ("on", "true", "1") else "关"
        entries = prop.get("value-list") or []
        for item in entries:
            if str(item.get("value")) == text:
                return self._enum_desc(item) or text
        # 旧式命令给的是枚举的英文名（"cooling"），按描述前半段反查
        for item in entries:
            desc = str(item.get("description") or "")
            en = desc.split(" / ")[0].strip().lower().replace(" ", "-")
            if en and (en == low or low in en.split("-") or en.startswith(low)):
                return self._enum_desc(item) or text
        if self._looks_number(text):
            return text + _scene_prop_unit(str(prop.get("name") or ""))
        return _SCENE_VALUE_ZH.get(low, text)

    @classmethod
    def _enum_desc(cls, item: dict) -> str:
        """枚举条目的中文；只有英文描述时按兜底表转（如 Set → 触发）。"""
        text = str(item.get("desc_zh_cn") or item.get("description") or "")
        text = text.split(" / ")[-1] if " / " in text else text
        text = text.strip()
        if not text or any("\u4e00" <= ch <= "\u9fff" for ch in text):
            return text
        mapped = _SCENE_VALUE_ZH.get(text.lower())
        if mapped:
            return mapped
        # 风速档位这类英文枚举：Level5 / Level 5 → 5 档
        match = re.fullmatch(r"level\s*(\d+)", text.lower())
        if match:
            return f"{match.group(1)} 档"
        return text

    @staticmethod
    def _looks_number(text: str) -> bool:
        try:
            float(text)
        except (TypeError, ValueError):
            return False
        return True

    def _scene_prop_text(self, did: str, model: str, siid, piid,
                         value) -> tuple[str, str]:
        """(属性中文名, 值的可读文本)。

        属性名走设备规格（与设备详情页同一套中文映射），值按类型翻译：
        bool → 开/关，带枚举 → 枚举描述，其余原样。
        """
        prop = self._scene_prop(did, model, siid, piid)
        if prop is None:
            return "", str(value)
        prop_name = str(prop.get("name") or "")
        # 只写属性 = 一次性命令（light 的 scene 服务：toggle / brightness-add…），
        # 值 true 只是"触发"，不能显示成 "→ 开"
        if prop_name in _SCENE_PROP_ZH:
            return _SCENE_PROP_ZH[prop_name], ""
        if str(prop.get("rw") or "") == "w" and str(prop.get("type")) == "bool":
            desc_w = str(prop.get("description") or "")
            zh_w = spec_zh.prop_desc_zh(prop_name, desc_w)
            return (zh_w.split(" / ")[-1] if " / " in zh_w else zh_w), ""

        desc = str(prop.get("description") or "")
        # 描述统一是「英文 / 中文」，取中文段；spec_zh 兜底补中文
        name = spec_zh.prop_desc_zh(prop_name, desc)
        name = name.split(" / ")[-1] if " / " in name else name
        # 「按键2状态」这类射频控制器通道：去掉"状态"二字，
        # 详情里读作「按键2 → 触发」，比「按键2状态 → 触发」顺
        name = re.sub(r"^(按键\s*\d+)状态$", r"\1", name)
        return name, self._enum_value_text(prop, value)

    def _scene_spec(self, did: str, model: str) -> dict | None:
        """动作所属设备的规格。

        只有 did 确实在设备索引里才走 `_specs_for`——它对未知 did 会触发
        一次完整的设备列表刷新，而场景里常留着已删除设备的 did，
        逐条动作都刷一遍会白打十几次请求。did 失效时按型号兜底。
        """
        info = None
        if not self._device_index:
            # 首次调用时索引还是空的，先补一次（内部会走设备列表接口）
            self._refresh_device_index()
        if did and did in self._device_index:
            info = self._specs_for([did]).get(did)
        if info is None and model:
            info = self._spec_by_model(model)
        return info

    def _scene_prop(self, did: str, model: str, siid, piid) -> dict | None:
        """按 siid/piid 在设备规格里找属性定义。"""
        info = self._scene_spec(did, model)
        if not info:
            return None
        for prop in info.get("properties", []):
            method = prop.get("method") or {}
            if (str(method.get("siid")) == str(siid)
                    and str(method.get("piid")) == str(piid)):
                return prop
        return None

    def _scene_action_name(self, did: str, model: str, siid, aiid) -> str:
        info = self._scene_spec(did, model)
        if not info:
            return ""
        for act in info.get("actions", []):
            method = act.get("method") or {}
            if (str(method.get("siid")) == str(siid)
                    and str(method.get("aiid")) == str(aiid)):
                desc = str(act.get("description") or "")
                name = spec_zh.action_desc_zh(str(act.get("name") or ""), desc)
                return name.split(" / ")[-1] if " / " in name else name
        return ""

    def _spec_by_model(self, model: str) -> dict | None:
        """按型号取规格：场景里的 did 失效时（设备被删/换过）的兜底。"""
        cached = getattr(self, "_model_specs", None)
        if cached is None:
            cached = {}
            self._model_specs = cached
        if model in cached:
            return cached[model]
        info = None
        for device in self._all_devices_cache():
            if device.model == model:
                info = self._specs_for([device.did]).get(device.did)
                break
        cached[model] = info
        return info

    def _all_devices_cache(self) -> list[DeviceInfo]:
        cached = getattr(self, "_device_list_cache", None)
        if cached is not None:
            return cached
        try:
            cached = self.list_devices()
        except Exception:
            cached = []
        self._device_list_cache = cached
        return cached

    def run_scene(self, scene: SceneInfo) -> None:
        """执行手动场景；上游返回值为假视为失败。"""
        try:
            ok = self._api.run_scene(scene.scene_id, scene.home_id)
        except Exception as exc:
            raise _wrap_error(exc, f"执行场景「{scene.name}」失败") from exc
        if not ok:
            raise ServiceError(f"执行场景「{scene.name}」失败")

    # ---------- 设备图标 ----------

    def icon_urls(self, models: list[str]) -> dict[str, str | None]:
        """model -> 图标 URL，增量拉取：只对缓存里没有的型号调接口。

        成功结果写入内存与磁盘缓存；失败仅记内存 None（本次会话不再
        重试，下次启动重新尝试），避免网络抖动时每轮刷新重复打接口。
        """
        if not models:
            return {}
        missing = [m for m in models if m not in self._icon_cache]
        if missing:
            for model in missing:
                self._icon_cache[model] = self._fetch_icon_url(model)
            icon_store.save_urls(
                {k: v for k, v in self._icon_cache.items() if v})
        return {m: self._icon_cache[m] for m in models if self._icon_cache.get(m)}

    def _fetch_icon_url(self, model: str) -> str | None:
        """调 productconfig/get_icon 取图标 CDN 地址（302 重定向的 Location）。

        与 _request 的差异：这个接口靠重定向返回图片地址而非 JSON 响应体，
        必须关闭自动跟随重定向自己读 Location；签名与刷新逻辑保持一致。
        """
        uri = "/v2/productconfig/get_icon"
        try:
            self._api._refresh_token()
            params = {"data": json.dumps(
                {"icon_name": "icon_real", "model": model},
                separators=(",", ":"))}
            nonce = gen_nonce()
            signed_nonce = get_signed_nonce(self._api.auth_data["ssecurity"], nonce)
            params = generate_enc_params(
                uri, "POST", signed_nonce, nonce, params,
                self._api.auth_data["ssecurity"])
            ret = self._api.session.post(
                self._api.api_base_url + uri, data=params,
                allow_redirects=False, timeout=30)
        except Exception as exc:
            logger.warning("获取设备图标失败 %s: %s", model, exc)
            return None
        if ret.status_code != 302:
            logger.info("获取设备图标未重定向 %s: HTTP %s", model, ret.status_code)
            return None
        return ret.headers.get("Location")

    def icon_file(self, model: str, url: str) -> Path | None:
        """下载图标到本地缓存并返回文件路径；已存在直接返回，失败返回 None。"""
        path = icon_store.icon_path(model)
        if path.exists():
            return path
        import requests

        try:
            r = requests.get(url, timeout=15)
            if r.status_code != 200 or not r.content:
                logger.warning("下载设备图标失败 %s: HTTP %s", model, r.status_code)
                return None
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(r.content)
            return path
        except Exception as exc:
            logger.warning("下载设备图标失败 %s: %s", model, exc)
            return None

    def has_product_page_name(self, model: str) -> bool:
        """该型号的产品页中文名是否已解析过（含“确认无”的情况）。"""
        return model in self._product_page_names

    def cached_product_page_names(self, models: list[str]) -> dict[str, str]:
        """已解析且含中文的产品页名称（model -> name）。"""
        result: dict[str, str] = {}
        for model in models:
            name = self._product_page_names.get(model)
            if name and any("\u4e00" <= ch <= "\u9fff" for ch in name):
                result[model] = name
        return result

    def product_page_names(self, models: list[str]) -> dict[str, str | None]:
        """并发抓取多个型号的产品页中文名，返回 model -> 名称或 None。

        逐个抓时每个型号 2~3 秒，二十多个型号要等一分钟，排在后面的
        （按型号名排序，空调网关在中间）用户基本等不到。并发抓一轮即可，
        结果统一落盘（见 product_name_store），下次启动直接命中。
        """
        todo = [m for m in dict.fromkeys(models)
                if m and m not in self._product_page_names]
        if todo:
            with ThreadPoolExecutor(max_workers=min(_NAME_WORKERS, len(todo))) as pool:
                # product_page_name 内部已吞异常并按型号记忆结果
                list(pool.map(self.product_page_name, todo))
        return {m: self._product_page_names.get(m) for m in models}

    def product_page_name(self, model: str) -> str | None:
        """抓取 miot-spec 产品页（/p/<model>）的中文商品名。

        用于无公开 spec 的设备（如仅蓝牙类）：这类设备在 /spec/ 路径
        404、specSummary.available=False，没有 spec 产品名可读，只有
        产品页里有本地化商品名。任何失败都缓存 None（含网络超时）：
        该功能只是显示名锦上添花，而 jobs 是单线程串行队列，若失败不
        记忆，秒级的轮询会把超时请求反复排进队列，堵死后续设备控制。
        老站已基本不可用（响应超时或 404），失败记忆让每型号一个会
        话只踩一次坑。阻塞，须后台线程调用。
        """
        if model in self._product_page_names:
            return self._product_page_names[model]
        import requests

        name = None
        try:
            r = requests.get(f"https://home.miot-spec.com/p/{model}", timeout=10,
                             headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                m = re.search(
                    r'<script data-page="app" type="application/json">(.*?)</script>',
                    r.text, re.S)
                if m:
                    product = json.loads(m.group(1)).get("props", {}).get("product", {})
                    name = product.get("name") or None
                    if name is not None and not any("一" <= ch <= "鿿" for ch in name):
                        name = None  # 产品名也非中文，无回退价值
        except Exception as exc:
            logger.warning("产品页名称查询失败 %s: %s", model, exc)
        self._product_page_names[model] = name
        # 落盘：成功与"确认无中文名"都记，避免下次启动重跑一遍
        product_name_store.save(self._product_page_names)
        return name

    def spec_has_published_functions(self, spec_type: str) -> bool | None:
        """该规格是否发布过含属性的功能 spec。

        True=有属性；False=无公开规格（云端未下发 spec_type）或无属性
        （无可控制功能）；None=spec 尚未拉取（未知，调用方应视为有并
        等待轮询证实）。
        """
        if not spec_type:
            # 云端不下发 spec_type 即官方确认无公开规格，无需等待轮询
            return False
        if spec_type not in self._spec_cache:
            return None
        spec = self._spec_cache[spec_type]
        return bool(spec and spec.get("properties"))

    def localized_product_names(self, dids: list[str], names: dict[str, str]) -> dict[str, str]:
        """did -> spec 中文产品名，用于替换未改名的英文默认设备名。

        米家 APP 对未改名设备显示的是产品库本地化商品名，而第三方
        设备列表接口的 name 字段只有英文默认名（国际版产品尤甚）。
        spec 数据里带有中文产品名，本方法从**已缓存的 spec**（不发起
        网络请求）读取：仅当云端名为纯 ASCII（未改名）且 spec 产品名
        含中文时返回，否则该 did 不在结果中（保持云端原名）。
        """
        def has_cjk(s: str) -> bool:
            return any("\u4e00" <= ch <= "\u9fff" for ch in s)

        result: dict[str, str] = {}
        for did in dids:
            name = names.get(did, "")
            if not name or has_cjk(name):
                continue  # 已是中文（用户改名或国内默认名），不替换
            spec_type = self._device_index.get(did, ("", "", ""))[1]
            spec = self._spec_cache.get(spec_type) if spec_type else None
            if not spec:
                continue
            spec_name = str(spec.get("name") or "")
            if spec_name and has_cjk(spec_name) and spec_name != name:
                result[did] = spec_name
        return result


# 会报温度但测的不是室温的机型：插座报的是自身内部器件温度（常年 40°C+），
# 净水器报的是进水温度。让它们参与"室温"比较会把胶囊带偏，源头就挡掉。
# 关键字同时要覆盖型号（xiaomi.waterpuri.s1600g）与规格 URN
# （urn:miot-spec-v2:device:water-purifier:...）两种写法。
_NON_AMBIENT_MODEL_KEYWORDS = (
    "plug", "powerstrip", "power-strip", "socket", "waterpuri", "water-purifier",
)


def is_ambient_source(*identifiers: str | None) -> bool:
    """这些标识（型号/规格 URN/名称）描述的机型报的是否环境温湿度。"""
    text = " ".join(i for i in identifiers if i).lower()
    return not any(key in text for key in _NON_AMBIENT_MODEL_KEYWORDS)


def _ambient_range_ok(rng) -> bool:
    """温度量程能装下室温才算环境温度（插座内部温度量程高到 255°C）。"""
    if not rng or len(rng) < 2:
        return True
    try:
        return float(rng[1]) <= 150
    except (TypeError, ValueError):
        return True


# 旧式场景命令名 → 中文。米家老款设备（如 zhimi 系空调）的手动场景
# 存的是 `{"command": "set_mode", "value": "cooling"}` 这种命令名 + 字符串值，
# 和新式的 set_properties + siid/piid 不是一套，必须单独翻。
_SCENE_COMMAND_ZH = {
    "set_power": "开关",
    "set_vertical": "上下摆风",
    "set_horizontal": "左右摆风",
    "set_mode": "模式",
    "set_spd_level": "风速",
    "set_comfort": "舒适模式",
    "set_temp": "设定温度",
    "set_temperature": "设定温度",
    "set_bright": "亮度",
    "set_brightness": "亮度",
    "set_volume": "音量",
    "set_color_temp": "色温",
    "toggle": "开/关",
}

# 命令名 → 规格里的属性名：能把枚举值也翻成中文就翻。
# 找不到对应属性时只用上面的中文名 + 值原文兜底。
_SCENE_COMMAND_PROPS = {
    "set_power": ("on",),
    "set_vertical": ("vertical-swing",),
    "set_horizontal": ("horizontal-swing",),
    "set_mode": ("mode",),
    "set_spd_level": ("fan-level", "speed-level"),
    "set_comfort": ("comfort-mode",),
    "set_temp": ("target-temperature", "temperature"),
    "set_temperature": ("target-temperature", "temperature"),
    "set_bright": ("brightness",),
    "set_volume": ("volume",),
    "set_color_temp": ("color-temperature",),
}

# 米家灯/开关类设备的 `scene` 服务属性：只写（access=write）、bool，
# 值恒为 true，表示"触发一次"而非"设为开"。名称由规格里是空的，
# 不单独翻译就会显示成 "toggle → 开"。
_SCENE_PROP_ZH = {
    "toggle": "开/关",
    "brightness-add": "亮度 +",
    "brightness-dec": "亮度 −",
    "brightness-sw": "亮度循环",
    "on-or-brightness": "开灯 / 调亮度",
    "colortemperature-add": "色温 +",
    "colortemperature-dec": "色温 −",
    "colortemperature-sw": "色温循环",
    "on-or-colortemp": "开灯 / 调色温",
}


# 查不到中文描述时的兜底：这些枚举名直接给中文
_SCENE_VALUE_ZH = {
    "on": "开", "off": "关", "true": "开", "false": "关",
    "cooling": "制冷", "heating": "制热", "auto": "自动",
    "dehumidify": "除湿", "dry": "除湿", "fan": "送风",
    # 门锁/电机这类"按键状态"属性只有英文描述（Set/Reset），
    # 在场景里表示"触发这个按键"
    "set": "触发", "reset": "复位", "none": "无",
}


def _scene_prop_unit(name: str) -> str:
    """数值型属性的单位后缀（温度 °C、色温 K、亮度音量 %、风速档）。"""
    lowered = name.lower().replace("-", "")
    # 色温是开尔文，不能跟着 "temperature" 一起当成摄氏度
    if "colortemperature" in lowered:
        return "K"
    if "temperature" in lowered:
        return "°C"
    if lowered in ("brightness", "volume"):
        return "%"
    if "fanlevel" in lowered or "speedlevel" in lowered:
        return " 档"
    return ""


def format_metrics_text(temp, hum) -> str | None:
    """温湿度展示文案（如 "28.3°C 60%"）；两项都无效时返回 None。

    卡片副标题与详情面板回读共用，避免量纲启发式两处漂移。
    """
    parts: list[str] = []
    if temp is not None:
        try:
            parts.append(_format_temp(float(temp)))
        except (TypeError, ValueError):
            pass
    if hum is not None:
        try:
            parts.append(_format_humidity(float(hum)))
        except (TypeError, ValueError):
            pass
    return " ".join(parts) if parts else None


def _format_temp(value: float) -> str:
    # SI 规范里 temperature 常以 0.1 摄氏度步进的整数存储（283 = 28.3），
    # 按合理室温范围启发式区分真实值与 0.1 度整数值
    if abs(value) > 60:
        value /= 10
    return f"{value:.1f}°C"


def _format_humidity(value: float) -> str:
    # 湿度同样存在 0.1% 步进的整数存储（683 = 68.3%）
    if value > 100:
        value /= 10
    return f"{value:.0f}%"
