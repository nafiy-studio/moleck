# (c) 2026 Nafiy Studio - Moleck. Code propriete de Nafiy Studio.
# Mention de propriete a conserver (voir LICENSE). Ne pas retirer : ADN du projet.
"""Bridge cockpit : pousse l'état RÉEL de DALIA vers le HTML via `window.DALIA`.

⚠️ Le cockpit est un PUR AFFICHEUR. Aucune méthode `window.DALIA.*` n'exécute
d'action serveur/fichier ; toutes les portes (code PROD, oui lab, gate locuteur,
kill switch) restent côté Python, AU-DESSUS de l'UI. Le code secret n'apparaît
JAMAIS ici — le masquage est fait par l'appelant.

Sécurité d'envoi :
- arguments sérialisés en JSON (jamais d'interpolation de chaîne brute → pas
  d'injection / de JS cassé) ;
- le texte de hear/say est aussi échappé HTML (le HTML les injecte via innerHTML
  sans échapper ; setAction/log sont déjà échappés côté HTML) ;
- fail-safe : si la fenêtre n'est pas prête, rien ne casse la boucle vocale.
"""
import base64
import json
import mimetypes
import sys
from pathlib import Path

_window = None


def set_window(w):
    global _window
    _window = w


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def push(method, *args):
    """window.DALIA.<method>(<args JSON>). Ne lève jamais."""
    w = _window
    if w is None:
        return
    try:
        payload = ",".join(json.dumps(a, ensure_ascii=False) for a in args)
        # garde l'existence de la méthode : la vue maison n'a pas toutes les méthodes
        # du cockpit (setAction/log/setSystem…) → ces push deviennent no-op, pas d'erreur.
        w.evaluate_js(f"window.DALIA&&DALIA.{method}&&DALIA.{method}({payload})")
    except Exception as e:  # l'UI ne doit jamais casser l'audio
        print(f"[cockpit] {method} échoué : {type(e).__name__}", file=sys.stderr)


# ── Helpers événements (échappent ce qui doit l'être) ──────────────────────
def state(s):
    push("setState", s)


def hear(text):
    push("hear", _esc(text))


def say(text, interrupted=False):
    push("say", _esc(text), {"interrupted": bool(interrupted)})


def action(command, host, klass, awaiting):
    # command : déjà échappé côté HTML (escapeHtml) → on passe le brut
    push("setAction", {"command": command, "host": host, "klass": klass, "awaiting": awaiting})


def action_clear():
    push("setAction", {"awaiting": "none"})


def journal(command, host, klass, status):
    push("log", {"command": command, "host": host, "klass": klass, "status": status})


def gate_score(score, name="propriétaire"):
    push("setGateScore", round(float(score), 2), name)


def pending(txt):
    push("setPending", txt)


def model(m):
    push("setModel", m)


def online(b):
    push("online", bool(b))


def system(cpu=None, ram_used=None, ram_total=None, disk_used=None, disk_total=None):
    push("setSystem", {"cpu": cpu, "ramUsed": ram_used, "ramTotal": ram_total,
                       "diskUsed": disk_used, "diskTotal": disk_total})


# ── Média dans le chat (kind: image | video | youtube | link) ──────────────
def user_media(kind, src, caption=None):
    push("userMedia", {"kind": kind, "src": src}, caption)


def dalia_media(kind, src, caption=None):
    push("daliaMedia", {"kind": kind, "src": src}, caption)


def now_playing(title=None, artist=None, art=None):
    """Barre « en lecture » de la vue maison. now_playing() (sans args) = rien."""
    push("setNowPlaying", {"title": title, "artist": artist, "art": art} if title else None)


def engine(model=None, repli=None, stt=None, mic=None, lat_stt=None, barge=None,
           vad=None, echo=None, seuil=None):
    """Réglages réels (moteur + full-duplex) : remplace les valeurs en dur."""
    push("setEngine", {"model": model, "repli": repli, "stt": stt, "mic": mic,
                       "latStt": lat_stt, "barge": barge, "vad": vad, "echo": echo,
                       "seuil": seuil})


def status(commit=None, tests=None):
    """Barre d'état : commit courant, tests."""
    push("setStatus", {"commit": commit, "tests": tests})


def image_data_url(path):
    """Image locale → data URL base64 (le webview n'affiche pas un chemin disque
    brut). None si illisible / pas une image."""
    try:
        p = Path(str(path)).expanduser()
        mime = mimetypes.guess_type(str(p))[0] or "image/jpeg"
        if not mime.startswith("image/"):
            return None
        return f"data:{mime};base64,{base64.b64encode(p.read_bytes()).decode()}"
    except Exception:
        return None
