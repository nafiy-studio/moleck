# (c) 2026 Nafiy Studio - Moleck. Code propriete de Nafiy Studio.
# Mention de propriete a conserver (voir LICENSE). Ne pas retirer : ADN du projet.
"""Mémoire long terme de DALIA — vault Obsidian LOCAL (~/SecondBrain/dalia/).

100% local : rien ne sort de la machine (cohérent avec le reste de DALIA).
Markdown lisible et éditable directement dans Obsidian.

Fichiers :
- faits.md   : faits durables sur le propriétaire (préférences, personnes, habitudes)
- projets.md : contexte des chantiers (projets, décisions)
- lecons.md  : corrections / règles de conduite (retenir / oublier)
- sessions/AAAA-MM-JJ.md : résumés de conversations (mémoire épisodique)
- MEMORY.md  : index, régénéré à chaque écriture
"""
import datetime
from pathlib import Path

VAULT = Path.home() / "SecondBrain" / "dalia"
SESSIONS = VAULT / "sessions"

FILES = {
    "fait": VAULT / "faits.md",
    "projet": VAULT / "projets.md",
    "lecon": VAULT / "lecons.md",
}
_TITLES = {"fait": "Faits sur le propriétaire", "projet": "Contexte projets", "lecon": "Leçons apprises"}

MAX_BULLETS = 60          # par fichier (borne le contexte injecté)
MAX_CONTEXT_CHARS = 3800  # taille max du bloc mémoire envoyé au modèle


def _ensure():
    VAULT.mkdir(parents=True, exist_ok=True)
    SESSIONS.mkdir(exist_ok=True)


def _bullets(text):
    return [l for l in text.splitlines() if l.strip().startswith("- ")]


def note(category, text):
    """Ajoute une note durable (dédup). category ∈ fait | projet | lecon."""
    category = (category or "").lower().strip()
    f = FILES.get(category)
    if f is None:
        return f"refusé: catégorie inconnue ({category}). Utilise fait, projet ou lecon."
    text = " ".join((text or "").split())
    if not text:
        return "refusé: note vide"
    _ensure()
    existing = f.read_text() if f.exists() else ""
    if any(text.lower() in b.lower() for b in _bullets(existing)):
        return "déjà noté"
    bullets = (_bullets(existing) + [f"- {text}"])[-MAX_BULLETS:]
    f.write_text(f"# {_TITLES[category]}\n\n" + "\n".join(bullets) + "\n")
    _reindex()
    return f"noté ({category})"


def forget(motif):
    """Supprime les notes contenant `motif`, dans tous les fichiers."""
    motif = (motif or "").lower().strip()
    if not motif:
        return "refusé: motif vide"
    removed = 0
    for cat, f in FILES.items():
        if not f.exists():
            continue
        kept = [b for b in _bullets(f.read_text()) if motif not in b.lower()]
        removed += len(_bullets(f.read_text())) - len(kept)
        f.write_text(f"# {_TITLES[cat]}\n\n" + "\n".join(kept) + ("\n" if kept else ""))
    _reindex()
    return f"{removed} note(s) oubliée(s)" if removed else "aucune note ne correspond"


def summarize_session(resume):
    """Ajoute un résumé horodaté dans sessions/AAAA-MM-JJ.md (mémoire épisodique)."""
    resume = " ".join((resume or "").split())
    if not resume:
        return "refusé: résumé vide"
    _ensure()
    day = datetime.date.today().isoformat()
    f = SESSIONS / f"{day}.md"
    stamp = datetime.datetime.now().strftime("%H:%M")
    head = "" if f.exists() else f"# Session {day}\n\n"
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(f"{head}- {stamp} — {resume}\n")
    _reindex()
    return "session notée"


def load_context():
    """Bloc mémoire injecté dans le persona à chaque tour (borné en taille)."""
    parts = []
    for f in FILES.values():
        if f.exists():
            txt = f.read_text().strip()
            if txt:
                parts.append(txt)
    sess = sorted(SESSIONS.glob("*.md")) if SESSIONS.exists() else []
    if sess:
        parts.append("## Dernière session\n" + sess[-1].read_text().strip()[-900:])
    blob = "\n\n".join(parts).strip()
    return blob[-MAX_CONTEXT_CHARS:]


def _reindex():
    _ensure()
    idx = ["# Mémoire de DALIA (vault local)", "",
           "Mémoire long terme, 100% locale. Éditable dans Obsidian.", ""]
    for cat, f in FILES.items():
        n = len(_bullets(f.read_text())) if f.exists() else 0
        idx.append(f"- [[{f.stem}]] — {_TITLES[cat]} ({n} notes)")
    sess = sorted(SESSIONS.glob("*.md")) if SESSIONS.exists() else []
    idx.append(f"- sessions/ — {len(sess)} résumé(s) de conversation")
    (VAULT / "MEMORY.md").write_text("\n".join(idx) + "\n")
