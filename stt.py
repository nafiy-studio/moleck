# (c) 2026 Nafiy Studio - Moleck. Code propriete de Nafiy Studio.
# Mention de propriete a conserver (voir LICENSE). Ne pas retirer : ADN du projet.
"""STT Dalia : mlx-whisper (local, Apple Silicon), push-to-talk.

Entrée pour démarrer l'enregistrement, Entrée pour l'arrêter.
Audio < 0.5 s ignoré. La transcription est affichée dans le terminal
AVANT d'être envoyée au cerveau : le propriétaire voit ce que Whisper a compris.
L'audio ne quitte jamais le Mac.
"""
import queue
import re
import sys
import tempfile
import threading

import numpy as np
import sounddevice as sd
from dotenv import dotenv_values
from pathlib import Path

ENV = dotenv_values(Path(__file__).parent / ".env")

SAMPLE_RATE = 16000
MIN_DURATION_S = 0.5
WHISPER_MODEL = ENV.get("WHISPER_MODEL", "mlx-community/whisper-medium-mlx")
LANGUAGE = ENV.get("LANGUAGE", "fr")

# Moteur STT primaire : "parakeet" (Parakeet TDT 0.6B v3, local MLX, défaut) ou
# "whisper" (large-v3-turbo). Parakeet échoue → repli AUTOMATIQUE sur Whisper.
# Whisper n'est JAMAIS retiré : c'est le filet « jamais muette ».
STT_PRIMARY = (ENV.get("STT_PRIMARY", "parakeet") or "parakeet").lower()
PARAKEET_MODEL = ENV.get("PARAKEET_MODEL", "mlx-community/parakeet-tdt-0.6b-v3")

# Périphérique d'entrée. None = défaut système (⚠️ sur ce Mac le défaut est
# « Micro externe » qui capte du quasi-silence ; mettre l'index du micro intégré,
# ex. STT_INPUT_DEVICE=2, ou un bout de nom comme "MacBook").
#
# ⚠️ Les INDEX de périphériques BOUGENT quand on branche/débranche un micro. On
# valide donc l'index au démarrage : s'il est invalide, on retrouve le micro
# « MacBook » par son nom, sinon on retombe sur le défaut système (jamais de crash).
def _resolve_device(v):
    if v is None or str(v).strip() == "":
        return None
    try:
        devs = sd.query_devices()
    except Exception:
        return None
    s = str(v).strip()
    if s.lstrip("-").isdigit():
        i = int(s)
        if 0 <= i < len(devs) and devs[i].get("max_input_channels", 0) > 0:
            return i  # index encore valide
        # index obsolète (un périphérique a été branché/débranché)
        for j, d in enumerate(devs):
            if d.get("max_input_channels", 0) > 0 and "macbook" in d["name"].lower():
                print(f"[stt] device {v} invalide (les index ont bougé) → micro MacBook (index {j})",
                      file=sys.stderr)
                return j
        print(f"[stt] device {v} invalide → micro système par défaut", file=sys.stderr)
        return None
    # nom (ex. « MacBook ») : on cherche une correspondance, sinon défaut
    for j, d in enumerate(devs):
        if d.get("max_input_channels", 0) > 0 and s.lower() in d["name"].lower():
            return j
    return None


INPUT_DEVICE = _resolve_device(ENV.get("STT_INPUT_DEVICE"))

# Normalisation de gain avant transcription : remonte une voix basse/chuchotée
# vers un niveau exploitable, SANS jamais atténuer ni amplifier au point de
# transformer un micro muet en bruit (gain plafonné → le silence reste silence).
NORM_TARGET_PEAK = 0.25
NORM_MAX_GAIN = 12.0


def _normalize(audio):
    peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
    if peak <= 1e-6:
        return audio
    gain = min(NORM_TARGET_PEAK / peak, NORM_MAX_GAIN)
    return (audio * gain).astype(audio.dtype) if gain > 1.0 else audio


_model_loaded = False
_parakeet_model = None


def _ensure_model():
    """Précharge mlx-whisper (télécharge le modèle au premier appel)."""
    global _model_loaded
    if not _model_loaded:
        import mlx_whisper  # noqa: F401  (import lourd, différé)
        _model_loaded = True


def _ensure_parakeet():
    """Charge Parakeet TDT v3 (téléchargé une fois via HF, puis 100 % local MLX)."""
    global _parakeet_model
    if _parakeet_model is None:
        from parakeet_mlx import from_pretrained  # import lourd, différé
        _parakeet_model = from_pretrained(PARAKEET_MODEL)
    return _parakeet_model


_noise_floor = None


def _rms(x):
    return float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))


def calibrate(duration_s=1.0):
    """Mesure le bruit ambiant UNE fois (au démarrage, pendant un silence)."""
    global _noise_floor
    block = SAMPLE_RATE // 15
    levels = []
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                        blocksize=block, device=INPUT_DEVICE) as stream:
        for _ in range(int(duration_s * 15)):
            data, _ = stream.read(block)
            levels.append(_rms(data))
    _noise_floor = float(np.median(levels))
    return _noise_floor


def record_vad(level_callback=None, max_duration_s=30):
    """Mode conversation : détecte le début de parole (amplitude) et s'arrête
    après ~1,2 s de silence. Aucun appui sur Entrée. Seule l'amplitude est
    surveillée en continu ; Whisper ne tourne que sur l'énoncé détecté.

    Le plancher de bruit est calibré une fois au démarrage (calibrate()) puis
    adapté lentement, UNIQUEMENT pendant les silences — parler tout de suite
    après la réponse de Dalia ne fausse donc plus le seuil.

    Renvoie un np.array float32 mono 16 kHz, ou None (bruit/trop court).
    """
    global _noise_floor
    block = SAMPLE_RATE // 15
    start_blocks = 2           # blocs consécutifs au-dessus du seuil = début
    end_blocks = 18            # ~1,2 s sous le seuil = fin de phrase
    if _noise_floor is None:
        calibrate()
    speech = []
    above = 0
    below = 0
    started = False

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                        blocksize=block, device=INPUT_DEVICE) as stream:
        while True:
            data, _ = stream.read(block)
            level = _rms(data)
            if level_callback is not None:
                level_callback(min(1.0, level * 12))

            # ×2,5 et plancher bas : le propriétaire peut chuchoter. Si trop de
            # faux départs (bruit de fond), remonter vers 3.5 / 0.012.
            threshold = max(_noise_floor * 2.5, 0.006)

            if not started:
                if level > threshold:
                    above += 1
                else:
                    above = 0
                    # adaptation lente du plancher, pendant les silences seulement
                    _noise_floor = 0.97 * _noise_floor + 0.03 * level
                speech.append(data.copy())
                if len(speech) > start_blocks + 2:   # petit tampon pré-parole
                    speech.pop(0)
                if above >= start_blocks:
                    started = True
                    below = 0
            else:
                speech.append(data.copy())
                below = below + 1 if level < threshold else 0
                if below >= end_blocks or len(speech) * block / SAMPLE_RATE > max_duration_s:
                    break

    audio = np.concatenate(speech).flatten()
    duration = len(audio) / SAMPLE_RATE - end_blocks * block / SAMPLE_RATE
    if duration < MIN_DURATION_S:
        return None
    return audio


def record_push_to_talk(level_callback=None):
    """Enregistre entre deux appuis sur Entrée. Renvoie un np.array float32 mono 16 kHz.

    level_callback(rms) est appelé ~15x/s pendant l'enregistrement (pour l'orbe).
    """
    chunks = queue.Queue()
    stop = threading.Event()

    def callback(indata, frames, time_info, status):
        chunks.put(indata.copy())
        if level_callback is not None:
            rms = float(np.sqrt(np.mean(indata.astype(np.float64) ** 2)))
            level_callback(min(1.0, rms * 12))

    input("🎙  Entrée pour parler...")
    print("🔴 enregistrement — Entrée pour arrêter")
    stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                            blocksize=SAMPLE_RATE // 15, callback=callback,
                            device=INPUT_DEVICE)
    with stream:
        input()
        stop.set()

    parts = []
    while not chunks.empty():
        parts.append(chunks.get())
    if not parts:
        return None
    audio = np.concatenate(parts).flatten()
    if len(audio) / SAMPLE_RATE < MIN_DURATION_S:
        print("(audio trop court, ignoré)")
        return None
    return audio


# Amorce le décodeur Whisper avec un vocabulaire technique courant : améliore
# la reconnaissance des noms propres et sigles. À adapter à ton propre jargon.
LEXIQUE = (
    "Conversation avec Dalia, l'assistante personnelle du propriétaire. "
    "Sujets : LiteLLM, Ollama, Docker, Git, VS Code, le navigateur, "
    "un conteneur, un service, un projet, un fichier, un dossier."
)


def _transcribe_whisper(audio):
    """Whisper large-v3-turbo : français forcé + amorçage du lexique technique."""
    audio = _normalize(audio)
    _ensure_model()
    import mlx_whisper
    result = mlx_whisper.transcribe(
        audio, path_or_hf_repo=WHISPER_MODEL, language=LANGUAGE, fp16=True,
        initial_prompt=LEXIQUE,
    )
    return result["text"].strip()


def _transcribe_parakeet(audio):
    """Parakeet TDT v3 (MLX, local). L'API prend un FICHIER : on écrit l'audio
    16 kHz mono dans un wav temporaire. Décodage greedy = déterministe."""
    import soundfile as sf  # import lourd, différé
    audio = _normalize(audio)
    model = _ensure_parakeet()
    wav = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            wav = Path(f.name)
        sf.write(str(wav), audio, SAMPLE_RATE, subtype="PCM_16")
        result = model.transcribe(str(wav))
        return result.text.strip()
    finally:
        if wav is not None:
            wav.unlink(missing_ok=True)


# Post-correction du jargon technique. Parakeet (sans amorçage `initial_prompt`)
# écorche les noms propres techniques. Corrections CONSERVATRICES (motifs rares,
# peu de risque sur du français courant), insensibles à la casse. Étends la liste
# librement avec TON propre jargon : (motif regex, remplacement canonique).
_JARGON_FIXES = [
    (r"\blit+(?:le|elle?|l)m\b", "litellm"),
    (r"\bl'?it[eé]lm\b", "litellm"),
    (r"\bo+\s*lama\b", "ollama"),
    (r"\bdoc+e?ur\b", "docker"),
]
_JARGON_FIXES = [(re.compile(p, re.IGNORECASE), r) for p, r in _JARGON_FIXES]


def _fix_jargon(text):
    for rx, repl in _JARGON_FIXES:
        text = rx.sub(repl, text)
    return text


_fw_model = None
FASTER_MODEL = ENV.get("FASTER_WHISPER_MODEL", "small")


def _transcribe_faster(audio):
    """Repli STT cross-OS (Windows, Linux, Mac Intel) : faster-whisper sur CPU.
    Les moteurs MLX (Parakeet, mlx-whisper) sont reserves a Apple Silicon."""
    global _fw_model
    audio = _normalize(audio)
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise RuntimeError("aucun moteur STT disponible : pip install faster-whisper")
    if _fw_model is None:
        _fw_model = WhisperModel(FASTER_MODEL, device="cpu", compute_type="int8")
    segs, _ = _fw_model.transcribe(audio, language=LANGUAGE, initial_prompt=LEXIQUE)
    return " ".join(s.text for s in segs).strip()


def transcribe(audio):
    """Transcrit l'audio en français. Renvoie le texte (affiché par l'appelant).

    Apple Silicon : moteur MLX (STT_PRIMARY, parakeet par défaut) avec repli
    mlx-whisper. Autres OS (Windows, Linux, Mac Intel) : faster-whisper sur CPU.
    Post-correction du jargon technique appliquée en sortie."""
    import platform as _pf
    if _pf.system() != "Darwin" or _pf.machine() not in ("arm64", "aarch64"):
        txt = _transcribe_faster(audio)
        return _fix_jargon(txt)
    if STT_PRIMARY == "parakeet":
        try:
            txt = _transcribe_parakeet(audio)
        except Exception as e:
            print(f"[stt] Parakeet a échoué ({type(e).__name__}: {e}) → repli Whisper",
                  file=sys.stderr)
            try:
                txt = _transcribe_whisper(audio)
            except Exception:
                txt = _transcribe_faster(audio)
    else:
        try:
            txt = _transcribe_whisper(audio)
        except Exception:
            txt = _transcribe_faster(audio)
    return _fix_jargon(txt)
