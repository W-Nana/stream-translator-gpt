import os
import re
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import numpy as np

SAMPLE_RATE = 16000
SAMPLES_PER_FRAME = 512  # Requested by silero-vad >= v5
FRAME_DURATION = SAMPLES_PER_FRAME / SAMPLE_RATE

RED = '\033[91m'
YELLOW = '\033[93m'
GREEN = "\033[32m"
BOLD = '\033[1m'
ENDC = '\033[0m'

INFO = f'{GREEN}[INFO]{ENDC} '
WARNING = f'{YELLOW}[WARNING]{ENDC} '
ERROR = f'{RED}[ERROR]{ENDC} '


class TranslationTask:

    _id_lock = threading.Lock()
    _next_id = 0

    def __init__(self,
                 audio: Optional[np.array],
                 time_range: tuple[float, float],
                 task_id: Optional[int] = None,
                 output_stage: str = 'complete'):
        self.audio = audio
        self.transcript = None
        self.context_transcripts = None
        self.translation = None
        self.time_range = time_range
        self.start_time = None
        self.translation_failed = False
        self.output_stage = output_stage
        self.transcription_started_at = None
        self.transcription_completed_at = None
        self.transcription_duration_ms = None
        self.translation_started_at = None
        self.translation_completed_at = None
        self.translation_duration_ms = None
        if task_id is None:
            with self._id_lock:
                self.task_id = self._next_id
                TranslationTask._next_id += 1
        else:
            self.task_id = task_id

    def make_output_task(self, output_stage: str):
        task = TranslationTask(audio=None, time_range=self.time_range, task_id=self.task_id, output_stage=output_stage)
        task.transcript = self.transcript
        task.context_transcripts = self.context_transcripts
        task.translation = self.translation
        task.start_time = self.start_time
        task.translation_failed = self.translation_failed
        task.transcription_started_at = self.transcription_started_at
        task.transcription_completed_at = self.transcription_completed_at
        task.transcription_duration_ms = self.transcription_duration_ms
        task.translation_started_at = self.translation_started_at
        task.translation_completed_at = self.translation_completed_at
        task.translation_duration_ms = self.translation_duration_ms
        return task

    @staticmethod
    def utcnow():
        return datetime.now(timezone.utc)

    @staticmethod
    def isoformat_or_none(value: Optional[datetime]):
        if value is None:
            return None
        return value.isoformat().replace('+00:00', 'Z')

    @staticmethod
    def elapsed_ms(start_monotonic: float, end_monotonic: Optional[float] = None):
        if end_monotonic is None:
            end_monotonic = time.perf_counter()
        return int((end_monotonic - start_monotonic) * 1000)


class LoopWorkerBase(ABC):

    @abstractmethod
    def loop(self):
        pass


def start_daemon_thread(func, *args, **kwargs):
    thread = threading.Thread(target=func, args=args, kwargs=kwargs)
    thread.daemon = True
    thread.start()
    return thread


def sec2str(second: float):
    dt = datetime.fromtimestamp(second, tz=timezone.utc)
    result = dt.strftime('%H:%M:%S')
    result += ',' + str(int(second * 10 % 10))
    return result


class ApiKeyPool():

    @classmethod
    def init(cls, openai_api_key, google_api_key):
        cls.openai_api_key_list = [key.strip() for key in openai_api_key.split(',')] if openai_api_key else None
        cls.openai_api_key_index = 0
        cls.google_api_key_list = [key.strip() for key in google_api_key.split(',')] if google_api_key else None
        cls.google_api_key_index = 0

    @classmethod
    def get_openai_api_key(cls):
        if not cls.openai_api_key_list:
            return None
        key = cls.openai_api_key_list[cls.openai_api_key_index]
        cls.openai_api_key_index = (cls.openai_api_key_index + 1) % len(cls.openai_api_key_list)
        return key

    @classmethod
    def get_google_api_key(cls):
        if not cls.google_api_key_list:
            return None
        key = cls.google_api_key_list[cls.google_api_key_index]
        cls.google_api_key_index = (cls.google_api_key_index + 1) % len(cls.google_api_key_list)
        return key


def is_url(address):
    parsed_url = urlparse(address)

    if parsed_url.scheme and parsed_url.scheme != 'file':
        if parsed_url.netloc or (parsed_url.scheme in ['mailto', 'tel', 'data']):
            return True

    if parsed_url.scheme == 'file':
        return False

    if parsed_url.netloc:
        return True

    if os.name == 'nt':
        if re.match(r'^[a-zA-Z]:[\\/]', address):
            return False
        if address.startswith('\\\\') or address.startswith('//'):
            return False
        if '\\' in address and '/' not in address:
            return False

    if address.startswith('/') or address.startswith('./') or address.startswith('../'):
        return False

    if '/' in address or (os.name == 'nt' and '\\' in address):
        if not parsed_url.scheme and not parsed_url.netloc:
            return False

    return False
