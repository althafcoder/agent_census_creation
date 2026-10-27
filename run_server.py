"""Launch script for the Census Comparison Agent Web UI.

Run from anywhere:
    python run_server.py
"""
import copy
import os
import sys
from pathlib import Path

# Force unbuffered output so terminal logs appear instantly
os.environ["PYTHONUNBUFFERED"] = "1"
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass

import uvicorn
from uvicorn.config import LOGGING_CONFIG

if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    backend = root if (root / "api.py").exists() else root / "backend"
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(backend))

    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))

    # Custom log config for uvicorn so census and pipeline logs stream to the terminal
    log_config = copy.deepcopy(LOGGING_CONFIG)
    log_config["formatters"]["census"] = {
        "()": "uvicorn.logging.DefaultFormatter",
        "fmt": "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        "datefmt": "%H:%M:%S",
    }
    log_config["handlers"]["census_console"] = {
        "class": "logging.StreamHandler",
        "formatter": "census",
        "stream": "ext://sys.stdout",
    }
    log_config["loggers"]["census"] = {
        "handlers": ["census_console"],
        "level": "INFO",
        "propagate": False,
    }
    log_config["loggers"]["census.caching"] = {
        "handlers": ["census_console"],
        "level": "INFO",
        "propagate": False,
    }
    log_config["loggers"][""] = {
        "handlers": ["census_console"],
        "level": "INFO",
    }

    print(f"\nStarting Census Agent UI on http://{host}:{port} ...\n")
    uvicorn.run("api:app", host=host, port=port, reload=True, app_dir=str(backend), log_config=log_config)
