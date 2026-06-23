# (c) 2026 Nafiy Studio - Moleck. Code propriete de Nafiy Studio.
# Mention de propriete a conserver (voir LICENSE). Ne pas retirer : ADN du projet.
"""TTS Dalia : edge-tts Vivienne (cloud) avec filtre de confidentialité,
fallback Piper siwis (local) puis say. Jamais d'assistante muette.

Lecture INTERRUPTIBLE (full-duplex) :
- la réponse est découpée en phrases ; chaque phrase est jouée par un `afplay`
  (ou `say`) dont on garde le handle ;
- l'événement `STOP_SPEAKING` coupe la lecture en cours dans la milliseconde
  (kill direct du process + arrêt des phrases suivantes) ;
- la phrase suivante est PRÉ-SYNTHÉTISÉE pendant que la courante joue, pour que
  Vivienne (cloud, latence réseau par phrase) reste fluide.

Le filtre de confidentialité s'applique PHRASE PAR PHRASE, AVANT tout appel cloud :
une phrase contenant un mot sensible ne quitte pas le Mac (voix locale).
"""
import queue
import re
import subprocess
import tempfile
import threading
from pathlib import Path

from dotenv import dotenv_values

DALIA_DIR = Path(__file__).parent
ENV = dotenv_values(DALIA_DIR / ".env")

VOICE_CLOUD = ENV.get("TTS_VOICE_CLOUD", "fr-FR-VivienneMultilingualNeural")
VOICE_LOCAL = ENV.get("TTS_VOICE_LOCAL", "Audrey")
# "local" = voix système macOS en principal (permet la voix Siri choisie dans
# Réglages → Accessibilité → Contenu énoncé) ; "cloud" = Vivienne via edge-tts.
PRIMARY = ENV.get("TTS_PRIMARY", "cloud")
RATE = "+10%"
CLOUD_TIMEOUT = 10

MOTS_SENSIBLES = ["mot de passe", "clé", "token", "secret", "client", "élève", "iban", "@"]

PIPER_MODEL = DALIA_DIR / "piper_voices" / "fr_FR-siwis-medium.onnx"
PIPER_PY = DALIA_DIR / ".venv" / "bin" / "python3"

# ── Interruption (barge-in) ───────────────────────────────────────────────
# STOP_SPEAKING levé par l'extérieur (VAD, Étape 3) → la lecture se coupe net.
# Granularité du POLL (ms) entre deux vérifications pendant qu'un son joue : le
# kill direct dans request_stop() rend la coupure quasi instantanée, ce poll
# n'est qu'un filet de sécurité + sert à ne pas enchaîner la phrase suivante.
POLL_S = 0.02

STOP_SPEAKING = threading.Event()
_current_proc = None
_proc_lock = threading.Lock()

# Phrases ENTIÈREMENT jouées au dernier speak() (frontière de chunk). Sert au
# re-raisonnement sur interruption (étape 6) : on ne garde dans l'historique que
# ce qui a vraiment été dit.
LAST_SPOKEN = []


def request_stop():
    """Coupe la parole en cours IMMÉDIATEMENT (kill du process audio) et empêche
    les phrases suivantes de démarrer. Appelé par le VAD quand le propriétaire interrompt."""
    STOP_SPEAKING.set()
    with _proc_lock:
        p = _current_proc
    if p is not None and p.poll() is None:
        try:
            p.terminate()
        except Exception:
            pass


def clear_stop():
    """Réarme le drapeau pour un nouveau tour de parole."""
    STOP_SPEAKING.clear()


def is_speaking():
    with _proc_lock:
        return _current_proc is not None and _current_proc.poll() is None


def _log(status, detail=""):
    from tools import log_action
    log_action("tts", {"detail": detail[:80]}, status)


def is_sensitive(text):
    low = text.lower()
    return any(m in low for m in MOTS_SENSIBLES)


def _split_sentences(text, min_len=12):
    """Découpe en phrases parlables (sur . ! ? … et sauts de ligne). Recolle les
    fragments trop courts au précédent pour limiter les allers-retours cloud."""
    raw = re.split(r"(?<=[.!?…])\s+|\n+", text.strip())
    chunks = []
    for part in raw:
        part = part.strip()
        if not part:
            continue
        if chunks and len(chunks[-1]) < min_len:
            chunks[-1] = f"{chunks[-1]} {part}"
        else:
            chunks.append(part)
    return chunks or [text.strip()]


# ── Synthèse (phrase → fichier audio) ─────────────────────────────────────

def _synth_cloud(text):
    """edge-tts Vivienne → mp3 temporaire. Renvoie le Path. Lève en cas d'échec."""
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        mp3 = Path(f.name)
    try:
        subprocess.run(
            [str(DALIA_DIR / ".venv/bin/edge-tts"), "--voice", VOICE_CLOUD,
             f"--rate={RATE}", "--text", text, "--write-media", str(mp3)],
            check=True, capture_output=True, timeout=CLOUD_TIMEOUT,
        )
        if mp3.stat().st_size == 0:
            raise RuntimeError("edge-tts a produit un fichier vide")
        return mp3
    except Exception:
        mp3.unlink(missing_ok=True)
        raise


def _synth_piper(text):
    """Voix neurale locale (Piper siwis) → wav temporaire. Renvoie le Path. Lève."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        wav = Path(f.name)
    try:
        subprocess.run(
            [str(PIPER_PY), "-m", "piper", "-m", str(PIPER_MODEL),
             "-f", str(wav), "--length-scale", "1.05"],
            input=text, text=True, check=True, capture_output=True, timeout=20,
        )
        if wav.stat().st_size == 0:
            raise RuntimeError("piper a produit un fichier vide")
        return wav
    except Exception:
        wav.unlink(missing_ok=True)
        raise


def _synth(text):
    """Synthétise une phrase. Renvoie (path|None, canal). path=None ⇒ jouer via
    `say` en direct (dernier repli, jamais muette). Applique le filtre de
    confidentialité par phrase : une phrase sensible ne part jamais au cloud."""
    if PRIMARY != "local" and not is_sensitive(text):
        try:
            return _synth_cloud(text), f"cloud ({VOICE_CLOUD})"
        except Exception as e:
            canal_suffix = f", fallback: {type(e).__name__}"
        else:
            canal_suffix = ""
    else:
        canal_suffix = ", confidentialité" if is_sensitive(text) else ""
    # local (confidentialité, repli, ou PRIMARY=local)
    if PIPER_MODEL.exists():
        try:
            return _synth_piper(text), f"piper siwis{canal_suffix}"
        except Exception:
            pass
    return None, f"say{canal_suffix}"


# ── Lecture interruptible ─────────────────────────────────────────────────

def _play_cmd(cmd):
    """Lance cmd (lecture audio) en process killable. Renvoie True si terminé
    normalement, False si interrompu par STOP_SPEAKING."""
    global _current_proc
    with _proc_lock:
        if STOP_SPEAKING.is_set():
            return False
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _current_proc = proc
    try:
        while True:
            if STOP_SPEAKING.is_set():
                try:
                    proc.terminate()
                    proc.wait(timeout=0.3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                except Exception:
                    pass
                return False
            try:
                proc.wait(timeout=POLL_S)
                return True
            except subprocess.TimeoutExpired:
                continue
    finally:
        with _proc_lock:
            if _current_proc is proc:
                _current_proc = None


def _play(path, text):
    """Joue une phrase (fichier afplay, ou say direct si path=None). Renvoie
    True si terminé, False si interrompu. Supprime le fichier dans tous les cas."""
    if path is None:
        cmd = ["say", text] if VOICE_LOCAL.lower() in ("system", "systeme", "siri", "") \
            else ["say", "-v", VOICE_LOCAL, text]
        return _play_cmd(cmd)
    try:
        return _play_cmd(["afplay", str(path)])
    finally:
        path.unlink(missing_ok=True)


# File bornée entre producteur (synthèse) et consommateur (lecture) : assez
# d'avance pour masquer la latence réseau de Vivienne, sans pré-synthétiser
# 20 phrases d'un coup (RAM/temporaires).
_QUEUE_MAX = 2
_GET_TIMEOUT = 0.05  # le consommateur re-sonde STOP_SPEAKING toutes les 50 ms
_SENTINEL = object()


def _drain_and_clean(q):
    """Vide la file et supprime les .mp3/.wav pré-synthétisés jamais joués."""
    while True:
        try:
            item = q.get_nowait()
        except queue.Empty:
            return
        if item is not _SENTINEL and item[0] is not None:
            item[0].unlink(missing_ok=True)


def speak(text):
    """Lit `text` à voix haute, phrase par phrase, de façon interruptible.
    Renvoie le canal utilisé (pour les logs/tests).

    Producteur-consommateur : un thread synthétise les phrases EN AVANCE dans une
    file bornée (Vivienne reste fluide, pas de trou entre phrases) ; le thread
    courant lit la file et coupe net dès que STOP_SPEAKING est levé — y compris
    pendant qu'il attend la synthèse de la phrase suivante (sonde toutes les 50 ms).
    """
    if not text.strip():
        return "vide"
    clear_stop()  # nouveau tour : on repart d'un drapeau propre
    global LAST_SPOKEN
    LAST_SPOKEN = []
    played = []   # phrases ENTIÈREMENT jouées (frontière de chunk, pas d'offset deviné)
    chunks = _split_sentences(text)
    q = queue.Queue(maxsize=_QUEUE_MAX)
    # local_stop : propre à CET appel. Évite qu'un clear_stop() du tour suivant
    # ne relance un producteur resté en vol (il vérifie son local_stop, pas que
    # le drapeau global). Posé dès que ce speak() rend la main.
    local_stop = threading.Event()
    canaux = []
    interrupted = False

    def _stop_now():
        return STOP_SPEAKING.is_set() or local_stop.is_set()

    def producer():
        """Synthétise chaque phrase et la pousse dans la file. S'arrête net si
        l'interruption est levée (y compris pendant l'attente d'une place libre)."""
        for chunk in chunks:
            if _stop_now():
                break
            path, canal = _synth(chunk)
            if _stop_now():  # interrompu pendant la synthèse
                if path is not None:
                    path.unlink(missing_ok=True)
                break
            item = (path, canal, chunk)
            while True:  # put borné, mais réactif à l'interruption
                if _stop_now():
                    if path is not None:
                        path.unlink(missing_ok=True)
                    q.put(_SENTINEL)
                    return
                try:
                    q.put(item, timeout=_GET_TIMEOUT)
                    break
                except queue.Full:
                    continue
        q.put(_SENTINEL)

    prod = threading.Thread(target=producer, daemon=True)
    prod.start()
    try:
        while True:
            if STOP_SPEAKING.is_set():
                interrupted = True
                break
            try:
                item = q.get(timeout=_GET_TIMEOUT)
            except queue.Empty:
                continue  # rien de prêt encore : on re-sonde STOP_SPEAKING
            if item is _SENTINEL:
                break
            path, canal, chunk = item
            if STOP_SPEAKING.is_set():
                if path is not None:
                    path.unlink(missing_ok=True)
                interrupted = True
                break
            canaux.append(canal)
            if not _play(path, chunk):  # coupé en pleine phrase (PAS jouée entièrement)
                interrupted = True
                break
            played.append(chunk)  # phrase jouée jusqu'au bout
    finally:
        # Couper le producteur sans BLOQUER le retour : une synthèse cloud en vol
        # (~1-2 s, non killable) est attendue puis nettoyée dans un thread de fond.
        # speak() rend la main dès que l'audio est coupé → la boucle réécoute vite.
        local_stop.set()
        threading.Thread(
            target=lambda: (prod.join(timeout=CLOUD_TIMEOUT + 1), _drain_and_clean(q)),
            daemon=True).start()

    LAST_SPOKEN = played  # phrases réellement dites (pour le re-raisonnement, étape 6)
    canal = canaux[0] if canaux else "vide"
    status = "interrompu" if interrupted else canal.split(" ")[0]
    _log(status, f"{canal} | {len(played)}/{len(chunks)} phrases dites")
    return f"{canal} (interrompu)" if interrupted else canal
