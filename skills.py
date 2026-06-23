# (c) 2026 Nafiy Studio - Moleck. Code propriete de Nafiy Studio.
# Mention de propriete a conserver (voir LICENSE). Ne pas retirer : ADN du projet.
"""Compétences (skills) de DALIA — des RECETTES réutilisables, 100% locales.

Un skill = une procédure nommée (QUAND l'utiliser + les ÉTAPES) que DALIA peut
créer, retrouver et appliquer. C'est de la DONNÉE (des instructions), pas du code
exécutable ni une nouvelle permission : un skill ne fait qu'orchestrer les outils
EXISTANTS, donc tous les garde-fous (confirmation, porte prod, maison) restent en
place. C'est ça, l'« autodidacte » sain : elle apprend des procédures, elle ne se
réécrit pas elle-même.

Stockés en markdown dans ~/SecondBrain/dalia/skills/<slug>.md (éditables Obsidian).
"""
import re
from pathlib import Path

VAULT_SKILLS = Path.home() / "SecondBrain" / "dalia" / "skills"
MAX_SKILLS = 60
MAX_CATALOG_CHARS = 1400


def _slug(nom):
    s = re.sub(r"[^a-z0-9]+", "-", (nom or "").lower().strip()).strip("-")
    return s[:50] or "skill"


def _ensure():
    VAULT_SKILLS.mkdir(parents=True, exist_ok=True)


def _parse(path):
    """(nom, quand) d'un fichier skill, lus dans ses métadonnées de tête."""
    nom, quand = path.stem, ""
    try:
        for line in path.read_text().splitlines():
            low = line.lower()
            if low.startswith("nom:"):
                nom = line.split(":", 1)[1].strip() or nom
            elif low.startswith("quand:"):
                quand = line.split(":", 1)[1].strip()
                break
    except Exception:
        pass
    return nom, quand


def _find(nom):
    """Chemin du skill par slug exact, sinon par nom approchant."""
    _ensure()
    f = VAULT_SKILLS / f"{_slug(nom)}.md"
    if f.exists():
        return f
    needle = (nom or "").lower().strip()
    for p in sorted(VAULT_SKILLS.glob("*.md")):
        n, _ = _parse(p)
        if needle and (needle in n.lower() or needle in p.stem):
            return p
    return None


def create(nom, quand="", etapes=None):
    nom = " ".join((nom or "").split())
    if not nom:
        return "refusé: nom de skill vide"
    if isinstance(etapes, str):
        etapes = [etapes]
    etapes = [str(e).strip() for e in (etapes or []) if str(e).strip()]
    _ensure()
    target = VAULT_SKILLS / f"{_slug(nom)}.md"
    if not target.exists() and len(list(VAULT_SKILLS.glob("*.md"))) >= MAX_SKILLS:
        return "refusé: trop de skills (limite atteinte), oublie-en un d'abord"
    steps_md = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(etapes)) or "(étapes à préciser)"
    body = (f"nom: {nom}\nquand: {quand}\n\n# {nom}\n\n"
            f"## Quand l'utiliser\n{quand or '(à préciser)'}\n\n"
            f"## Étapes\n{steps_md}\n")
    target.write_text(body)
    verbe = "mis à jour" if target.stat().st_size else "enregistré"
    return f"skill « {nom} » {verbe} ({len(etapes)} étape(s))"


def delete(nom):
    p = _find(nom)
    if p is None:
        return f"aucun skill « {nom} »"
    n, _ = _parse(p)
    p.unlink()
    return f"skill « {n} » oublié"


def get(nom):
    p = _find(nom)
    if p is None:
        return f"aucun skill « {nom} ». Skills disponibles :\n{list_text()}"
    return p.read_text()


def list_text():
    _ensure()
    items = []
    for p in sorted(VAULT_SKILLS.glob("*.md")):
        n, q = _parse(p)
        items.append(f"- {n}" + (f" — {q}" if q else ""))
    return "\n".join(items) if items else "(aucun skill pour l'instant)"


def catalog():
    """Catalogue compact (nom : quand) injecté au persona, borné en taille."""
    _ensure()
    items = []
    for p in sorted(VAULT_SKILLS.glob("*.md")):
        n, q = _parse(p)
        items.append(f"- {n}" + (f" : {q}" if q else ""))
    return "\n".join(items)[:MAX_CATALOG_CHARS]
