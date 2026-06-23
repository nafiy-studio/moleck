#!/usr/bin/env python3
# (c) 2026 Nafiy Studio - Moleck. Code propriete de Nafiy Studio.
# Mention de propriete a conserver (voir LICENSE). Ne pas retirer : ADN du projet.
"""DALIA — assistante vocale personnelle locale.

Phase 1 : mode texte. `python3 dalia.py --text "question"` (sans micro ni TTS).
L'état de conversation persiste dans state.json entre deux invocations ;
`--reset` repart de zéro.
"""
import argparse
import os
import sys
from pathlib import Path

# Bootstrap : `python3 dalia.py` relance automatiquement dans le venv du projet.
_VENV_PY = Path(__file__).parent / ".venv" / "bin" / "python3"
if _VENV_PY.exists() and Path(sys.prefix).name != ".venv":
    os.execv(str(_VENV_PY), [str(_VENV_PY), *sys.argv])

import threading

# Un seul tour de conversation à la fois : la voix et le chat tapé partagent ce
# verrou pour ne jamais écraser l'état (state.json) ni se chevaucher.
TURN_LOCK = threading.Lock()


def _strip_self_name(text):
    """Coupe un éventuel « <Nom>, » en tête de réponse (ne doit pas être lu)."""
    import re, orchestrator
    return re.sub(rf"^\s*{re.escape(orchestrator.ASSISTANT_NAME)}\s*[,:!.]?\s*", "", text, flags=re.IGNORECASE) or text


def voice_loop(set_state=None, push_to_talk=False, set_view=None):
    """Boucle vocale. Par défaut : conversation continue (détection de silence).
    push_to_talk=True : Entrée pour parler / Entrée pour arrêter.
    set_state(état, niveau) pilote l'orbe/cockpit. set_view(vue) bascule la fenêtre
    cockpit↔maison quand le périmètre change."""
    import barge_in
    import cockpit_ui
    import orchestrator
    import speaker
    import stt
    import tts

    state = set_state or (lambda s, level=0.0: None)
    switch_view = set_view or (lambda v: None)
    orchestrator.purge_stale_pending()  # aucun pending d'une session passée au démarrage
    mode = "push-to-talk" if push_to_talk else "conversation full-duplex (parlez quand vous voulez, même pendant que je parle)"
    print(f"Dalia en écoute — mode {mode}. Ctrl+C pour quitter.")
    stt._ensure_model()       # précharge Whisper (repli) avant le premier tour
    barge_in._get_model()     # précharge le VAD Silero (barge-in)

    # Gate locuteur : actif seulement si un profil est enrôlé (enroll_speaker.py).
    # Sans profil → n'importe quelle voix coupe (comportement étape 3).
    if speaker.has_profile():
        speaker.warmup()      # précharge l'encodeur (sinon 1er appel ~1,7 s)

        def speaker_gate(raw):
            score = speaker.verify_score(raw)
            cockpit_ui.gate_score(score)      # affiche le score au cockpit
            return score >= speaker.SPEAKER_THRESHOLD

        print(f"🔒 gate locuteur actif (seuil {speaker.SPEAKER_THRESHOLD}) — seule ta voix me coupe.")
    else:
        speaker_gate = None
        print("🔓 gate locuteur inactif (pas de profil) — toute voix me coupe. "
              "Enrôle-toi : enroll_speaker.py")

    def gate_now():
        # Gate actif seulement en mode cockpit (ALL). En mode maison, n'importe
        # qui peut couper DALIA (pas besoin de la voix du propriétaire).
        if speaker_gate is not None and orchestrator.current_perimeter() == "ALL":
            return speaker_gate
        return None

    last_view = orchestrator.current_view()
    if not push_to_talk:
        print("🤫 calibration du micro, une seconde de silence...")
        stt.calibrate()
        print("✅ prête.")
    state("idle")
    pending_audio = None   # audio d'une interruption déjà capturé (carry-forward)
    while True:
        try:
            if pending_audio is not None:
                # L'interruption a déjà été captée en entier (amorce + suite) par le
                # barge-in : on la transcrit directement, sans re-capturer (pas de trou).
                audio, pending_audio = pending_audio, None
            elif push_to_talk:
                audio = stt.record_push_to_talk(level_callback=lambda lv: state("listening", lv))
            else:
                state("listening", 0.0)
                audio = stt.record_vad(level_callback=lambda lv: state("listening", lv))
            state("idle")
            if audio is None:
                continue
            state("thinking")
            text = stt.transcribe(audio)
            if not text or len(text.strip()) < 2:
                state("idle")
                continue
            # Ne jamais afficher/journaliser un code secret parlé (run.log + cockpit).
            masked = orchestrator.awaiting_secret()
            if masked:
                print("📝 Whisper a compris : [code secret — masqué]")
                cockpit_ui.hear("[code secret — masqué]")
            else:
                print(f"📝 Whisper a compris : {text}")
                cockpit_ui.hear(text)
            with TURN_LOCK:
                out = orchestrator.ask(text)
            reply = _strip_self_name(out["text"])
            print(f"💬 {reply}")
            print(f"[{out['model']} | {out['latency_s']}s]", file=sys.stderr)
            cockpit_ui.model(out["model"])
            # Bascule de fenêtre si le périmètre a changé (« passe en maison » / code).
            v = orchestrator.current_view()
            if v != last_view:
                switch_view(v)
                last_view = v
            state("speaking")
            # Micro ouvert pendant la parole : barge-in. Le gate locuteur n'agit qu'en
            # mode cockpit (en maison, n'importe qui peut couper).
            canal, interrupted, captured = barge_in.speak_with_barge_in(reply, on_gate=gate_now())
            print(f"[voix: {canal}]", file=sys.stderr)
            if interrupted:
                print("✋ interruption — je t'écoute", file=sys.stderr)
                spoken = " ".join(tts.LAST_SPOKEN)
                cockpit_ui.say(spoken or reply, interrupted=True)  # bulle marquée [interrompue]
                # Re-raisonnement (étape 6) : ne garder que les phrases dites,
                # marquer qu'elle n'avait pas fini, annuler toute action en attente.
                orchestrator.mark_interrupted(spoken)
                # Carry-forward (correctif A) : l'audio complet de l'interruption
                # (début capté par le gate + suite) est transcrit au prochain tour,
                # sans repasser par record_vad → pas de début perdu.
                if captured is not None and len(captured) >= int(0.3 * stt.SAMPLE_RATE):
                    pending_audio = captured
                state("listening", 0.0)
            else:
                cockpit_ui.say(reply)   # bulle Dalia (réponse complète)
                state("idle")
        except KeyboardInterrupt:
            print("\nau revoir")
            return


def run_with_orb(push_to_talk=False):
    """Fenêtre orbe (main thread, exigence macOS) + boucle vocale en thread."""
    import json
    import threading

    import webview

    dalia_dir = Path(__file__).parent
    pos_file = dalia_dir / "orb_position.json"
    pos = {}
    if pos_file.exists():
        try:
            pos = json.loads(pos_file.read_text())
        except json.JSONDecodeError:
            pos = {}

    ORB_SIZE = (300, 300)
    COCKPIT_SIZE = (300, 560)

    class Api:
        """Exposée au JS de l'orbe (boutons de la barre au survol)."""

        def __init__(self):
            self.cockpit_open = False

        def toggle_cockpit(self):
            self.cockpit_open = not self.cockpit_open
            w, h = COCKPIT_SIZE if self.cockpit_open else ORB_SIZE
            window.resize(w, h)
            window.evaluate_js(f"showCockpit({'true' if self.cockpit_open else 'false'})")
            return self.cockpit_open

        def quit(self):
            save_position()
            os._exit(0)

    api = Api()

    window = webview.create_window(
        "Dalia", url=str(dalia_dir / "orb" / "orb.html"),
        width=ORB_SIZE[0], height=ORB_SIZE[1], x=pos.get("x"), y=pos.get("y"),
        frameless=True, on_top=True, transparent=True, easy_drag=True,
        resizable=False, js_api=api,
    )

    def set_state(state, level=0.0):
        try:
            window.evaluate_js(f"setState({state!r}, {float(level):.3f})")
        except Exception:
            pass  # la voix prime sur le visuel : l'orbe ne casse jamais la boucle

    def save_position():
        try:
            pos_file.write_text(json.dumps({"x": window.x, "y": window.y}))
        except Exception:
            pass

    window.events.closing += save_position

    def runner():
        try:
            voice_loop(set_state=set_state, push_to_talk=push_to_talk)
        finally:
            save_position()
            try:
                window.destroy()
            except Exception:
                pass

    def cockpit_runner():
        """Rafraîchit le panneau UNIQUEMENT quand il est ouvert (RAM friendly)."""
        import time as _time

        import cockpit

        while True:
            if not api.cockpit_open:
                _time.sleep(1)
                continue
            stats = cockpit.local_snapshot()
            try:
                window.evaluate_js(f"updateStats({json.dumps(stats, ensure_ascii=False)})")
            except Exception:
                pass
            _time.sleep(5)

    threading.Thread(target=runner, daemon=True).start()
    threading.Thread(target=cockpit_runner, daemon=True).start()
    webview.start()


def _system_monitor():
    """Pousse CPU/RAM/disque du Mac au cockpit toutes les ~1,5 s (psutil)."""
    import time as _time

    import cockpit_ui
    import psutil

    total_gb = round(psutil.virtual_memory().total / 1e9)
    disk_total = round(psutil.disk_usage("/").total / 1e9)
    psutil.cpu_percent()  # amorce (1er appel renvoie 0)
    while True:
        try:
            vm = psutil.virtual_memory()
            cockpit_ui.system(
                cpu=round(psutil.cpu_percent()),
                ram_used=round(vm.used / 1e9, 1), ram_total=total_gb,
                disk_used=round(psutil.disk_usage("/").used / 1e9), disk_total=disk_total)
        except Exception:
            pass
        _time.sleep(1.5)


def _push_engine_status():
    """Pousse au cockpit les réglages RÉELS (moteur, full-duplex) + le commit."""
    import subprocess

    import barge_in
    import cockpit_ui
    import speaker
    import stt

    try:
        import sounddevice as sd
        idx = stt.INPUT_DEVICE
        mic = sd.query_devices()[idx]["name"] if isinstance(idx, int) else "défaut système"
    except Exception:
        mic = "?"
    cockpit_ui.engine(
        repli="modèle de repli local", stt=stt.STT_PRIMARY, mic=mic,
        lat_stt="~110 ms", barge="~120 ms",
        vad=f"actif · {barge_in.VAD_SPEECH_THRESHOLD}",
        echo="haut-parleur" if barge_in.SPEAKER_MODE else "casque",
        seuil=str(speaker.SPEAKER_THRESHOLD))
    cockpit_ui.model("principal")   # route principale, avant le 1er tour
    try:
        commit = subprocess.run(["git", "-C", str(Path(__file__).parent),
                                 "rev-parse", "--short", "HEAD"],
                                capture_output=True, text=True, timeout=5).stdout.strip()
        cockpit_ui.status(commit=commit or "?")
    except Exception:
        pass


def run_with_cockpit(push_to_talk=False):
    """Fenêtre cockpit (dalia_cockpit.html) pilotée par window.DALIA, boucle vocale
    en thread (l'audio n'est jamais bloqué par l'UI). Le cockpit n'exécute RIEN."""
    import threading
    import time as _time

    import webview

    import cockpit_ui
    import orchestrator

    dalia_dir = Path(__file__).parent
    _HTML = {"cockpit": dalia_dir / "dalia_cockpit.html", "maison": dalia_dir / "dalia_maison.html"}
    view0 = orchestrator.current_view()   # reprend la vue de l'état (maison si verrouillé)
    _view = {"cur": view0}

    def submit_text(text):
        # Tour de conversation TAPÉ (chat du cockpit / de la maison). Même cerveau
        # que la voix ; TURN_LOCK évite tout chevauchement avec un tour vocal. Pas
        # de TTS : la réponse s'affiche dans le chat et les actions s'exécutent. Le
        # périmètre (maison fail-closed, code pour repasser cockpit) reste appliqué
        # par orchestrator.ask, exactement comme à la voix.
        text = (text or "").strip()
        if not text:
            return
        with TURN_LOCK:
            masked = orchestrator.awaiting_secret()
            cockpit_ui.hear("[code secret — masqué]" if masked else text)
            cockpit_ui.state("think")
            try:
                out = orchestrator.ask(text)
            except Exception as e:
                cockpit_ui.say(f"(erreur interne : {type(e).__name__})")
                cockpit_ui.state("standby")
                return
            cockpit_ui.model(out["model"])
            cockpit_ui.say(_strip_self_name(out["text"]))
            cockpit_ui.state("standby")
            set_view(orchestrator.current_view())   # bascule si le périmètre a changé

    class CockpitApi:
        """Exposée au JS du chat (cockpit + maison) : envoie le texte tapé au
        cerveau dans un thread, pour ne pas bloquer l'UI."""

        def send_text(self, text):
            threading.Thread(target=submit_text, args=(text,), daemon=True).start()
            return True

    window = webview.create_window(
        "DALIA", url=str(_HTML.get(view0, _HTML["cockpit"])),
        width=1320, height=880, min_size=(1040, 720), js_api=CockpitApi())
    cockpit_ui.set_window(window)

    _STATE = {"idle": "standby", "listening": "listen", "thinking": "think", "speaking": "speak"}

    def set_state(s, level=0.0):
        cockpit_ui.state(_STATE.get(s, "standby"))

    def set_view(v):
        # bascule cockpit↔maison à chaud (charge la bonne page) — idempotent pour
        # ne pas recharger (et vider le chat) quand la vue n'a pas changé
        if v == _view["cur"]:
            return
        _view["cur"] = v
        try:
            window.load_url(str(_HTML.get(v, _HTML["cockpit"])))
        except Exception:
            pass

    def runner():
        _time.sleep(1.3)            # laisse le DOM se charger avant les premiers push
        cockpit_ui.online(True)
        _push_engine_status()       # réglages réels + commit (une fois au démarrage)
        try:
            voice_loop(set_state=set_state, push_to_talk=push_to_talk, set_view=set_view)
        finally:
            try:
                window.destroy()
            except Exception:
                pass

    threading.Thread(target=runner, daemon=True).start()
    threading.Thread(target=_system_monitor, daemon=True).start()
    webview.start()


def main():
    parser = argparse.ArgumentParser(description="Dalia, assistante vocale")
    parser.add_argument("--text", metavar="QUESTION", help="mode texte : un tour de conversation, sans micro ni TTS")
    parser.add_argument("--reset", action="store_true", help="efface l'historique de conversation")
    parser.add_argument("--no-orb", action="store_true", help="boucle vocale sans fenêtre orbe")
    parser.add_argument("--cockpit", action="store_true", help="tableau de bord complet (dalia_cockpit.html)")
    parser.add_argument("--maison", action="store_true", help="mode maison : vue maison + périmètre média (pas d'accès système)")
    parser.add_argument("--push-to-talk", action="store_true", help="ancien mode : Entrée pour parler/arrêter")
    parser.add_argument("--serve", action="store_true", help="serveur API pour l'appli mobile (PWA), à exposer uniquement sur réseau privé")
    args = parser.parse_args()

    import orchestrator

    if args.serve:
        import server
        server.main()
        return

    if args.reset:
        orchestrator.reset()
        print("historique effacé")
        if not args.text:
            return

    if args.maison:
        orchestrator.set_perimeter("MEDIA")   # verrouille avant le lancement
        run_with_cockpit(push_to_talk=args.push_to_talk)  # charge la vue maison via current_view()
        return

    if args.text:
        out = orchestrator.ask(args.text)
        print(out["text"])
        print(f"[{out['model']} | {out['latency_s']}s]", file=sys.stderr)
        return

    # Défaut prioritaire : COCKPIT (le mode le plus utilisé). Un lancement normal
    # repart toujours en mode complet, même si la session précédente était en maison.
    orchestrator.set_perimeter("ALL")
    if args.cockpit:
        run_with_cockpit(push_to_talk=args.push_to_talk)
    elif args.no_orb:
        voice_loop(push_to_talk=args.push_to_talk)
    else:
        run_with_orb(push_to_talk=args.push_to_talk)


if __name__ == "__main__":
    main()
