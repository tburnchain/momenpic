"""Optional import of the YAP-1000 package (§0 "기존 YAP-1000(TTS·씬·렌더 파이프라인)을 M6 렌더 엔진으로").

The checkout is not a dependency of ACVM: when ``RenderSpec.yap1000_path`` points at it and it imports
cleanly the bridge is enabled, otherwise everything runs on ACVM's native pipeline.
"""

from __future__ import annotations

import importlib
import sys
import threading
from pathlib import Path
from types import ModuleType

_lock = threading.Lock()
_cache: dict[str, ModuleType | None] = {}


class YapModules:
    """Namespace of the YAP modules ACVM uses."""

    def __init__(self, providers: ModuleType, textproc: ModuleType) -> None:
        self.providers = providers
        self.textproc = textproc


def load_yap(path: str) -> YapModules | None:
    """Import ``yap.providers`` and ``yap.textproc`` from the checkout at ``path`` (cached, thread-safe)."""
    root = str(Path(path).expanduser().resolve())
    with _lock:
        if root in _cache:
            mod = _cache[root]
            return _modules() if mod is not None else None
        if not (Path(root) / "yap" / "__init__.py").is_file():
            _cache[root] = None
            return None
        if root not in sys.path:
            sys.path.append(root)
        try:
            pkg = importlib.import_module("yap")
            importlib.import_module("yap.providers")
            importlib.import_module("yap.textproc")
        except Exception:  # any import-time failure only disables the optional bridge
            _cache[root] = None
            return None
        _cache[root] = pkg
        return _modules()


def _modules() -> YapModules:
    return YapModules(sys.modules["yap.providers"], sys.modules["yap.textproc"])
