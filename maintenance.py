#!/usr/bin/env python3
# (c) 2026 Nafiy Studio - Moleck. Code propriete de Nafiy Studio.
# Mention de propriete a conserver (voir LICENSE). Ne pas retirer : ADN du projet.
"""Entretien quotidien des projets du propriétaire — LECTURE SEULE + backup DALIA.

Lancé par launchd (maintenance planifiée). Il ne MODIFIE aucun projet : il
récupère l'état (git fetch/status, lecture seule), pousse le backup hors-site de
DALIA (remote « backup » configuré dans le repo), et écrit un digest lisible
dans ~/dalia/maintenance_report.md (que DALIA peut lire à la demande).

Sûr par conception : aucune mutation de projet, aucune action destructive. Les
vraies mutations (deploy, restart prod) restent manuelles et gardées.
"""
import datetime
import os
import subprocess
from pathlib import Path

HOME = Path.home()
ROOTS = [HOME / "dev", HOME / "dalia"]
REPORT = HOME / "dalia" / "maintenance_report.md"
_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}  # jamais de prompt d'auth (sinon blocage)


def _git(args, cwd, timeout=90):
    try:
        r = subprocess.run(["git", *args], cwd=str(cwd), env=_ENV,
                           capture_output=True, text=True, timeout=timeout)
        return (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return "(délai dépassé)"
    except Exception as e:
        return f"(erreur {type(e).__name__})"


def _projects():
    out = []
    for root in ROOTS:
        if not root.is_dir():
            continue
        for d in sorted(root.iterdir()):
            if d.is_dir() and (d / ".git").is_dir():
                out.append(d)
    return out


def _status(p):
    _git(["fetch", "--quiet", "--all"], p, timeout=120)   # LECTURE SEULE
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], p) or "?"
    dirty = [l for l in _git(["status", "--porcelain"], p).splitlines() if l.strip()]
    ab = _git(["rev-list", "--left-right", "--count", "HEAD...@{upstream}"], p)
    ahead = behind = None
    if ab and "\t" in ab:
        try:
            a, b = ab.split("\t")
            ahead, behind = int(a), int(b)
        except ValueError:
            pass
    flags = []
    if dirty:
        flags.append(f"{len(dirty)} fichier(s) non commités")
    if ahead:
        flags.append(f"{ahead} commit(s) à pousser")
    if behind:
        flags.append(f"{behind} commit(s) en retard")
    return {"name": p.name, "branch": branch, "flags": flags}


def _backup_dalia():
    p = HOME / "dalia"
    if not (p / ".git").is_dir():
        return "DALIA : pas de repo"
    remotes = _git(["remote"], p).split()
    if "backup" not in remotes:
        return "DALIA : remote « backup » absent (push ignoré)"
    out = _git(["push", "backup", "HEAD:main"], p, timeout=120)
    ok = "rejected" not in out.lower() and "erreur" not in out.lower() and "error" not in out.lower()
    return "DALIA : backup hors-site poussé ✓" if ok else f"DALIA : backup ÉCHEC ({out[:80]})"


def main():
    now = datetime.datetime.now()
    projs = _projects()
    lines = [f"# Entretien projets — {now.strftime('%Y-%m-%d %H:%M')}", ""]
    attention = []
    for p in projs:
        s = _status(p)
        if s["flags"]:
            lines.append(f"- **{s['name']}** ({s['branch']}) : {', '.join(s['flags'])}")
            attention.append(s["name"])
        else:
            lines.append(f"- {s['name']} ({s['branch']}) : à jour ✓")
    lines += ["", "## Sauvegarde", "- " + _backup_dalia()]
    head = (f"{len(attention)} projet(s) demandent ton attention : {', '.join(attention)}."
            if attention else "Tous les projets sont propres et à jour.")
    lines.insert(2, f"_{head}_")
    lines.append("")
    REPORT.write_text("\n".join(lines))
    print(head)
    print(f"(digest écrit dans {REPORT})")


if __name__ == "__main__":
    main()
