"""Shared compatibility registry used during the no-behavior-change refactor.

The original bot grew as sequential feature layers. Some later layers intentionally
capture earlier function objects before overriding them. This registry preserves that
exact initialization order while allowing the source to live in focused modules.

New code should prefer explicit imports. The registry is retained only for compatibility
with the existing layered implementation and can be removed gradually.
"""
from __future__ import annotations

from typing import Any, MutableMapping

_REGISTRY: dict[str, Any] = {}
_MODULE_GLOBALS: list[MutableMapping[str, Any]] = []
_SKIP_NAMES = {
    "__name__", "__file__", "__package__", "__loader__", "__spec__",
    "__builtins__", "__cached__", "_seed_globals", "_export_globals",
}


def seed_globals(namespace: MutableMapping[str, Any]) -> None:
    """Seed a module with symbols exported by modules loaded before it."""
    for name, value in _REGISTRY.items():
        if name not in _SKIP_NAMES:
            namespace[name] = value
    if namespace not in _MODULE_GLOBALS:
        _MODULE_GLOBALS.append(namespace)


def export_globals(namespace: MutableMapping[str, Any]) -> None:
    """Publish a module's public and compatibility symbols to the registry."""
    for name, value in namespace.items():
        if name not in _SKIP_NAMES and not name.startswith("__"):
            _REGISTRY[name] = value


def sync_all() -> None:
    """Inject final public symbols without corrupting module-private helpers.

    The compatibility loader intentionally allows public handlers to be layered and
    overridden.  Private helpers (single-leading-underscore names), however, belong
    to the module that defined them.  Overwriting those helpers caused unrelated
    modules with the same private function name to corrupt each other at runtime
    (for example the CAPTCHA and PDF font-discovery helpers).

    Private symbols that a module received during its initial seed remain available;
    they are simply not replaced later when another module reuses the same private
    name.
    """
    for namespace in _MODULE_GLOBALS:
        for name, value in _REGISTRY.items():
            if name in _SKIP_NAMES:
                continue
            if name.startswith("_") and name in namespace:
                continue
            namespace[name] = value


def get(name: str) -> Any:
    return _REGISTRY[name]


def snapshot() -> dict[str, Any]:
    return dict(_REGISTRY)
