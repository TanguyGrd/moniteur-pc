"""Règles d'alerte : seuils, durée minimale, anti-répétition."""
from __future__ import annotations

import time
from dataclasses import dataclass

from collector import Snapshot, fmt_bytes

GB = 1024 ** 3


@dataclass
class Alert:
    key: str
    level: str      # "warn" ou "crit"
    message: str
    ts: float


class AlertEngine:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.active: dict[str, Alert] = {}       # conditions vraies en ce moment
        self._since: dict[str, float] = {}       # début d'une condition « soutenue »
        self._last_fired: dict[str, float] = {}

    def _sustained(self, key: str, cond: bool, seconds: float, now: float) -> bool:
        if not cond:
            self._since.pop(key, None)
            return False
        start = self._since.setdefault(key, now)
        return now - start >= seconds

    def evaluate(self, s: Snapshot) -> list[Alert]:
        """Renvoie les NOUVELLES alertes à notifier ; met à jour self.active."""
        c = self.cfg
        now = s.ts
        found: dict[str, tuple[str, str]] = {}

        # Disques
        thresholds = c.get("alert_disk_free_gb", {})
        for letter, d in s.disks.items():
            lim = thresholds.get(letter)
            if lim is None or not d.get("present"):
                continue
            free_gb = d["free"] / GB
            if free_gb < lim:
                lvl = "crit" if free_gb < lim / 2 else "warn"
                found[f"disk_{letter}"] = (lvl, f"Disque {letter}: presque plein — {free_gb:.1f} Go libres (seuil {lim} Go)")

        # RAM
        if s.ram_percent >= c["alert_ram_percent"]:
            lvl = "crit" if s.ram_percent >= 97 else "warn"
            found["ram"] = (lvl, f"RAM saturée — {s.ram_percent:.0f} % utilisés "
                                 f"({fmt_bytes(s.ram_used)} / {fmt_bytes(s.ram_total)})")

        # CPU soutenu
        if self._sustained("cpu", s.cpu_total >= c["alert_cpu_percent"], c["alert_cpu_sustained_s"], now):
            found["cpu"] = ("warn", f"CPU à {s.cpu_total:.0f} % depuis plus de {c['alert_cpu_sustained_s']} s")

        # Température CPU
        if s.cpu_temp is not None and s.cpu_temp >= c["alert_cpu_temp_c"]:
            found["cpu_temp"] = ("crit", f"CPU qui chauffe — {s.cpu_temp:.0f} °C")

        # GPU
        for i, g in enumerate(s.gpus):
            t = g.get("temperature.gpu")
            if t is not None and t >= c["alert_gpu_temp_c"]:
                lvl = "crit" if t >= c["alert_gpu_temp_c"] + 7 else "warn"
                found[f"gpu_temp_{i}"] = (lvl, f"GPU qui chauffe — {t:.0f} °C ({g['name']})")
            used, total = g.get("memory.used"), g.get("memory.total")
            if used is not None and total:
                pct = used / total * 100
                if pct >= c["alert_vram_percent"]:
                    found[f"vram_{i}"] = ("warn", f"VRAM presque pleine — {pct:.0f} % ({used:.0f}/{total:.0f} Mo)")

        # Processus qui s'emballent
        live = set()
        for p in s.procs:
            k = f"proc_cpu_{p['pid']}"
            live.add(k)
            if self._sustained(k, p["cpu"] >= c["alert_process_cpu_percent"],
                               c["alert_process_sustained_s"], now):
                found[k] = ("warn", f"Processus qui s'emballe — {p['name']} (PID {p['pid']}) "
                                    f"à {p['cpu']:.0f} % du CPU depuis plus de {c['alert_process_sustained_s']} s")
            if p["rss"] >= c["alert_process_ram_gb"] * GB:
                found[f"proc_ram_{p['pid']}"] = ("warn", f"{p['name']} (PID {p['pid']}) utilise "
                                                         f"{fmt_bytes(p['rss'])} de RAM")
        for k in [k for k in self._since if k.startswith("proc_cpu_") and k not in live]:
            del self._since[k]

        new_alerts = []
        cooldown = c["alert_cooldown_s"]
        for key, (lvl, msg) in found.items():
            was_active = key in self.active
            alert = Alert(key, lvl, msg, now)
            self.active[key] = alert
            if not was_active and now - self._last_fired.get(key, 0) >= cooldown:
                self._last_fired[key] = now
                new_alerts.append(alert)
        for key in [k for k in self.active if k not in found]:
            del self.active[key]
        return new_alerts

    def status(self) -> tuple[str, str]:
        """(niveau global, résumé court) pour la pastille d'état."""
        if not self.active:
            return "ok", "Tout va bien"
        crit = [a for a in self.active.values() if a.level == "crit"]
        lvl = "crit" if crit else "warn"
        n = len(self.active)
        return lvl, f"{n} point{'s' if n > 1 else ''} d'attention"
