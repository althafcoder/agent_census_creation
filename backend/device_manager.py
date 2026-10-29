"""Hardware device detection and acceleration manager.

Probes available compute devices (NVIDIA CUDA, system GPUs, CPU) and provides
robust auto-detection with zero-crash fallback to CPU execution.
"""
from __future__ import annotations

import logging
import os
import platform
import subprocess
import threading
from typing import Any, Dict, Optional

log = logging.getLogger("census.device_manager")


class DeviceManager:
    """Thread-safe hardware device manager for local compute workloads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._probed = False
        
        # Configuration
        self.force_device = os.getenv("FORCE_DEVICE", "auto").strip().lower()
        self.gpu_device_id = int(os.getenv("GPU_DEVICE_ID", "0"))
        self.gpu_mem_limit_mb = int(os.getenv("GPU_MEM_LIMIT_MB", "0"))

        # Detected state
        self.cuda_available: bool = False
        self.cuda_version: Optional[str] = None
        self.gpu_name: Optional[str] = None
        self.gpu_count: int = 0
        self.system_gpu_name: Optional[str] = None
        self.vram_total_mb: Optional[int] = None
        
        # Runtime operational status
        self.active_mode: str = "CPU"
        self.active_engine: str = "PaddleOCR-CPU"
        self.fallback_occurred: bool = False
        self.fallback_reason: Optional[str] = None

    def probe_device(self, force_refresh: bool = False) -> None:
        """Safely probe system hardware and CUDA environment."""
        with self._lock:
            if self._probed and not force_refresh:
                return

            self._probe_system_gpu()
            self._probe_cuda_environment()
            self._determine_initial_mode()
            self._probed = True

    def _probe_system_gpu(self) -> None:
        """Inspect physical display/GPU adapters via WMI (Windows) or lspci (Linux)."""
        try:
            if platform.system() == "Windows":
                cmd = ["powershell", "-NoProfile", "-Command",
                       "(Get-CimInstance Win32_VideoController).Name"]
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                if res.returncode == 0 and res.stdout.strip():
                    names = [line.strip() for line in res.stdout.splitlines() if line.strip()]
                    self.system_gpu_name = ", ".join(names)
            elif platform.system() == "Linux":
                res = subprocess.run(["lspci"], capture_output=True, text=True, timeout=5)
                if res.returncode == 0:
                    lines = [ln for ln in res.stdout.splitlines() if "VGA" in ln or "3D" in ln]
                    if lines:
                        self.system_gpu_name = lines[0].split(":")[-1].strip()
        except Exception as exc:
            log.debug("System hardware adapter query omitted: %s", exc)

    def _probe_cuda_environment(self) -> None:
        """Check CUDA availability via paddle and torch without throwing unhandled exceptions."""
        # 1. Probe PaddlePaddle CUDA
        try:
            import paddle
            if hasattr(paddle, "is_compiled_with_cuda") and paddle.is_compiled_with_cuda():
                cuda_devs = paddle.device.cuda.device_count() if hasattr(paddle.device, "cuda") else 0
                if cuda_devs > 0:
                    self.cuda_available = True
                    self.gpu_count = cuda_devs
                    try:
                        props = paddle.device.cuda.get_device_properties(self.gpu_device_id)
                        self.gpu_name = getattr(props, "name", f"CUDA Device {self.gpu_device_id}")
                        self.vram_total_mb = int(getattr(props, "total_memory", 0) / (1024 * 1024))
                    except Exception:
                        self.gpu_name = f"CUDA Device {self.gpu_device_id}"
                    
                    if hasattr(paddle, "version") and hasattr(paddle.version, "cuda"):
                        self.cuda_version = paddle.version.cuda()
                    return
        except Exception as exc:
            log.debug("Paddle CUDA probe encountered: %s", exc)

        # 2. Probe PyTorch CUDA if paddle-gpu was not compiled
        try:
            import torch
            if torch.cuda.is_available():
                self.cuda_available = True
                self.gpu_count = torch.cuda.device_count()
                self.gpu_name = torch.cuda.get_device_name(self.gpu_device_id)
                self.cuda_version = torch.version.cuda
                try:
                    self.vram_total_mb = int(torch.cuda.get_device_properties(self.gpu_device_id).total_memory / (1024 * 1024))
                except Exception:
                    pass
                return
        except Exception as exc:
            log.debug("PyTorch CUDA probe encountered: %s", exc)

    def _determine_initial_mode(self) -> None:
        """Decide initial compute mode based on probed environment and config overrides."""
        if self.force_device == "cpu":
            self.active_mode = "CPU"
            self.active_engine = "PaddleOCR-CPU"
            log.info("Compute device explicitly configured to CPU (FORCE_DEVICE=cpu)")
            return

        if self.cuda_available and self.force_device in ("auto", "gpu"):
            self.active_mode = "GPU"
            self.active_engine = "PaddleOCR-CUDA"
            log.info("Compute device selected: GPU (%s, CUDA %s, %s MB VRAM)",
                     self.gpu_name, self.cuda_version or "Unknown", self.vram_total_mb or "N/A")
        else:
            self.active_mode = "CPU"
            self.active_engine = "PaddleOCR-CPU"
            if self.force_device == "gpu":
                self.fallback_occurred = True
                self.fallback_reason = "FORCE_DEVICE=gpu requested but no compatible CUDA device/runtime found"
                log.warning("⚠️ %s. Falling back to CPU.", self.fallback_reason)
            else:
                reason = "No CUDA-capable GPU runtime detected"
                if self.system_gpu_name:
                    reason += f" (Detected display adapter: {self.system_gpu_name})"
                log.info("Compute device: CPU (%s)", reason)

    def should_use_gpu(self) -> bool:
        """Return True if GPU acceleration should be attempted."""
        self.probe_device()
        if self.force_device == "cpu":
            return False
        if self.fallback_occurred and self.fallback_reason:
            return False
        return self.cuda_available

    def record_fallback(self, reason: str) -> None:
        """Record that GPU initialization failed and CPU fallback was activated."""
        with self._lock:
            self.fallback_occurred = True
            self.fallback_reason = reason
            self.active_mode = "CPU"
            self.active_engine = "PaddleOCR-CPU"
            log.warning("⚠️ [FALLBACK TO CPU] %s", reason)

    def record_active_mode(self, mode: str, engine: str) -> None:
        """Update active runtime compute mode and engine label."""
        with self._lock:
            self.active_mode = mode
            self.active_engine = engine

    def get_status(self) -> dict[str, Any]:
        """Return structured telemetry for API health check and Web UI."""
        self.probe_device()
        return {
            "active_mode": self.active_mode,
            "active_engine": self.active_engine,
            "cuda_available": self.cuda_available,
            "cuda_version": self.cuda_version,
            "gpu_name": self.gpu_name or self.system_gpu_name,
            "gpu_count": self.gpu_count,
            "vram_total_mb": self.vram_total_mb,
            "fallback_occurred": self.fallback_occurred,
            "fallback_reason": self.fallback_reason,
            "force_device_config": self.force_device,
        }


# Singleton instance
_INSTANCE: DeviceManager | None = None
_INSTANCE_LOCK = threading.Lock()


def get_device_manager() -> DeviceManager:
    """Return the global singleton DeviceManager."""
    global _INSTANCE
    if _INSTANCE is None:
        with _INSTANCE_LOCK:
            if _INSTANCE is None:
                _INSTANCE = DeviceManager()
                _INSTANCE.probe_device()
    return _INSTANCE

