# (c) 2026 Nafiy Studio - Moleck. Code propriete de Nafiy Studio.
# Mention de propriete a conserver (voir LICENSE). Ne pas retirer : ADN du projet.
"""Outils média locaux pour DALIA : Spotify (recherche API + lecture AppleScript)
et YouTube (recherche API + ouverture navigateur).

Clés dans .env (lues au démarrage) :
- SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET (app sur developer.spotify.com)
- YOUTUBE_API_KEY (YouTube Data API v3)

Sécurité : recherche en lecture seule ; la lecture Spotify passe par AppleScript
sur l'app locale avec un URI VALIDÉ (pas d'injection). Aucune clé ne sort d'ici.
"""
import base64
import json
import re
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

from dotenv import dotenv_values

ENV = dotenv_values(Path(__file__).parent / ".env")
_HTTP_TIMEOUT = 10

# URI Spotify strict (anti-injection AppleScript) : spotify:track:<base62>
_SPOTIFY_URI_RE = re.compile(r"^spotify:(track|album|playlist|artist):[A-Za-z0-9]+$")


def _get(key):
    return (ENV.get(key) or "").strip()


def _osa(script):
    return subprocess.run(["osascript", "-e", script], capture_output=True,
                          text=True, timeout=10)


# ── Spotify ────────────────────────────────────────────────────────────────

def _spotify_token():
    cid, secret = _get("SPOTIFY_CLIENT_ID"), _get("SPOTIFY_CLIENT_SECRET")
    if not cid or not secret:
        return None
    auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token",
        data=b"grant_type=client_credentials",
        headers={"Authorization": f"Basic {auth}",
                 "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
        return json.loads(r.read()).get("access_token")


def spotify_play(query):
    """Cherche un morceau/artiste sur Spotify et le joue dans l'app locale."""
    token = _spotify_token()
    if token is None:
        return "refusé: Spotify non configuré (SPOTIFY_CLIENT_ID/SECRET manquants dans .env)"
    try:
        url = "https://api.spotify.com/v1/search?" + urllib.parse.urlencode(
            {"q": query, "type": "track", "limit": 1})
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
            items = json.loads(r.read()).get("tracks", {}).get("items", [])
    except Exception as e:
        return f"erreur recherche Spotify: {type(e).__name__}"
    if not items:
        return f"aucun résultat Spotify pour « {query} »"
    t = items[0]
    uri, name, artist = t["uri"], t["name"], t["artists"][0]["name"]
    if not _SPOTIFY_URI_RE.match(uri):
        return "refusé: URI Spotify inattendu"
    _osa('tell application "Spotify" to activate')
    r = _osa(f'tell application "Spotify" to play track "{uri}"')
    if r.returncode != 0:
        return f"trouvé « {name} » de {artist}, mais lecture impossible (Spotify installé/ouvert ?)"
    try:
        import cockpit_ui
        art = (t.get("album", {}).get("images") or [{}])[0].get("url")
        cockpit_ui.now_playing(name, artist, art)   # barre « en lecture » (vue maison)
    except Exception:
        pass
    return f"Je joue « {name} » de {artist} sur Spotify."


_SPOTIFY_ACTIONS = {
    "pause": 'pause', "stop": 'pause',
    "play": 'play', "reprends": 'play', "resume": 'play', "lecture": 'play',
    "next": 'next track', "suivant": 'next track', "suivante": 'next track',
    "previous": 'previous track', "précédent": 'previous track', "precedent": 'previous track',
}


def spotify_control(action):
    """Contrôle la lecture Spotify : pause, play, suivant, précédent."""
    a = (action or "").lower().strip()
    cmd = _SPOTIFY_ACTIONS.get(a)
    if cmd is None:
        return f"action Spotify inconnue: {action} (pause/play/suivant/précédent)"
    r = _osa(f'tell application "Spotify" to {cmd}')
    if r.returncode != 0:
        return "Spotify ne répond pas (installé/ouvert ?)"
    return f"Spotify : {a}."


# ── YouTube ────────────────────────────────────────────────────────────────

def youtube_play(query):
    """Cherche une vidéo YouTube et l'ouvre dans le navigateur."""
    key = _get("YOUTUBE_API_KEY")
    if not key:
        return "refusé: YouTube non configuré (YOUTUBE_API_KEY manquant dans .env)"
    try:
        url = "https://www.googleapis.com/youtube/v3/search?" + urllib.parse.urlencode(
            {"part": "snippet", "q": query, "type": "video", "maxResults": 1, "key": key})
        with urllib.request.urlopen(url, timeout=_HTTP_TIMEOUT) as r:
            items = json.loads(r.read()).get("items", [])
    except Exception as e:
        return f"erreur recherche YouTube: {type(e).__name__}"
    if not items:
        return f"aucune vidéo YouTube pour « {query} »"
    vid = items[0]["id"]["videoId"]
    title = items[0]["snippet"]["title"]
    if not re.match(r"^[A-Za-z0-9_-]{6,15}$", vid):
        return "refusé: identifiant vidéo inattendu"
    # la page « watch » lance la lecture automatiquement dans le navigateur
    subprocess.run(["open", f"https://www.youtube.com/watch?v={vid}"])
    return f"Je lance « {title} » sur YouTube."


def recherche_web(query):
    """Ouvre une recherche web dans le navigateur (autorisé en mode maison)."""
    q = (query or "").strip()
    if not q:
        return "refusé: recherche vide"
    subprocess.run(["open", "https://www.google.com/search?q=" + urllib.parse.quote(q)])
    return f"Je cherche « {q} » sur le web."


# ── Affichage de média dans le chat du cockpit (chantier 1.1) ──────────────
_YT_RE = re.compile(r"(youtube\.com|youtu\.be)", re.IGNORECASE)
_IMG_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic")
_VID_EXT = (".mp4", ".mov", ".webm", ".m4v", ".avi")


def afficher_media(source):
    """Affiche une image, une vidéo, un lien ou une vidéo YouTube dans le chat
    (cockpit). Accepte un chemin de fichier local OU une URL. 100 % local : une
    image locale est convertie en data URL base64, jamais envoyée ailleurs."""
    import cockpit_ui  # import différé (évite cycle au chargement)
    s = (source or "").strip()
    if not s:
        return "refusé: source vide"
    low = s.lower()
    if _YT_RE.search(low):
        cockpit_ui.dalia_media("youtube", s)
        return "Vidéo YouTube affichée dans le chat."
    if low.startswith(("http://", "https://")):
        base = low.split("?")[0]
        kind = "image" if base.endswith(_IMG_EXT) else ("video" if base.endswith(_VID_EXT) else "link")
        cockpit_ui.dalia_media(kind, s)
        return "Média affiché dans le chat."
    p = Path(s).expanduser()
    if not p.exists():
        return f"introuvable: {s}"
    ext = p.suffix.lower()
    if ext in _IMG_EXT:
        url = cockpit_ui.image_data_url(p)
        if not url:
            return "refusé: image illisible"
        cockpit_ui.dalia_media("image", url)
        return f"Image {p.name} affichée dans le chat."
    if ext in _VID_EXT:
        return "Affichage d'une vidéo locale pas encore supporté — donne-moi un lien."
    return f"refusé: type non affichable ({ext})"


# ── Analyse d'image : modèle vision via l'endpoint LLM configuré ──
# Routage strict : SEUL cet outil touche la vision ; le cerveau principal reste le
# défaut. Image envoyée à l'endpoint LiteLLM configuré (.env), qui la route vers le
# modèle vision. Le modèle est lu depuis la config (MODEL_VISION).
import mimetypes as _mt  # noqa: E402

VISION_MODEL = (ENV.get("MODEL_VISION") or "").strip() or "vision"
_VISION_PROMPT = ("Décris cette image en une ou deux phrases courtes, en français. "
                  "Ne mentionne QUE ce que tu vois réellement, n'invente rien.")


def analyser_image(source):
    """Décrit/analyse une image locale avec le modèle vision local, et l'affiche
    dans le chat du cockpit. `source` = chemin de fichier local."""
    import cockpit_ui
    import json as _json
    import urllib.request as _ur
    p = Path(str(source).strip()).expanduser()
    if not p.exists():
        return f"introuvable: {source}"
    mime = _mt.guess_type(str(p))[0] or ""
    if not mime.startswith("image/"):
        return "refusé: ce n'est pas une image"
    data_url = "data:%s;base64,%s" % (mime, base64.b64encode(p.read_bytes()).decode())
    cockpit_ui.dalia_media("image", data_url, p.name)   # affiche l'image au cockpit
    base = (ENV.get("LITELLM_BASE_URL") or "").rstrip("/")
    key = ENV.get("LITELLM_API_KEY") or ""
    body = _json.dumps({"model": VISION_MODEL, "messages": [{"role": "user", "content": [
        {"type": "text", "text": _VISION_PROMPT},
        {"type": "image_url", "image_url": {"url": data_url}}]}]}).encode()
    try:
        req = _ur.Request(base + "/chat/completions", data=body,
                          headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        r = _json.loads(_ur.urlopen(req, timeout=120).read())
        desc = (r["choices"][0]["message"]["content"] or "").strip()
    except Exception as e:
        return f"image affichée, mais analyse indisponible ({type(e).__name__})"
    return desc or "image affichée, mais je n'ai pas réussi à la décrire"
