# PV60 协议适配来源

`bridge/pv60.py` 的协议常量与动态 challenge 算法适配自 **Remote Bridge Hub**：

- 作者/仓库维护者：`xxb26553663-star`
- https://github.com/xxb26553663-star/remote-bridge-hub
- 固定提交：`8a93f321ac71a602300c6cd77f7256fa4b63068e`
- 来源文件：`source/bridges/hanvon/voice_typing_hid.py`
- 许可证：GNU GPL version 3，随包全文 `licenses/remote-bridge-hub-GPL-3.0.txt`
- DotVoice 改动：严格接口白名单、必须确认握手、单线程生命周期、故障关闭、同步刷新、仅语音键按下/松开回调、不注入键盘事件。

对应的适配源码与测试随 DotVoice 源码包提供。未使用该项目 `2655 AI` 名称、Logo、品牌资产、广告或安装器；品牌资产不在其代码 GPL 授权范围内。

hidapi 是运行时依赖，另保留其自身许可证。本适配不包含原厂闭源 DLL，不下载或安装虚拟声卡驱动。
