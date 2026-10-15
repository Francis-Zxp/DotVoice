"""Speech engines DotVoice can drive: OpenLess (bridge.backend), built-in cloud ASR
(Doubao / Bailian streaming + optional LLM polish), and a hotkey bridge for other
dictation apps. Every engine has the OpenLessBackend interface: connect, start,
feed, stop, cancel, poll, close and the insert_text flag."""
