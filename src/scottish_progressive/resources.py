from __future__ import annotations

from dataclasses import asdict, dataclass
import ctypes
import os
import sys
from typing import Any


MIB = 1024 * 1024


def _windows_cpu_count() -> int | None:
    if sys.platform != "win32":
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        all_groups = 0xFFFF
        active = int(kernel32.GetActiveProcessorCount(all_groups))
        process_mask = ctypes.c_size_t()
        system_mask = ctypes.c_size_t()
        affinity = active
        if kernel32.GetProcessAffinityMask(
            kernel32.GetCurrentProcess(),
            ctypes.byref(process_mask),
            ctypes.byref(system_mask),
        ):
            affinity = process_mask.value.bit_count()
        return max(1, min(active, affinity))
    except (AttributeError, OSError, ValueError):
        return None


def detected_logical_cpus() -> int:
    try:
        affinity = len(os.sched_getaffinity(0))  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        affinity = None
    windows = _windows_cpu_count()
    detected = windows or affinity or os.cpu_count() or 1
    return max(1, int(detected))


def _windows_available_memory() -> int | None:
    if sys.platform != "win32":
        return None

    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    try:
        status = MemoryStatusEx()
        status.dwLength = ctypes.sizeof(status)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return int(status.ullAvailPhys)
    except (AttributeError, OSError, ValueError):
        return None


def detected_available_memory() -> int:
    windows = _windows_available_memory()
    if windows is not None:
        return windows
    try:
        pages = int(os.sysconf("SC_AVPHYS_PAGES"))
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        if pages > 0 and page_size > 0:
            return pages * page_size
    except (AttributeError, OSError, TypeError, ValueError):
        pass
    # Conservative fallback when the platform exposes no reliable query.
    return 1024 * MIB


@dataclass(frozen=True, slots=True)
class ResourceBudget:
    detected_logical_cpus: int
    available_memory_bytes: int
    memory_per_worker_bytes: int
    reserved_memory_bytes: int
    cpu_worker_cap: int
    memory_worker_cap: int
    worker_cap: int
    requested_workers: int | None
    workers: int

    def as_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "envelope_kind": "detected-estimated",
            "cpu_worker_limit_enforced": True,
            "ram_limit_enforced": False,
            "ram_note": (
                "memory_per_worker is a conservative planning estimate, "
                "not an operating-system process memory limit"
            ),
        }


def detect_resource_budget(
    requested_workers: int | None = None,
    *,
    memory_per_worker_mb: int = 512,
    reserve_memory_mb: int = 512,
) -> ResourceBudget:
    """Returns a detected CPU limit and estimated RAM planning envelope.

    The estimate is intentionally recorded with every league run.  A caller
    can request fewer workers, but requesting more is clamped rather than
    spawning beyond the detected CPU count or estimated memory envelope.  RAM
    is not hard-enforced by the operating system and is reported accordingly.
    """

    if requested_workers is not None and requested_workers < 1:
        raise ValueError("requested_workers must be positive")
    if memory_per_worker_mb < 64:
        raise ValueError("memory_per_worker_mb must be at least 64")
    if reserve_memory_mb < 0:
        raise ValueError("reserve_memory_mb cannot be negative")

    cpus = detected_logical_cpus()
    available = detected_available_memory()
    per_worker = memory_per_worker_mb * MIB
    reserve = reserve_memory_mb * MIB
    usable = max(0, available - reserve)
    memory_cap = usable // per_worker
    if memory_cap < 1:
        raise ValueError(
            "insufficient detected available memory for one worker after reserve"
        )
    cap = min(cpus, memory_cap)
    workers = cap if requested_workers is None else min(requested_workers, cap)
    return ResourceBudget(
        detected_logical_cpus=cpus,
        available_memory_bytes=available,
        memory_per_worker_bytes=per_worker,
        reserved_memory_bytes=reserve,
        cpu_worker_cap=cpus,
        memory_worker_cap=memory_cap,
        worker_cap=cap,
        requested_workers=requested_workers,
        workers=workers,
    )
