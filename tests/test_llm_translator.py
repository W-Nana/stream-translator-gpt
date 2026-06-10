import queue
import threading

import numpy as np

from stream_translator_gpt.common import TranslationTask
from stream_translator_gpt.llm_translator import LLMTranslator


class FlakyTranslator(LLMTranslator):

    def __init__(self, retry_if_translation_fails):
        super().__init__(model="fake",
                         prompt="translate",
                         history_size=0,
                         use_json_result=False,
                         timeout=0,
                         retry_if_translation_fails=retry_if_translation_fails)
        self.attempts = 0

    def translate(self, translation_task):
        self.attempts += 1
        if self.attempts == 1:
            translation_task.translation_failed = True
            return
        translation_task.translation = "ok"

    def _retrigger_failed_tasks(self):
        for task in list(self.processing_queue):
            if task.translation_failed and task.llm_latency_ms is not None:
                self._trigger(task)


class AlwaysFailTranslator(LLMTranslator):

    def __init__(self):
        super().__init__(model="fake",
                         prompt="translate",
                         history_size=0,
                         use_json_result=False,
                         timeout=0,
                         retry_if_translation_fails=False)

    def translate(self, translation_task):
        translation_task.translation_failed = True


def run_translator(translator):
    task = TranslationTask(np.zeros(1, dtype=np.float32), (0.0, 1.0))
    task.transcript = "hello"
    input_queue = queue.Queue()
    output_queue = queue.Queue()
    input_queue.put(task)
    input_queue.put(None)
    thread = threading.Thread(target=translator.loop, args=(input_queue, output_queue))
    thread.start()
    result = output_queue.get(timeout=5)
    sentinel = output_queue.get(timeout=5)
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert sentinel is None
    return result


def test_retry_enabled_continues_retrying_after_input_eof():
    translator = FlakyTranslator(retry_if_translation_fails=True)

    result = run_translator(translator)

    assert result.translation == "ok"
    assert translator.attempts == 2


def test_retry_disabled_releases_failed_task_after_input_eof():
    result = run_translator(AlwaysFailTranslator())

    assert result.translation is None
    assert result.translation_failed is True
