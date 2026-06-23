# Moleck

Assistante vocale locale pour Mac, branchable sur **ton propre cerveau LLM**.
Voix, memoire long terme, execution d'actions sur le Mac (fichiers, apps, shell),
skills reutilisables, garde-fous de confirmation. Tout tourne chez toi : aucune
donnee vocale ne quitte la machine, le LLM est celui que TU configures.

Moleck est un modele distribuable : tu l'installes, tu branches ton endpoint LLM
et tes cles, tu la renommes si tu veux. Rien n'est lie a l'auteur.

## Compatibilite OS
Moleck tourne sur **macOS, Linux et Windows**.
- Cerveau LLM, memoire, outils (fichiers, shell, web), skills : toutes plateformes.
- Voix (STT) : Apple Silicon utilise les moteurs MLX (Parakeet / mlx-whisper) ;
  Windows, Linux et Mac Intel utilisent `faster-whisper` (CPU), installe et
  selectionne automatiquement.
- TTS : `edge-tts` (cloud) et `piper` (local), cross-OS.
- Outils specifiques macOS (AppleScript, Raccourcis) se desactivent proprement
  ailleurs ; ouverture d'apps/URL et corbeille (`send2trash`) marchent partout.

## Prerequis
- macOS 12+, Linux, ou Windows 10/11.
- Python 3.11+ (`python3 --version` / `python --version`).
- Un endpoint LLM compatible OpenAI : LiteLLM, Ollama (`/v1`), vLLM, OpenAI,
  Together, etc. C'est TON cerveau, tu fournis l'URL et la cle.
- ffmpeg pour l'audio (`brew install ffmpeg`, `apt install ffmpeg`, ou
  `winget install ffmpeg`).

## Installation
macOS / Linux :
```bash
git clone <ton-fork-ou-ce-depot> moleck
cd moleck
bash setup.sh
```
Windows (PowerShell) :
```powershell
git clone <ton-fork-ou-ce-depot> moleck
cd moleck
powershell -ExecutionPolicy Bypass -File setup.ps1
```
Le script cree le venv, installe les dependances, copie `.env.example` en `.env`
et te demande le nom de l'assistante et ton nom.

Puis edite `.env` et renseigne au minimum ton cerveau :
```
LITELLM_BASE_URL=...     # ton endpoint
LITELLM_API_KEY=...      # ta cle
MODEL_PRIMARY=...        # le modele expose par ton endpoint
```

## Lancement
```bash
.venv/bin/python3 moleck.py            # boucle vocale + orbe
.venv/bin/python3 moleck.py --text "bonjour"   # mode texte (sans micro)
.venv/bin/python3 moleck.py --cockpit  # tableau de bord
.venv/bin/python3 moleck.py --serve    # API pour appli mobile (reseau prive)
```

## La renommer
Change `ASSISTANT_NAME` (et `OWNER_NAME`) dans `.env`. Le nom devient le mot qui
la reveille et son identite. Aucun code a toucher.

## Reconnaissance du locuteur (optionnel)
Pour qu'elle ne reponde qu'a ta voix :
```bash
.venv/bin/python3 enroll_speaker.py
```
Regle `SPEAKER_THRESHOLD` dans `.env`.

## Tes serveurs (optionnel)
Les outils SSH (`tools.py`, section SSH) sont concus pour piloter TES machines.
Mets tes hotes dans `~/.ssh/config` et adapte ces outils, ou retire-les si tu
n'as pas de serveur. Le garde-fou `PROD_PASSPHRASE` exige une phrase parlee
avant toute mutation sur un serveur.

## Toujours active (optionnel)
Des modeles launchd sont dans `launchd/`. Remplace `__INSTALL_DIR__` et
`__HOME__` par tes chemins, renomme le label, puis `launchctl load`.

## Guide
Le guide d'installation et d'utilisation complet est dans
`GUIDE_INSTALLATION_MOLECK.pdf` (a la racine).

## Confidentialite
- `.env`, memoire (ecrite dans `~/SecondBrain/<nom>/`), profil vocal et logs sont
  locaux et gitignores.
- Le seul appel reseau sortant est vers l'endpoint LLM que TU choisis (et les
  integrations optionnelles que tu actives : TTS cloud, YouTube, Spotify).

## Licence
MIT. Voir LICENSE.
