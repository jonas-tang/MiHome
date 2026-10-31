# MiHome
本项目fork自大佬幻月觉的项目 [MiHome-Windows](https://github.com/huanyuejue/MiHome-Windows) 。

同时也向开发相关接口和ui的大佬致敬
- [mijia-api](https://github.com/Do1e/mijia-api) - 米家 API 封装
- [PySide6-SiliconUI](https://github.com/H1DDENADM1N/PySide6-SiliconUI) - UI 组件库（已内置至 `app/siui/`）

目前基于自己的使用场景进行了适配，持续改造中
## 修改内容
- 增加PyInstaller作为可选的编译工具，加速编译，方便测试，最终发行版用Nuitka编译可以提高运行效率
- 适配了多家庭设备的显示，包括不同家庭相同名称房间也可以分区显示设备
- 多通道设备的聚合，在设备详情可以展示多个标签，点击可以对相应通道进行设置
- 完善温湿度显示，过滤了非环境设备的温度数据
- 把场景标签页改成了智能，展示设置的手动和自动化任务
- 持续改进


## 开源许可

本项目基于 [GPL-3.0](LICENSE) 或更高版本发布。

对本项目代码的使用、修改与分发同样须遵循 GPL-3.0。

本程序不含任何担保。请自行承担使用风险，并遵守小米的服务条款。
