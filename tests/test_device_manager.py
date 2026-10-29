"""Unit and integration tests for GPU acceleration auto-detection and resilient CPU fallback."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure backend is on sys.path
root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root / "backend"))

import readers
from device_manager import DeviceManager, get_device_manager


def test_device_manager_initial_state():
    """Verify singleton DeviceManager probes system cleanly and produces structured telemetry."""
    dm = get_device_manager()
    status = dm.get_status()

    assert "active_mode" in status
    assert status["active_mode"] in ("CPU", "GPU")
    assert "active_engine" in status
    assert "cuda_available" in status
    assert "fallback_occurred" in status
    assert isinstance(status["fallback_occurred"], bool)


def test_force_device_cpu():
    """Verify FORCE_DEVICE=cpu forces CPU execution even if hardware/CUDA is detected."""
    with patch.dict(os.environ, {"FORCE_DEVICE": "cpu"}):
        dm = DeviceManager()
        dm.cuda_available = True
        dm.gpu_name = "NVIDIA GeForce RTX 4090"
        dm._determine_initial_mode()

        assert dm.should_use_gpu() is False
        assert dm.active_mode == "CPU"
        assert dm.active_engine == "PaddleOCR-CPU"


def test_force_device_gpu_fallback_when_no_cuda():
    """Verify FORCE_DEVICE=gpu with no CUDA flags fallback and runs on CPU safely."""
    with patch.dict(os.environ, {"FORCE_DEVICE": "gpu"}):
        dm = DeviceManager()
        dm.cuda_available = False
        dm._determine_initial_mode()

        assert dm.should_use_gpu() is False
        assert dm.active_mode == "CPU"
        assert dm.fallback_occurred is True
        assert "FORCE_DEVICE=gpu requested" in (dm.fallback_reason or "")


def test_gpu_auto_selection_when_cuda_present():
    """Verify auto mode selects GPU when CUDA runtime is probed successfully."""
    with patch.dict(os.environ, {"FORCE_DEVICE": "auto"}):
        dm = DeviceManager()
        dm.cuda_available = True
        dm.gpu_name = "NVIDIA RTX A5000"
        dm.cuda_version = "12.2"
        dm.vram_total_mb = 24576
        dm._determine_initial_mode()

        assert dm.should_use_gpu() is True
        assert dm.active_mode == "GPU"
        assert dm.active_engine == "PaddleOCR-CUDA"
        assert dm.fallback_occurred is False


def test_record_fallback_updates_state():
    """Verify record_fallback transitions the device manager to CPU mode permanently."""
    dm = DeviceManager()
    dm.cuda_available = True
    dm.active_mode = "GPU"
    dm.active_engine = "PaddleOCR-CUDA"

    dm.record_fallback("Simulated CUDA driver error: initialization failed")

    assert dm.fallback_occurred is True
    assert "Simulated CUDA driver error" in dm.fallback_reason
    assert dm.active_mode == "CPU"
    assert dm.active_engine == "PaddleOCR-CPU"
    assert dm.should_use_gpu() is False


def test_paddle_ocr_gpu_init_failure_fallback_to_cpu():
    """Verify that a failure during GPU PaddleOCR init or smoke test falls back to CPU cleanly."""
    fake_dm = DeviceManager()
    fake_dm.cuda_available = True
    fake_dm.active_mode = "GPU"
    fake_dm.force_device = "gpu"

    readers._paddle_ocr_engine = None

    with patch("device_manager.get_device_manager", return_value=fake_dm):
        with patch.dict("sys.modules", {"paddle": MagicMock(), "paddleocr": MagicMock()}):
            with patch("paddle.set_device"):
                def mock_paddle_init(*args, **kwargs):
                    if kwargs.get("use_gpu") is True:
                        raise RuntimeError("CUDA driver version is insufficient for CUDA runtime")
                    mock_cpu_engine = MagicMock()
                    mock_cpu_engine.ocr.return_value = []
                    return mock_cpu_engine

                with patch("paddleocr.PaddleOCR", side_effect=mock_paddle_init):
                    engine = readers._get_paddle_ocr()
                    assert engine is not None
                    assert fake_dm.fallback_occurred is True
                    assert "CUDA driver version is insufficient" in fake_dm.fallback_reason
                    assert fake_dm.active_mode == "CPU"


def test_paddle_ocr_cuda_oom_page_recovery():
    """Verify that a CUDA out-of-memory error during page OCR triggers CPU fallback and completes."""
    fake_dm = DeviceManager()
    fake_dm.cuda_available = True
    fake_dm.active_mode = "GPU"

    call_count = 0

    mock_gpu_engine = MagicMock()
    def mock_gpu_ocr(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        raise RuntimeError("CUDA out of memory. Tried to allocate 2.00 GiB")
    mock_gpu_engine.ocr.side_effect = mock_gpu_ocr

    mock_cpu_engine = MagicMock()
    mock_cpu_engine.ocr.return_value = [[
        [[[0, 0], [10, 0], [10, 10], [0, 10]], ("Sample Text", 0.99)]
    ]]

    from PIL import Image
    test_page = Image.new("RGB", (100, 100), color="white")

    with patch("device_manager.get_device_manager", return_value=fake_dm), \
         patch("readers._get_paddle_ocr") as mock_get_ocr, \
         patch("pdf2image.convert_from_path", return_value=[test_page]):

        # First return GPU engine, then after OOM fallback return CPU engine
        mock_get_ocr.side_effect = [mock_gpu_engine, mock_cpu_engine]

        result_text = readers._ocr_pdf(Path("dummy_scan.pdf"))
        assert "Sample Text" in result_text
        assert fake_dm.fallback_occurred is True
        assert "CUDA out of memory" in (fake_dm.fallback_reason or "")


def test_api_device_info_and_health_endpoints():
    """Verify /api/device-info and /api/health report compute device telemetry."""
    from fastapi.testclient import TestClient
    from api import app

    client = TestClient(app)

    # 1. Test /api/device-info
    res_info = client.get("/api/device-info")
    assert res_info.status_code == 200
    data_info = res_info.json()
    assert "active_mode" in data_info
    assert "active_engine" in data_info
    assert "cuda_available" in data_info
    assert "fallback_occurred" in data_info

    # 2. Test /api/health
    res_health = client.get("/api/health")
    assert res_health.status_code == 200
    data_health = res_health.json()
    assert "device" in data_health
    dev = data_health["device"]
    assert "mode" in dev
    assert "engine" in dev
    assert "name" in dev
    assert "fallback" in dev
