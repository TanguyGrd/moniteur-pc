"""Collecte des statistiques système (CPU, RAM, GPU NVIDIA, disques, réseau, processus).

Tout est lu localement : psutil, nvidia-smi et (pour la température CPU) WMI via PowerShell.
"""
from __future__ import annotations

import csv
import io
import os
import platform
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field

import psutil

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
IDLE_NAMES = {"system idle process", "idle"}


def _run(cmd: list[str], timeout: float = 5.0) -> str | None:
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            creationflags=CREATE_NO_WINDOW, encoding="utf-8", errors="replace",
        )
        return out.stdout if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _powershell(script: str, timeout: float = 10.0) -> str | None:
    return _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], timeout)


def cpu_model_name() -> str:
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                return str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip()
        except OSError:
            pass
    return platform.processor() or "CPU inconnu"


# --------------------------------------------------------------------------- GPU
def find_nvidia_smi() -> str | None:
    p = shutil.which("nvidia-smi")
    if p:
        return p
    for cand in (r"C:\Windows\System32\nvidia-smi.exe",
                 r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"):
        if os.path.exists(cand):
            return cand
    return None


GPU_FIELDS = ["name", "utilization.gpu", "memory.used", "memory.total", "temperature.gpu",
              "power.draw", "power.limit", "fan.speed", "clocks.sm"]


def _num(s: str) -> float | None:
    s = s.strip()
    try:
        return float(s)
    except ValueError:
        return None  # "[N/A]", "[Not Supported]"...


def read_gpus(smi: str | None) -> list[dict]:
    if not smi:
        return []
    out = _run([smi, f"--query-gpu={','.join(GPU_FIELDS)}", "--format=csv,noheader,nounits"], 4)
    if not out:
        return []
    gpus = []
    for row in csv.reader(io.StringIO(out)):
        if len(row) < len(GPU_FIELDS):
            continue
        d = {"name": row[0].strip()}
        for key, val in zip(GPU_FIELDS[1:], row[1:]):
            d[key] = _num(val)
        gpus.append(d)
    return gpus


# --------------------------------------------------------------- Température CPU
class CpuTempReader(threading.Thread):
    """Lit la température CPU en arrière-plan (lent : PowerShell/WMI).

    Ordre : LibreHardwareMonitor -> OpenHardwareMonitor (s'ils tournent, WMI activé)
    -> zone thermique ACPI (souvent réservée aux administrateurs, parfois imprécise).
    """

    SCRIPTS = [
        ("LibreHardwareMonitor",
         "Get-CimInstance -Namespace root/LibreHardwareMonitor -ClassName Sensor -ErrorAction Stop | "
         "Where-Object { $_.SensorType -eq 'Temperature' -and $_.Identifier -match 'cpu' } | "
         "ForEach-Object { '{0}|{1}' -f $_.Name, $_.Value }"),
        ("OpenHardwareMonitor",
         "Get-CimInstance -Namespace root/OpenHardwareMonitor -ClassName Sensor -ErrorAction Stop | "
         "Where-Object { $_.SensorType -eq 'Temperature' -and $_.Identifier -match 'cpu' } | "
         "ForEach-Object { '{0}|{1}' -f $_.Name, $_.Value }"),
        ("ACPI",
         "Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature -ErrorAction Stop | "
         "ForEach-Object { 'Zone|{0}' -f (($_.CurrentTemperature / 10) - 273.15) }"),
    ]

    def __init__(self, interval: float = 5.0):
        super().__init__(daemon=True)
        self.interval = interval
        self.value: float | None = None
        self.source: str | None = None
        self._stop_evt = threading.Event()

    def stop(self):
        self._stop_evt.set()

    def _try(self) -> tuple[float, str] | None:
        order = self.SCRIPTS
        if self.source:  # source déjà trouvée : on la tente en premier
            order = sorted(order, key=lambda s: s[0] != self.source)
        for name, script in order:
            out = _powershell(script, timeout=8)
            if not out:
                continue
            vals: list[tuple[str, float]] = []
            for line in out.splitlines():
                if "|" in line:
                    label, v = line.rsplit("|", 1)
                    n = _num(v.replace(",", "."))
                    if n is not None and 0 < n < 130:
                        vals.append((label, n))
            if not vals:
                continue
            # Préférer "Package"/"Tctl" (température globale), sinon le max des cœurs.
            pref = [v for lbl, v in vals if any(k in lbl.lower() for k in ("package", "tctl", "tdie"))]
            return (max(pref) if pref else max(v for _, v in vals)), name
        return None

    def run(self):
        while not self._stop_evt.is_set():
            res = self._try()
            if res:
                self.value, self.source = res
                wait = self.interval
            else:
                self.value, self.source = None, None
                wait = 60  # rien de dispo : on réessaie rarement
            self._stop_evt.wait(wait)


# ------------------------------------------------------------------ Disques
def map_letters_to_physical() -> dict[str, str]:
    """{'C': 'PhysicalDrive0', 'D': 'PhysicalDrive1'} via PowerShell (une seule fois)."""
    out = _powershell("Get-Partition | Where-Object DriveLetter | "
                      "ForEach-Object { '{0}|{1}' -f $_.DriveLetter, $_.DiskNumber }")
    mapping = {}
    for line in (out or "").splitlines():
        if "|" in line:
            letter, num = line.strip().split("|", 1)
            if letter and num.strip().isdigit():
                mapping[letter.strip().upper()] = f"PhysicalDrive{num.strip()}"
    return mapping


def _swap() -> tuple[int, int]:
    """Mémoire validée (« Validée » dans le Gestionnaire des tâches) : utilisée / limite (RAM + fichier d'échange)."""
    if os.name == "nt":
        import ctypes

        class MS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        m = MS()
        m.dwLength = ctypes.sizeof(MS)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            return m.ullTotalPageFile - m.ullAvailPageFile, m.ullTotalPageFile
    sw = psutil.swap_memory()
    return sw.used, sw.total


# ------------------------------------------------------------------ Snapshot
@dataclass
class Snapshot:
    ts: float
    cpu_total: float
    cpu_per_core: list[float]
    cpu_freq_mhz: float | None
    cpu_temp: float | None
    cpu_temp_source: str | None
    ram_used: int
    ram_total: int
    ram_percent: float
    swap_used: int
    swap_total: int
    gpus: list[dict]
    disks: dict[str, dict]
    net_down_bps: float
    net_up_bps: float
    procs: list[dict] = field(default_factory=list)  # triés par CPU desc
    uptime_s: float = 0.0


class Collector:
    def __init__(self, drives: list[str], history_seconds: int = 300):
        self.drives = [d.upper().rstrip(":\\") for d in drives]
        self.cpu_name = cpu_model_name()
        self.n_logical = psutil.cpu_count(logical=True) or 1
        self.n_physical = psutil.cpu_count(logical=False) or self.n_logical
        self.smi = find_nvidia_smi()
        self.temp_reader = CpuTempReader()
        self.temp_reader.start()
        self.letter_map: dict[str, str] = {}
        threading.Thread(target=self._load_letter_map, daemon=True).start()

        self._proc_cache: dict[int, psutil.Process] = {}
        self._fast_procs = None
        if os.name == "nt":
            try:
                from winproc import ProcessSampler
                self._fast_procs = ProcessSampler(self.n_logical)
                self._fast_procs.sample(time.monotonic())  # amorçage
            except Exception:
                self._fast_procs = None
        self._last_disk = None
        self._last_net = None
        self._last_t = None
        self.history: deque[Snapshot] = deque(maxlen=history_seconds)
        psutil.cpu_percent(percpu=True)  # amorçage

    def _load_letter_map(self):
        self.letter_map = map_letters_to_physical()

    # ---- processus
    def _processes(self) -> list[dict]:
        if self._fast_procs is not None:
            try:
                return self._fast_procs.sample(time.monotonic())
            except Exception:
                self._fast_procs = None  # repli sur psutil (plus lent)
        seen = set()
        rows = []
        for p in psutil.process_iter(["pid", "name", "memory_info"]):
            pid = p.info["pid"]
            seen.add(pid)
            proc = self._proc_cache.get(pid)
            if proc is None:
                self._proc_cache[pid] = p
                try:
                    p.cpu_percent(None)  # amorçage : la valeur arrive au prochain tour
                except psutil.Error:
                    pass
                continue
            name = p.info["name"] or "?"
            if name.lower() in IDLE_NAMES or pid == 0:
                continue
            try:
                cpu = proc.cpu_percent(None) / self.n_logical  # % du CPU total (comme le Gestionnaire des tâches)
            except psutil.Error:
                continue
            mem = p.info["memory_info"]
            rows.append({"pid": pid, "name": name, "cpu": cpu,
                         "rss": mem.rss if mem else 0})
        for pid in list(self._proc_cache):
            if pid not in seen:
                del self._proc_cache[pid]
        rows.sort(key=lambda r: r["cpu"], reverse=True)
        return rows

    # ---- échantillon complet
    def sample(self) -> Snapshot:
        now = time.monotonic()
        per_core = psutil.cpu_percent(percpu=True)
        cpu_total = sum(per_core) / len(per_core) if per_core else 0.0
        try:
            f = psutil.cpu_freq()
            freq = f.current if f else None
        except Exception:
            freq = None
        vm = psutil.virtual_memory()
        try:
            sw_used, sw_total = _swap()
        except Exception:  # compteurs de performance Windows parfois désactivés
            sw_used, sw_total = 0, 0

        # disques : espace + activité
        try:
            io_now = psutil.disk_io_counters(perdisk=True)
        except Exception:
            io_now = {}
        dt = (now - self._last_t) if self._last_t else None
        disks = {}
        for letter in self.drives:
            root = f"{letter}:\\"
            d: dict = {"present": False}
            try:
                u = psutil.disk_usage(root)
                d.update(present=True, total=u.total, used=u.used, free=u.free, percent=u.percent)
            except OSError:
                pass
            phys = self.letter_map.get(letter)
            d["physical"] = phys
            if dt and phys and self._last_disk and phys in io_now and phys in self._last_disk:
                a, b = self._last_disk[phys], io_now[phys]
                d["read_bps"] = max(0, b.read_bytes - a.read_bytes) / dt
                d["write_bps"] = max(0, b.write_bytes - a.write_bytes) / dt
                busy_ms = (b.read_time - a.read_time) + (b.write_time - a.write_time)
                d["busy"] = max(0.0, min(100.0, busy_ms / (dt * 1000) * 100))
            disks[letter] = d
        self._last_disk = io_now

        # réseau
        net = psutil.net_io_counters()
        down = up = 0.0
        if dt and self._last_net:
            down = max(0, net.bytes_recv - self._last_net.bytes_recv) / dt
            up = max(0, net.bytes_sent - self._last_net.bytes_sent) / dt
        self._last_net = net
        self._last_t = now

        snap = Snapshot(
            ts=time.time(), cpu_total=cpu_total, cpu_per_core=per_core, cpu_freq_mhz=freq,
            cpu_temp=self.temp_reader.value, cpu_temp_source=self.temp_reader.source,
            ram_used=vm.total - vm.available, ram_total=vm.total, ram_percent=vm.percent,
            swap_used=sw_used, swap_total=sw_total,
            gpus=read_gpus(self.smi), disks=disks, net_down_bps=down, net_up_bps=up,
            procs=self._processes(), uptime_s=time.time() - psutil.boot_time(),
        )
        self.history.append(snap)
        return snap


# ------------------------------------------------------------------ Formatage
def fmt_bytes(n: float, digits: int = 1) -> str:
    n = float(n)
    for unit in ("o", "Ko", "Mo", "Go", "To"):
        if abs(n) < 1024 or unit == "To":
            return f"{n:.{digits}f} {unit}" if unit != "o" else f"{n:.0f} o"
        n /= 1024
    return f"{n:.1f} To"


def fmt_rate(bps: float) -> str:
    return fmt_bytes(bps) + "/s"


def fmt_duration(s: float) -> str:
    s = int(s)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, _ = divmod(s, 60)
    return (f"{d} j " if d else "") + f"{h} h {m:02d} min"
