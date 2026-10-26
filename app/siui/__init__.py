import app.siui.components as components
import app.siui.components.widgets as widgets
import app.siui.core as core
import app.siui.core.globals
import app.siui.gui as gui

# 这里原本会在导入时按系统 DPI 写一次 QT_SCALE_FACTOR，而应用入口 run.py
# 之后又会按「基准 × 用户设置」再写一次——两处各自写入同一个变量，谁后写
# 谁生效，很容易叠成双倍缩放（系统 200% 时被放大到 250%）。缩放现在统一由
# run.py 在创建 QApplication 之前写入一次，siui 只跟随 QApplication 的
# 设备像素比（见 components/widgets/abstracts/widget.py），不再自己设。
