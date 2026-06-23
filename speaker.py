# (c) 2026 Nafiy Studio - Moleck. Code propriete de Nafiy Studio.
# Mention de propriete a conserver (voir LICENSE). Ne pas retirer : ADN du projet.
"""Gate locuteur (full-duplex, étape 5) : seule la voix du propriétaire peut couper DALIA.

Embedding de voix (Resemblyzer, local). Profil enrôlé sur DEUX registres — voix
normale ET chuchotée — car un chuchotement n'a presque pas de fondamentale et se
ferait rejeter sinon. Vérification sur l'audio BRUT de l'interruption, AVANT de
couper le TTS. Biais assumé vers le FAUX REJET : en cas de doute ou d'erreur,
on ne coupe pas (mieux vaut ignorer le propriétaire parfois que d'obéir à une autre voix).

Données biométriques (`speaker_profile.npz`) : locales, gitignorées.
"""
import numpy as np
from pathlib import Path

from dotenv import dotenv_values

DALIA_DIR = Path(__file__).parent
ENV = dotenv_values(DALIA_DIR / ".env")
PROFILE_FILE = DALIA_DIR / "speaker_profile.npz"
SAMPLE_RATE = 16000

# Seuil de similarité cosinus pour reconnaître le propriétaire (0..1). Plus haut =
# plus strict (plus de faux rejets, moins de faux accepts). Calibrer avec enroll_speaker.py.
SPEAKER_THRESHOLD = float(ENV.get("SPEAKER_THRESHOLD", "0.75"))

_encoder = None


def _get_encoder():
    global _encoder
    if _encoder is None:
        from resemblyzer import VoiceEncoder
        _encoder = VoiceEncoder(verbose=False)
    return _encoder


def _embed(audio):
    """Embedding 256-d d'un extrait audio (np.float32 16 kHz)."""
    from resemblyzer import preprocess_wav
    enc = _get_encoder()
    wav = preprocess_wav(np.asarray(audio, dtype=np.float32), source_sr=SAMPLE_RATE)
    return enc.embed_utterance(wav)


def has_profile():
    return PROFILE_FILE.exists()


def _load_embeddings():
    if not PROFILE_FILE.exists():
        return None
    return np.load(PROFILE_FILE)["embeddings"]


def enroll(samples):
    """Construit et sauvegarde le profil : un embedding par échantillon (mélange
    voix normale + chuchotée). Renvoie le nombre d'échantillons retenus."""
    embs = [_embed(a) for a in samples
            if a is not None and len(a) > SAMPLE_RATE // 2]
    if not embs:
        raise ValueError("aucun échantillon exploitable pour l'enrôlement")
    np.savez(PROFILE_FILE, embeddings=np.stack(embs))
    return len(embs)


def verify_score(audio):
    """Similarité max (0..1) entre l'audio et les embeddings enrôlés. 0 si pas de profil."""
    prof = _load_embeddings()
    if prof is None:
        return 0.0
    e = _embed(audio)
    sims = prof @ e / (np.linalg.norm(prof, axis=1) * np.linalg.norm(e) + 1e-9)
    return float(np.max(sims))


def verify(audio):
    """True si la voix correspond au profil (≥ seuil). Biais FAUX REJET : sans
    profil ou en cas d'erreur → False (DALIA finit sa phrase)."""
    try:
        if not has_profile():
            return False
        return verify_score(audio) >= SPEAKER_THRESHOLD
    except Exception:
        return False


def warmup():
    """Précharge l'encodeur (sinon le 1er appel coûte ~1,7 s — inacceptable dans
    le chemin de coupure ; en régime établi c'est ~15 ms)."""
    try:
        _embed((np.random.RandomState(0).randn(SAMPLE_RATE) * 0.05).astype(np.float32))
    except Exception:
        _get_encoder()
