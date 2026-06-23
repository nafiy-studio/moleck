# (c) 2026 Nafiy Studio - Moleck. Code propriete de Nafiy Studio.
# Mention de propriete a conserver (voir LICENSE). Ne pas retirer : ADN du projet.
"""Outils Dalia : définitions OpenAI function calling + exécution + garde-fous.

Trois régimes (spec §5) :
- libre : exécution directe
- confirmation : l'orchestrateur exige "confirme"/"vas-y" au tour suivant
- interdits durs (§6.3) : refus, AUCUNE confirmation possible
"""
import datetime
import json
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path

import media
import memory
import skills

DALIA_DIR = Path(__file__).parent
LOG_FILE = DALIA_DIR / "dalia.log"
ENV_FILE = DALIA_DIR / ".env"

# ── Interdits absolus (§6.3) : matchés sur la commande complète ──
HARD_DENY_PATTERNS = [
    (r"(^|[;&|]\s*)rm(\s|$)", "rm"),
    (r"\bsudo\b", "sudo"),
    (r"\bmv\s+\S+\s+/dev/null", "mv destructif"),
    (r"\bkill(all)?\b", "kill"),
    (r"\b(shutdown|reboot|halt|poweroff)\b", "shutdown"),
    (r"\b(curl|wget)\b", "curl/wget en shell"),
    (r"\b(drop|truncate)\s+(table|database|schema)\b", "drop/truncate de base"),
    (r"\b(ufw|fail2ban|iptables|nft)\b", "modification firewall"),
    (r"\b(useradd|usermod|userdel|passwd|adduser|deluser)\b", "gestion d'utilisateurs"),
    (r"git\s+push\s+(-f\b|--force)", "git push --force"),
]

APPLESCRIPT_DENY = re.compile(r"delete|empty trash|send message|keystroke", re.IGNORECASE)

# Redirections > / >> autorisées uniquement vers ~/dalia/
REDIR_RE = re.compile(r">>?\s*([^\s;|&]+)")

WHITELIST_SHELL_READ = {
    "ls": None, "pwd": None, "df": "-h", "uptime": None, "ps": "aux",
    "git": ("status", "log"), "cat": "DALIA_ONLY",
}


def log_action(tool, args, status, extra=""):
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    with open(LOG_FILE, "a") as f:
        f.write(f"{ts} | {tool} | {json.dumps(args, ensure_ascii=False)} | {status}{' | ' + extra if extra else ''}\n")


def check_hard_deny(cmd: str):
    """Renvoie le motif d'interdiction, ou None si la commande est admissible."""
    for pattern, label in HARD_DENY_PATTERNS:
        if re.search(pattern, cmd, re.IGNORECASE):
            return label
    for m in REDIR_RE.finditer(cmd):
        target = m.group(1)
        expanded = str(Path(target.replace("~", str(Path.home()))).resolve()) if not target.startswith("/dev/") else target
        if target.startswith("/dev/null"):
            continue
        if not expanded.startswith(str(DALIA_DIR)):
            return f"redirection hors ~/dalia ({target})"
    return None


def _run(argv, timeout=30):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        out = (r.stdout + r.stderr).strip()
        return out[:4000] if out else f"(ok, code {r.returncode})"
    except subprocess.TimeoutExpired:
        return "erreur: délai dépassé"


# ── Exécuteurs ──────────────────────────────────────────────────────────────

def get_time():
    now = datetime.datetime.now()
    jours = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
    mois = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
            "août", "septembre", "octobre", "novembre", "décembre"]
    return f"{jours[now.weekday()]} {now.day} {mois[now.month-1]} {now.year}, {now.hour} heures {now.minute:02d}"


# ── Portabilite OS (macOS, Linux, Windows) ──────────────────────────────────
import os as _os
import platform as _platform
PLATFORM = _platform.system()          # 'Darwin' | 'Linux' | 'Windows'
IS_MAC = PLATFORM == "Darwin"
IS_WIN = PLATFORM == "Windows"
IS_LINUX = PLATFORM == "Linux"


def open_app(name):
    if IS_MAC:
        return _run(["open", "-a", name])
    if IS_WIN:
        return _run(["cmd", "/c", "start", "", name])
    # Linux : commande directe si dans le PATH, sinon gtk-launch (.desktop)
    if shutil.which(name):
        return _run([name])
    return _run(["gtk-launch", name]) if shutil.which("gtk-launch") else \
        f"refusé: lancement d'app non supporté sur {PLATFORM} pour « {name} »"


def running_apps():
    if IS_MAC:
        out = _run(["osascript", "-e",
                    'tell application "System Events" to get name of (processes where background only is false)'])
    elif IS_WIN:
        out = _run(["tasklist", "/fo", "csv", "/nh"])
    else:
        out = _run(["ps", "-e", "-o", "comm="])
    return f"applications ouvertes : {out}"


def open_url(url):
    if not re.match(r"^https?://", url):
        return "refusé: seules les URL http/https sont autorisées"
    import webbrowser
    try:
        return "(ouvert)" if webbrowser.open(url) else "erreur: navigateur introuvable"
    except Exception as e:
        return f"erreur: {type(e).__name__}"


def applescript(script, summary=""):
    if not IS_MAC:
        return f"refusé: AppleScript n'existe que sur macOS (OS detecte : {PLATFORM})"
    if APPLESCRIPT_DENY.search(script):
        return "refusé: cette action n'est pas autorisée"
    return _run(["osascript", "-e", script])


def run_shortcut(name):
    if not IS_MAC:
        return f"refusé: les raccourcis Shortcuts n'existent que sur macOS (OS : {PLATFORM})"
    return _run(["shortcuts", "run", name], timeout=60)


def shell_read(cmd):
    parts = shlex.split(cmd)
    if not parts:
        return "refusé: commande vide"
    # Pas de vrai shell ici : on coupe au premier opérateur (| && ; 2> ...)
    for i, p in enumerate(parts):
        if p in ("|", "||", "&&", ";") or re.match(r"^\d?>>?$", p) or p.startswith(">"):
            parts = parts[:i]
            break
    parts = [str(Path(p).expanduser()) if p.startswith("~") else p for p in parts]
    base = parts[0]
    rule = WHITELIST_SHELL_READ.get(base, "MISSING")
    if rule == "MISSING":
        return "refusé: cette action n'est pas autorisée"
    if base == "git" and (len(parts) < 2 or parts[1] not in rule):
        return "refusé: seuls git status et git log sont autorisés"
    if base == "cat":
        target = str(Path(parts[1].replace("~", str(Path.home()))).resolve()) if len(parts) > 1 else ""
        if not target.startswith(str(DALIA_DIR)):
            return "refusé: cat est limité à ~/dalia/"
    if base in ("df", "ps") and rule and len(parts) > 1 and parts[1] != rule:
        return "refusé: cette action n'est pas autorisée"
    return _run(parts)


PROJECT_ROOTS = [Path.home() / "dev", Path.home() / "dalia"]


def _find_project(name):
    """Trouve un dossier projet par nom (insensible à la casse), profondeur 2."""
    name_low = name.lower().replace(" ", "").replace("-", "").replace("_", "")
    candidates = []
    for root in PROJECT_ROOTS:
        if not root.is_dir():
            continue
        if root.name.lower() == name_low:
            return root
        for d in root.iterdir():
            if d.is_dir() and not d.name.startswith("."):
                if d.name.lower().replace("-", "").replace("_", "") == name_low:
                    return d
                candidates.append(d)
                for sub in d.iterdir() if d.is_dir() else []:
                    if sub.is_dir() and sub.name.lower().replace("-", "").replace("_", "") == name_low:
                        return sub
    return None


def claude_terminal(project, instruction=""):
    """Ouvre Claude Code dans un Terminal sur le projet, avec l'instruction."""
    path = _find_project(project)
    if path is None:
        dispo = sorted({d.name for r in PROJECT_ROOTS if r.is_dir()
                        for d in r.iterdir() if d.is_dir() and not d.name.startswith(".")})
        return f"projet introuvable. Projets disponibles : {', '.join(dispo)}"
    shell_cmd = f"cd {shlex.quote(str(path))} && claude {shlex.quote(instruction)}" if instruction \
        else f"cd {shlex.quote(str(path))} && claude"
    as_script = shell_cmd.replace("\\", "\\\\").replace('"', '\\"')
    out = _run(["osascript", "-e",
                f'tell application "Terminal"\nactivate\ndo script "{as_script}"\nend tell'])
    if "error" in out.lower():
        return f"erreur d'ouverture du terminal : {out[:200]}"
    return f"Claude Code lancé dans {path.name} avec l'instruction transmise"


def retenir(lecon):
    """Mémorise une leçon durable (correction, préférence) dans le vault."""
    return memory.note("lecon", lecon)


def oublier(motif):
    """Supprime du vault les notes (toutes catégories) contenant le motif."""
    return memory.forget(motif)


def memoriser(categorie, note):
    """Note durable dans le vault : un fait sur le propriétaire, un contexte projet, une leçon."""
    return memory.note(categorie, note)


def resumer_session(resume):
    """Enregistre un résumé daté de la conversation (mémoire épisodique)."""
    return memory.summarize_session(resume)


def lecons_text():
    """Bloc mémoire (faits + projets + leçons + dernière session) injecté au persona."""
    return memory.load_context()


# ── Skills (compétences réutilisables, autodidacte) ─────────────────────────

def creer_skill(nom, quand="", etapes=None):
    """Enregistre une procédure réutilisable (recette) dans le vault."""
    return skills.create(nom, quand, etapes)


def utiliser_skill(nom):
    """Relit les étapes d'un skill pour l'appliquer (via les outils existants)."""
    return skills.get(nom)


def lister_skills():
    """Liste les skills disponibles (nom + quand)."""
    return skills.list_text()


def oublier_skill(nom):
    """Supprime un skill du vault."""
    return skills.delete(nom)


def skills_catalog():
    """Catalogue compact injecté au persona (pour que le cerveau sache quoi faire)."""
    return skills.catalog()


def etat_projets(_=None):
    """Lit le dernier digest d'entretien des projets (généré chaque jour à 9 h)."""
    f = Path.home() / "dalia" / "maintenance_report.md"
    if not f.exists():
        return "Pas encore de point d'entretien (le premier tourne demain à 9 h, ou lance-le à la main)."
    return f.read_text()[:3000]


def propose_plan(summary="", steps=None):
    """Intercepté par l'orchestrateur quand un plan n'est pas encore approuvé.
    Ce corps ne sert que si appelé alors qu'un plan est déjà en cours."""
    return "Plan déjà en cours, exécute les étapes."


def _set_env_var(key, value):
    """Met à jour (ou ajoute) une ligne KEY=value dans le .env, préserve le reste."""
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    out, found = [], False
    for line in lines:
        if line.startswith(f"{key}="):
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(out) + "\n")


def set_mic(target):
    """Change le micro d'entrée de Dalia : écrit STT_INPUT_DEVICE dans le .env ET
    l'applique à chaud (prochain tour d'écoute, sans redémarrage).
    target : 'casque'/'externe'/'usb' (entrée non intégrée), 'macbook'/'intégré'
    (micro interne), ou un index numérique."""
    import sounddevice as sd
    inputs = [(i, d["name"]) for i, d in enumerate(sd.query_devices())
              if d["max_input_channels"] > 0]
    if not inputs:
        return "refusé: aucun micro détecté"
    t = str(target).strip().lower()
    chosen = None
    if t.lstrip("-").isdigit():
        chosen = int(t)
        if chosen not in dict(inputs):
            dispo = ", ".join(f"{i} ({n})" for i, n in inputs)
            return f"refusé: index {chosen} introuvable. Micros : {dispo}"
    elif any(k in t for k in ("macbook", "intégr", "integr", "interne", "built", "mac")):
        chosen = next((i for i, n in inputs if "macbook" in n.lower()), None)
    else:  # casque / externe / jack / usb / bluetooth / écouteurs
        chosen = next((i for i, n in inputs if "macbook" not in n.lower()), None)
    if chosen is None:
        dispo = ", ".join(f"{i} ({n})" for i, n in inputs)
        return f"micro introuvable pour « {target} ». Micros disponibles : {dispo}"
    _set_env_var("STT_INPUT_DEVICE", str(chosen))
    try:
        import stt
        stt.INPUT_DEVICE = chosen  # prise en compte immédiate au prochain tour
    except Exception:
        pass
    name = dict(inputs).get(chosen, str(chosen))
    try:
        import cockpit_ui
        cockpit_ui.engine(mic=name)   # met à jour le chip micro du cockpit
    except Exception:
        pass
    return f"micro réglé sur {name}, index {chosen}. Actif au prochain tour de parole."


# ── Fichiers : créer / éditer / supprimer (Jarvis) ──────────────────────────
# Sécurité : TOUT est borné au dossier personnel (jamais les fichiers système).
# Suppression = CORBEILLE (réversible), jamais de rm brutal. L'orchestrateur gate
# l'écrasement d'un fichier existant + la suppression par une confirmation.
HOME = Path.home()


def _safe_target(path):
    """Résout un chemin et n'autorise QUE l'intérieur du dossier personnel.
    Renvoie (Path résolu, None) ou (None, message de refus)."""
    raw = str(path or "").strip()
    if not raw:
        return None, "refusé: chemin vide"
    try:
        rp = Path(raw.replace("~", str(HOME))).expanduser().resolve()
    except Exception:
        return None, f"refusé: chemin invalide ({raw})"
    home = HOME.resolve()
    if rp == home:
        return None, "refusé: je ne touche pas à la racine de ton dossier personnel"
    if home not in rp.parents:
        return None, f"refusé: hors de ton dossier personnel ({rp})"
    return rp, None


def write_file(path, content=""):
    """Crée un fichier, ou écrase son contenu. Borné au dossier personnel.
    (L'écrasement d'un fichier EXISTANT passe par une confirmation, cf. orchestrateur.)"""
    rp, err = _safe_target(path)
    if err:
        return err
    text = content if isinstance(content, str) else str(content)
    try:
        rp.parent.mkdir(parents=True, exist_ok=True)
        existed = rp.exists()
        rp.write_text(text)
    except Exception as e:
        return f"erreur écriture: {type(e).__name__}"
    log_action("write_file", {"path": str(rp), "chars": len(text)},
               "écrit", "écrasé" if existed else "créé")
    return f"{'Modifié' if existed else 'Créé'} : {rp} ({len(text)} caractères)."


def delete_path(path):
    """Supprime un fichier ou dossier en l'envoyant à la CORBEILLE (réversible).
    Borné au dossier personnel. Jamais de suppression définitive."""
    rp, err = _safe_target(path)
    if err:
        return err
    if not rp.exists():
        return f"introuvable: {rp}"
    # Corbeille cross-OS : send2trash (pip) si dispo, sinon repli macOS ~/.Trash.
    try:
        from send2trash import send2trash as _s2t
        _s2t(str(rp))
        log_action("delete_path", {"path": str(rp)}, "corbeille", "send2trash")
        return f"Envoyé à la corbeille : {rp.name} (récupérable depuis la Corbeille)."
    except ImportError:
        if not IS_MAC:
            return ("refusé: installe send2trash pour la corbeille cross-OS "
                    "(pip install send2trash). Suppression non effectuée.")
        trash = HOME / ".Trash"
        try:
            trash.mkdir(exist_ok=True)
            dest = trash / rp.name
            if dest.exists():
                dest = trash / f"{rp.stem}-{int(time.time())}{rp.suffix}"
            shutil.move(str(rp), str(dest))
        except Exception as e:
            return f"erreur suppression: {type(e).__name__}"
        log_action("delete_path", {"path": str(rp)}, "corbeille", str(dest))
        return f"Envoyé à la corbeille : {rp.name} (récupérable depuis la Corbeille)."
    except Exception as e:
        return f"erreur suppression: {type(e).__name__}"


def shell_exec(cmd, project=""):
    """Exécute une commande shell sur le Mac (régime confirmation). Full shell
    (pipes, &&, redirections). Les interdits durs restent bloqués (sudo, rm,
    curl/wget, shutdown, firewall, prod sans code). Pour supprimer, utilise
    delete_path (corbeille) ; pour écrire un fichier, write_file."""
    cmd = (cmd or "").strip()
    if not cmd:
        return "refusé: commande vide"
    deny = check_hard_deny(cmd)
    if deny:
        return f"refusé: interdit absolu ({deny})"
    cwd = DALIA_DIR
    if project:
        p = _find_project(project)
        if p is None:
            return f"refusé: projet « {project} » introuvable"
        cwd = p
    try:
        r = subprocess.run(["bash", "-lc", cmd], capture_output=True, text=True,
                           timeout=120, cwd=str(cwd))
    except subprocess.TimeoutExpired:
        return "erreur: délai dépassé (120 s)"
    out = (r.stdout + r.stderr).strip()
    log_action("shell_exec", {"cmd": cmd[:200], "cwd": str(cwd)}, "exécuté", f"code={r.returncode}")
    return out[:3000] if out else f"(ok, code {r.returncode})"


# Fichiers sensibles : DALIA ne lit JAMAIS clés/secrets (même borné au home).
_SENSITIVE_RE = re.compile(
    r"(^|/)\.(ssh|aws|gnupg|docker|kube)(/|$)"
    r"|(^|/)\.config/(gcloud|gh)(/|$)"
    r"|(^|/)\.env(\.[^/]*)?$"
    r"|(^|/)\.(netrc|git-credentials|npmrc|pypirc)$"
    r"|\.(pem|key|p12|pfx|keychain|keystore)$"
    r"|id_rsa|id_ed25519|id_ecdsa",
    re.IGNORECASE)


def _resolve_read(path):
    """Résout un chemin pour LECTURE : intérieur du home (racine du home admise,
    car lister/lire est sans danger). Renvoie (Path, None) ou (None, refus)."""
    raw = str(path or "~").strip() or "~"
    try:
        rp = Path(raw.replace("~", str(HOME))).expanduser().resolve()
    except Exception:
        return None, f"refusé: chemin invalide ({raw})"
    home = HOME.resolve()
    if rp != home and home not in rp.parents:
        return None, f"refusé: hors de ton dossier personnel ({rp})"
    return rp, None


def read_file(path, max_chars=4000):
    """Lit le contenu d'un fichier texte (borné au home). Refuse les fichiers
    sensibles (clés, secrets, .env). Tronque les gros fichiers."""
    rp, err = _resolve_read(path)
    if err:
        return err
    if _SENSITIVE_RE.search(str(rp)):
        return "refusé: fichier sensible (clé/secret) — je ne le lis pas"
    if not rp.exists():
        return f"introuvable: {rp}"
    if rp.is_dir():
        return f"{rp} est un dossier — utilise list_dir"
    try:
        data = rp.read_text(errors="replace")
    except Exception as e:
        return f"erreur lecture: {type(e).__name__}"
    log_action("read_file", {"path": str(rp)}, "lu", f"{len(data)} chars")
    if len(data) > max_chars:
        return data[:max_chars] + f"\n[... tronqué, {len(data)} caractères au total]"
    return data or "(fichier vide)"


def list_dir(path="~"):
    """Liste le contenu d'un dossier (borné au home). Lecture seule."""
    rp, err = _resolve_read(path)
    if err:
        return err
    if not rp.exists():
        return f"introuvable: {rp}"
    if not rp.is_dir():
        return f"{rp} n'est pas un dossier (utilise read_file)"
    try:
        entries = sorted(rp.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except Exception as e:
        return f"erreur: {type(e).__name__}"
    names = [e.name + ("/" if e.is_dir() else "") for e in entries[:200]]
    log_action("list_dir", {"path": str(rp)}, "listé", f"{len(names)} entrées")
    if not names:
        return f"{rp} est vide"
    extra = "" if len(entries) <= 200 else f"\n[... {len(entries) - 200} de plus]"
    return f"{rp} :\n" + "\n".join(names) + extra


def claude_code(task):
    r = subprocess.run(
        ["claude", "-p", task, "--output-format", "json"],
        capture_output=True, text=True, timeout=300, cwd=DALIA_DIR,
    )
    try:
        data = json.loads(r.stdout)
        cost = data.get("total_cost_usd", "?")
        log_action("claude_code", {"task": task[:120]}, "exécuté", f"cost_usd={cost}")
        return f"[coût: {cost} USD] {data.get('result', r.stdout[:2000])}"
    except (json.JSONDecodeError, AttributeError):
        return f"erreur claude code: {(r.stdout + r.stderr)[:500]}"


# Pont vers le second cerveau Nafiy (vault Obsidian de Claude Code, distinct de la
# mémoire locale de Dalia). Lecture seule via qmd (recherche BM25, sans modèle).
QMD_BIN = shutil.which("qmd") or "/opt/homebrew/bin/qmd"
VAULT_NAFIY = Path.home() / "SecondBrain" / "claude"


def chercher_vault(query):
    """Cherche dans le second cerveau Nafiy (état projets, infra, décisions, historique)."""
    query = " ".join((query or "").split())
    if not query:
        return "refusé: requête vide"
    if not VAULT_NAFIY.exists():
        return "le second cerveau Nafiy est introuvable"
    try:
        r = subprocess.run([QMD_BIN, "search", query, "-c", "claude"],
                           capture_output=True, text=True, timeout=30, cwd=str(VAULT_NAFIY))
        out = (r.stdout or "").strip()
        if not out:
            return f"rien trouvé dans le second cerveau pour : {query}"
        return out[:3500]
    except FileNotFoundError:
        return "refusé: qmd n'est pas installé"
    except subprocess.TimeoutExpired:
        return "erreur: délai dépassé"


# ── Registre : nom → (fonction, régime) ────────────────────────────────────
# Régimes : "libre" | "confirmation" | "claude" (gating par phrase explicite)
REGISTRY = {
    "get_time": (get_time, "libre"),
    "open_app": (open_app, "libre"),
    "running_apps": (running_apps, "libre"),
    "open_url": (open_url, "libre"),
    # Local Mac : exécution DIRECTE (le propriétaire veut qu'elle agisse).
    # AppleScript reste protégé par APPLESCRIPT_DENY (pas de
    # suppression/corbeille/message/keystroke).
    "applescript": (applescript, "libre"),
    "run_shortcut": (run_shortcut, "libre"),
    "shell_read": (shell_read, "libre"),
    # Fichiers (Jarvis) : créer direct, écraser/supprimer/shell → gardés.
    "write_file": (write_file, "libre"),
    "delete_path": (delete_path, "confirmation"),
    "shell_exec": (shell_exec, "confirmation"),
    "read_file": (read_file, "libre"),
    "list_dir": (list_dir, "libre"),
    "creer_skill": (creer_skill, "libre"),
    "utiliser_skill": (utiliser_skill, "libre"),
    "lister_skills": (lister_skills, "libre"),
    "oublier_skill": (oublier_skill, "libre"),
    "etat_projets": (etat_projets, "libre"),
    "chercher_vault": (chercher_vault, "libre"),
    "claude_code": (claude_code, "claude"),
    # Lancer Claude Code dans un Terminal : exécution DIRECTE en cockpit.
    # Reste bloqué en mode maison (hors allowlist média) et n'est proposé que si
    # « claude » est mentionné.
    "claude_terminal": (claude_terminal, "libre"),
    "retenir": (retenir, "libre"),
    "oublier": (oublier, "libre"),
    "memoriser": (memoriser, "libre"),
    "resumer_session": (resumer_session, "libre"),
    "set_mic": (set_mic, "libre"),
    "propose_plan": (propose_plan, "libre"),
    "spotify_play": (media.spotify_play, "libre"),
    "spotify_control": (media.spotify_control, "libre"),
    "youtube_play": (media.youtube_play, "libre"),
    "recherche_web": (media.recherche_web, "libre"),
    "afficher_media": (media.afficher_media, "libre"),
    "analyser_image": (media.analyser_image, "libre"),
}

TOOLS_SPEC = [
    {"type": "function", "function": {"name": "get_time", "description": "Donne l'heure et la date locales.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "open_app", "description": "Ouvre une application macOS.", "parameters": {"type": "object", "properties": {"name": {"type": "string", "description": "Nom de l'application"}}, "required": ["name"]}}},
    {"type": "function", "function": {"name": "running_apps", "description": "Liste les applications actuellement ouvertes sur le Mac. À utiliser AVANT de proposer d'ouvrir quelque chose : si c'est déjà ouvert, le dire au lieu de demander confirmation.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "open_url", "description": "Ouvre une URL http/https dans le navigateur.", "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {"name": "applescript", "description": "Exécute un AppleScript sur le Mac (volume, notifications, contrôle d'apps, ouvrir/piloter une application). Exécution DIRECTE. La suppression de fichiers/corbeille/envoi de message/keystroke est bloquée.", "parameters": {"type": "object", "properties": {"script": {"type": "string"}, "summary": {"type": "string", "description": "description orale TRÈS courte de l'action, ex: « ouvrir mon-projet dans VS Code »"}}, "required": ["script", "summary"]}}},
    {"type": "function", "function": {"name": "run_shortcut", "description": "Lance un raccourci macOS Shortcuts. Exécution directe.", "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}}},
    {"type": "function", "function": {"name": "shell_read", "description": "Commande shell LECTURE SEULE sur le Mac : ls, pwd, df -h, uptime, ps aux, git status, git log, cat (limité à ~/dalia/).", "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "Crée un fichier ou écrase son contenu, dans le dossier personnel du propriétaire (projets, Bureau, etc.). À utiliser quand le propriétaire demande de créer/écrire/enregistrer un fichier, ou d'y mettre un contenu. La création est directe ; l'écrasement d'un fichier existant demande une confirmation. Pour une édition chirurgicale d'un gros fichier de code, préfère claude_terminal.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "chemin du fichier, ex: ~/Desktop/note.md ou ~/dev/projet/config.json"}, "content": {"type": "string", "description": "contenu complet du fichier"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "delete_path", "description": "Supprime un fichier ou un dossier en l'envoyant à la CORBEILLE (récupérable), dans le dossier personnel du propriétaire. À utiliser quand le propriétaire demande de supprimer/effacer/jeter un fichier. Demande une confirmation. Jamais de suppression définitive.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "chemin du fichier ou dossier à supprimer"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "shell_exec", "description": "Exécute une commande shell sur le Mac pour ACCOMPLIR une tâche (git, npm, build, scripts, mkdir, cp, mv, exécuter un programme...). Full shell (pipes, &&). Demande une confirmation. Les commandes dangereuses (sudo, rm, curl/wget, shutdown, firewall) sont refusées : pour supprimer utilise delete_path, pour écrire un fichier utilise write_file. Optionnel: 'project' = dossier de travail (un projet sous ~/dev ou ~/dalia).", "parameters": {"type": "object", "properties": {"cmd": {"type": "string", "description": "la commande shell à exécuter"}, "project": {"type": "string", "description": "nom du projet où exécuter (optionnel)"}}, "required": ["cmd"]}}},
    {"type": "function", "function": {"name": "read_file", "description": "Lit le contenu d'un fichier texte dans le dossier personnel du propriétaire (code source, note, config). À utiliser quand le propriétaire demande de lire/voir/montrer un fichier, ou avant de l'éditer. Lecture seule. Les fichiers sensibles (clés, secrets, .env) sont refusés.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "chemin du fichier à lire"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "list_dir", "description": "Liste le contenu d'un dossier dans le dossier personnel du propriétaire. À utiliser pour explorer un projet, retrouver un fichier, voir ce qu'il y a quelque part. Lecture seule.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "chemin du dossier (ex: ~/dev/dalia, ~/Desktop). Par défaut le dossier personnel."}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "creer_skill", "description": "Enregistre une COMPÉTENCE réutilisable (recette) : une procédure nommée que DALIA pourra réappliquer. À utiliser quand le propriétaire t'apprend une façon de faire, dit « retiens cette procédure », « fais-en un skill », ou quand tu viens de réussir une tâche en plusieurs étapes qu'il pourrait redemander. Les étapes décrivent comment enchaîner les outils existants.", "parameters": {"type": "object", "properties": {"nom": {"type": "string", "description": "nom court du skill, ex: « déployer le backend »"}, "quand": {"type": "string", "description": "dans quelle situation l'utiliser"}, "etapes": {"type": "array", "items": {"type": "string"}, "description": "les étapes ordonnées de la procédure"}}, "required": ["nom", "quand", "etapes"]}}},
    {"type": "function", "function": {"name": "utiliser_skill", "description": "Relit les étapes d'un skill existant pour l'appliquer. À utiliser quand la demande du propriétaire correspond à un skill que tu connais : relis-le puis exécute ses étapes via les outils, sans improviser.", "parameters": {"type": "object", "properties": {"nom": {"type": "string", "description": "nom du skill à appliquer"}}, "required": ["nom"]}}},
    {"type": "function", "function": {"name": "lister_skills", "description": "Liste les compétences (skills) que DALIA sait faire. À utiliser quand le propriétaire demande « qu'est-ce que tu sais faire », « quels skills tu as ».", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "etat_projets", "description": "Donne le point d'entretien des projets du propriétaire (état git de ~/dev et ~/dalia, sauvegarde) — digest généré chaque matin. À utiliser quand le propriétaire demande « fais le point sur mes projets », « où en sont mes repos », « quoi à committer ».", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "chercher_vault", "description": "Cherche dans le second cerveau (vault Obsidian de Claude Code : état réel des projets, décisions figées, historique des sessions). À utiliser dès que le propriétaire pose une question sur l'état d'un projet, une décision passée, ou « qu'est-ce qu'on sait sur X », « où en est Y ». Lecture seule, distinct de ta mémoire perso.", "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "ce qu'il faut chercher dans le second cerveau"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "oublier_skill", "description": "Supprime un skill du vault, quand le propriétaire dit de l'oublier ou qu'il n'est plus valable.", "parameters": {"type": "object", "properties": {"nom": {"type": "string"}}, "required": ["nom"]}}},
    {"type": "function", "function": {"name": "claude_code", "description": "Escalade vers Claude Code pour une tâche de développement lourde. UNIQUEMENT quand le propriétaire dit explicitement 'demande à Claude'.", "parameters": {"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]}}},
    {"type": "function", "function": {"name": "claude_terminal", "description": "Ouvre Claude Code dans une fenêtre Terminal sur un projet du propriétaire (~/dev ou ~/dalia) et lui transmet ses instructions. À utiliser quand le propriétaire demande d'ouvrir ou lancer Claude (Code) sur un projet. Exécution directe.", "parameters": {"type": "object", "properties": {"project": {"type": "string", "description": "nom du projet, ex: mon-projet, dalia"}, "instruction": {"type": "string", "description": "instructions du propriétaire pour Claude, reformulées clairement"}}, "required": ["project", "instruction"]}}},
    {"type": "function", "function": {"name": "retenir", "description": "Enregistre une leçon durable quand le propriétaire te corrige ou exprime une préférence (« retiens que », « ne fais plus », « la prochaine fois », « j'aime pas quand »). La leçon doit être courte, généralisable, à la deuxième personne (ex: « Ne propose pas d'ouvrir une app déjà ouverte »).", "parameters": {"type": "object", "properties": {"lecon": {"type": "string"}}, "required": ["lecon"]}}},
    {"type": "function", "function": {"name": "oublier", "description": "Supprime une note de la mémoire (leçon, fait ou projet) quand le propriétaire dit d'oublier ou que ça ne s'applique plus.", "parameters": {"type": "object", "properties": {"motif": {"type": "string", "description": "mot ou bout de phrase de la note à supprimer"}}, "required": ["motif"]}}},
    {"type": "function", "function": {"name": "memoriser", "description": "Enregistre durablement une information dans la mémoire long terme (vault local). À utiliser quand le propriétaire partage quelque chose à retenir au-delà de la session : un FAIT sur lui (préférence, habitude, un proche), un contexte PROJET (état/décision), ou une LEÇON de conduite.", "parameters": {"type": "object", "properties": {"categorie": {"type": "string", "description": "fait | projet | lecon"}, "note": {"type": "string", "description": "l'information à retenir, courte et claire"}}, "required": ["categorie", "note"]}}},
    {"type": "function", "function": {"name": "resumer_session", "description": "Enregistre un résumé daté de la conversation en cours (ce qui a été dit/fait), dans la mémoire épisodique. À utiliser quand le propriétaire clôt une session, dit « note ce qu'on a fait », ou en fin d'échange important.", "parameters": {"type": "object", "properties": {"resume": {"type": "string", "description": "résumé court de la session, une à trois phrases"}}, "required": ["resume"]}}},
    {"type": "function", "function": {"name": "set_mic", "description": "Change le micro d'entrée utilisé par Dalia. À appeler quand le propriétaire demande de changer de micro : « passe au casque », « utilise le micro du casque », « reviens au micro du MacBook », « change de micro ». Effet immédiat + persistant.", "parameters": {"type": "object", "properties": {"target": {"type": "string", "description": "« casque » (micro externe/jack/USB/Bluetooth), « macbook » (micro intégré), ou un index numérique"}}, "required": ["target"]}}},
    {"type": "function", "function": {"name": "propose_plan", "description": "À appeler EN PREMIER pour une tâche à plusieurs étapes dont au moins une modifie un état (mutation/destructif), AVANT d'appeler tout outil de mutation. Annonce le plan au propriétaire et attend son approbation. Ne pas utiliser pour une tâche en lecture seule.", "parameters": {"type": "object", "properties": {"summary": {"type": "string", "description": "résumé oral très court du plan, une phrase"}, "steps": {"type": "array", "items": {"type": "string"}, "description": "liste ordonnée des étapes, chacune en quelques mots"}}, "required": ["summary", "steps"]}}},
    {"type": "function", "function": {"name": "spotify_play", "description": "Cherche un morceau, un artiste ou une chanson sur Spotify et le joue dans l'app Spotify locale. Ex: « mets la musique d'Iron Man », « joue du Daft Punk », « lance Bohemian Rhapsody ».", "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "ce qu'il faut chercher : titre, artiste, ou les deux"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "spotify_control", "description": "Contrôle la lecture Spotify en cours : mettre en pause, reprendre, passer au morceau suivant ou précédent. Ex: « pause », « reprends », « suivant », « précédent ».", "parameters": {"type": "object", "properties": {"action": {"type": "string", "description": "pause | play | suivant | précédent"}}, "required": ["action"]}}},
    {"type": "function", "function": {"name": "youtube_play", "description": "Cherche une vidéo sur YouTube et l'ouvre dans le navigateur. Ex: « lance une vidéo de chats », « ouvre le clip de Thriller sur YouTube », « montre-moi un tuto Docker ».", "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "ce qu'il faut chercher sur YouTube"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "recherche_web", "description": "Fait une recherche sur le web et ouvre les résultats dans le navigateur. Ex: « cherche la recette du tiramisu », « trouve les horaires de la pharmacie », « fais une recherche sur les volcans ».", "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "ce qu'il faut chercher sur le web"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "afficher_media", "description": "Affiche une image, une vidéo, un lien ou une vidéo YouTube DANS LE CHAT du cockpit (sans l'ouvrir ailleurs). Accepte un chemin de fichier local (ex: une photo du Mac) ou une URL. Ex: « montre-moi la photo ~/Desktop/x.jpg », « affiche ce lien ».", "parameters": {"type": "object", "properties": {"source": {"type": "string", "description": "chemin de fichier local ou URL"}}, "required": ["source"]}}},
    {"type": "function", "function": {"name": "analyser_image", "description": "ANALYSE/décrit le contenu d'une image avec le modèle vision local (ce que montre la photo, ce qui est écrit dessus). À utiliser quand le propriétaire demande « c'est quoi sur cette photo ? », « décris cette image », « que vois-tu sur X ». Affiche aussi l'image au cockpit. Donne le chemin du fichier local.", "parameters": {"type": "object", "properties": {"source": {"type": "string", "description": "chemin du fichier image local"}}, "required": ["source"]}}},
]


def execute(name, args):
    """Exécute un tool en régime libre (les confirmations passent par l'orchestrateur)."""
    fn, _ = REGISTRY[name]
    result = fn(**args)
    if name != "claude_code":  # claude_code logge lui-même (avec le coût)
        status = "refusé" if str(result).startswith("refusé") else "exécuté"
        log_action(name, args, status)
    return str(result)
