# Moniteur PC

Petit widget Windows qui surveille ton PC en direct, t'alerte quand quelque chose cloche
et te laisse poser des questions à Claude sur l'état de la machine.

Tout tourne en local (Python + psutil + Tkinter). Seul le chat contacte l'API Claude,
et uniquement quand tu poses une question.

## Lancer

Double-clique sur **`Lancer Moniteur PC.bat`**.

Au premier lancement, le script crée un environnement Python dans `.venv` (environ 75 Mo),
installe `psutil` et `anthropic`, puis copie `config.exemple.json` vers `config.json`.
Les lancements suivants sont immédiats. Il faut Python 3.10 ou plus récent (`py -3.11` est utilisé en priorité).

> Mets le dossier dans un chemin court (par ex. `Desktop\Projets\MoniteurPC`). Si tu le déplaces,
> supprime `.venv` : il sera recréé au lancement suivant.

## Ce qui est affiché (rafraîchi chaque seconde)

| Zone | Contenu |
|---|---|
| CPU | charge totale, barre par cœur logique, fréquence, température (voir plus bas), historique |
| Mémoire | RAM utilisée, mémoire validée (RAM + fichier d'échange, comme dans le Gestionnaire des tâches) |
| GPU | charge, VRAM, température, consommation, ventilateur (via `nvidia-smi`) |
| Disques | espace libre de C: et D:, lecture/écriture par seconde, activité approximative |
| Réseau | débit descendant et montant, historique |
| Processus | les 7 plus gourmands, triés par CPU ou par RAM (clic sur « tri ») |
| Alertes | journal horodaté, pastille d'état en haut (vert, orange ou rouge) |

La case **Premier plan** garde la fenêtre au-dessus des autres. Le bouton **Chat ▸** ouvre le panneau de discussion.

## Alertes

Une alerte déclenche un bip et fait clignoter l'icône dans la barre des tâches. Elle n'est répétée
qu'après sa disparition, et pas avant 5 minutes. Seuils par défaut, modifiables dans `config.json` :

- disque C: sous 15 Go libres, D: sous 20 Go
- RAM au-dessus de 90 %
- CPU au-dessus de 95 % pendant 20 s
- température CPU au-dessus de 90 °C, GPU au-dessus de 83 °C
- VRAM au-dessus de 95 %
- un processus seul au-dessus de 50 % du CPU pendant 20 s, ou au-dessus de 6 Go de RAM

## Configurer le chat Claude

Le chat a besoin d'une clé API Anthropic (https://console.anthropic.com). L'appli ne te la demande
jamais : tu la renseignes toi-même, au choix.

**Option 1, recommandée : variable d'environnement.** Dans un terminal PowerShell :

```powershell
setx ANTHROPIC_API_KEY "ta-clé-ici"
```

Ferme ensuite le terminal et relance l'appli.

**Option 2 : fichier de config.** Ouvre `config.json` et remplis `"anthropic_api_key": "ta-clé-ici"`.
Ce fichier contient alors ta clé : ne le partage pas et ne le publie pas (il est exclu par `.gitignore`).

Sans clé, le chat affiche un message explicite et le reste de l'appli fonctionne normalement.

Chaque question envoie à Claude un instantané des mesures : chiffres du CPU, de la RAM, du GPU,
des disques et du réseau, les 12 processus les plus gourmands, les alertes en cours et la tendance
des dernières minutes. Le modèle par défaut est `claude-sonnet-5-5` (champ `"model"`). Le champ `"effort"`
(`low`, `medium` ou `high`) règle la profondeur de réflexion : plus c'est élevé, plus c'est lent et coûteux.
« Nouvelle conversation » vide l'historique.

## Température du CPU

Windows ne donne pas la température du CPU aux applications ordinaires. Pour l'afficher :

1. Installe **LibreHardwareMonitor** (gratuit, open source).
2. Lance-le en administrateur et laisse-le ouvert, éventuellement réduit.

Moniteur PC lit alors ses capteurs automatiquement. Sinon, il tente la zone thermique ACPI
(souvent vide sans droits administrateur) et affiche « temp. n/d ». La température du GPU, elle,
fonctionne toujours via `nvidia-smi`.

## Dépannage

- **Rien ne s'ouvre** : regarde `erreurs.log` dans le dossier. Tu peux aussi lancer
  `.venv\Scripts\python.exe app.py` dans un terminal pour voir les erreurs.
- **GPU « n/d »** : `nvidia-smi` introuvable. Il est installé avec le pilote NVIDIA.
- **« Clé API refusée (401) »** : la clé est invalide ou a été révoquée.
- **Modèle introuvable** : corrige `"model"` dans `config.json`.

## Fichiers

| Fichier | Rôle |
|---|---|
| `app.py` | interface (Tkinter) |
| `collector.py` | collecte des mesures (psutil, nvidia-smi, WMI) |
| `winproc.py` | liste rapide des processus via l'API Windows |
| `alerts.py` | règles d'alerte |
| `chat.py` | instantané envoyé à Claude et appel à l'API |
| `config.py` | valeurs par défaut et lecture de `config.json` |

## Licence

MIT — voir [LICENSE](LICENSE). Contributions bienvenues.
