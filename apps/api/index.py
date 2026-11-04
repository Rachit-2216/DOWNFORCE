"""Root-service ASGI entrypoint using the API package's existing factory."""

import sys
from pathlib import Path

api_root = str(Path(__file__).resolve().parent)
if api_root not in sys.path:
    sys.path.insert(0, api_root)

from app.main import app  # noqa: E402

__all__ = ["app"]
