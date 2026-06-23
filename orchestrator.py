# (c) 2026 Nafiy Studio - Moleck. Code propriete de Nafiy Studio.
# Mention de propriete a conserver (voir LICENSE). Ne pas retirer : ADN du projet.
"""Orchestrateur Dalia : boucle tool-calling manuelle (format OpenAI).

- max 5 itérations d'outils par tour, historique borné à 20 messages
- persona.md envoyé comme system à CHAQUE appel
- route principale (modèle principal) → bascule repli local annoncée si échec
- régime confirmation : phrase déterministe + état persisté entre deux tours
- état persisté dans state.json pour que `--text` conserve le contexte
"""
import fcntl
import json
import re
import threading
import time
from pathlib import Path

import openai
from dotenv import dotenv_values

import cockpit_ui
import tools

DALIA_DIR = Path(__file__).parent
STATE_FILE = DALIA_DIR / "state.json"
ENV = dotenv_values(DALIA_DIR / ".env")

MAX_TOOL_ITERATIONS = 8  # chaînes multi-étapes (lecture surtout) sans troncature prématurée
MAX_HISTORY = 30  # tours précédents gardés ; le tour courant n'est JAMAIS rogné (cf. _context)
_ASK_LOCK = threading.Lock()  # un seul tour à la fois DANS un process (voix / cockpit)
_ASK_LOCKFILE = DALIA_DIR / ".ask.lock"  # verrou INTER-process (boucle vocale + serveur mobile)
# Durée de vie d'une action en attente de confirmation/code : passé ce délai, le
# code prononcé ne la valide plus (il faut la re-proposer). Verrou anti-action
# fantôme : un code dit aujourd'hui ne doit jamais valider une action d'avant.
PENDING_TTL_S = 120
PRIMARY_TIMEOUT = 30
FALLBACK_RETRY_AFTER = 300  # réessaie la route principale après 5 min

CONFIRM_RE = re.compile(
    r"\b(oui|ouais|ok|okay|d'?\s?accord|d'?acc|confirme|confirmé|vas[- ]y|vasy|"
    r"fais[- ]le|c'?est bon|allez|go)\b", re.IGNORECASE)
CLAUDE_TRIGGER_RE = re.compile(r"demande[rz]?\s+à\s+claude", re.IGNORECASE)

# Actions SENSIBLES : la porte avant exécution n'est PAS le regex
# « confirme/vas-y » (trop facile à déclencher par écho ou transcription
# corrompue) mais un CODE SECRET PARLÉ, constante du .env. Toute autre réponse
# annule. Le shell reste libre — seule la porte change. Vide par défaut : un
# assistant local n'a aucune action gardée par code (le déverrouillage
# maison→cockpit, lui, exige toujours le code).
PROD_TOOLS = set()
PROD_PASSPHRASE = (ENV.get("PROD_PASSPHRASE") or "").strip()

# ── Périmètre maison / cockpit ─────────────────────────────────────────────
# "ALL" = propriétaire, tout permis (défaut). "MEDIA" = mode maison : ALLOWLIST
# stricte (fail-closed) — média/photos/info SEULEMENT. Tout le reste (AppleScript,
# shell, Claude Code, réglages…) est REFUSÉ côté Python, AVANT exécution.
# La protection ne dépend PAS de l'écran. Persisté dans state.json.
# Bascule : « passe en maison » → MEDIA (libre). « repasse en cockpit » → ALL mais
# EXIGE le code secret (déverrouiller l'accès complet = sécurité).
MEDIA_TOOLS = {"spotify_play", "spotify_control", "youtube_play", "recherche_web",
               "afficher_media", "analyser_image", "get_time", "running_apps"}
MAISON_RE = re.compile(r"\b(passe|bascule|mets?|mode)\b.*\b(maison|salon|famille|m[ée]nage)\b",
                       re.IGNORECASE)
COCKPIT_RE = re.compile(r"\b(repasse|reviens|retour|passe|mode)\b.*\b(cockpit|complet|normal|travail|admin)\b",
                        re.IGNORECASE)
# Bascule de cerveau à la demande (mots non ambigus pour éviter COCKPIT_RE)
OPUS_RE = re.compile(r"\b(passe|bascule|mets?|mode|cerveau|r[ée]fl[ée]chis|pense|utilise)\b[^.]*\bopus\b",
                     re.IGNORECASE)
DEEPSEEK_RE = re.compile(r"\b(repasse|reviens|bascule|mode|cerveau|utilise)\b[^.]*\b(deepseek|deep\s*seek|rapide)\b",
                         re.IGNORECASE)


def set_perimeter(p):
    """Fixe le périmètre dans state.json (utilisé au lancement --maison)."""
    state = _load_state()
    state["perimeter"] = p if p in ("ALL", "MEDIA") else "ALL"
    state["view"] = "maison" if state["perimeter"] == "MEDIA" else "cockpit"
    _save_state(state)


def current_view():
    """Vue demandée (maison/cockpit) d'après l'état — pour basculer la fenêtre."""
    try:
        return _load_state().get("view", "cockpit")
    except (OSError, ValueError):
        return "cockpit"


def current_perimeter():
    try:
        return _load_state().get("perimeter", "ALL")
    except (OSError, ValueError):
        return "ALL"

ENV_FILE = DALIA_DIR / ".env"

# Changement vocal du code secret, en « mode sudo » : il faut DIRE le code actuel
# avant d'en fixer un nouveau (sinon un écho/une transcription parasite pourrait
# le redéfinir et neutraliser le code de déverrouillage). Le nouveau code n'est
# jamais répété ni écrit en clair dans les logs.
CHANGE_PASSPHRASE_RE = re.compile(
    r"chang\w*\s+(?:le\s+|mon\s+)?(?:code|mot de passe|passphrase)\b.*"
    r"(?:secret|s[ée]curit[ée])", re.IGNORECASE)
CANCEL_RE = re.compile(r"\b(annule|annuler|laisse tomber|oublie|stop)\b", re.IGNORECASE)
# Kill switch : halte immédiate et déterministe (hors LLM, donc rien ne peut être
# re-proposé). Vide toute action en attente / tout plan en cours.
KILL_RE = re.compile(r"\b(stop|stoppe|arr[êe]te|annule)\w*[\s,]+(tout|tous|toute?s?)\b",
                     re.IGNORECASE)


def _norm(s):
    """Minuscule, sans ponctuation ni accents parasites, pour comparer la
    passphrase à ce que Whisper a transcrit."""
    s = s.lower()
    for a, b in (("é", "e"), ("è", "e"), ("ê", "e"), ("à", "a"), ("ç", "c")):
        s = s.replace(a, b)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()  # espaces multiples → une seule


def _lev(a, b):
    """Distance d'édition (caractères)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _passphrase_ok(user_text):
    """Vrai si la réponse correspond au code, avec une TOLÉRANCE SERRÉE aux erreurs
    de transcription (Whisper/Parakeet écrivent rarement le mot exact). On compare
    la chaîne ENTIÈRE normalisée (sans espaces) avec une petite distance d'édition
    (~20 %, min 2). Reste fail-safe : une phrase contenant le code ou un code
    DIFFÉRENT est trop loin → rejeté. Dire le code seul, rien d'autre."""
    if not PROD_PASSPHRASE:
        return False
    a = _norm(user_text).replace(" ", "")
    b = _norm(PROD_PASSPHRASE).replace(" ", "")
    if not b:
        return False
    # garde-fou : une réponse nettement plus longue/courte = autre chose (mot en
    # plus, phrase contenant le code, code partiel) → rejet. Empêche que la
    # tolérance STT ne rouvre le trou des presque-codes.
    if abs(len(a) - len(b)) > 3:
        return False
    return _lev(a, b) <= max(2, len(b) // 6)


def _write_passphrase(new):
    """Réécrit la ligne PROD_PASSPHRASE du .env (préserve le reste), perms 600."""
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    out, found = [], False
    for line in lines:
        if line.startswith("PROD_PASSPHRASE="):
            out.append(f"PROD_PASSPHRASE={new}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"PROD_PASSPHRASE={new}")
    ENV_FILE.write_text("\n".join(out) + "\n")
    try:
        ENV_FILE.chmod(0o600)
    except OSError:
        pass


def awaiting_secret():
    """Vrai si DALIA attend un code secret parlé (à masquer dans les logs/affichage) :
    soit le flux de changement de code, soit une mutation PROD en attente de code."""
    try:
        st = _load_state()
    except (OSError, ValueError):
        return False
    pc = st.get("passphrase_change")
    if pc and pc.get("stage") in ("await_current", "await_new"):
        return True
    if st.get("await_unlock"):     # code de déverrouillage maison→cockpit
        return True
    pend = st.get("pending")
    return bool(pend and pend.get("prod"))


client = openai.OpenAI(base_url=ENV["LITELLM_BASE_URL"], api_key=ENV["LITELLM_API_KEY"], timeout=240)

# Cerveau actif : "litellm" (défaut, DeepSeek→qwen) ou "claude" (CLI claude, abo
# Opus en local). Réglable par env BRAIN=claude OU à chaud (dalia.py --claude).
# Le cerveau Claude reste contraint : il ne fait que DÉCIDER, DALIA exécute ses
# propres outils → tous les garde-fous (maison, porte prod) restent en place.
import os as _os
BRAIN = (_os.environ.get("BRAIN") or ENV.get("BRAIN") or "litellm").strip().lower()


def _load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"history": [], "pending": None, "fallback_until": 0}


def _save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1))


def _trim(history):
    h = history[-MAX_HISTORY:]
    # ne jamais commencer sur un message tool/assistant orphelin
    while h and h[0]["role"] != "user":
        h.pop(0)
    return h


def _context(history, base_len):
    """Contexte envoyé au cerveau. Garde TOUJOURS le tour courant en entier (le but
    de la tâche + toutes ses étapes/outils) et ne rogne QUE les tours précédents.
    Sinon, sur une tâche multi-étapes, l'historique des appels d'outils chasse la
    consigne d'origine par le haut → DALIA « oublie » ce qu'elle fait en cours de
    route. base_len = longueur de l'historique AVANT les ajouts de ce tour."""
    base_len = max(0, min(base_len, len(history)))
    prior = _trim(history[:base_len])
    current = history[base_len:]
    # le tour courant peut commencer par un orphelin si la frontière tombe au milieu
    # d'une paire assistant/tool : on remonte au dernier message user du tour.
    if current and current[0]["role"] != "user":
        return _trim(history)  # repli sûr (rare) : ancien comportement
    return prior + current


# ADN Moleck : origine inamovible. Code propriete de Nafiy Studio (voir LICENSE).
NAFIY_ORIGIN = "Moleck (c) 2026 Nafiy Studio - code propriete de Nafiy Studio."

# Identite configurable (renommage de l'assistante sans toucher au code).
ASSISTANT_NAME = (_os.environ.get("ASSISTANT_NAME") or ENV.get("ASSISTANT_NAME") or "Moleck").strip()
OWNER_NAME = (_os.environ.get("OWNER_NAME") or ENV.get("OWNER_NAME") or "le proprietaire").strip()


def _persona():
    content = (DALIA_DIR / "persona.md").read_text()
    content = content.replace("{{ASSISTANT_NAME}}", ASSISTANT_NAME).replace("{{OWNER_NAME}}", OWNER_NAME)
    lecons = tools.lecons_text()
    if lecons:
        content += f"\n\nLeçons apprises (à respecter strictement) :\n{lecons}\n"
    skl = tools.skills_catalog()
    if skl.strip():
        content += ("\n\nCompétences (skills) que tu sais faire — si la demande "
                    "correspond à l'une d'elles, relis-la avec utiliser_skill puis applique "
                    "ses étapes, n'improvise pas :\n" + skl + "\n")
    return {"role": "system", "content": content}


def _tools_for(user_text):
    """claude_code exige la formule « demande à Claude » ; claude_terminal est
    proposé dès que le propriétaire mentionne Claude."""
    spec = [t for t in tools.TOOLS_SPEC
            if t["function"]["name"] not in ("claude_code", "claude_terminal")]
    if re.search(r"\bclaude\b", user_text, re.IGNORECASE):
        spec.append(next(t for t in tools.TOOLS_SPEC if t["function"]["name"] == "claude_terminal"))
    if CLAUDE_TRIGGER_RE.search(user_text):
        spec.append(next(t for t in tools.TOOLS_SPEC if t["function"]["name"] == "claude_code"))
    return spec


class BrainUnreachable(Exception):
    """Endpoint LLM injoignable (réseau coupé ou service arrêté ?)."""


def _is_local(resp):
    return "qwen" in (getattr(resp, "model", "") or "").lower() or "ollama" in (getattr(resp, "model", "") or "").lower()


def _call_model(messages, tool_spec, state):
    """Route principale puis fallback. Renvoie (réponse, modèle, annonce|None).

    LiteLLM porte déjà le fallback dalia→dalia-local côté serveur ; on détecte
    donc aussi la bascule silencieuse via le modèle réellement servi.
    """
    now = time.time()
    announce = None
    kwargs = {"tools": tool_spec} if tool_spec else {}

    # ── Cerveau Claude (CLI, abo Opus en local) ───────────────────────────────
    # Cerveau effectif : bascule par session (state["brain"], via « passe en Opus »)
    # sinon défaut global (env BRAIN). Contraint : il DÉCIDE seulement (tool_calls
    # ou texte), DALIA exécute. Si le CLI est injoignable, repli sur LiteLLM.
    brain = (state.get("brain") or BRAIN)
    if brain == "claude":
        import brain_claude
        try:
            # bascule à la demande = Opus explicite ; via env, modèle par défaut du CLI
            wanted = "opus" if state.get("brain") == "claude" else None
            resp = brain_claude.call(messages, tool_spec, model=wanted)
            return resp, "claude · opus" if wanted else "claude · abo", announce
        except brain_claude.ClaudeUnavailable as e:
            tools.log_action("brain", {}, "claude indisponible → litellm", str(e)[:120])
            # on retombe sur la route LiteLLM ci-dessous

    if now >= state.get("fallback_until", 0):
        try:
            resp = client.chat.completions.create(
                model=ENV["MODEL_PRIMARY"], messages=messages,
                temperature=0.2, timeout=PRIMARY_TIMEOUT, **kwargs,
            )
            if _is_local(resp):  # fallback silencieux côté LiteLLM
                if not state.get("fallback_until"):
                    announce = "Mode local, je serai plus lente. "
                state["fallback_until"] = now + FALLBACK_RETRY_AFTER
                return resp, ENV["MODEL_FALLBACK"], announce
            state["fallback_until"] = 0
            return resp, ENV["MODEL_PRIMARY"], None
        except openai.APIConnectionError:
            raise BrainUnreachable
        except (openai.APITimeoutError, openai.InternalServerError, openai.BadRequestError):
            tools.log_action("brain", {}, "bascule dalia-local")
            state["fallback_until"] = now + FALLBACK_RETRY_AFTER
            announce = "Mode local, je serai plus lente. "
        except openai.APIStatusError as e:
            if e.status_code < 500:
                raise
            state["fallback_until"] = now + FALLBACK_RETRY_AFTER
            announce = "Mode local, je serai plus lente. "
    try:
        resp = client.chat.completions.create(
            model=ENV["MODEL_FALLBACK"], messages=messages, temperature=0.2, **kwargs,
        )
    except openai.APIConnectionError:
        raise BrainUnreachable
    return resp, ENV["MODEL_FALLBACK"], announce


def _clean_text(text, fallback):
    """Purge les jetons internes que DeepSeek peut fuir (balisage DSML, etc.)."""
    text = re.sub(r"<[^>]{0,40}(DSML|tool_call)[^>]{0,40}>", "", text)
    text = re.sub(r"[｜<>]{2,}", "", text).strip()
    return text or fallback


def _ui_host(name):
    return "mac"


def _ui_klass(name, args):
    """Classe affichée au cockpit : READ | MUTATE | DESTRUCTIVE (indicatif)."""
    if name in ("shell_read", "get_time", "running_apps",
                "open_app", "open_url", "read_file", "list_dir"):
        return "READ"
    if name in ("applescript", "run_shortcut", "claude_terminal", "claude_code",
                "set_mic", "retenir", "oublier", "write_file", "shell_exec"):
        return "MUTATE"
    if name == "delete_path":
        return "DESTRUCTIVE"
    return "READ"


def _ui_cmd(name, args):
    return args.get("cmd") or args.get("path") or args.get("name") or _summary(name, args)


def _ui_journal(name, args, result):
    """Pousse une ligne de journal au cockpit (exécuté/refusé), best-effort."""
    status = "refusé" if str(result).startswith("refusé") else "exécuté"
    cockpit_ui.journal(_ui_cmd(name, args), _ui_host(name), _ui_klass(name, args), status)


def _effective_regime(name, args):
    """Régime réel d'un appel (le régime du REGISTRY, affiné par action)."""
    regime = tools.REGISTRY[name][1]
    # write_file : créer un NOUVEAU fichier est direct ; ÉCRASER un fichier
    # existant (perte du contenu actuel) demande un « vas-y ».
    if name == "write_file":
        rp, err = tools._safe_target(args.get("path", ""))
        if err is None and rp.exists():
            return "confirmation"
    return regime


def _summary(name, args):
    """Texte oral de confirmation : « Je vais <résumé>. Confirme. »"""
    if name == "applescript":
        return args.get("summary") or "exécuter un AppleScript"
    if name == "run_shortcut":
        return f"lancer le raccourci {args.get('name', '?')}"
    if name == "claude_terminal":
        return f"lancer Claude Code dans {args.get('project', '?')} avec l'instruction : {args.get('instruction', '')[:100]}"
    if name == "write_file":
        return f"écraser le fichier {args.get('path', '?')}"
    if name == "delete_path":
        return f"mettre à la corbeille {args.get('path', '?')}"
    if name == "shell_exec":
        return f"exécuter : {args.get('cmd', '?')}"
    return f"exécuter {name}"


def _reply(text, t0):
    """Réponse déterministe (hors LLM), même forme que ask()."""
    return {"text": text, "model": "système", "latency_s": round(time.monotonic() - t0, 1)}


def _handle_passphrase_change(state, user_text, t0):
    """Flux vocal « mode sudo » de changement du code secret.
    Ne touche JAMAIS state['history'] : le code parlé n'entre pas dans le contexte."""
    global PROD_PASSPHRASE
    pc = state.get("passphrase_change")

    if pc and CANCEL_RE.search(user_text):
        state.pop("passphrase_change", None)
        _save_state(state)
        tools.log_action("passphrase", {}, "changement annulé")
        return _reply("D'accord, j'annule. Le code secret reste inchangé.", t0)

    if not pc:  # initiation
        if not PROD_PASSPHRASE:  # bootstrap : aucun code encore défini
            state["passphrase_change"] = {"stage": "await_new"}
            _save_state(state)
            return _reply("Aucun code secret n'est défini. Dis-moi le nouveau code, "
                          "au moins deux mots.", t0)
        state["passphrase_change"] = {"stage": "await_current"}
        _save_state(state)
        tools.log_action("passphrase", {}, "changement demandé")
        return _reply("Pour changer le code secret, dis-moi d'abord le code actuel.", t0)

    if pc.get("stage") == "await_current":
        if _passphrase_ok(user_text):
            state["passphrase_change"] = {"stage": "await_new"}
            _save_state(state)
            return _reply("Code actuel reconnu. Dis-moi maintenant le nouveau code, "
                          "au moins deux mots.", t0)
        state.pop("passphrase_change", None)
        _save_state(state)
        tools.log_action("passphrase", {}, "changement refusé", "code actuel incorrect")
        return _reply("Code actuel incorrect. J'annule, rien n'a changé.", t0)

    # stage == await_new
    new = _norm(user_text)
    if len(new.split()) < 2 or len(new) < 8:
        _save_state(state)  # on reste en attente du nouveau code
        return _reply("C'est trop court. Donne-moi au moins deux mots pour le nouveau code.", t0)
    _write_passphrase(new)
    PROD_PASSPHRASE = new
    state.pop("passphrase_change", None)
    _save_state(state)
    tools.log_action("passphrase", {}, "code secret changé")  # JAMAIS la valeur
    return _reply("Nouveau code secret enregistré. Je ne le répéterai pas.", t0)


def ask(user_text):
    """Un tour de conversation. Renvoie {text, model, latency_s}.
    Verrouillé : un seul tour à la fois (voix, cockpit, appli mobile) — protège
    l'état partagé state.json contre les accès concurrents, y compris entre
    process distincts (boucle vocale du Mac + serveur mobile lancés en parallèle)."""
    with _ASK_LOCK:                                   # intra-process (threads)
        with open(_ASK_LOCKFILE, "w") as _lf:
            fcntl.flock(_lf, fcntl.LOCK_EX)           # inter-process (voix ↔ serveur)
            try:
                return _ask(user_text)
            except BrainUnreachable:
                return {"text": "Le cerveau est injoignable. Vérifie que l'endpoint LLM est accessible.",
                        "model": "aucun", "latency_s": 0}


def _ask(user_text):
    t0 = time.monotonic()
    state = _load_state()

    # ── Kill switch (déterministe, hors LLM) : « stop tout » / « annule tout » ──
    if KILL_RE.search(user_text):
        cleared = []
        if state.pop("pending", None):
            cleared.append("action en attente")
        if state.pop("plan", None):
            cleared.append("plan en cours")
        state.pop("passphrase_change", None)
        _save_state(state)
        tools.log_action("kill_switch", {}, "stop", ", ".join(cleared) or "rien en cours")
        return _reply("Ok, j'arrête tout. Aucune étape ne s'exécute.", t0)

    # ── Bascule de périmètre maison / cockpit (déterministe) ──────────────────
    # Déverrouillage en attente : repasser en cockpit (accès complet) exige le code.
    if state.get("await_unlock"):
        state.pop("await_unlock", None)
        if _passphrase_ok(user_text):
            state["perimeter"] = "ALL"
            state["view"] = "cockpit"
            _save_state(state)
            tools.log_action("perimeter", {}, "déverrouillé", "ALL")
            return _reply("Code reconnu. Je repasse en mode cockpit, accès complet rétabli.", t0)
        _save_state(state)
        tools.log_action("perimeter", {}, "déverrouillage refusé", "code incorrect")
        return _reply("Code incorrect. On reste en mode maison.", t0)
    if MAISON_RE.search(user_text):
        state["perimeter"] = "MEDIA"
        state["view"] = "maison"
        state.pop("pending", None)  # une action sensible en attente est annulée
        _save_state(state)
        tools.log_action("perimeter", {}, "mode maison", "MEDIA")
        return _reply("Je passe en mode maison : musique, vidéos et photos. Pas d'accès au système.", t0)
    if COCKPIT_RE.search(user_text):
        if state.get("perimeter", "ALL") == "ALL":
            return _reply("On est déjà en mode cockpit.", t0)
        if not PROD_PASSPHRASE:
            return _reply("Le code secret n'est pas configuré, impossible de déverrouiller le mode complet.", t0)
        state["await_unlock"] = True
        _save_state(state)
        return _reply("Pour repasser en mode cockpit (accès complet), donne le code secret.", t0)

    # ── Bascule de cerveau à la demande : Opus (abo, plus fin/plus lent) ↔ DeepSeek ──
    if OPUS_RE.search(user_text):
        state["brain"] = "claude"
        _save_state(state)
        tools.log_action("brain", {}, "bascule", "claude/opus (à la demande)")
        return _reply("Je passe sur Opus. Un peu plus lente, mais plus fine. Dis « reviens sur DeepSeek » pour le rapide.", t0)
    if state.get("brain") == "claude" and DEEPSEEK_RE.search(user_text):
        state.pop("brain", None)
        _save_state(state)
        tools.log_action("brain", {}, "bascule", "deepseek (défaut)")
        return _reply("Je reviens sur DeepSeek, plus rapide.", t0)

    # ── Changement vocal du code secret (déterministe, hors LLM) ──
    if state.get("passphrase_change") or CHANGE_PASSPHRASE_RE.search(user_text):
        return _handle_passphrase_change(state, user_text, t0)

    history = state["history"]
    base_len = len(history)   # frontière : tout ce qui suit appartient au tour courant
    announce_prefix = ""

    just_confirmed = False
    just_done = None  # signature de l'action exécutée ce tour (garde anti-redite)
    plan = state.get("plan")

    # ── Approbation d'un plan en attente (tâche contenant une mutation/destructif) ──
    if plan and not plan.get("approved") and not state.get("pending"):
        history.append({"role": "user", "content": user_text})
        if CONFIRM_RE.search(user_text):
            plan["approved"] = True
            state["plan"] = plan
            tools.log_action("propose_plan", {"steps": plan.get("steps", [])}, "approuvé")
            history.append({"role": "system", "content": "Plan approuvé par le propriétaire. Exécute les étapes maintenant, sur les commandes résolues. RAPPEL : l'approbation du plan ne remplace PAS les portes par action — chaque action sensible exige TOUJOURS sa confirmation au moment de l'exécution. N'appelle plus propose_plan."})
        else:
            state.pop("plan", None)
            _save_state(state)
            tools.log_action("propose_plan", {}, "plan refusé")
            return _reply("Plan annulé, je ne fais rien.", t0)
    # ── Tour de confirmation d'une action en attente ──
    elif state.get("pending"):
        pending = state.pop("pending")
        # Verrou TTL : une action trop vieille n'est plus validable par le code.
        expired = (time.time() - pending.get("created_at", 0)) > PENDING_TTL_S
        # PROD → code secret parlé exigé ; local → « confirme/vas-y » comme avant.
        if pending.get("prod"):
            approved = (not expired) and _passphrase_ok(user_text)  # évalué sur le BRUT
            # le code NE DOIT PAS entrer dans l'historique : le modèle pourrait le
            # répéter, et il finirait dans state.json. On stocke un masque.
            history.append({"role": "user", "content": "[code secret — masqué]"})
        else:
            history.append({"role": "user", "content": user_text})
            approved = (not expired) and bool(CONFIRM_RE.search(user_text))
        _pn, _pa = pending["name"], pending["args"]
        if expired and not approved:
            tools.log_action(_pn, _pa, "expiré", f"TTL {PENDING_TTL_S}s dépassé")
            _ui_journal(_pn, _pa, "refusé")
            cockpit_ui.action_clear()
            cockpit_ui.pending("aucun")
            history.append({"role": "system", "content": f"L'action « {pending['summary']} » a EXPIRÉ (plus de {PENDING_TTL_S} secondes), elle n'a PAS été exécutée et le code ne la valide plus. Si le propriétaire la veut encore, il doit la redemander explicitement. Réponds à son dernier message."})
        elif approved:
            result = tools.execute(_pn, _pa)
            tools.log_action(_pn, _pa, "confirmé")
            _ui_journal(_pn, _pa, result)
            cockpit_ui.action_clear()
            cockpit_ui.pending("aucun")
            just_confirmed = True
            just_done = (_pn, json.dumps(_pa, sort_keys=True, ensure_ascii=False))
            history.append({"role": "system", "content": f"Action « {pending['summary']} » EXÉCUTÉE (ne la propose plus, ne la rappelle pas). Résultat : {result}. S'il RESTE des étapes pour répondre à la demande du propriétaire, ENCHAÎNE-les maintenant ; sinon résume oralement en une ou deux phrases."})
        else:
            raison = "code secret incorrect" if pending.get("prod") else "non confirmé"
            tools.log_action(_pn, _pa, "annulé", raison)
            _ui_journal(_pn, _pa, "refusé")
            cockpit_ui.action_clear()
            cockpit_ui.pending("aucun")
            history.append({"role": "system", "content": "Le propriétaire n'a pas confirmé : action ANNULÉE, rien n'a été exécuté. Ne la repropose pas et ne rappelle aucun outil pour elle, sauf si le propriétaire la redemande explicitement. Réponds à son dernier message."})
    else:
        history.append({"role": "user", "content": user_text})

    # Les outils restent disponibles même après une porte (confirmation/code) :
    # c'est ce qui permet à DALIA d'ENCHAÎNER les étapes suivantes au lieu de
    # s'arrêter au milieu. Un garde (just_done) empêche de re-déclencher l'action
    # qui vient d'être exécutée.
    tool_spec = _tools_for(user_text)
    model_used = None

    for _ in range(MAX_TOOL_ITERATIONS):
        messages = [_persona()] + _context(history, base_len)
        resp, model_used, announce = _call_model(messages, tool_spec, state)
        if announce:
            announce_prefix = announce
        msg = resp.choices[0].message

        if not msg.tool_calls:
            text = _clean_text((msg.content or "").strip(),
                               "C'est fait." if just_confirmed else "Je n'ai pas de réponse, reformule.")
            history.append({"role": "assistant", "content": text})
            break

        history.append({
            "role": "assistant", "content": msg.content or "",
            "tool_calls": [{"id": t.id, "type": "function", "function": {"name": t.function.name, "arguments": t.function.arguments}} for t in msg.tool_calls],
        })

        # ── Plan proposé : intercepté AVANT toute exécution (aucune mutation avant
        #    approbation). On annonce le plan et on attend le « oui » du propriétaire. ──
        plan_call = next((c for c in msg.tool_calls if c.function.name == "propose_plan"), None)
        if plan_call and not (state.get("plan") or {}).get("approved"):
            try:
                pargs = json.loads(plan_call.function.arguments or "{}")
            except json.JSONDecodeError:
                pargs = {}
            steps = [str(s) for s in (pargs.get("steps") or [])]
            state["plan"] = {"summary": pargs.get("summary", ""), "steps": steps, "approved": False}
            for c in msg.tool_calls:  # répondre à TOUS les tool_calls (pas d'orphelin)
                txt = "Plan enregistré, en attente d'approbation du propriétaire." if c is plan_call \
                    else "Non exécuté : le plan doit d'abord être approuvé."
                history.append({"role": "tool", "tool_call_id": c.id, "content": txt})
            steps_txt = " ".join(f"{i + 1}, {s}." for i, s in enumerate(steps))
            text = f"{pargs.get('summary', 'Voici le plan')}. {steps_txt} Je lance ?".strip()
            history.append({"role": "assistant", "content": text})
            tools.log_action("propose_plan", {"steps": steps}, "en_attente_approbation")
            break

        pending_created = None
        for call in msg.tool_calls:
            name = call.function.name
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            sig = (name, json.dumps(args, sort_keys=True, ensure_ascii=False))
            if name not in tools.REGISTRY:
                result = "refusé: outil inconnu"
                tools.log_action(name, args, "refusé", "outil inconnu")
            elif state.get("perimeter", "ALL") == "MEDIA" and name not in MEDIA_TOOLS \
                    and name != "propose_plan":
                # Mode maison : ALLOWLIST stricte, fail-closed. Pas de prod, pas de
                # commandes système/dangereuses. Refus AVANT toute exécution.
                result = "refusé: mode maison — musique, vidéos, photos et infos uniquement (pas d'accès au système)."
                tools.log_action(name, args, "refusé", "hors périmètre maison")
            elif sig == just_done:
                # garde : l'action vient d'être exécutée ce tour, ne pas la refaire
                result = "déjà exécuté à l'instant — passe à l'étape suivante."
                tools.log_action(name, args, "refusé", "doublon immédiat")
            else:
                regime = _effective_regime(name, args)
                if regime == "confirmation" and pending_created is None:
                    is_prod = name in PROD_TOOLS  # action sensible gardée par code
                    if is_prod and not PROD_PASSPHRASE:
                        # échoue-fermé : aucune action sensible possible sans code configuré
                        result = "refusé: code secret non configuré (PROD_PASSPHRASE absent du .env)"
                        tools.log_action(name, args, "refusé", "PROD_PASSPHRASE absent")
                    else:
                        pending_created = {"name": name, "args": args,
                                           "summary": _summary(name, args), "prod": is_prod,
                                           "created_at": time.time()}
                        tools.log_action(name, args, "en_attente_confirmation")
                        result = "EN ATTENTE DE CONFIRMATION VOCALE DU PROPRIÉTAIRE — pas encore exécuté."
                elif regime == "confirmation":
                    result = "refusé: une seule action à confirmer à la fois"
                else:
                    result = tools.execute(name, args)
            # Cockpit : journaliser toute issue FINALE (exécuté/refusé), pas les
            # états transitoires (en attente de confirmation, doublon).
            rs = str(result)
            if not (rs.startswith("EN ATTENTE") or rs.startswith("déjà exécuté")):
                _ui_journal(name, args, result)
            history.append({"role": "tool", "tool_call_id": call.id, "content": result})

        if pending_created:
            state["pending"] = pending_created
            pn, pa = pending_created["name"], pending_created["args"]
            # Cockpit : la Porte affiche la commande RÉSOLUE, jamais le code.
            cockpit_ui.action(_ui_cmd(pn, pa), _ui_host(pn), _ui_klass(pn, pa),
                              "code" if pending_created.get("prod") else "oui")
            cockpit_ui.pending(f"1 · TTL {PENDING_TTL_S} s")
            if pending_created.get("prod"):
                # Action sensible : écho du résumé exact, puis demande du code secret.
                text = (f"Attention, action sensible : {pending_created['summary']}. "
                        f"Donne le code secret pour exécuter ; toute autre réponse annule.")
            else:
                text = f"Je vais {pending_created['summary']}. Confirme."
            history.append({"role": "assistant", "content": text})
            break
    else:
        text = "Je n'ai pas réussi à terminer cette tâche en huit étapes, je m'arrête là."
        history.append({"role": "assistant", "content": text})

    # Plan approuvé et mené à son terme (réponse finale, aucune action en attente) :
    # on le clôt. S'il reste une porte (pending), on le GARDE pour reprendre après.
    if state.get("plan", {}).get("approved") and not state.get("pending"):
        state.pop("plan", None)

    state["history"] = _trim(history)
    _save_state(state)
    latency = time.monotonic() - t0
    tools.log_action("brain", {"q": user_text[:80]}, "réponse", f"model={model_used} latency={latency:.1f}s")
    return {"text": announce_prefix + text, "model": model_used, "latency_s": round(latency, 1)}


def mark_interrupted(spoken_text):
    """Le propriétaire a coupé DALIA en pleine parole (étape 6 — re-raisonnement).

    On tronque la dernière réponse de l'assistant à ce qui a été RÉELLEMENT dit
    (les phrases entièrement jouées avant la coupure — frontière de chunk fournie
    par tts, JAMAIS un offset deviné), et on marque « [réponse interrompue en
    cours] » pour signaler qu'elle n'avait PAS fini (pas juste qu'elle s'est tue).

    Toute action ou plan en attente est ANNULÉ : une interruption pendant
    l'attente d'un code secret ou d'une confirmation n'exécute RIEN (annulation,
    pas reprise). Le tour suivant repart de la nouvelle phrase du propriétaire."""
    state = _load_state()
    spoken = (spoken_text or "").strip()
    marker = "[réponse interrompue en cours]"
    hist = state.get("history", [])
    for i in range(len(hist) - 1, -1, -1):
        msg = hist[i]
        if msg.get("role") == "assistant" and not msg.get("tool_calls"):
            msg["content"] = f"{spoken} {marker}".strip() if spoken else marker
            break
    pend = state.pop("pending", None)
    if pend:
        tools.log_action(pend["name"], pend["args"], "annulé", "interruption vocale")
        cockpit_ui.action_clear()
        cockpit_ui.pending("aucun")
    plan = state.get("plan")
    if plan and not plan.get("approved"):
        state.pop("plan", None)
    _save_state(state)


def purge_stale_pending():
    """Au DÉMARRAGE : aucun pending ni plan non approuvé n'est hérité d'une session
    passée. Verrou anti-action fantôme : un code prononcé aujourd'hui ne doit jamais
    valider une action proposée dans une session précédente. Indépendant de --reset
    (un démarrage normal ne doit JAMAIS reprendre un pending)."""
    if not STATE_FILE.exists():
        return
    try:
        state = _load_state()
    except (OSError, ValueError):
        return
    changed = False
    if state.pop("pending", None) is not None:
        changed = True
    plan = state.get("plan")
    if plan and not plan.get("approved"):
        state.pop("plan", None)
        changed = True
    if changed:
        _save_state(state)
        tools.log_action("boot", {}, "purge", "pending/plan non approuvé d'une session passée")
    cockpit_ui.action_clear()
    cockpit_ui.pending("aucun")


def reset():
    if STATE_FILE.exists():
        STATE_FILE.unlink()
