"""Count transmitted PCM and locally classified speech; never retain audio."""
import webrtcvad


class SpeechMeter:
    def __init__(self, detector=None):
        self.vad = detector or webrtcvad.Vad(2)
        self.remainder = b''
        self.audio_seconds = self.speech_seconds = 0.0

    def feed(self, pcm):
        audio_seconds = len(pcm) / 32000
        data = self.remainder + pcm
        speech_seconds = 0.0
        end = len(data) // 320 * 320  # 10 ms, 16 kHz S16LE mono
        for offset in range(0, end, 320):
            if self.vad.is_speech(data[offset:offset + 320], 16000):
                speech_seconds += .01
        self.remainder = data[end:]
        self.audio_seconds += audio_seconds
        self.speech_seconds += speech_seconds
        return {'audio_seconds': audio_seconds, 'speech_seconds': speech_seconds}

    def snapshot(self):
        return {'audio_seconds': round(self.audio_seconds, 3),
                'speech_seconds': round(min(self.audio_seconds, self.speech_seconds), 3)}
