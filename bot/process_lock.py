"""قفل تشغيل محلي يمنع تشغيل نسختين من الخدمة نفسها من مجلد المشروع."""
from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO


class ProcessLockError(RuntimeError):
    pass


class ProcessLock:
    def __init__(self, service_name: str, root: Path) -> None:
        self.service_name = str(service_name).strip().lower().replace(" ", "-")
        runtime_dir = Path(root).resolve() / ".runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        self.path = runtime_dir / f"{self.service_name}.lock"
        self._handle: BinaryIO | None = None

    def acquire(self) -> "ProcessLock":
        handle = open(self.path, "a+b")
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                handle.close()
                raise ProcessLockError(
                    f"الخدمة {self.service_name} تعمل مسبقًا من هذا المشروع. أوقف النسخة القديمة أولًا."
                ) from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                handle.close()
                raise ProcessLockError(
                    f"الخدمة {self.service_name} تعمل مسبقًا من هذا المشروع. أوقف النسخة القديمة أولًا."
                ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}\n".encode("utf-8"))
        handle.flush()
        self._handle = handle
        return self

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._handle = None

    def __enter__(self) -> "ProcessLock":
        return self.acquire()

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.release()


def acquire_process_lock(service_name: str, root: Path) -> ProcessLock:
    return ProcessLock(service_name, root).acquire()
