import queue

import numpy as np

from stream_translator_gpt import audio_slicer
from stream_translator_gpt.common import SAMPLES_PER_FRAME


class FakeVad:

    def __init__(self, probs):
        self.probs = list(probs)
        self.last = self.probs[-1] if self.probs else 0.0

    def get_speech_prob(self, audio):
        if self.probs:
            self.last = self.probs.pop(0)
        return self.last

    def reset_states(self):
        pass


def make_slicer(monkeypatch, probs, prefix_retention_length=0.0, min_audio_length=10.0):
    monkeypatch.setattr(audio_slicer, "create_vad_adapter", lambda *args, **kwargs: FakeVad(probs))
    return audio_slicer.AudioSlicer(min_audio_length=min_audio_length,
                                    max_audio_length=30.0,
                                    target_audio_length=5.0,
                                    continuous_no_speech_threshold=1.0,
                                    dynamic_no_speech_threshold=False,
                                    prefix_retention_length=prefix_retention_length,
                                    vad_threshold=0.5,
                                    dynamic_vad_threshold=False)


def test_eof_flushes_trailing_speech_even_below_min_length(monkeypatch):
    slicer = make_slicer(monkeypatch, [1.0], min_audio_length=10.0)
    frame = np.ones(SAMPLES_PER_FRAME, dtype=np.float32)
    input_queue = queue.Queue()
    output_queue = queue.Queue()
    input_queue.put(frame)
    input_queue.put(None)

    slicer.loop(input_queue, output_queue)

    task = output_queue.get_nowait()
    assert task.transcript is None
    assert len(task.audio) == SAMPLES_PER_FRAME
    assert output_queue.get_nowait() is None


def test_eof_does_not_emit_pure_silence(monkeypatch):
    slicer = make_slicer(monkeypatch, [0.0], min_audio_length=10.0)
    input_queue = queue.Queue()
    output_queue = queue.Queue()
    input_queue.put(np.zeros(SAMPLES_PER_FRAME, dtype=np.float32))
    input_queue.put(None)

    slicer.loop(input_queue, output_queue)

    assert output_queue.get_nowait() is None
    assert output_queue.empty()


def test_zero_prefix_retention_does_not_keep_previous_slice(monkeypatch):
    slicer = make_slicer(monkeypatch, [1.0, 1.0], prefix_retention_length=0.0, min_audio_length=0.0)
    frame = np.ones(SAMPLES_PER_FRAME, dtype=np.float32)

    slicer.put(frame)
    first_audio, _ = slicer.slice()
    assert len(first_audio) == SAMPLES_PER_FRAME
    assert slicer.prefix_audio_buffer == []

    slicer.put(frame)
    second_audio, _ = slicer.slice()
    assert len(second_audio) == SAMPLES_PER_FRAME
