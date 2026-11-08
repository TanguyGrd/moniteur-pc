"""Chat avec Claude : on joint un instantané des stats à chaque question."""
from __future__ import annotations

import platform
import threading
import time
from typing import Callable

import anthropic

from collector import Collector, Snapshot, fmt_bytes, fmt_duration, fmt_rate
from config import api_key

SYSTEM_PROMPT = """Tu es l'assistant de diagnostic intégré à « Moniteur PC », un petit widget qui surveille \
le PC Windows de l'utilisateur. À chaque message, l'utilisateur te transmet un instantané des mesures \
en direct (CPU, RAM, GPU NVIDIA, disques, réseau, processus, alertes actives et tendance des dernières minutes), \
placé entre les balises <etat_pc>.

Règles :
- Réponds en français, de façon concise et concrète, en texte simple (pas de tableaux Markdown ni de titres ; \
des tirets pour les listes suffisent).
- Appuie-toi sur les chiffres de l'instantané : cite les valeurs et les noms de processus qui expliquent la situation. \
Si une mesure est absente ou « n/d », dis-le plutôt que de l'inventer.
- Donne des actions précises et sûres (ex. fermer telle appli, vider tel dossier, vérifier tel paramètre). \
Signale clairement quand un processus est un composant système de Windows à ne pas arrêter.
- Le disque C: est le disque système ; le D: sert surtout aux jeux et applications.
- Tu ne peux rien exécuter toi-même sur le PC : tu conseilles, l'utilisateur agit."""


def build_snapshot_text(col: Collector, s: Snapshot, active_alerts: list) -> str:
    L = []
    L.append(f"Horodatage : {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(s.ts))}")
    L.append(f"Système : {platform.system()} {platform.release()} (build {platform.version()}), "
             f"allumé depuis {fmt_duration(s.uptime_s)}")
    temp = f"{s.cpu_temp:.0f} °C (source {s.cpu_temp_source})" if s.cpu_temp is not None else "n/d"
    freq = f"{s.cpu_freq_mhz:.0f} MHz" if s.cpu_freq_mhz else "n/d"
    L.append(f"CPU : {col.cpu_name} — {col.n_physical} cœurs / {col.n_logical} threads — "
             f"charge {s.cpu_total:.0f} % — fréquence {freq} — température {temp}")
    L.append("Charge par cœur logique (%) : " + ", ".join(f"{v:.0f}" for v in s.cpu_per_core))
    L.append(f"RAM : {fmt_bytes(s.ram_used)} / {fmt_bytes(s.ram_total)} ({s.ram_percent:.0f} %) — "
             f"mémoire validée (RAM + fichier d'échange) {fmt_bytes(s.swap_used)} / limite {fmt_bytes(s.swap_total)}")
    if s.gpus:
        for g in s.gpus:
            def v(k, unit="", fmt="{:.0f}"):
                x = g.get(k)
                return (fmt.format(x) + unit) if x is not None else "n/d"
            L.append(f"GPU : {g['name']} — charge {v('utilization.gpu', ' %')} — VRAM {v('memory.used')}/"
                     f"{v('memory.total', ' Mo')} — température {v('temperature.gpu', ' °C')} — "
                     f"conso {v('power.draw', ' W')}/{v('power.limit', ' W')} — ventilateur {v('fan.speed', ' %')} — "
                     f"horloge {v('clocks.sm', ' MHz')}")
    else:
        L.append("GPU : aucune donnée (nvidia-smi introuvable ou sans réponse)")
    for letter, d in s.disks.items():
        if not d.get("present"):
            L.append(f"Disque {letter}: absent")
            continue
        act = ""
        if "read_bps" in d:
            act = (f" — lecture {fmt_rate(d['read_bps'])}, écriture {fmt_rate(d['write_bps'])}, "
                   f"activité ~{d['busy']:.0f} %")
        L.append(f"Disque {letter}: {fmt_bytes(d['free'])} libres sur {fmt_bytes(d['total'])} "
                 f"({d['percent']:.0f} % utilisé){act}")
    L.append(f"Réseau : ↓ {fmt_rate(s.net_down_bps)}  ↑ {fmt_rate(s.net_up_bps)}")

    hist = list(col.history)[-120:]
    if len(hist) > 5:
        def stats(vals):
            return f"min {min(vals):.0f} / moy {sum(vals)/len(vals):.0f} / max {max(vals):.0f}"
        span = hist[-1].ts - hist[0].ts
        L.append(f"Tendance sur les {span/60:.1f} dernières minutes : CPU % {stats([h.cpu_total for h in hist])} ; "
                 f"RAM % {stats([h.ram_percent for h in hist])}"
                 + (f" ; GPU % {stats([h.gpus[0].get('utilization.gpu') or 0 for h in hist if h.gpus])}"
                    if hist[-1].gpus else ""))

    L.append(f"Processus actifs : {len(s.procs)}")
    L.append("Top 12 processus par CPU (% du CPU total, RAM) :")
    for p in s.procs[:12]:
        L.append(f"  - {p['name']} (PID {p['pid']}) : {p['cpu']:.1f} %, {fmt_bytes(p['rss'])}")
    L.append("Top 12 processus par RAM :")
    for p in sorted(s.procs, key=lambda r: r["rss"], reverse=True)[:12]:
        L.append(f"  - {p['name']} (PID {p['pid']}) : {fmt_bytes(p['rss'])}, {p['cpu']:.1f} % CPU")
    # Regroupement par nom (ex. 30 processus chrome.exe)
    groups: dict[str, list] = {}
    for p in s.procs:
        g = groups.setdefault(p["name"], [0, 0.0, 0])
        g[0] += 1; g[1] += p["cpu"]; g[2] += p["rss"]
    top_groups = sorted(groups.items(), key=lambda kv: kv[1][2], reverse=True)[:8]
    L.append("RAM cumulée par application (toutes instances) :")
    for name, (n, cpu, rss) in top_groups:
        L.append(f"  - {name} ×{n} : {fmt_bytes(rss)}, {cpu:.1f} % CPU")

    if active_alerts:
        L.append("Alertes actives :")
        for a in active_alerts:
            L.append(f"  - [{'CRITIQUE' if a.level == 'crit' else 'attention'}] {a.message}")
    else:
        L.append("Alertes actives : aucune")
    return "\n".join(L)


class ClaudeChat:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.messages: list = []
        self.busy = False
        self._client = None
        self._client_key = None

    def reset(self):
        self.messages = []

    def _client_or_error(self):
        key = api_key(self.cfg)
        if not key:
            return None, ("Aucune clé API configurée. Ajoute ta clé dans la variable d'environnement "
                          "ANTHROPIC_API_KEY ou dans le champ \"anthropic_api_key\" du fichier config.json "
                          "(voir README), puis relance l'appli.")
        if self._client is None or self._client_key != key:
            self._client = anthropic.Anthropic(api_key=key)
            self._client_key = key
        return self._client, None

    def ask(self, question: str, snapshot_text: str,
            on_text: Callable[[str], None], on_done: Callable[[str | None], None]):
        """Lance la requête dans un thread. on_text reçoit le texte au fil de l'eau,
        on_done(None) en cas de succès, on_done("message d'erreur") sinon."""
        client, err = self._client_or_error()
        if err:
            on_done(err)
            return
        user_msg = {"role": "user",
                    "content": f"<etat_pc>\n{snapshot_text}\n</etat_pc>\n\nQuestion : {question}"}
        self.busy = True
        threading.Thread(target=self._run, args=(client, user_msg, on_text, on_done), daemon=True).start()

    def _stream(self, client, history, on_text, full: bool):
        model = self.cfg.get("model") or "claude-sonnet-5-5"
        if full:
            ctx = client.beta.messages.stream(
                model=model,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                messages=history,
                output_config={"effort": self.cfg.get("effort") or "medium"},
                cache_control={"type": "ephemeral"},  # met en cache l'historique entre deux questions
                # Si un filtre de sécurité refuse à tort, l'API relance sur un modèle de repli.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        else:  # version minimale, si l'API refuse une des options ci-dessus
            ctx = client.messages.stream(model=model, max_tokens=16000, system=SYSTEM_PROMPT, messages=history)
        with ctx as stream:
            for text in stream.text_stream:
                on_text(text)
            return stream.get_final_message()

    def _run(self, client, user_msg, on_text, on_done):
        history = self.messages + [user_msg]
        try:
            try:
                final = self._stream(client, history, on_text, full=True)
            except anthropic.BadRequestError:
                final = self._stream(client, history, on_text, full=False)
            if final.stop_reason == "refusal":
                on_done("Claude a décliné cette demande. Reformule la question autrement.")
                return
            # Historique en ajout seul : on renvoie le contenu tel quel au tour suivant.
            self.messages = history + [{"role": "assistant", "content": final.content}]
            if final.stop_reason == "max_tokens":
                on_text("\n[réponse tronquée]")
            on_done(None)
        except anthropic.AuthenticationError:
            on_done("Clé API refusée (401). Vérifie la clé dans ANTHROPIC_API_KEY ou config.json.")
        except anthropic.PermissionDeniedError:
            on_done("Accès refusé (403) : cette clé n'a pas accès à ce modèle.")
        except anthropic.NotFoundError:
            on_done(f"Modèle introuvable : « {self.cfg.get('model')} ». Corrige \"model\" dans config.json.")
        except anthropic.RateLimitError:
            on_done("Limite de requêtes atteinte (429). Réessaie dans un instant.")
        except anthropic.BadRequestError as e:
            on_done(f"Requête refusée (400) : {getattr(e, 'message', e)}")
        except anthropic.APIStatusError as e:
            on_done(f"Erreur de l'API Claude ({e.status_code}). Réessaie plus tard.")
        except anthropic.APIConnectionError:
            on_done("Impossible de joindre l'API Claude. Vérifie ta connexion Internet.")
        except Exception as e:  # filet de sécurité pour l'UI
            on_done(f"Erreur inattendue : {type(e).__name__}: {e}")
        finally:
            self.busy = False
