import contextlib
import gc
import os
import io
import logging
import queue
import re
import tempfile
import time
from abc import abstractmethod
from scipy.io.wavfile import write as write_audio

import numpy as np

from . import filters
from .common import TranslationTask, SAMPLE_RATE, LoopWorkerBase, sec2str, ClientPool, INFO, WARNING
from .simul_streaming.simul_whisper.whisper.utils import compression_ratio


def _filter_text(text: str, transcription_filters: str):
    filter_name_list = transcription_filters.split(',')
    for filter_name in filter_name_list:
        filter = getattr(filters, filter_name)
        if not filter:
            raise Exception('Unknown filter: %s' % filter_name)
        text = filter(text)
    return text


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

    def prepare_for_reuse(self):
        """Reset per-run state before reusing a preloaded transcriber."""
        self.reset_context()

    def loop(self, input_queue: queue.SimpleQueue[TranslationTask], output_queue: queue.SimpleQueue[TranslationTask]):
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

            asr_started_at = time.perf_counter()
            text, tokens = self.transcribe(task.audio, initial_prompt=initial_prompt)
            task.asr_latency_ms = (time.perf_counter() - asr_started_at) * 1000

            if self.constant_prompt and text.strip().rstrip(',') == self.constant_prompt.strip().rstrip(','):
                text = ""

            # Repetition detection: reset context if compression ratio too high OR token diversity too low
            is_repetitive = False
            if text:
                zlib_ratio = compression_ratio(text)
                unique_ratio = len(set(tokens)) / len(tokens) if tokens else 1.0

                if zlib_ratio > 1.5 or unique_ratio < 0.6:
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


def _apply_hf_proxy(proxy: str):
    try:
        import huggingface_hub
        session = huggingface_hub.utils.get_session()
        session.proxies = {'http': proxy, 'https': proxy}
        session.verify = False
    except Exception:
        pass


def _parse_torch_cuda_arch(arch: str) -> tuple[int, int] | None:
    match = re.fullmatch(r'sm_(\d+)(\d)', arch)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


class FasterWhisper(AudioTranscriber):

    def __init__(self, model: str, language: str, proxy: str, **kwargs) -> None:
        super().__init__(**kwargs)
        from faster_whisper import WhisperModel

        if proxy:
            _apply_hf_proxy(proxy)
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


class SimulStreaming(AudioTranscriber):

    def __init__(self, model: str, language: str, use_faster_whisper: bool, proxy: str, **kwargs) -> None:
        super().__init__(**kwargs)
        from .simul_streaming.simulstreaming_whisper import SimulWhisperASR, SimulWhisperOnline

        fw_encoder = None
        if use_faster_whisper:
            print(f'{INFO}Loading Faster-Whisper as encoder for SimulStreaming: {model}')
            from faster_whisper import WhisperModel
            if proxy:
                _apply_hf_proxy(proxy)
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

    def prepare_for_reuse(self):
        self.asr_online.init()


class RemoteOpenaiTranscriber(AudioTranscriber):
    # https://platform.openai.com/docs/api-reference/audio/createTranscription?lang=python

    def __init__(self, model: str, language: str, proxy: str, **kwargs) -> None:
        super().__init__(**kwargs)
        print(f'{INFO}Using {model} API as transcription engine.')
        self.model = model
        self.language = language

    def transcribe(self, audio: np.array, initial_prompt: str = None) -> tuple[str, list | None]:
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

        client = ClientPool.get_openai_client()
        result = client.audio.transcriptions.create(**call_args).text
        return result, None


class HFTranscriber(AudioTranscriber):

    def __init__(self, model: str, language: str, proxy: str, **kwargs) -> None:
        super().__init__(**kwargs)
        from transformers import pipeline

        if proxy:
            _apply_hf_proxy(proxy)

        if not os.path.exists(model):
            try:
                from huggingface_hub import model_info
                info = model_info(model)
                tag = info.pipeline_tag
                if tag and tag != 'automatic-speech-recognition':
                    raise ValueError(
                        f'Model "{model}" has pipeline_tag="{tag}", not "automatic-speech-recognition". '
                        f'It is not compatible with --use_hf_asr. '
                        f'Please choose a model with pipeline_tag="automatic-speech-recognition" on HuggingFace Hub.')
            except ImportError:
                pass

        print(f'{INFO}Loading HuggingFace ASR model: {model}')
        self.language = language
        self.pipe = pipeline('automatic-speech-recognition', model=model, device_map='auto')

    def transcribe(self, audio: np.array, initial_prompt: str = None) -> tuple[str, list | None]:
        generate_kwargs = {}
        if self.language:
            generate_kwargs['language'] = self.language
        result = self.pipe(
            {
                'array': audio,
                'sampling_rate': SAMPLE_RATE
            },
            generate_kwargs=generate_kwargs,
        )
        return result['text'], None


class NemoASRTranscriber(AudioTranscriber):

    def __init__(self, model: str, proxy: str, device: str, decoding: str, **kwargs) -> None:
        super().__init__(**kwargs)
        try:
            import torch
            import nemo.collections.asr as nemo_asr
        except ImportError as e:
            raise ImportError(
                'NeMo ASR support requires the nemo_asr extra. Install it with: '
                'pip install "stream-translator-gpt[nemo_asr]"'
            ) from e

        try:
            from nemo.collections.asr.parts.mixins.transcription import TranscribeConfig
        except ImportError:
            TranscribeConfig = None
        try:
            from nemo.utils import logging as nemo_logging
        except ImportError:
            nemo_logging = None

        self._nemo_logging = nemo_logging
        self._transcribe_config_cls = TranscribeConfig
        if proxy:
            _apply_hf_proxy(proxy)

        self.device = self._normalize_device(torch, device)
        self._validate_cuda_device_supported(torch, self.device)
        self._maybe_disable_incompatible_cudnn(torch, self.device)

        print(f'{INFO}Loading NeMo ASR model: {model}')
        restore_device = self._restore_device(torch, self.device)
        restore_kwargs = {'model_name': model}
        if restore_device is not None:
            restore_kwargs['map_location'] = restore_device
        if self.device is not None and restore_device is not None and str(restore_device) != str(self.device):
            print(f'{INFO}Using CPU staged load for NeMo ASR to reduce CUDA memory peak.')
            self._cleanup_cuda_cache(torch)
        with self._quiet_nemo_logging():
            self.model = nemo_asr.models.ASRModel.from_pretrained(**restore_kwargs)
        if self.device is not None:
            if restore_device is not None and str(restore_device) != str(self.device):
                self._cleanup_cuda_cache(torch)
            self.model.to(self.device)
            self._cleanup_cuda_cache(torch)
        if hasattr(self.model, 'eval'):
            self.model.eval()
        self.decoding = (decoding or 'tdt').strip().lower()
        self._configure_decoding(self.decoding)
        self._cleanup_cuda_cache(torch)

    @staticmethod
    def _normalize_device(torch, device: str | None):
        device = str(device or 'auto').strip()
        if not device or device == 'auto':
            return None
        if device.startswith('cuda') and not torch.cuda.is_available():
            raise RuntimeError('CUDA is not available for NeMo ASR. Use --nemo_asr_device cpu or install CUDA PyTorch.')
        return torch.device(device)

    @staticmethod
    def _restore_device(torch, target_device):
        if target_device is None:
            return None
        device_text = str(target_device).strip().lower()
        if device_text.startswith('cuda'):
            return torch.device('cpu')
        return target_device

    @classmethod
    def _validate_cuda_device_supported(cls, torch, device) -> None:
        if not getattr(torch, 'cuda', None) or not torch.cuda.is_available():
            return

        supported_caps = cls._torch_cuda_supported_caps(torch)
        if not supported_caps:
            return

        for index in cls._cuda_device_indices(torch, device):
            try:
                capability = torch.cuda.get_device_capability(index)
            except Exception:
                continue
            if capability >= min(supported_caps):
                continue

            device_name_fn = getattr(torch.cuda, 'get_device_name', None)
            device_name = device_name_fn(index) if callable(device_name_fn) else f'CUDA device {index}'
            torch_version = getattr(torch, '__version__', 'unknown')
            torch_cuda = getattr(getattr(torch, 'version', None), 'cuda', 'unknown')
            supported = ', '.join(f'sm_{major}{minor}' for major, minor in supported_caps)
            raise RuntimeError(
                f'Current PyTorch CUDA build does not support {device_name} (sm_{capability[0]}{capability[1]}) '
                f'for NeMo ASR. Installed torch={torch_version}, CUDA={torch_cuda}, supported architectures: '
                f'{supported}. Install a PyTorch build that includes sm_{capability[0]}{capability[1]}, then run '
                f'the existing environment entry point or use "uv run --no-sync" so uv does not replace it.')

    @staticmethod
    def _torch_cuda_supported_caps(torch) -> list[tuple[int, int]]:
        supported_caps = []
        for arch in torch.cuda.get_arch_list():
            capability = _parse_torch_cuda_arch(arch)
            if capability is not None:
                supported_caps.append(capability)
        return sorted(supported_caps)

    @classmethod
    def _maybe_disable_incompatible_cudnn(cls, torch, device) -> None:
        cudnn = getattr(getattr(torch, 'backends', None), 'cudnn', None)
        if cudnn is None or not getattr(cudnn, 'enabled', False):
            return
        if not getattr(torch, 'cuda', None) or not torch.cuda.is_available():
            return

        incompatible_device = None
        for index in cls._cuda_device_indices(torch, device):
            try:
                capability = torch.cuda.get_device_capability(index)
            except Exception:
                continue
            if capability < (7, 5):
                incompatible_device = (index, capability)
                break
        if incompatible_device is None:
            return

        version_fn = getattr(cudnn, 'version', None)
        try:
            cudnn_version = version_fn() if callable(version_fn) else None
        except RuntimeError as e:
            cudnn.enabled = False
            index, capability = incompatible_device
            print(
                f'{WARNING}Disabled cuDNN for NeMo ASR because cuDNN initialization failed on CUDA device '
                f'{index} SM {capability[0]}.{capability[1]}: {e}. CUDA will still be used.')
            return
        if not cudnn_version or cudnn_version < 90000:
            return

        index, capability = incompatible_device
        cudnn.enabled = False
        print(
            f'{WARNING}Disabled cuDNN for NeMo ASR because cuDNN {cudnn_version} is not compatible '
            f'with CUDA device {index} SM {capability[0]}.{capability[1]}. CUDA will still be used.')

    @staticmethod
    def _cuda_device_indices(torch, device) -> list[int]:
        if not torch.cuda.is_available():
            return []

        device_text = str(device or 'auto').strip().lower()
        if not device_text or device_text == 'auto':
            current_device = getattr(torch.cuda, 'current_device', None)
            return [current_device() if callable(current_device) else 0]
        if device_text == 'cuda':
            current_device = getattr(torch.cuda, 'current_device', None)
            return [current_device() if callable(current_device) else 0]
        if device_text.startswith('cuda:'):
            try:
                return [int(device_text.split(':', 1)[1])]
            except ValueError:
                return []
        return []

    def _configure_decoding(self, decoding: str) -> None:
        if decoding not in {'tdt', 'ctc'}:
            raise ValueError('Unsupported NeMo ASR decoding mode: %s' % decoding)
        if decoding == 'tdt':
            # Keep the model's default TDT/RNNT-style decoder unless CTC is explicitly requested.
            return

        aux_ctc = getattr(getattr(self.model, 'cfg', None), 'aux_ctc', None)
        ctc_decoding = getattr(aux_ctc, 'decoding', None)
        if not hasattr(self.model, 'change_decoding_strategy') or ctc_decoding is None:
            raise ValueError('The selected NeMo ASR model does not expose a CTC decoder.')
        try:
            self.model.change_decoding_strategy(ctc_decoding, decoder_type='ctc')
        except TypeError:
            self.model.change_decoding_strategy(ctc_decoding)

    def _quiet_nemo_logging(self):
        nemo_logging = getattr(self, '_nemo_logging', None)
        temp_verbosity = getattr(nemo_logging, 'temp_verbosity', None)
        if temp_verbosity is None:
            return contextlib.nullcontext()
        return temp_verbosity(getattr(nemo_logging, 'ERROR', 40))

    @staticmethod
    def _cleanup_cuda_cache(torch) -> None:
        gc.collect()
        if not getattr(torch, 'cuda', None) or not torch.cuda.is_available():
            return
        empty_cache = getattr(torch.cuda, 'empty_cache', None)
        if callable(empty_cache):
            empty_cache()

    @staticmethod
    def _extract_text(result) -> str:
        if result is None:
            return ''
        if isinstance(result, str):
            return result
        if isinstance(result, dict):
            return str(result.get('text') or result.get('pred_text') or '')
        if isinstance(result, tuple):
            return NemoASRTranscriber._extract_text(result[0] if result else None)
        if isinstance(result, list):
            return NemoASRTranscriber._extract_text(result[0] if result else None)
        text = getattr(result, 'text', None)
        if text is not None:
            return str(text)
        pred_text = getattr(result, 'pred_text', None)
        if pred_text is not None:
            return str(pred_text)
        return str(result)

    def transcribe(self, audio: np.array, initial_prompt: str = None) -> tuple[str, list | None]:
        audio = np.asarray(audio, dtype=np.float32)
        temp_path = None
        temp_dir = os.environ.get('TMPDIR') or None
        try:
            with tempfile.NamedTemporaryFile(suffix='.wav', dir=temp_dir, delete=False) as temp_file:
                temp_path = temp_file.name
            write_audio(temp_path, SAMPLE_RATE, audio)
            result = self._transcribe_paths([temp_path])
            return self._extract_text(result), None
        finally:
            if temp_path:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    def _transcribe_paths(self, paths: list[str]):
        kwargs = {
            'batch_size': 1,
            'num_workers': 0,
            'verbose': False,
        }
        if self._transcribe_config_cls is not None:
            kwargs['override_config'] = self._transcribe_config_cls(use_lhotse=False,
                                                                    batch_size=1,
                                                                    num_workers=0,
                                                                    verbose=False)
        with self._quiet_nemo_logging():
            try:
                return self.model.transcribe(paths, **kwargs)
            except TypeError:
                kwargs.pop('override_config', None)
                try:
                    return self.model.transcribe(paths, **kwargs)
                except TypeError:
                    return self.model.transcribe(paths)


def _load_qwen3_asr_model_class():
    import sys
    import types

    # qwen_asr imports the forced aligner eagerly, but plain ASR does not need it.
    # Avoid importing nagisa/dynet here because some older CPUs hit SIGILL in dynet wheels.
    module_name = 'qwen_asr.inference.qwen3_forced_aligner'
    if module_name not in sys.modules:
        forced_aligner_stub = types.ModuleType(module_name)

        class Qwen3ForcedAligner:

            @classmethod
            def from_pretrained(cls, *args, **kwargs):
                raise RuntimeError('Qwen3 forced alignment is not available in this transcription backend.')

        forced_aligner_stub.Qwen3ForcedAligner = Qwen3ForcedAligner
        sys.modules[module_name] = forced_aligner_stub

    from qwen_asr import Qwen3ASRModel

    return Qwen3ASRModel


class _TransformersPadTokenLogFilter(logging.Filter):

    def filter(self, record):
        return 'Setting `pad_token_id` to `eos_token_id`' not in record.getMessage()


def _install_transformers_pad_token_log_filter():
    logger = logging.getLogger('transformers.generation.utils')
    if any(isinstance(filter_, _TransformersPadTokenLogFilter) for filter_ in logger.filters):
        return
    logger.addFilter(_TransformersPadTokenLogFilter())


class Qwen3ASRTranscriber(AudioTranscriber):
    LANGUAGE_NAMES = {
        'ar': 'Arabic',
        'cs': 'Czech',
        'da': 'Danish',
        'de': 'German',
        'el': 'Greek',
        'en': 'English',
        'es': 'Spanish',
        'fa': 'Persian',
        'fi': 'Finnish',
        'fil': 'Filipino',
        'fr': 'French',
        'hi': 'Hindi',
        'hu': 'Hungarian',
        'id': 'Indonesian',
        'it': 'Italian',
        'ja': 'Japanese',
        'ko': 'Korean',
        'mk': 'Macedonian',
        'ms': 'Malay',
        'nl': 'Dutch',
        'pl': 'Polish',
        'pt': 'Portuguese',
        'ro': 'Romanian',
        'ru': 'Russian',
        'sv': 'Swedish',
        'th': 'Thai',
        'tl': 'Filipino',
        'tr': 'Turkish',
        'vi': 'Vietnamese',
        'yue': 'Cantonese',
        'zh': 'Chinese',
        'zh-cn': 'Chinese',
        'zh-hans': 'Chinese',
        'zh-hant': 'Chinese',
        'zh-tw': 'Chinese',
    }
    SUPPORTED_LANGUAGE_NAMES = set(LANGUAGE_NAMES.values())

    def __init__(self, model: str, language: str, proxy: str, dtype: str, device_map: str, max_new_tokens: int,
                 quantization: str, bnb_4bit_quant_type: str, bnb_4bit_use_double_quant: bool, **kwargs) -> None:
        super().__init__(**kwargs)
        try:
            import torch
            Qwen3ASRModel = _load_qwen3_asr_model_class()
        except ImportError as e:
            raise ImportError(
                'Qwen3-ASR support requires the qwen_asr extra. Install it with: '
                'pip install "stream-translator-gpt[qwen_asr]"'
            ) from e

        if proxy:
            _apply_hf_proxy(proxy)

        self._validate_device_map(torch, device_map)

        dtype_obj = torch.bfloat16
        if dtype:
            dtype_obj = getattr(torch, dtype, None)
            if not isinstance(dtype_obj, torch.dtype):
                raise ValueError(f'Unsupported Qwen3-ASR dtype: {dtype}')

        model_kwargs = {
            'dtype': dtype_obj,
            'device_map': device_map,
            'max_new_tokens': max_new_tokens,
        }
        quantization_config = self._build_quantization_config(torch,
                                                              quantization=quantization,
                                                              dtype=dtype_obj,
                                                              bnb_4bit_quant_type=bnb_4bit_quant_type,
                                                              bnb_4bit_use_double_quant=bnb_4bit_use_double_quant)
        if quantization_config is not None:
            model_kwargs['quantization_config'] = quantization_config

        print(f'{INFO}Loading Qwen3-ASR model: {model}')
        self.model = Qwen3ASRModel.from_pretrained(model, **model_kwargs)
        self._set_generation_pad_token_id()
        _install_transformers_pad_token_log_filter()
        self.language = self._normalize_language(language)

    @classmethod
    def _normalize_language(cls, language: str | None) -> str | None:
        if language is None:
            return None
        language = str(language).strip()
        if not language or language.lower() == 'auto':
            return None

        language_key = language.lower().replace('_', '-')
        if language_key in cls.LANGUAGE_NAMES:
            return cls.LANGUAGE_NAMES[language_key]

        language_name = language[:1].upper() + language[1:].lower()
        if language_name in cls.SUPPORTED_LANGUAGE_NAMES:
            return language_name

        supported = ', '.join(sorted(cls.LANGUAGE_NAMES.keys()))
        raise ValueError(
            f'Qwen3-ASR does not support language "{language}". '
            f'Use "auto" or one of these language codes: {supported}.')

    @classmethod
    def _validate_device_map(cls, torch, device_map: str | None) -> None:
        device_map = str(device_map or 'auto').strip() or 'auto'
        if device_map == 'cpu':
            return
        if device_map == 'auto':
            if not torch.cuda.is_available() or cls._cuda_has_supported_device(torch):
                return
            raise RuntimeError(
                'Current PyTorch CUDA build does not support the available GPU(s) for Qwen3-ASR. '
                'Install a PyTorch build that supports your GPU compute capability, or explicitly use '
                '--qwen3_asr_device_map cpu.')
        if device_map.startswith('cuda'):
            if cls._cuda_has_supported_device(torch):
                return
            raise RuntimeError(
                'Current PyTorch CUDA build does not support the available GPU(s) for Qwen3-ASR. '
                'Install a PyTorch build that supports your GPU compute capability, or explicitly use '
                '--qwen3_asr_device_map cpu.')

    @staticmethod
    def _cuda_has_supported_device(torch) -> bool:
        if not torch.cuda.is_available():
            return False
        supported_caps = []
        for arch in torch.cuda.get_arch_list():
            capability = _parse_torch_cuda_arch(arch)
            if capability is not None:
                supported_caps.append(capability)
        if not supported_caps:
            return True
        min_cap = min(supported_caps)
        for index in range(torch.cuda.device_count()):
            if torch.cuda.get_device_capability(index) >= min_cap:
                return True
        return False

    @staticmethod
    def _build_quantization_config(torch, quantization: str, dtype, bnb_4bit_quant_type: str,
                                   bnb_4bit_use_double_quant: bool):
        quantization = (quantization or 'none').strip().lower()
        if quantization == 'none':
            return None
        try:
            from transformers import BitsAndBytesConfig
        except ImportError as e:
            raise ImportError('Qwen3-ASR quantization requires transformers BitsAndBytesConfig support.') from e

        if quantization in {'bnb_8bit', '8bit'}:
            return BitsAndBytesConfig(load_in_8bit=True)
        if quantization in {'bnb_4bit', '4bit'}:
            return BitsAndBytesConfig(load_in_4bit=True,
                                      bnb_4bit_compute_dtype=dtype,
                                      bnb_4bit_quant_type=bnb_4bit_quant_type or 'nf4',
                                      bnb_4bit_use_double_quant=bool(bnb_4bit_use_double_quant))
        raise ValueError('Unsupported Qwen3-ASR quantization mode: %s' % quantization)

    def _set_generation_pad_token_id(self) -> None:
        hf_model = getattr(self.model, 'model', None)
        generation_config = getattr(hf_model, 'generation_config', None)
        model_config = getattr(hf_model, 'config', None)
        pad_token_id = getattr(generation_config, 'pad_token_id', None)
        if pad_token_id is None:
            pad_token_id = getattr(model_config, 'pad_token_id', None)
        if pad_token_id is None:
            pad_token_id = getattr(generation_config, 'eos_token_id', None)
            if pad_token_id is None:
                pad_token_id = getattr(model_config, 'eos_token_id', None)
            if isinstance(pad_token_id, (list, tuple)):
                pad_token_id = pad_token_id[0] if pad_token_id else None
            if pad_token_id is None:
                return

        if generation_config is not None:
            generation_config.pad_token_id = pad_token_id
        if model_config is not None:
            model_config.pad_token_id = pad_token_id
        self._wrap_generate_with_pad_token_id(hf_model, pad_token_id)

    @staticmethod
    def _wrap_generate_with_pad_token_id(hf_model, pad_token_id) -> None:
        if hf_model is None or getattr(hf_model, '_stream_translator_pad_token_wrapped', False):
            return
        original_generate = getattr(hf_model, 'generate', None)
        if original_generate is None:
            return

        def generate_with_pad_token_id(*args, **kwargs):
            kwargs.setdefault('pad_token_id', pad_token_id)
            return original_generate(*args, **kwargs)

        hf_model.generate = generate_with_pad_token_id
        hf_model._stream_translator_pad_token_wrapped = True

    def transcribe(self, audio: np.array, initial_prompt: str = None) -> tuple[str, list | None]:
        results = self.model.transcribe(audio=(audio, SAMPLE_RATE), context=initial_prompt or '', language=self.language)
        result = results[0] if results else None
        if result is None:
            return '', None
        if isinstance(result, dict):
            return result.get('text', ''), None
        return getattr(result, 'text', ''), None
