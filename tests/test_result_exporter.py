import queue

import numpy as np

from stream_translator_gpt.common import TranslationTask
from stream_translator_gpt.result_exporter import ResultExporter


def make_task():
    task = TranslationTask(np.zeros(1, dtype=np.float32), (0.0, 1.0))
    task.transcript = "hello"
    task.translation = "world"
    return task


def test_file_output_worker_is_drained_before_loop_returns(tmp_path):
    output_file = tmp_path / "result.txt"
    exporter = ResultExporter(cqhttp_url=None,
                              cqhttp_token=None,
                              discord_webhook_url=None,
                              telegram_token=None,
                              telegram_chat_id=None,
                              output_file_path=str(output_file),
                              proxy=None,
                              output_whisper_result=True,
                              output_timestamps=False)
    input_queue = queue.Queue()
    input_queue.put(make_task())
    input_queue.put(None)

    exporter.loop(input_queue)

    assert output_file.read_text(encoding="utf-8") == "hello\nworld\n\n"


def test_network_output_worker_is_drained_before_loop_returns(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))

    monkeypatch.setattr("stream_translator_gpt.result_exporter.requests.post", fake_post)
    exporter = ResultExporter(cqhttp_url=None,
                              cqhttp_token=None,
                              discord_webhook_url="https://discord.example/webhook",
                              telegram_token=None,
                              telegram_chat_id=None,
                              output_file_path=None,
                              proxy=None,
                              output_whisper_result=False,
                              output_timestamps=False)
    input_queue = queue.Queue()
    input_queue.put(make_task())
    input_queue.put(None)

    exporter.loop(input_queue)

    assert calls[0][0] == "https://discord.example/webhook"
    assert calls[0][1]["json"] == {"content": "world"}
    assert calls[1][1]["json"] == {"content": "\u200b"}
