"""Moniteur PC — widget de surveillance système avec alertes et chat Claude."""
from __future__ import annotations

import ctypes
import os
import queue
import sys
import threading
import time
import tkinter as tk
from collections import deque

from alerts import AlertEngine
from chat import ClaudeChat, build_snapshot_text
from collector import Collector, fmt_bytes, fmt_rate
from config import api_key, load_config

# ------------------------------------------------------------------ Thème
BG = "#14161b"
PANEL = "#1c1f26"
TRACK = "#2a2e38"
FG = "#e8e9ec"
MUTED = "#8c92a0"
ACCENT = "#4fa3ff"
OK = "#3ecf8e"
WARN = "#f5b942"
CRIT = "#ff5c5c"
FONT = ("Segoe UI", 9)
FONT_B = ("Segoe UI Semibold", 9)
FONT_T = ("Segoe UI Semibold", 10)
MONO = ("Consolas", 9)
W_STATS = 390


def load_color(frac: float) -> str:
    return OK if frac < 0.6 else WARN if frac < 0.85 else CRIT


def temp_color(t: float | None, warn: float, crit: float) -> str:
    if t is None:
        return MUTED
    return OK if t < warn else WARN if t < crit else CRIT


# ------------------------------------------------------------------ Widgets
class Bar(tk.Canvas):
    def __init__(self, master, height=18, **kw):
        super().__init__(master, height=height, width=340, bg=TRACK, highlightthickness=0, **kw)
        self.h = height
        self._rect = self.create_rectangle(0, 0, 0, height, fill=OK, width=0)
        self._txt = self.create_text(6, height // 2, anchor="w", fill=FG, font=FONT)
        self._rtxt = self.create_text(0, height // 2, anchor="e", fill=FG, font=FONT)
        self.bind("<Configure>", lambda e: self.coords(self._rtxt, e.width - 6, self.h // 2))

    def set(self, frac: float, text: str, right: str = "", color: str | None = None):
        frac = max(0.0, min(1.0, frac))
        w = self.winfo_width()
        self.coords(self._rect, 0, 0, w * frac, self.h)
        self.itemconfigure(self._rect, fill=color or load_color(frac))
        self.itemconfigure(self._txt, text=text)
        self.itemconfigure(self._rtxt, text=right)


class Spark(tk.Canvas):
    """Mini-graphe des dernières valeurs (0-100)."""

    def __init__(self, master, n=90, height=26, color=ACCENT, **kw):
        super().__init__(master, height=height, width=340, bg=PANEL, highlightthickness=0, **kw)
        self.vals: deque = deque(maxlen=n)
        self.n, self.h, self.color = n, height, color
        self._line = self.create_line(0, 0, 0, 0, fill=color, width=1.5)

    def push(self, v: float, vmax: float = 100.0):
        self.vals.append(max(0.0, v) / vmax if vmax else 0.0)
        w = self.winfo_width()
        if len(self.vals) < 2 or w < 10:
            return
        step = w / (self.n - 1)
        x0 = w - step * (len(self.vals) - 1)
        pts = []
        for i, v in enumerate(self.vals):
            pts += [x0 + i * step, self.h - 2 - min(1.0, v) * (self.h - 4)]
        self.coords(self._line, *pts)


class Cores(tk.Canvas):
    def __init__(self, master, n: int, height=30, **kw):
        super().__init__(master, height=height, width=340, bg=PANEL, highlightthickness=0, **kw)
        self.n, self.h = n, height
        self.rects = [self.create_rectangle(0, 0, 0, 0, width=0) for _ in range(n)]
        for i in range(n):
            self.create_rectangle(0, 0, 0, 0, width=0, fill=TRACK, tags=f"bg{i}")
            self.tag_lower(f"bg{i}")

    def set(self, vals: list[float]):
        w = self.winfo_width()
        if w < 10 or not vals:
            return
        gap = 2
        bw = (w - gap * (self.n - 1)) / self.n
        for i, v in enumerate(vals[: self.n]):
            x = i * (bw + gap)
            self.coords(f"bg{i}", x, 0, x + bw, self.h)
            top = self.h - (v / 100) * self.h
            self.coords(self.rects[i], x, top, x + bw, self.h)
            self.itemconfigure(self.rects[i], fill=load_color(v / 100))


def section(master, title: str):
    f = tk.Frame(master, bg=PANEL, padx=8, pady=6)
    f.pack(fill="x", padx=6, pady=3)
    head = tk.Frame(f, bg=PANEL)
    head.pack(fill="x")
    tk.Label(head, text=title, bg=PANEL, fg=MUTED, font=FONT_B).pack(side="left")
    info = tk.Label(head, text="", bg=PANEL, fg=FG, font=FONT)
    info.pack(side="right")
    return f, head, info


# ------------------------------------------------------------------ Application
class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = load_config()
        self.col = Collector(self.cfg["drives"])
        self.alerts = AlertEngine(self.cfg)
        self.chat = ClaudeChat(self.cfg)
        self.snap = None
        self.snap_q: queue.Queue = queue.Queue(maxsize=2)
        self.chat_q: queue.Queue = queue.Queue()
        self.sort_by = "cpu"
        self.chat_open = False

        root.title("Moniteur PC")
        root.report_callback_exception = self._log_exception
        root.configure(bg=BG)
        root.minsize(W_STATS, 300)
        try:
            root.attributes("-alpha", float(self.cfg.get("opacity", 1.0)))
        except tk.TclError:
            pass
        self.topmost = tk.BooleanVar(value=bool(self.cfg.get("always_on_top", True)))
        root.attributes("-topmost", self.topmost.get())

        self._build()
        root.update_idletasks()
        root.geometry(f"{W_STATS}x{root.winfo_reqheight()}")

        threading.Thread(target=self._sampler, daemon=True).start()
        self.root.after(150, self._tick)
        if self.cfg.get("_error"):
            self._log_alert("warn", self.cfg["_error"])

    # ---------------------------------------------------------------- UI
    def _build(self):
        self.main = tk.Frame(self.root, bg=BG)
        self.main.pack(fill="both", expand=True)
        self.stats = tk.Frame(self.main, bg=BG, width=W_STATS)
        self.stats.pack(side="left", fill="y")

        # En-tête
        top = tk.Frame(self.stats, bg=BG, padx=8, pady=6)
        top.pack(fill="x")
        tk.Label(top, text="Moniteur PC", bg=BG, fg=FG, font=FONT_T).pack(side="left")
        self.pill = tk.Label(top, text=" … ", bg=TRACK, fg=BG, font=FONT_B, padx=6)
        self.pill.pack(side="left", padx=8)
        self.chat_btn = tk.Button(top, text="Chat ▸", command=self.toggle_chat, bg=TRACK, fg=FG,
                                  activebackground=ACCENT, relief="flat", font=FONT, padx=6, cursor="hand2")
        self.chat_btn.pack(side="right")
        tk.Checkbutton(top, text="Premier plan", variable=self.topmost, command=self._apply_topmost,
                       bg=BG, fg=MUTED, selectcolor=PANEL, activebackground=BG, activeforeground=FG,
                       font=FONT, bd=0, highlightthickness=0).pack(side="right", padx=4)

        # CPU
        f, _, self.cpu_info = section(self.stats, "CPU")
        tk.Label(f, text=self.col.cpu_name, bg=PANEL, fg=MUTED, font=("Segoe UI", 8), anchor="w").pack(fill="x")
        self.cpu_bar = Bar(f)
        self.cpu_bar.pack(fill="x", pady=(3, 2))
        self.cores = Cores(f, self.col.n_logical)
        self.cores.pack(fill="x", pady=2)
        self.cpu_spark = Spark(f)
        self.cpu_spark.pack(fill="x")

        # RAM
        f, _, self.ram_info = section(self.stats, "Mémoire")
        self.ram_bar = Bar(f)
        self.ram_bar.pack(fill="x", pady=(3, 0))

        # GPU
        f, _, self.gpu_info = section(self.stats, "GPU")
        self.gpu_name = tk.Label(f, text="", bg=PANEL, fg=MUTED, font=("Segoe UI", 8), anchor="w")
        self.gpu_name.pack(fill="x")
        self.gpu_bar = Bar(f)
        self.gpu_bar.pack(fill="x", pady=(3, 2))
        self.vram_bar = Bar(f)
        self.vram_bar.pack(fill="x")

        # Disques
        f, _, _ = section(self.stats, "Disques")
        self.disk_widgets = {}
        for letter in self.col.drives:
            bar = Bar(f)
            bar.pack(fill="x", pady=(3, 0))
            lbl = tk.Label(f, text="", bg=PANEL, fg=MUTED, font=("Segoe UI", 8), anchor="w")
            lbl.pack(fill="x")
            self.disk_widgets[letter] = (bar, lbl)

        # Réseau
        f, _, self.net_info = section(self.stats, "Réseau")
        self.net_spark = Spark(f, height=20, color="#b38cff")
        self.net_spark.pack(fill="x", pady=(2, 0))
        self.net_max = 1.0

        # Processus
        f, head, self.proc_info = section(self.stats, "Processus")
        self.sort_btn = tk.Label(head, text="tri : CPU ⇅", bg=PANEL, fg=ACCENT, font=FONT, cursor="hand2")
        self.sort_btn.pack(side="right", padx=8)
        self.sort_btn.bind("<Button-1>", lambda e: self._toggle_sort())
        self.proc_rows = []
        grid = tk.Frame(f, bg=PANEL)
        grid.pack(fill="x", pady=(3, 0))
        grid.columnconfigure(0, weight=1)
        for i in range(7):
            row = [tk.Label(grid, text="", bg=PANEL, fg=FG, font=MONO, anchor=a)
                   for a in ("w", "e", "e")]
            for j, w in enumerate(row):
                w.grid(row=i, column=j, sticky="ew", padx=(0, 6 if j < 2 else 0))
            self.proc_rows.append(row)

        # Alertes
        f, _, self.alert_count = section(self.stats, "Alertes")
        self.alert_log = tk.Text(f, height=4, bg=PANEL, fg=FG, font=("Segoe UI", 8), relief="flat",
                                 wrap="word", state="disabled", cursor="arrow")
        self.alert_log.pack(fill="x", pady=(3, 0))
        for tag, col in (("warn", WARN), ("crit", CRIT), ("ok", OK), ("time", MUTED)):
            self.alert_log.tag_configure(tag, foreground=col)
        self._log_alert("ok", "Surveillance démarrée.")

        # Chat (masqué au départ)
        self.chat_frame = tk.Frame(self.main, bg=PANEL, padx=8, pady=8)
        head = tk.Frame(self.chat_frame, bg=PANEL)
        head.pack(fill="x")
        tk.Label(head, text="Demande à Claude", bg=PANEL, fg=FG, font=FONT_T).pack(side="left")
        tk.Button(head, text="Nouvelle conversation", command=self._reset_chat, bg=TRACK, fg=FG,
                  relief="flat", font=("Segoe UI", 8), cursor="hand2").pack(side="right")
        self.chat_log = tk.Text(self.chat_frame, bg=BG, fg=FG, font=("Segoe UI", 10), relief="flat",
                                wrap="word", state="disabled", padx=8, pady=6, width=46)
        self.chat_log.pack(fill="both", expand=True, pady=6)
        self.chat_log.tag_configure("user", foreground=ACCENT, font=("Segoe UI Semibold", 10))
        self.chat_log.tag_configure("bot", foreground=FG)
        self.chat_log.tag_configure("sys", foreground=MUTED, font=("Segoe UI", 9, "italic"))
        self.chat_log.tag_configure("err", foreground=CRIT)

        sugg = tk.Frame(self.chat_frame, bg=PANEL)
        sugg.pack(fill="x")
        for i, q in enumerate(("Pourquoi mon PC rame ?", "Qui prend ma RAM ?",
                               "Libérer de la place sur C:", "Mon GPU chauffe-t-il ?")):
            b = tk.Label(sugg, text=q, bg=TRACK, fg=FG, font=("Segoe UI", 8), padx=6, pady=2, cursor="hand2")
            b.grid(row=i // 2, column=i % 2, sticky="w", padx=(0, 4), pady=(0, 4))
            b.bind("<Button-1>", lambda e, q=q: self._send(q))

        entry_row = tk.Frame(self.chat_frame, bg=PANEL)
        entry_row.pack(fill="x")
        self.entry = tk.Entry(entry_row, bg=BG, fg=FG, insertbackground=FG, relief="flat", font=("Segoe UI", 10))
        self.entry.pack(side="left", fill="x", expand=True, ipady=5)
        self.entry.bind("<Return>", lambda e: self._send())
        self.send_btn = tk.Button(entry_row, text="Envoyer", command=self._send, bg=ACCENT, fg=BG,
                                  relief="flat", font=FONT_B, padx=10, cursor="hand2")
        self.send_btn.pack(side="right", padx=(6, 0))

        if api_key(self.cfg):
            self._chat_write("Pose une question sur l'état de ton PC. Les mesures en direct sont jointes "
                             f"automatiquement (modèle : {self.cfg.get('model')}).\n", "sys")
        else:
            self._chat_write("Chat désactivé : aucune clé API trouvée.\nRenseigne ANTHROPIC_API_KEY (variable "
                             "d'environnement) ou \"anthropic_api_key\" dans config.json, puis relance l'appli. "
                             "Voir le README.\n", "err")

    def _log_exception(self, *exc):
        import traceback
        from config import APP_DIR
        with open(APP_DIR / "erreurs.log", "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + "".join(traceback.format_exception(*exc)) + "\n")

    def _apply_topmost(self):
        self.root.attributes("-topmost", self.topmost.get())

    def _toggle_sort(self):
        self.sort_by = "ram" if self.sort_by == "cpu" else "cpu"
        self.sort_btn.configure(text=f"tri : {'RAM' if self.sort_by == 'ram' else 'CPU'} ⇅")
        if self.snap:
            self._update_procs(self.snap)

    def toggle_chat(self):
        self.chat_open = not self.chat_open
        h = self.root.winfo_height()
        if self.chat_open:
            self.chat_frame.pack(side="left", fill="both", expand=True, padx=(0, 6), pady=6)
            self.chat_btn.configure(text="Chat ◂")
            self.root.geometry(f"{W_STATS + 430}x{h}")
            self.entry.focus_set()
        else:
            self.chat_frame.pack_forget()
            self.chat_btn.configure(text="Chat ▸")
            self.root.geometry(f"{W_STATS}x{h}")

    # ---------------------------------------------------------------- Collecte
    def _sampler(self):
        period = max(0.5, float(self.cfg.get("refresh_seconds", 1.0)))
        while True:
            t0 = time.monotonic()
            try:
                snap = self.col.sample()
                try:
                    self.snap_q.put_nowait(snap)
                except queue.Full:
                    pass
            except Exception as e:  # ne jamais tuer la boucle
                print("Erreur de collecte :", e, file=sys.stderr)
            time.sleep(max(0.05, period - (time.monotonic() - t0)))

    def _tick(self):
        try:
            snap = None
            while True:
                snap = self.snap_q.get_nowait()
        except queue.Empty:
            pass
        if snap is not None:
            self.snap = snap
            self._render(snap)
            for a in self.alerts.evaluate(snap):
                self._notify(a)
            lvl, txt = self.alerts.status()
            self.pill.configure(text=f" {txt} ", bg={"ok": OK, "warn": WARN, "crit": CRIT}[lvl])
            n = len(self.alerts.active)
            self.alert_count.configure(text=f"{n} active{'s' if n > 1 else ''}" if n else "",
                                       fg=CRIT if lvl == "crit" else WARN)
        self._drain_chat()
        self.root.after(100, self._tick)

    def _render(self, s):
        c = self.cfg
        # CPU
        freq = f"{s.cpu_freq_mhz / 1000:.2f} GHz" if s.cpu_freq_mhz else ""
        if s.cpu_temp is not None:
            t = f"{s.cpu_temp:.0f} °C"
            col = temp_color(s.cpu_temp, c["alert_cpu_temp_c"] - 10, c["alert_cpu_temp_c"])
        else:
            t, col = "temp. n/d", MUTED
        self.cpu_info.configure(text=f"{freq}   {t}", fg=col)
        self.cpu_bar.set(s.cpu_total / 100, f"Charge {s.cpu_total:.0f} %",
                         f"{self.col.n_physical} cœurs / {self.col.n_logical} threads")
        self.cores.set(s.cpu_per_core)
        self.cpu_spark.push(s.cpu_total)

        # RAM
        self.ram_bar.set(s.ram_percent / 100, f"{fmt_bytes(s.ram_used)} / {fmt_bytes(s.ram_total)}",
                         f"{s.ram_percent:.0f} %")
        self.ram_info.configure(text=f"validée {fmt_bytes(s.swap_used)} / {fmt_bytes(s.swap_total, 0)}", fg=MUTED)

        # GPU
        if s.gpus:
            g = s.gpus[0]
            self.gpu_name.configure(text=g["name"] + (f"  (+{len(s.gpus) - 1} autre)" if len(s.gpus) > 1 else ""))
            util = g.get("utilization.gpu") or 0
            self.gpu_bar.set(util / 100, f"Charge {util:.0f} %",
                             f"{g['power.draw']:.0f} W" if g.get("power.draw") is not None else "")
            used, tot = g.get("memory.used"), g.get("memory.total")
            if used is not None and tot:
                self.vram_bar.set(used / tot, f"VRAM {used / 1024:.1f} / {tot / 1024:.1f} Go", f"{used / tot * 100:.0f} %")
            tg = g.get("temperature.gpu")
            fan = g.get("fan.speed")
            self.gpu_info.configure(
                text=(f"{tg:.0f} °C" if tg is not None else "temp. n/d") + (f"   ventilo {fan:.0f} %" if fan is not None else ""),
                fg=temp_color(tg, c["alert_gpu_temp_c"] - 10, c["alert_gpu_temp_c"]))
        else:
            self.gpu_name.configure(text="nvidia-smi introuvable — pas de données GPU")
            self.gpu_bar.set(0, "n/d")
            self.vram_bar.set(0, "")

        # Disques
        lims = c.get("alert_disk_free_gb", {})
        for letter, (bar, lbl) in self.disk_widgets.items():
            d = s.disks.get(letter, {})
            if not d.get("present"):
                bar.set(0, f"{letter}:  absent")
                lbl.configure(text="")
                continue
            free_gb = d["free"] / 1024 ** 3
            lim = lims.get(letter, 0)
            color = CRIT if free_gb < lim / 2 else WARN if free_gb < lim else (OK if lim else load_color(d["percent"] / 100))
            bar.set(d["percent"] / 100, f"{letter}:  {fmt_bytes(d['free'])} libres", f"sur {fmt_bytes(d['total'], 0)}",
                    color=color)
            if "read_bps" in d:
                lbl.configure(text=f"lecture {fmt_rate(d['read_bps'])}   écriture {fmt_rate(d['write_bps'])}   "
                                   f"activité ~{d['busy']:.0f} %")
            else:
                lbl.configure(text="activité : mesure en cours…")

        # Réseau
        self.net_info.configure(text=f"↓ {fmt_rate(s.net_down_bps)}   ↑ {fmt_rate(s.net_up_bps)}")
        total = s.net_down_bps + s.net_up_bps
        self.net_max = max(self.net_max * 0.995, total, 64 * 1024)
        self.net_spark.push(total, self.net_max)

        self._update_procs(s)

    def _update_procs(self, s):
        procs = s.procs if self.sort_by == "cpu" else sorted(s.procs, key=lambda p: p["rss"], reverse=True)
        self.proc_info.configure(text=f"{len(s.procs)} actifs", fg=MUTED)
        thr = self.cfg["alert_process_cpu_percent"]
        for i, row in enumerate(self.proc_rows):
            if i < len(procs):
                p = procs[i]
                name = p["name"][:-4] if p["name"].lower().endswith(".exe") else p["name"]
                row[0].configure(text=name[:26], fg=CRIT if p["cpu"] >= thr else FG)
                row[1].configure(text=f"{p['cpu']:5.1f} %")
                row[2].configure(text=fmt_bytes(p["rss"]))
            else:
                for w in row:
                    w.configure(text="")

    # ---------------------------------------------------------------- Alertes
    def _log_alert(self, level: str, msg: str):
        self.alert_log.configure(state="normal")
        self.alert_log.insert("1.0", f"{msg}\n", level)
        self.alert_log.insert("1.0", time.strftime("%H:%M:%S  "), "time")
        if int(self.alert_log.index("end-1c").split(".")[0]) > 200:
            self.alert_log.delete("200.0", "end")
        self.alert_log.configure(state="disabled")

    def _notify(self, a):
        self._log_alert(a.level, a.message)
        if self.cfg.get("sound_on_alert", True) and os.name == "nt":
            try:
                import winsound
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION if a.level == "warn" else winsound.MB_ICONHAND)
            except Exception:
                pass
        self._flash_taskbar()

    def _flash_taskbar(self):
        if os.name != "nt":
            return
        try:
            class FLASHWINFO(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint), ("hwnd", ctypes.c_void_p), ("dwFlags", ctypes.c_uint),
                            ("uCount", ctypes.c_uint), ("dwTimeout", ctypes.c_uint)]
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, 0x3 | 0xC, 5, 0)  # FLASHW_ALL | TIMERNOFG
            ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
        except Exception:
            pass

    # ---------------------------------------------------------------- Chat
    def _chat_write(self, text: str, tag: str = "bot"):
        self.chat_log.configure(state="normal")
        self.chat_log.insert("end", text, tag)
        self.chat_log.see("end")
        self.chat_log.configure(state="disabled")

    def _reset_chat(self):
        if self.chat.busy:
            return
        self.chat.reset()
        self.chat_log.configure(state="normal")
        self.chat_log.delete("1.0", "end")
        self.chat_log.configure(state="disabled")
        self._chat_write("Nouvelle conversation.\n", "sys")

    def _send(self, question: str | None = None):
        if not self.chat_open:
            self.toggle_chat()
        q = (question or self.entry.get()).strip()
        if not q or self.chat.busy:
            return
        if self.snap is None:
            self._chat_write("Les mesures ne sont pas encore prêtes, réessaie dans une seconde.\n", "sys")
            return
        self.entry.delete(0, "end")
        self._chat_write(f"\nToi : {q}\n", "user")
        snapshot_text = build_snapshot_text(self.col, self.snap, list(self.alerts.active.values()))
        self.send_btn.configure(state="disabled", text="…")
        self._chat_write("Claude : ", "user")
        self.chat.ask(q, snapshot_text,
                      on_text=lambda t: self.chat_q.put(("text", t)),
                      on_done=lambda err: self.chat_q.put(("done", err)))

    def _drain_chat(self):
        try:
            while True:
                kind, payload = self.chat_q.get_nowait()
                if kind == "text":
                    self._chat_write(payload, "bot")
                else:
                    if payload:
                        self._chat_write(f"\n{payload}\n", "err")
                    else:
                        self._chat_write("\n", "bot")
                    self.send_btn.configure(state="normal", text="Envoyer")
        except queue.Empty:
            pass


def main():
    if os.name == "nt":
        try:  # rendu net sur écrans à mise à l'échelle
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    try:
        App(root)
    except Exception:
        import traceback
        from tkinter import messagebox
        from config import APP_DIR
        log = APP_DIR / "erreurs.log"
        log.write_text(traceback.format_exc(), encoding="utf-8")
        messagebox.showerror("Moniteur PC", f"Erreur au démarrage. Détails dans :\n{log}")
        root.destroy()
        return
    root.mainloop()


if __name__ == "__main__":
    main()
