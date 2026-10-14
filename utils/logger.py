"""Application loggers: one console stream and logs/app.log."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

_CONFIGURED = False
_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_DATE = "%Y-%m-%d %H:%M:%S"


def configure_logging(repo_root: Optional[Path] = None) -> logging.Logger:
    """Attach console and file handlers once. Later calls return the same logger."""
    global _CONFIGURED
    logger = logging.getLogger("safeagent")
    if _CONFIGURED:
        return logger
    root = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parent.parent
    log_dir = root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    formatter = logging.Formatter(_FORMAT, datefmt=_DATE)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    file_handler = logging.FileHandler(log_dir / "app.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(stream)
    logger.addHandler(file_handler)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        server_logger = logging.getLogger(name)
        server_logger.setLevel(logging.INFO)
        server_logger.addHandler(file_handler)
    _CONFIGURED = True
    return logger


def get_logger(name: str = "safeagent") -> logging.Logger:
    if not _CONFIGURED:
        configure_logging()
    if not name or name == "safeagent":
        return logging.getLogger("safeagent")
    return logging.getLogger("safeagent." + name)
