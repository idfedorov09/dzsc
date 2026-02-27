from __future__ import annotations

from importlib.resources import files
from importlib.resources.abc import Traversable


_PAYLOADS = {
    "agentation_gradle": "static/gradle/agentation-debug-overlay.gradle",
    "sourcemap_gradle": "static/gradle/z8-debug-sourcemaps.gradle",
    "sourcemap_toggle_py": "static/tools/toggle_z8_debug_sourcemaps.py",
    "sourcemap_config": "static/config/frontend_debug_sourcemap.yml",
}


def _payload_resource(name: str) -> Traversable:
    try:
        rel = _PAYLOADS[name]
    except KeyError as exc:
        raise KeyError(f"unknown payload: {name}") from exc
    resource = files("dzsc").joinpath(rel)
    if not resource.is_file():
        raise SystemExit(f"required payload resource not found: dzsc/{rel}")
    return resource


def payload_bytes(name: str) -> bytes:
    return _payload_resource(name).read_bytes()


def payload_text(name: str) -> str:
    return _payload_resource(name).read_text(encoding="utf-8")
