import ctypes
import logging
import os

logger = logging.getLogger(__name__)


def get_windows_scaling_factor():
    try:
        # 调用 Windows API 函数获取缩放比例
        user32 = ctypes.windll.user32
        user32.SetProcessDPIAware()
        scaling_factor = user32.GetDpiForSystem()

        # 计算缩放比例
        return scaling_factor / 96.0

    except Exception as e:
        logger.debug("无法获取缩放比例，按 1 处理: %s", e)
        return 1


def reload_scale_factor():
    set_scale_factor(get_windows_scaling_factor(), identity="Windows API")


def set_scale_factor(factor, identity="External calls"):
    os.environ["QT_SCALE_FACTOR"] = str(factor)
    logger.debug("QT_SCALE_FACTOR=%s（来源: %s）", factor, identity)
