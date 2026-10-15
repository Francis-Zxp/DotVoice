# 点声 DotVoice 0.3 · 设计与行为

根据用户对旧界面的反馈重新设计。保留 Python / pywebview / 系统 WebView2，以原生 HTML、CSS、Canvas 实现，不引入新前端框架或网络字体。

## 视觉方向

暖纸 #F2F0EB、炭黑 #292B28、朱红 #B94A35。两行大字与单一声音图形组织注意力；录音时切换朱红色面，完成后以转写文字为主体。四页顶部导航：语音、设备、续航、设置。设备页容纳倍思、汉王，以及独立的 Tidepool 控制区。

原创 SVG Logo 是三段声流与中心点；ICO 包含 16–256 像素七种尺寸。设备轮廓是示意图。中文使用 Microsoft YaHei UI，拉丁文字使用 Segoe UI Variable Text。

## 参考与取舍

- [Oil UI「应声」](https://ui.oiloil.org/works/voice/live/index.html)：已查看四态和源码；参考声音、排版与状态连续变化。原案例使用固定文字/声调数组模拟交互。本项目独立实现音量包络，只显示 OpenLess 实际返回的最终文字。
- [Phyllotaxis Bloom](https://bookofshapes.com/patterns/phyllotaxis-bloom)：参考黄金角花序，以数学公式绘制 180 条短线。
- [Amicro Loaders](https://amicro.vercel.app/loaders) 与 [Mono Charts](https://amicro.vercel.app/mono-charts)：参考节奏及单色数据表现，图表只使用实际记录。
- Healthcare Sphere 尚未定位。[Halftone Sphere](https://bookofshapes.com/patterns/halftone_sphere) 是候选，未认作同一案例。
- Inspora / GSAP 为补充入口；Awwwards 本轮访问失败，未安装 Refero MCP。

未复制参考站的 JS/CSS 或品牌资产。研究下载放在 design/references，发布脚本排除该目录。

## 四态

| 状态 | 形态 | 数据和时序 |
| --- | --- | --- |
| 等待 | 静态花序、朱红中心点 | 不运行持续绘制循环 |
| 聆听 | 朱红背景、浅色包络、文字轻微位移 | 实际 PCM 音量；120 个历史点；250ms 无电平归零 |
| 识别 | 缓慢聚合花序 | 表示等待，不伪造进度 |
| 结果 | 声音轨迹与文字卡 | 最终结果到达后显示，一次有限揭示 |

这是音量可视化，不是声纹身份识别、音高分析或实时逐字转写。Canvas 上限 30fps、DPR 上限 1.5。隐藏、最小化、减少动态效果时停止持续动画。结果轨迹揭示不超过 950ms，长文本直接显示。动效不发送按键或粘贴。

## 验证

应用本地 redesign-existing-projects 与 ui-ux-pro-max 规范。状态有文字说明、键盘焦点可见、模态框可退出、图表附数字。状态更新不抢编辑框焦点。

tools/ui_qa.cjs 使用 Edge 与假后端检查导航、状态、设备控制、文字安全、记录与窄窗。ui/screens.html 是四态并排预览，明确标注模拟数据，不接麦克风。真实 Windows / Python 通信用 --smoke-ui 检查。实笔验收、笔耗电和续航预测不由截图或构建结果代替；见 VALIDATION.md。


## 0.4 视觉修订

以用户明确指定的 Oil UI「应声」为当前动效参考。删除花序与对称音量包络，独立绘制逐行文字上方的单条细声线：玫红聆听（当时的品牌色，0.11.3 起改为标识靛蓝，见文末）、灰墨识别、白纸结果卡。等待静止，活动上限 30fps；隐藏、最小化与减少动态时停绘。结果文字逐步着色，不伪装流式 ASR。新 Logo 是连续 d 形笔画和声点，SVG 与多尺寸 ICO 同源。参考截图/源码仅保留在研究目录，不装入产品。

## 0.5 设备与可观测状态

保留 0.4 的排版和细声线。重绘三个设备 SVG，麦克风/暂停/停止使用完整的统一线宽图标。绿色 #66ed91 表示连接，红色 #ed5869 表示未连接或暂停；状态有文字，不用颜色掩盖差异。接收器存在和 OpenLess 就绪分别显示。电量读取真实 HID，标明时间，过期数值降对比。录音和 VAD 说话时长分开，不混淆会话长度与发声。

## 0.11.3 品牌色：标识靛蓝

0.7 起沿用的玫红品牌色（#d23a68 / #ef6a8b）与 0.10.4 起的靛蓝标识不一致，现统一为标识的靛蓝（shell/tools/make-icons.mjs：#7b74ff → #5b4ff0 → #3d2fc7）。令牌在 shell/src/styles.css，浅色、深色、液态玻璃三套主题一致：

| 令牌 | 浅色 | 深色 / 液态玻璃 | 用途 |
| --- | --- | --- | --- |
| --brand | #5145e4 | #5b4ff0 | 主按钮、开关、滑杆、语音页大圆钮、向导步骤点；其上白字均达 WCAG AA |
| --brand-2 / --brand-strong | #7b74ff / #3d2fc7 | 同左 | 大圆钮按标识的三段渐变 |
| --brand-text | #4b3fd9 | #a5a0ff（玻璃 #b4afff） | 品牌色作文字或图标：导航当前图标、链接、录制快捷键、当前型号与标签建议 |
| --ring | 品牌色 80% | #8f88ff 85% | 键盘焦点环 |
| --engine-openless | #7c3aed | #7c3aed | OpenLess 引擎色（填充），与品牌色区分：正在使用的引擎图标底色，其上白色图标 ≥3:1 |
| --engine-openless-text | #7c3aed | #a78bfa | OpenLess 引擎色作文字或图标（卡片图标、向导单选点） |
| --engine-volc-text | #2850c8 | #8aa8ff（玻璃 #94b0ff） | 豆包引擎色作文字或图标：豆包提示条的「使用豆包语音」、卡片图标与「推荐」；填充仍用 #3a6df0 |

模式色不变（输入靛蓝、笔记琥珀、演示青绿）；红色只表示功能状态（错误、关闭按钮悬停、激光、录音点、暂停灯）。浅色主题右下的暖桃色光斑与胶囊边缘的粉色色散改为冷调青蓝 / 淡紫。ui-qa.mjs 检查四套令牌下 --brand 为靛蓝、主按钮不含旧玫红。

引擎色与品牌色一样分两套（0.11.4）：卡片上 --tone 是填充（色调底、正在使用的实心图标），--tone-text 是同一色相作文字或图标（lib/engine.ts 的 ENGINE_TONE / ENGINE_TONE_TEXT）。深色下曾把作文字用的浅紫 #a78bfa 当作填充，正在使用的 OpenLess 图标成了浅紫底白字（2.4:1）；现在填充三套主题都是 #7c3aed（白字约 4.3–4.5:1）。src/theme-tokens.test.ts 按令牌计算对比度；ui-qa.mjs 在浅色、深色、液态玻璃下按像素检查豆包提示条按钮 ≥4.5:1、正在使用的 OpenLess 图标白字 ≥3:1。
