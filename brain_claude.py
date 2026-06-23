# (c) 2026 Nafiy Studio - Moleck. Code propriete de Nafiy Studio.
# Mention de propriete a conserver (voir LICENSE). Ne pas retirer : ADN du projet.
"""Adaptateur « cerveau Claude » pour DALIA.

Utilise le CLI `claude` (Claude Code) déjà installé sur le Mac, authentifié avec
TON ABONNEMENT — l'inférence part directement Mac → Anthropic, sans passer par
l'endpoint LiteLLM. Pas de clé API, pas de coût au token.

Le piège : `claude -p` est un AGENT (il a ses propres outils). On ne veut pas qu'il
agisse. On le force donc en pur cerveau : on lui passe la persona DALIA + la liste
des OUTILS DE DALIA + la conversation, et on exige UNE réponse JSON STRICTE — soit
des appels d'outils, soit du texte. C'est DALIA qui exécute ensuite les outils dans
SA boucle, donc TOUS ses garde-fous (maison fail-closed, porte prod par code)
restent en place.

L'objet renvoyé imite le contrat OpenAI attendu par l'orchestrateur :
    resp.choices[0].message.content        (str | None)
    resp.choices[0].message.tool_calls     (liste | None)
        chaque appel : .id, .type, .function.name, .function.arguments (JSON str)

Latence ~4-5 s par tour : acceptable en test, lourd en vocal multi-étapes.
"""
import json
import os
import subprocess
import uuid
from types import SimpleNamespace

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
CLAUDE_MODEL = (os.environ.get("CLAUDE_MODEL") or "").strip()  # vide = modèle par défaut de l'abo
_TIMEOUT = 150

# Outils propres de Claude Code coupés (on ne veut PAS qu'il agisse lui-même).
_DENY_TOOLS = ["Bash", "Edit", "Write", "Read", "Glob", "Grep", "WebFetch",
               "WebSearch", "NotebookEdit", "Task", "TodoWrite", "Agent"]

_BRAIN_RULES = (
    "Tu es le CERVEAU de décision de DALIA, une assistante vocale personnelle. "
    "Tu n'utilises JAMAIS tes propres outils (Bash, Read, Edit...). Ton UNIQUE "
    "sortie est UN SEUL objet JSON, sans aucun texte autour, sans bloc markdown :\n"
    '- pour agir : {"tool_calls":[{"name":"<outil>","arguments":{<args>}}]}\n'
    '- pour répondre à l\'oral (aucune action) : {"content":"<phrase>"}\n'
    "N'invente aucun outil hors de la liste fournie. Si le propriétaire demande une action, "
    "appelle l'outil correspondant plutôt que de décrire. Respecte la persona "
    "DALIA ci-dessus. Réponds en français. JSON STRICT, rien d'autre."
)


class ClaudeUnavailable(RuntimeError):
    """Le CLI claude est injoignable / non authentifié."""


def _serialize(messages):
    """Persona (system) à part ; le reste de la conversation rendu en texte lisible."""
    persona = ""
    lines = []
    for m in messages:
        role = m.get("role")
        content = m.get("content") or ""
        if role == "system":
            persona = content
        elif role == "user":
            lines.append(f"Propriétaire : {content}")
        elif role == "assistant":
            if m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    fn = tc.get("function", {})
                    lines.append(f"DALIA a appelé l'outil {fn.get('name')}({fn.get('arguments')})")
            if content:
                lines.append(f"DALIA : {content}")
        elif role == "tool":
            lines.append(f"[résultat outil] {content}")
    return persona, "\n".join(lines)


def _tools_brief(tool_spec):
    out = []
    for t in (tool_spec or []):
        fn = t.get("function", {})
        out.append({"name": fn.get("name"), "description": fn.get("description"),
                    "parameters": fn.get("parameters")})
    return json.dumps(out, ensure_ascii=False)


def _extract_json(text):
    """Récupère l'objet JSON de la réponse de Claude (tolère fences markdown / texte
    autour). Renvoie un dict, ou None si rien d'exploitable."""
    s = (text or "").strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1] if s.count("```") >= 2 else s.strip("`")
        if s.startswith("json"):
            s = s[4:]
        s = s.strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # repli : isoler le premier { ... } équilibré
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _to_resp(content, tool_calls):
    calls = None
    if tool_calls:
        calls = []
        for tc in tool_calls:
            name = tc.get("name")
            if not name:
                continue
            args = tc.get("arguments")
            args_str = args if isinstance(args, str) else json.dumps(args or {}, ensure_ascii=False)
            calls.append(SimpleNamespace(
                id="call_" + uuid.uuid4().hex[:20], type="function",
                function=SimpleNamespace(name=name, arguments=args_str)))
        calls = calls or None
    msg = SimpleNamespace(content=content, tool_calls=calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)], model="claude-cli")


def call(messages, tool_spec, model=None):
    """Un tour de cerveau via le CLI claude. Renvoie un objet façon réponse OpenAI.
    model : surcharge ponctuelle (ex: « opus » pour la bascule à la demande)."""
    persona, convo = _serialize(messages)
    system = (persona + "\n\n" if persona else "") + _BRAIN_RULES
    prompt = (f"OUTILS DE DALIA (n'utilise QUE ceux-là) :\n{_tools_brief(tool_spec)}\n\n"
              f"CONVERSATION :\n{convo}\n\n"
              "Décide maintenant. Réponds par le seul objet JSON.")
    cmd = [CLAUDE_BIN, "-p", "--output-format", "json", "--max-turns", "1",
           "--append-system-prompt", system, "--disallowedTools", *_DENY_TOOLS]
    m = (model or CLAUDE_MODEL or "").strip()
    if m:
        cmd += ["--model", m]
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=_TIMEOUT)
    except FileNotFoundError:
        raise ClaudeUnavailable("CLI claude introuvable")
    except subprocess.TimeoutExpired:
        raise ClaudeUnavailable("claude a dépassé le délai")
    if r.returncode != 0:
        raise ClaudeUnavailable(f"claude code {r.returncode}: {(r.stderr or '')[:200]}")
    # enveloppe --output-format json : {"type":"result","result":"<texte>",...}
    try:
        env = json.loads(r.stdout)
        raw = env.get("result", "") if isinstance(env, dict) else r.stdout
    except json.JSONDecodeError:
        raw = r.stdout
    data = _extract_json(raw)
    if not isinstance(data, dict):
        # Claude n'a pas suivi le format : on traite tout comme une réponse parlée.
        return _to_resp((raw or "").strip() or None, None)
    tcs = data.get("tool_calls")
    if tcs:
        return _to_resp(data.get("content") or "", tcs)
    return _to_resp((data.get("content") or "").strip() or None, None)
