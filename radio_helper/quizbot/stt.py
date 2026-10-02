"""음성 인식 (faster-whisper, 이 PC에서 무료로 실행).

처음 실행할 때 모델 파일을 내려받는다 (small 약 0.5GB). 이후에는 인터넷 없이 동작한다.
"""

from __future__ import annotations

import numpy as np

QUIZ_VOCAB = "퀴즈, 정답, 문제, 초성, 고릴라, 공감로그, 응모, 선물, 파워FM"


class WhisperTranscriber:
    def __init__(self, model_size: str = "small", program_title: str = ""):
        from faster_whisper import WhisperModel

        self.model = WhisperModel(model_size, device="auto", compute_type="int8")
        self.prompt = f"SBS 파워FM {program_title} 라디오 방송. {QUIZ_VOCAB}".strip()

    def transcribe(self, audio: np.ndarray) -> str:
        if audio is None or len(audio) < 16_000:  # 1초 미만
            return ""
        segments, _info = self.model.transcribe(
            audio, language="ko", beam_size=1, vad_filter=True,
            condition_on_previous_text=False, initial_prompt=self.prompt)
        return " ".join(seg.text.strip() for seg in segments).strip()
