# 气声离线基准（whisper bench）

0.11.4 调气声前端（bridge/voice_dsp.py）用的离线基准。只在本机运行，不开麦克风、不上传音频。

这是可选开发工具，依赖独立环境中的 sherpa-onnx 和开发者已有的模型；点声发行版不包含、加载或调用本机 ASR。不要为了运行点声安装这些额外依赖。

1. 建独立环境（不进发布包）：`python -m venv .venv`，再 `.venv\Scripts\pip install numpy scipy sherpa-onnx==1.13.8 webrtcvad`。
2. `tts.ps1` 用 Windows 中文语音朗读 `corpus.json` 的 30 句，生成 `tts\`。
3. `build_bench.py`：LPC 合成气声，加上倍思笔的信道模型（开头数字零、开麦爆音、门控把停顿变成数字零），生成 `wavs\`。`build_ungated.py` 生成无门控麦克风的对照集，`build_noise.py` 生成没人说话的录音集。
4. `run_bench.py --dsp ..\..\bridge\voice_dsp.py --label v2`：按 controller 的方式逐 50 ms 处理，用 SenseVoice（OpenLess 的模型目录）识别，输出字错率；`--dsp raw` 为不处理。`compare.py` 做配对 bootstrap。
5. `gtcrn_post.py` 是 GTCRN 降噪实验（未采用：会把气声当噪声去掉）。`level_test.py` 验证代理识别器对电平不敏感。

`results\` 里是 0.11.4 的最终对比表。信道模型的参数是推测的；有了真实笔录音（设置里打开调试录音）后，应先用它们校准门限、爆音形状，再调参。
