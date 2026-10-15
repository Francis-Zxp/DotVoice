# 让 OpenLess 远程接口转发实时文字

## 结论

OpenLess（Open-Less/openless，beta 分支，提交 6f9af89，2026-09-30）在识别过程中**已经**按会话发布实时文字：

- `crates/openless-core/src/dictation_engine.rs` 的 `TranscriptProgressForwarder` 把云端流式识别的每次更新发布为 `EngineProgress::TranscriptDelta { text, offset, is_final: false }`，带本次会话的 `session_id`。设置里开启“停止后再识别”（`transcribe_after_stop`）时不发布。
- 识别结束时再发一次 `TranscriptDelta { text: 全文, offset: 0, is_final: true }`。
- 远程输入服务 `src-tauri/src/remote_server/mod.rs` 的 `backend_event_to_phone` 已按 `session_id` 只放行本连接自己的会话，但 `match` 里只处理 `DictationCompleted` 和 `DictationStateChanged`，`TranscriptDelta` 落进默认分支被丢弃。

所以点声拿不到实时文字，不是协议做不到，只是少了一个分支。这些文字本来就属于点声自己开的会话，不会泄露本机或其他手机的听写内容，符合源码注释里的隐私约束。

## 补丁（约 8 行）

`openless-all/app/src-tauri/src/remote_server/mod.rs`，在 `backend_event_to_phone` 的 `match &event.kind {` 里加：

```rust
        BackendEventKind::TranscriptDelta(delta) => vec![serde_json::json!({
            "type": "partial",
            "text": delta.text,
            "offset": delta.offset,
            "final": delta.is_final,
        })
        .to_string()],
```

`offset` 的含义与 `TranscriptAccumulator::apply` 相同：从第 offset 个字符起替换为 text。

兼容性：OpenLess 自带的手机网页（`assets/remote-input/app.js` 约 1197 行）用 `switch (msg.type)` 分派，未知类型直接忽略，所以旧手机页面不受影响。

## 点声这边

0.9 已经支持：收到 `{"type":"partial"}` 后按 offset 累积文字，立刻停止本机预览模型，胶囊和便签改为显示 OpenLess 的实时文字（同一个云端引擎，准确度与最终结果一致）；最终结果到达后照旧替换。旧版 OpenLess 不发这条消息时，自动退回本机预览。见 `bridge/controller.py` 的 `_remote_partial` 和 `tests/test_live_notes.py`。

## 可选的推进方式（需要你决定）

1. 向 OpenLess 提交 issue / PR（公开发布内容，需要你同意或由你提交）。PR 说明可直接用本文。
2. 在本机用补丁自行编译 OpenLess 并替换安装（需要完整 Rust/Tauri 构建，会替换你正在用的 OpenLess，需要你同意）。
3. 等待上游合并期间，用更准的本机预览模型过渡（需要你同意下载模型文件）。
