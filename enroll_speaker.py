#!/usr/bin/env python3
# (c) 2026 Nafiy Studio - Moleck. Code propriete de Nafiy Studio.
# Mention de propriete a conserver (voir LICENSE). Ne pas retirer : ADN du projet.
"""Enrôlement du locuteur (étape 5) — À LANCER AU CASQUE dans ton Terminal.

Enregistre ta voix en DEUX registres (normale ET chuchotée) pour que tu puisses
couper DALIA même en parlant bas. Puis aide à calibrer le seuil : ta voix vs une
autre (fais parler quelqu'un / la TV). Données biométriques locales, gitignorées.

   cd ~/dalia && .venv/bin/python3 enroll_speaker.py
"""
import sys
import threading

import numpy as np
import sounddevice as sd

import speaker
import stt

SR = 16000
MIN_PEAK = 0.02
PHRASES = ["Dalia tu m'entends", "attends arrête-toi", "non écoute-moi",
           "j'ai une question", "stop deux secondes"]


def record_one(prompt):
    print(f"  {prompt}")
    chunks = []

    def cb(indata, frames, t, status):
        chunks.append(indata.copy())
        rms = float(np.sqrt(np.mean(indata.astype(np.float64) ** 2)))
        bars = min(30, int(rms * 300))
        sys.stdout.write("\r    " + "█" * bars + "·" * (30 - bars))
        sys.stdout.flush()

    input("    ⏎ pour DÉMARRER…")
    print("    🔴 parle puis ⏎")
    with sd.InputStream(samplerate=SR, channels=1, dtype="float32",
                        blocksize=SR // 15, callback=cb, device=stt.INPUT_DEVICE):
        input()
    sys.stdout.write("\r" + " " * 40 + "\r")
    if not chunks:
        return None
    a = np.concatenate(chunks).flatten()
    if len(a) < SR // 2 or float(np.max(np.abs(a))) < MIN_PEAK:
        print("    ⚠️ trop court/faible, on refait.")
        return record_one(prompt)
    return a


def main():
    print("=== Enrôlement du locuteur (au casque) ===\n")
    speaker.warmup()
    samples = []
    print("--- 1) VOIX NORMALE ---")
    for p in PHRASES:
        samples.append(record_one(f"voix normale : « {p} »"))
    print("\n--- 2) VOIX CHUCHOTÉE (parle tout bas) ---")
    for p in PHRASES:
        samples.append(record_one(f"chuchote : « {p} »"))

    n = speaker.enroll([s for s in samples if s is not None])
    print(f"\n✅ profil enregistré ({n} échantillons, normale + chuchotée).")

    # ── Calibration du seuil ──
    print("\n--- Calibration (seuil actuel = %.2f) ---" % speaker.SPEAKER_THRESHOLD)
    print("Teste TA voix :")
    me = record_one("dis une phrase normalement")
    if me is not None:
        print(f"   → score TA voix : {speaker.verify_score(me):.3f} "
              f"(doit être ≥ seuil pour te reconnaître)")
    wme = record_one("chuchote une phrase")
    if wme is not None:
        print(f"   → score TON chuchotement : {speaker.verify_score(wme):.3f}")
    print("\nMaintenant une AUTRE voix (quelqu'un d'autre, ou la TV) :")
    other = record_one("fais parler une autre voix")
    if other is not None:
        print(f"   → score AUTRE voix : {speaker.verify_score(other):.3f} "
              f"(doit être < seuil pour la rejeter)")
    print("\nRègle le seuil dans .env : SPEAKER_THRESHOLD=<valeur entre les deux>")
    print("Biais sécurité : préfère un seuil un peu HAUT (rejette les autres, "
          "quitte à parfois t'ignorer).")


if __name__ == "__main__":
    main()
