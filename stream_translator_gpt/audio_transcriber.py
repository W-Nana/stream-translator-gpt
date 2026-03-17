import os
import io
import queue
import re
import atexit
import threading
from abc import abstractmethod
from scipy.io.wavfile import write as write_audio

import numpy as np

from . import filters
from .common import TranslationTask, SAMPLE_RATE, LoopWorkerBase, sec2str, ApiKeyPool, INFO
from .simul_streaming.simul_whisper.whisper.utils import compression_ratio


def _filter_text(text: str, transcription_filters: str):
    filter_name_list = transcription_filters.split(',')
    for filter_name in filter_name_list:
        filter = getattr(filters, filter_name)
        if not filter:
            raise Exception('Unknown filter: %s' % filter_name)
        text = filter(text)
    return text


_stderr_filter_installed = False


def _install_stderr_filter():
    global _stderr_filter_installed
    if _stderr_filter_installed:
        return

    source_fd = os.dup(2)
    read_fd, write_fd = os.pipe()
    os.dup2(write_fd, 2)
    os.close(write_fd)

    ignored_markers = (
        'NNPACK.cpp:57',
        'Could not initialize NNPACK! Reason: Unsupported hardware.',
    )

    def _pump_stderr():
        with os.fdopen(read_fd, 'rb', closefd=True) as reader:
            while True:
                chunk = reader.readline()
                if not chunk:
                    break
                text = chunk.decode(errors='replace')
                if all(marker in text for marker in ignored_markers):
                    continue
                os.write(source_fd, chunk)

    threading.Thread(target=_pump_stderr, daemon=True).start()
    atexit.register(lambda: os.close(source_fd))
    _stderr_filter_installed = True


class AudioTranscriber(LoopWorkerBase):

    def __init__(self, transcription_filters: str, print_result: bool, output_timestamps: bool,
                 disable_transcription_context: bool, transcription_initial_prompt: str):
        self.transcription_filters = transcription_filters
        self.print_result = print_result
        self.output_timestamps = output_timestamps
        self.disable_transcription_context = disable_transcription_context
        self.transcription_initial_prompt = transcription_initial_prompt

        self.constant_prompt = re.sub(r',\s*', ', ',
                                      transcription_initial_prompt) if transcription_initial_prompt else ""
        if self.constant_prompt and not self.constant_prompt.strip().endswith(','):
            self.constant_prompt += ','

    @abstractmethod
    def transcribe(self, audio: np.array, initial_prompt: str = None) -> tuple[str, list | None]:
        """Returns (text, tokens). tokens can be None if not available."""
        pass

    def reset_context(self):
        """Override in subclass to reset model context when repetition is detected."""
        pass

    def loop(self,
             input_queue: queue.SimpleQueue[TranslationTask],
             output_queue: queue.SimpleQueue[TranslationTask],
             preview_output_queue: queue.SimpleQueue[TranslationTask] = None):
        previous_text = ""

        while True:
            task = input_queue.get()
            if task is None:
                output_queue.put(None)
                break

            dynamic_context = filters.symbol_filter(previous_text) if not self.disable_transcription_context else ""

            if self.constant_prompt:
                limit = 500 - len(self.constant_prompt) - 1
                if len(dynamic_context) > limit:
                    if limit > 0:
                        dynamic_context = dynamic_context[-limit:]
                    else:
                        dynamic_context = ""

            initial_prompt = f"{self.constant_prompt} {dynamic_context}".strip()
            if not initial_prompt:
                initial_prompt = None

            text, tokens = self.transcribe(task.audio, initial_prompt=initial_prompt)

            if self.constant_prompt and text.strip().rstrip(',') == self.constant_prompt.strip().rstrip(','):
                text = ""

            # Repetition detection: reset context if compression ratio too high OR token diversity too low
            is_repetitive = False
            if len(text) > 10:
                zlib_ratio = compression_ratio(text)
                unique_ratio = len(set(tokens)) / len(tokens) if tokens else 1.0

                if zlib_ratio > 2.0 or unique_ratio < 0.4:
                    self.reset_context()
                    is_repetitive = True

            task.transcript = _filter_text(text, self.transcription_filters).strip()
            if not task.transcript:
                continue
            previous_text = "" if is_repetitive else task.transcript
            if self.print_result:
                if self.output_timestamps:
                    timestamp_text = f'{sec2str(task.time_range[0])} --> {sec2str(task.time_range[1])}'
                    print(timestamp_text + ' ' + task.transcript)
                else:
                    print(task.transcript)
            if preview_output_queue is not None:
                preview_output_queue.put(task.make_output_task(output_stage='transcript'))
                task.output_stage = 'translation'
            else:
                task.output_stage = 'complete'
            output_queue.put(task)


class OpenaiWhisper(AudioTranscriber):

    def __init__(self, model: str, language: str, **kwargs) -> None:
        super().__init__(**kwargs)
        import whisper

        print(f'{INFO}Loading Whisper model: {model}')
        self.model = whisper.load_model(model)
        self.language = language

    def transcribe(self, audio: np.array, initial_prompt: str = None) -> tuple[str, list | None]:
        result = self.model.transcribe(audio,
                                       without_timestamps=True,
                                       language=self.language,
                                       initial_prompt=initial_prompt)
        text = result.get('text', '')
        tokens = []
        for segment in result.get('segments', []):
            tokens.extend(segment.get('tokens', []))
        return text, tokens if tokens else None


class FasterWhisper(AudioTranscriber):

    def __init__(self, model: str, language: str, **kwargs) -> None:
        super().__init__(**kwargs)
        from faster_whisper import WhisperModel

        print(f'{INFO}Loading Faster-Whisper model: {model}')
        self.model = WhisperModel(model, device='auto', compute_type='auto')
        self.language = language

    def transcribe(self, audio: np.array, initial_prompt: str = None) -> tuple[str, list | None]:
        segments, info = self.model.transcribe(audio, language=self.language, initial_prompt=initial_prompt)
        text = ''
        tokens = []
        for segment in segments:
            text += segment.text
            tokens.extend(getattr(segment, 'tokens', None) or [])
        return text, tokens if tokens else None


class Qwen3ASR(AudioTranscriber):

    LANGUAGE_ALIASES = {
        'zh': 'Chinese',
        'en': 'English',
        'yue': 'Cantonese',
        'ja': 'Japanese',
        'ko': 'Korean',
        'de': 'German',
        'fr': 'French',
        'es': 'Spanish',
        'ru': 'Russian',
        'it': 'Italian',
        'pt': 'Portuguese',
        'vi': 'Vietnamese',
        'id': 'Indonesian',
        'th': 'Thai',
        'ms': 'Malay',
        'ar': 'Arabic',
    }

    def __init__(self, model: str, language: str, **kwargs) -> None:
        super().__init__(**kwargs)
        try:
            import torch
            from packaging.version import Version
        except ImportError as exc:
            raise ImportError(
                'Qwen3-ASR backend requires the official runtime package in the active venv. '
                'Install it with "pip install -U qwen-asr" and retry. '
                'If installation fails in Python 3.10, the official repo recommends using a fresh Python 3.12 environment.'
            ) from exc
        if Version(torch.__version__.split('+')[0]) < Version('2.2.0'):
            raise RuntimeError(
                f'Qwen3-ASR requires torch>=2.2, but the active venv has torch {torch.__version__}. '
                'Please upgrade torch in this venv, then retry.'
            )
        _install_stderr_filter()
        if hasattr(torch.backends, 'nnpack') and torch.backends.nnpack.is_available():
            torch.backends.nnpack.set_flags(False)
        try:
            import sys
            import types
            if 'nagisa' not in sys.modules:
                nagisa_stub = types.ModuleType('nagisa')

                def _unsupported_nagisa(*args, **kwargs):
                    raise RuntimeError(
                        'Qwen3-ASR forced alignment is unavailable in this environment because the nagisa package '
                        'cannot run on this CPU. Basic ASR still works.'
                    )

                nagisa_stub.tagging = _unsupported_nagisa
                sys.modules['nagisa'] = nagisa_stub
            from qwen_asr import Qwen3ASRModel
            from transformers import logging as transformers_logging
        except ImportError as exc:
            raise ImportError(
                'Qwen3-ASR runtime package is installed incompletely. '
                'Please reinstall it with "pip install -U qwen-asr".'
            ) from exc

        transformers_logging.set_verbosity_error()
        print(f'{INFO}Loading Qwen3-ASR model: {model}')
        dtype = torch.bfloat16 if torch.cuda.is_available() else torch.float32
        self.model = Qwen3ASRModel.from_pretrained(
            model,
            dtype=dtype,
            device_map='auto',
        )
        generation_config = getattr(getattr(self.model, 'model', None), 'generation_config', None)
        if generation_config is not None:
            if getattr(generation_config, 'eos_token_id', None) is not None:
                generation_config.pad_token_id = generation_config.eos_token_id
            if hasattr(generation_config, 'temperature'):
                generation_config.temperature = None
        self.language = self.LANGUAGE_ALIASES.get(language, language)

    @staticmethod
    def _extract_text(result):
        if result is None:
            return ''
        if isinstance(result, str):
            return result
        if isinstance(result, dict):
            return result.get('text', '') or result.get('transcript', '') or result.get('content', '')
        if isinstance(result, (list, tuple)):
            return ''.join(Qwen3ASR._extract_text(item) for item in result)
        text = getattr(result, 'text', None)
        if text is not None:
            return text
        return str(result)

    def transcribe(self, audio: np.array, initial_prompt: str = None) -> tuple[str, list | None]:
        results = self.model.transcribe(
            audio=(audio, SAMPLE_RATE),
            language=self.language,
        )
        return self._extract_text(results), None


class SimulStreaming(AudioTranscriber):

    def __init__(self, model: str, language: str, use_faster_whisper: bool, **kwargs) -> None:
        super().__init__(**kwargs)
        from .simul_streaming.simulstreaming_whisper import SimulWhisperASR, SimulWhisperOnline

        fw_encoder = None
        if use_faster_whisper:
            print(f'{INFO}Loading Faster-Whisper as encoder for SimulStreaming: {model}')
            from faster_whisper import WhisperModel
            fw_encoder = WhisperModel(model, device='auto', compute_type='auto')

        print(f'{INFO}Loading SimulStreaming model: {model}')
        simulstreaming_params = {
            "language": language,
            "model": model,
            "cif_ckpt_path": None,
            "frame_threshold": 25,
            "audio_max_len": 10.0,
            "audio_min_len": 0.0,
            "segment_length": 0.5,
            "task": "transcribe",
            "beams": 1,
            "decoder_type": "greedy",
            "never_fire": False,
            "init_prompt": self.constant_prompt,
            "static_init_prompt": None,
            "max_context_tokens": 50,
            "logdir": None,
            "fw_encoder": fw_encoder,
        }
        asr = SimulWhisperASR(**simulstreaming_params)
        self.asr_online = SimulWhisperOnline(asr)
        self.asr_online.init()

    def transcribe(self, audio: np.array, initial_prompt: str = None) -> tuple[str, list | None]:
        self.asr_online.insert_audio_chunk(audio)
        result = self.asr_online.process_iter(is_last=True)
        return result.get('text', ''), result.get('tokens', None)

    def reset_context(self):
        self.asr_online.model.refresh_segment(complete=True)
        self.asr_online.unicode_buffer = []


class RemoteOpenaiTranscriber(AudioTranscriber):
    # https://platform.openai.com/docs/api-reference/audio/createTranscription?lang=python

    def __init__(self, model: str, language: str, proxy: str, **kwargs) -> None:
        super().__init__(**kwargs)
        print(f'{INFO}Using {model} API as transcription engine.')
        self.model = model
        self.language = language
        self.proxy = proxy

    def transcribe(self, audio: np.array, initial_prompt: str = None) -> tuple[str, list | None]:
        from openai import OpenAI
        import httpx

        # Create an in-memory buffer
        audio_buffer = io.BytesIO()
        audio_buffer.name = 'audio.wav'
        write_audio(audio_buffer, SAMPLE_RATE, audio)
        audio_buffer.seek(0)

        call_args = {
            'model': self.model,
            'file': audio_buffer,
            'language': self.language,
        }
        if initial_prompt:
            call_args['prompt'] = initial_prompt

        api_key = ApiKeyPool.get_openai_api_key()
        client = OpenAI(api_key=api_key, http_client=httpx.Client(proxy=self.proxy, verify=False))
        result = client.audio.transcriptions.create(**call_args).text
        return result, None
