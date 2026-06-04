#!/usr/bin/env bash

# Source this file before running uv or the app to keep caches, models,
# config, and temporary files inside the project directory.

if [ -z "${PROJECT_ROOT:-}" ]; then
    PROJECT_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
fi

export VIRTUAL_ENV="${PROJECT_ROOT}/.venv"
export UV_PROJECT_ENVIRONMENT="${VIRTUAL_ENV}"
export UV_PYTHON_INSTALL_DIR="${PROJECT_ROOT}/.local/share/uv/python"
export PROJECT_LOCAL_BIN="${PROJECT_ROOT}/.local/bin"

case ":${PATH}:" in
    *":${VIRTUAL_ENV}/bin:"*) ;;
    *) export PATH="${VIRTUAL_ENV}/bin:${PATH}" ;;
esac

case ":${PATH}:" in
    *":${PROJECT_LOCAL_BIN}:"*) ;;
    *) export PATH="${PROJECT_LOCAL_BIN}:${PATH}" ;;
esac

export XDG_CACHE_HOME="${PROJECT_ROOT}/.cache"
export XDG_CONFIG_HOME="${PROJECT_ROOT}/.config"
export XDG_DATA_HOME="${PROJECT_ROOT}/.local/share"

export UV_CACHE_DIR="${PROJECT_ROOT}/.cache/uv"
export PIP_CACHE_DIR="${PROJECT_ROOT}/.cache/pip"

export TMPDIR="${PROJECT_ROOT}/.tmp"
export TEMP="${PROJECT_ROOT}/.tmp"
export TMP="${PROJECT_ROOT}/.tmp"

export HF_HOME="${PROJECT_ROOT}/.cache/huggingface"
export HF_HUB_CACHE="${PROJECT_ROOT}/.cache/huggingface/hub"
export HUGGINGFACE_HUB_CACHE="${PROJECT_ROOT}/.cache/huggingface/hub"
export HF_DATASETS_CACHE="${PROJECT_ROOT}/.cache/huggingface/datasets"

export TORCH_HOME="${PROJECT_ROOT}/.cache/torch"
export TORCH_EXTENSIONS_DIR="${PROJECT_ROOT}/.cache/torch/extensions"
export WHISPER_CACHE_DIR="${PROJECT_ROOT}/.cache/whisper"
export TRITON_CACHE_DIR="${PROJECT_ROOT}/.cache/triton"
export NUMBA_CACHE_DIR="${PROJECT_ROOT}/.cache/numba"
export CUDA_CACHE_PATH="${PROJECT_ROOT}/.cache/nv/ComputeCache"

export GRADIO_TEMP_DIR="${PROJECT_ROOT}/.tmp/gradio"
export MPLCONFIGDIR="${PROJECT_ROOT}/.config/matplotlib"
export IPYTHONDIR="${PROJECT_ROOT}/.config/ipython"
export JUPYTER_CONFIG_DIR="${PROJECT_ROOT}/.config/jupyter"
export JUPYTER_DATA_DIR="${PROJECT_ROOT}/.local/share/jupyter"
export JUPYTER_RUNTIME_DIR="${PROJECT_ROOT}/.tmp/jupyter"
export PYTHONPYCACHEPREFIX="${PROJECT_ROOT}/.cache/pycache"

mkdir -p \
    "${UV_CACHE_DIR}" \
    "${PIP_CACHE_DIR}" \
    "${TMPDIR}" \
    "${GRADIO_TEMP_DIR}" \
    "${HF_HOME}" \
    "${HF_HUB_CACHE}" \
    "${HF_DATASETS_CACHE}" \
    "${TORCH_HOME}" \
    "${TORCH_EXTENSIONS_DIR}" \
    "${WHISPER_CACHE_DIR}" \
    "${TRITON_CACHE_DIR}" \
    "${NUMBA_CACHE_DIR}" \
    "${CUDA_CACHE_PATH}" \
    "${MPLCONFIGDIR}" \
    "${IPYTHONDIR}" \
    "${JUPYTER_CONFIG_DIR}" \
    "${JUPYTER_DATA_DIR}" \
    "${JUPYTER_RUNTIME_DIR}" \
    "${XDG_CONFIG_HOME}" \
    "${XDG_DATA_HOME}" \
    "${PROJECT_LOCAL_BIN}" \
    "${UV_PYTHON_INSTALL_DIR}" \
    "${PYTHONPYCACHEPREFIX}"
