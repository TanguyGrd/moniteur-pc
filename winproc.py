"""Liste des processus Windows en UN seul appel système (NtQuerySystemInformation).

psutil interroge chaque processus séparément (~1-3 s pour 350 processus) ; ici on lit
tout d'un coup en ~1 ms, avec la même mémoire « privée » que le Gestionnaire des tâches.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

SystemProcessInformation = 5
STATUS_INFO_LENGTH_MISMATCH = 0xC0000004


class UNICODE_STRING(ctypes.Structure):
    _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT),
                ("Buffer", ctypes.c_void_p)]


class SYSTEM_PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("NextEntryOffset", wintypes.ULONG),
        ("NumberOfThreads", wintypes.ULONG),
        ("WorkingSetPrivateSize", ctypes.c_longlong),
        ("HardFaultCount", wintypes.ULONG),
        ("NumberOfThreadsHighWatermark", wintypes.ULONG),
        ("CycleTime", ctypes.c_ulonglong),
        ("CreateTime", ctypes.c_longlong),
        ("UserTime", ctypes.c_longlong),
        ("KernelTime", ctypes.c_longlong),
        ("ImageName", UNICODE_STRING),
        ("BasePriority", ctypes.c_long),
        ("UniqueProcessId", ctypes.c_void_p),
        ("InheritedFromUniqueProcessId", ctypes.c_void_p),
        ("HandleCount", wintypes.ULONG),
        ("SessionId", wintypes.ULONG),
        ("UniqueProcessKey", ctypes.c_void_p),
        ("PeakVirtualSize", ctypes.c_size_t),
        ("VirtualSize", ctypes.c_size_t),
        ("PageFaultCount", wintypes.ULONG),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivatePageCount", ctypes.c_size_t),
    ]


_ntdll = ctypes.WinDLL("ntdll")
_NtQSI = _ntdll.NtQuerySystemInformation
_NtQSI.restype = ctypes.c_ulong  # NTSTATUS lu en non signé pour comparer facilement
_NtQSI.argtypes = [ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]


class ProcessSampler:
    def __init__(self, n_logical: int):
        self.n = max(1, n_logical)
        self._size = 1 << 20
        self._prev: dict[tuple[int, int], int] = {}  # (pid, create_time) -> temps CPU (100 ns)
        self._prev_t: float | None = None

    def _query(self) -> ctypes.Array:
        while True:
            buf = ctypes.create_string_buffer(self._size)
            needed = ctypes.c_ulong(0)
            st = _NtQSI(SystemProcessInformation, buf, self._size, ctypes.byref(needed))
            if st == STATUS_INFO_LENGTH_MISMATCH:
                self._size = max(self._size * 2, needed.value + 65536)
                continue
            if st != 0:
                raise OSError(f"NtQuerySystemInformation a échoué (0x{st:08X})")
            return buf

    def sample(self, now: float) -> list[dict]:
        buf = self._query()
        base = ctypes.addressof(buf)
        off = 0
        rows, cur = [], {}
        dt = (now - self._prev_t) if self._prev_t else None
        while True:
            spi = SYSTEM_PROCESS_INFORMATION.from_address(base + off)
            pid = spi.UniqueProcessId or 0
            if pid:
                if spi.ImageName.Buffer and spi.ImageName.Length:
                    name = ctypes.wstring_at(spi.ImageName.Buffer, spi.ImageName.Length // 2)
                else:
                    name = "?"
                key = (pid, spi.CreateTime)
                cpu_t = spi.UserTime + spi.KernelTime
                cur[key] = cpu_t
                cpu = 0.0
                if dt and key in self._prev:
                    cpu = (cpu_t - self._prev[key]) / (dt * 1e7 * self.n) * 100
                rows.append({"pid": pid, "name": name, "cpu": max(0.0, min(100.0, cpu)),
                             "rss": spi.WorkingSetPrivateSize,      # mémoire « privée » (Gestionnaire des tâches)
                             "ws": spi.WorkingSetSize})
            if not spi.NextEntryOffset:
                break
            off += spi.NextEntryOffset
        self._prev, self._prev_t = cur, now
        rows.sort(key=lambda r: r["cpu"], reverse=True)
        return rows
