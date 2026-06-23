# (c) 2026 Nafiy Studio - Moleck. Code propriete de Nafiy Studio.
# Mention de propriete a conserver (voir LICENSE). Ne pas retirer : ADN du projet.
"""Barge-in (full-duplex, étape 3) : micro ouvert PENDANT que DALIA parle, VAD
continu (Silero, local). Dès que de la parole est détectée, on coupe le TTS
(`tts.request_stop()`) et on bascule en écoute.

Réutilise la fondation TTS interruptible de l'étape 2 (request_stop < 200 ms) et
le périphérique d'entrée configuré dans stt (STT_INPUT_DEVICE).

⚠️ Étape 3 = barge-in BASIQUE, à utiliser AU CASQUE (la voix de DALIA part dans
l'oreille, le micro ne l'entend pas → pas d'auto-coupure). L'anti-écho haut-parleur
(étape 4) et le gate locuteur (étape 5) viennent ensuite.
"""
import threading
from pathlib import Path

import numpy as np
import sounddevice as sd
import torch
from dotenv import dotenv_values
from silero_vad import load_silero_vad

import stt

ENV = dotenv_values(Path(__file__).parent / ".env")
SAMPLE_RATE = 16000
VAD_WINDOW = 512               # échantillons (32 ms) — taille requise par Silero

# ── Anti-écho (étape 4) ──────────────────────────────────────────────────────
# DÉFAUT = casque : la voix de DALIA part dans l'oreille, le micro ne l'entend
# pas → aucun traitement nécessaire (validé : 14,5 s de parole sans auto-coupure).
# SPEAKER_MODE (haut-parleur) : SANS annulation d'écho acoustique (AEC), le micro
# capte la voix de DALIA et elle se couperait elle-même. L'AEC propre (Voice
# Processing I/O d'AVAudioEngine, ou WebRTC AEC) n'est PAS encore implémentée —
# voir TODO ci-dessous. En attendant, on durcit le VAD pour limiter (sans
# éliminer) l'auto-coupure. RESTE déconseillé : préférer le casque.
SPEAKER_MODE = (ENV.get("SPEAKER_MODE", "false") or "").strip().lower() in (
    "1", "true", "oui", "yes", "on")

# ── Réglages VAD (ajuster ici la sensibilité de l'interruption) ──
if SPEAKER_MODE:
    VAD_SPEECH_THRESHOLD = 0.85   # plus strict : limite l'auto-coupure sans AEC
    VAD_MIN_FRAMES = 8            # ~256 ms de parole confirmée
else:
    VAD_SPEECH_THRESHOLD = 0.6    # proba de parole pour compter une fenêtre (0..1)
    VAD_MIN_FRAMES = 3           # fenêtres consécutives au-dessus du seuil (~100 ms)

# Quand un gate locuteur est actif, on COLLECTE ce nombre de secondes de parole
# AVANT de vérifier l'embedding : un embedding fiable exige ~0,8 s de voix, pas
# les 100 ms de la détection (sinon on calcule sur du quasi-silence → faux rejet).
GATE_COLLECT_S = 1.0

# Carry-forward (correctif A) : une fois l'interruption confirmée et coupée, on
# CONTINUE de capturer la fin de la phrase SUR LE MÊME flux micro (pas de
# fermeture/réouverture → aucun trou ni recouvrement à la jointure), jusqu'à un
# silence de fin d'énoncé. L'audio complet (amorce + suite) part à la transcription.
END_SILENCE_S = 1.0       # silence continu = fin de l'énoncé d'interruption
MAX_INTERRUPT_S = 15      # garde-fou de durée
RECENT_S = 0.5            # pré-roll d'onset gardé en continu (mode sans gate)

# TODO étape 4 (haut-parleur) : AEC réelle via AVAudioEngine Voice Processing I/O
# (setVoiceProcessingEnabled) ou WebRTC AEC, branchée sur le flux micro avant le
# VAD. Tant que ce n'est pas fait, SPEAKER_MODE reste une mitigation partielle.

_model = None


def _get_model():
    global _model
    if _model is None:
        if SPEAKER_MODE:
            print("[barge_in] ⚠️ SPEAKER_MODE actif SANS AEC : DALIA risque de se "
                  "couper elle-même. Casque fortement recommandé.")
        _model = load_silero_vad()
    return _model


class BargeInMonitor:
    """Surveille le micro pendant la parole de DALIA. À la première parole
    confirmée (VAD_MIN_FRAMES fenêtres consécutives), appelle on_speech() une
    seule fois. `detected` indique si une interruption a eu lieu.

    on_gate(chunk) optionnel (étape 5) : reçoit l'audio brut accumulé et renvoie
    True pour autoriser l'interruption (voix reconnue) ; None = toujours autoriser.
    """

    def __init__(self, on_speech, threshold=VAD_SPEECH_THRESHOLD,
                 min_frames=VAD_MIN_FRAMES, on_gate=None):
        self.on_speech = on_speech
        self.on_gate = on_gate
        self.threshold = threshold
        self.min_frames = min_frames
        self.detected = False
        self.captured = None        # audio COMPLET de l'interruption (amorce + suite)
        self._stop = threading.Event()
        self._thread = None
        self._model = _get_model()

    def _trigger(self, seed):
        """Interruption confirmée : couper le TTS, puis continuer à capturer la
        FIN de l'énoncé sur le même flux (carry-forward, sans trou)."""
        self.detected = True
        self.on_speech()
        return list(seed)           # amorce = ce qui a déjà été capturé (le début)

    def _run(self):
        self._model.reset_states()
        consecutive = 0
        recent = []                 # pré-roll d'onset (gardé en continu)
        recent_max = max(self.min_frames + 2, int(RECENT_S * SAMPLE_RATE / VAD_WINDOW))
        collecting = False          # phase de collecte avant vérif locuteur
        collected = []
        need = int(GATE_COLLECT_S * SAMPLE_RATE)
        capturing = False           # phase carry-forward (après la coupure)
        tail = []
        silence = 0
        end_frames = int(END_SILENCE_S * SAMPLE_RATE / VAD_WINDOW)
        max_frames = int(MAX_INTERRUPT_S * SAMPLE_RATE / VAD_WINDOW)
        try:
            with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                                blocksize=VAD_WINDOW, device=stt.INPUT_DEVICE) as stream:
                while not self._stop.is_set():
                    data, _ = stream.read(VAD_WINDOW)
                    chunk = np.ascontiguousarray(data.flatten(), dtype=np.float32)
                    if len(chunk) < VAD_WINDOW:
                        continue

                    # ── Carry-forward : capturer la fin de l'énoncé jusqu'au silence ──
                    if capturing:
                        tail.append(chunk)
                        prob = self._model(torch.from_numpy(chunk), SAMPLE_RATE).item()
                        silence = silence + 1 if prob < self.threshold else 0
                        if silence >= end_frames or len(tail) >= max_frames:
                            self.captured = np.concatenate(self._seed + tail)
                            return
                        continue

                    # ── Collecte (gate actif) : accumuler ~GATE_COLLECT_S puis vérifier ──
                    if collecting:
                        collected.append(chunk)
                        if sum(len(c) for c in collected) >= need:
                            raw = np.concatenate(collected)  # ce que reçoit Resemblyzer : INCHANGÉ
                            if self.on_gate(raw):
                                self._seed = self._trigger(collected)  # amorce = la 1 s + onset
                                capturing = True
                                tail, silence = [], 0
                            else:
                                collecting = False     # voix rejetée → retour en veille
                                collected = []
                                consecutive = 0
                                self._model.reset_states()
                        continue

                    recent.append(chunk)
                    if len(recent) > recent_max:
                        recent.pop(0)
                    prob = self._model(torch.from_numpy(chunk), SAMPLE_RATE).item()
                    consecutive = consecutive + 1 if prob >= self.threshold else 0
                    if consecutive >= self.min_frames:
                        if self.on_gate is None:
                            self._seed = self._trigger(recent)   # sans gate : coupe direct
                            capturing = True
                            tail, silence = [], 0
                        else:
                            collecting = True          # gate : collecter avant de vérifier
                            collected = list(recent)   # amorce avec l'attaque de la parole
        except Exception as e:  # le micro ne doit jamais casser la boucle vocale
            print(f"[barge_in] moniteur arrêté : {type(e).__name__}: {e}")

    def start(self):
        self._stop.clear()
        self.detected = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)

    def finish(self):
        """Si interruption : attend que la fin de l'énoncé soit capturée (carry-
        forward) et renvoie l'audio complet. Sinon : arrête le moniteur."""
        if self.detected and self._thread is not None:
            self._thread.join(timeout=MAX_INTERRUPT_S + 2)
        else:
            self.stop()
        return self.captured


def speak_with_barge_in(reply, on_gate=None):
    """Joue `reply` au TTS avec le micro ouvert. Si le propriétaire parle, coupe net puis
    capture la fin de sa phrase. Renvoie (canal, interrupted, captured) où
    `captured` est l'audio COMPLET de l'interruption (None si pas d'interruption)."""
    import tts
    monitor = BargeInMonitor(on_speech=tts.request_stop, on_gate=on_gate)
    monitor.start()
    try:
        canal = tts.speak(reply)
    except BaseException:
        monitor.stop()
        raise
    captured = monitor.finish()
    return canal, monitor.detected, captured
