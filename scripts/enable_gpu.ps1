<#
.SYNOPSIS
    Enable NVIDIA GPU acceleration for Census Comparison Agent.
.DESCRIPTION
    Checks for NVIDIA drivers, detects CUDA version, and installs
    the matching paddlepaddle-gpu wheel into the active environment.
#>

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "   Census Comparison Agent - GPU Acceleration Setup         " -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

# 1. Check for NVIDIA GPU
$nvidiaSmi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if (-not $nvidiaSmi) {
    # Check default install path
    $defaultPath = "C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"
    if (Test-Path $defaultPath) {
        $nvidiaSmi = $defaultPath
    }
}

if (-not $nvidiaSmi) {
    Write-Host "`n[INFO] No NVIDIA GPU or driver detected on this system." -ForegroundColor Yellow
    Write-Host "The pipeline will continue running using multi-threaded CPU execution with zero issues." -ForegroundColor Green
    Exit 0
}

Write-Host "`n[1/3] Querying NVIDIA Driver & CUDA support..." -ForegroundColor Green
& $nvidiaSmi

# 2. Detect CUDA version string
$smiOut = & $nvidiaSmi
$cudaMatch = [regex]::Match($smiOut, "CUDA Version:\s*([0-9]+\.[0-9]+)")
$cudaVer = if ($cudaMatch.Success) { $cudaMatch.Groups[1].Value } else { "12.0" }
Write-Host "`n[2/3] Detected maximum supported CUDA Version: $cudaVer" -ForegroundColor Green

# 3. Determine wheel
if ($cudaVer -match "^11\.") {
    $wheel = "paddlepaddle-gpu==2.6.2.post118"
    Write-Host "Targeting CUDA 11.8 package: $wheel" -ForegroundColor Cyan
} else {
    $wheel = "paddlepaddle-gpu==2.6.2.post120"
    Write-Host "Targeting CUDA 12.x package: $wheel" -ForegroundColor Cyan
}

Write-Host "`n[3/3] Installing $wheel..." -ForegroundColor Green
pip install $wheel -f https://www.paddlepaddle.org.cn/whl/windows/mkl/avx/stable.html

Write-Host "`n============================================================" -ForegroundColor Cyan
Write-Host "Verification:" -ForegroundColor Cyan
python -c "import paddle; print('Paddle version:', paddle.__version__, '| Compiled with CUDA:', paddle.is_compiled_with_cuda(), '| GPU count:', paddle.device.cuda.device_count() if hasattr(paddle.device, 'cuda') else 0)"
Write-Host "============================================================" -ForegroundColor Cyan

