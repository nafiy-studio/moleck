# (c) 2026 Nafiy Studio - Moleck. Code propriete de Nafiy Studio.
# Mention de propriete a conserver (voir LICENSE). Ne pas retirer : ADN du projet.
"""Cockpit Dalia : collecte d'état LOCAL pour le panneau de l'orbe.

Économe : appelé UNIQUEMENT quand le panneau est ouvert (~5 s).
Chaque entrée renvoyée : {"txt": str, "ok": True|False|None}.
"""
import os
import re
import socket
import subprocess
import urllib.parse

from dotenv import dotenv_values

ENV = dotenv_values(os.path.join(os.path.dirname(__file__), ".env"))


def _sh(argv, timeout=8):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def mac_stats():
    try:
        load = os.getloadavg()[0]
        cores = os.cpu_count() or 8
        page = 16384 if os.uname().machine == "arm64" else 4096
        vm = _sh(["vm_stat"])
        pages = {m[0]: int(m[1]) for m in re.findall(r"Pages ([a-z ]+):\s+(\d+)", vm)}
        used = (pages.get("active", 0) + pages.get("wired down", 0)
                + pages.get("occupied by compressor", 0)) * page / 1e9
        total = int(_sh(["sysctl", "-n", "hw.memsize"])) / 1e9
        warn = load / cores > 0.8 or used / total > 0.9
        return {"txt": f"CPU {load:.1f}/{cores} · RAM {used:.1f}/{total:.0f} Go",
                "ok": not warn}
    except Exception:
        return {"txt": "indisponible", "ok": None}


def llm_stats():
    """Joignabilité de l'endpoint LLM configuré (.env LITELLM_BASE_URL)."""
    base = (ENV.get("LITELLM_BASE_URL") or "http://localhost:4000").strip()
    try:
        u = urllib.parse.urlparse(base)
        host = u.hostname or "localhost"
        port = u.port or (443 if u.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=1.5):
            return {"txt": "endpoint LLM ✓", "ok": True}
    except OSError:
        return {"txt": "endpoint LLM injoignable", "ok": False}
    except Exception:
        return {"txt": "endpoint LLM indisponible", "ok": None}


def local_snapshot():
    return {"mac": mac_stats(), "llm": llm_stats()}
