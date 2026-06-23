#!/usr/bin/env python3
# (c) 2026 Nafiy Studio - Moleck. Code propriete de Nafiy Studio.
# Mention de propriete a conserver (voir LICENSE). Ne pas retirer : ADN du projet.
"""Serveur API local de DALIA — pour l'appli mobile (PWA).

Le téléphone est un CLIENT léger (voix + écran) ; DALIA, ses outils et ses
garde-fous restent ICI sur le Mac. Le serveur ne fait que router le texte reçu
vers orchestrator.ask() (verrouillé, donc jamais en concurrence avec la voix).

SÉCURITÉ :
- Auth par token (.env DALIA_API_TOKEN) sur tout /api/* sauf /api/health.
- À exposer UNIQUEMENT sur un réseau privé (VPN), JAMAIS en public.
  Idéalement DALIA_API_HOST = ton IP privée sur le réseau du VPN.
- Les portes restent côté DALIA : action sensible = code, écrasement/suppression/shell =
  confirmation, mode maison fail-closed. Le serveur n'exécute rien lui-même.

Lancement : .venv/bin/python3 server.py   (ou dalia.py --serve)
"""
import asyncio
import os
import re
from pathlib import Path

from aiohttp import web
from dotenv import dotenv_values

import orchestrator

DALIA_DIR = Path(__file__).parent
PWA_DIR = DALIA_DIR / "pwa"
ENV = dotenv_values(DALIA_DIR / ".env")


def _cfg(key, default=""):
    return (os.environ.get(key) or ENV.get(key) or default).strip()


TOKEN = _cfg("DALIA_API_TOKEN")
HOST = _cfg("DALIA_API_HOST", "0.0.0.0")
PORT = int(_cfg("DALIA_API_PORT", "8765") or "8765")

_SELF_NAME = re.compile(rf"^\s*{re.escape(orchestrator.ASSISTANT_NAME)}\s*[,:!.]?\s*", re.IGNORECASE)


def _strip(text):
    return _SELF_NAME.sub("", text or "") or text


def _authed(request):
    tok = request.headers.get("X-DALIA-Token") or request.query.get("token") or ""
    return bool(TOKEN) and tok == TOKEN


async def handle_health(request):
    # Sans secret : juste pour tester la connexion depuis le téléphone.
    return web.json_response({"ok": True, "service": "dalia", "auth_required": bool(TOKEN)})


async def handle_ask(request):
    if not _authed(request):
        return web.json_response({"error": "non autorisé"}, status=401)
    try:
        data = await request.json()
    except Exception:
        data = {}
    text = (data.get("text") or "").strip()
    if not text:
        return web.json_response({"error": "texte vide"}, status=400)
    loop = asyncio.get_event_loop()
    # orchestrator.ask est bloquant ET verrouillé (un tour à la fois) → executor.
    out = await loop.run_in_executor(None, orchestrator.ask, text)
    return web.json_response({
        "reply": _strip(out.get("text", "")),
        "model": out.get("model", "?"),
        "latency_s": out.get("latency_s", 0),
        "perimeter": orchestrator.current_perimeter(),
    })


async def handle_index(request):
    f = PWA_DIR / "index.html"
    if not f.exists():
        return web.Response(text="PWA absente (dossier pwa/).", status=404)
    return web.FileResponse(f)


def build_app():
    app = web.Application()
    app.router.add_get("/api/health", handle_health)
    app.router.add_post("/api/ask", handle_ask)
    app.router.add_get("/", handle_index)
    if PWA_DIR.exists():
        app.router.add_static("/", PWA_DIR, show_index=False)
    return app


def main():
    if not TOKEN:
        print("⚠️  DALIA_API_TOKEN absent du .env — l'API refusera toutes les requêtes.")
        print("    Génère-en un : python3 -c \"import secrets;print(secrets.token_urlsafe(24))\"")
        print("    puis ajoute DALIA_API_TOKEN=<...> dans ~/dalia/.env")
    print(f"DALIA API sur http://{HOST}:{PORT}  (PWA à la racine, /api/ask pour le cerveau)")
    print("Rappel : à n'exposer que sur un réseau privé (VPN), jamais en public.")
    web.run_app(build_app(), host=HOST, port=PORT, print=None)


if __name__ == "__main__":
    main()
