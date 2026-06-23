#!/usr/bin/env bash
# Moleck — installateur. Cree le venv, installe les deps, prepare .env.
set -euo pipefail
cd "$(dirname "$0")"

echo "== Moleck : installation =="

# 1. Python venv
if [ ! -d .venv ]; then
  echo "-> creation du venv (.venv)"
  python3 -m venv .venv
fi
echo "-> installation des dependances"
./.venv/bin/python3 -m pip install --quiet --upgrade pip
./.venv/bin/python3 -m pip install --quiet -r requirements.txt

# 2. .env
if [ ! -f .env ]; then
  cp .env.example .env
  echo "-> .env cree depuis .env.example"

  read -r -p "Nom de l'assistante [Moleck] : " name
  name="${name:-Moleck}"
  read -r -p "Ton nom (proprietaire) : " owner
  # injecte dans .env
  /usr/bin/sed -i '' "s/^ASSISTANT_NAME=.*/ASSISTANT_NAME=${name}/" .env 2>/dev/null || \
    sed -i "s/^ASSISTANT_NAME=.*/ASSISTANT_NAME=${name}/" .env
  if [ -n "${owner}" ]; then
    /usr/bin/sed -i '' "s/^OWNER_NAME=.*/OWNER_NAME=${owner}/" .env 2>/dev/null || \
      sed -i "s/^OWNER_NAME=.*/OWNER_NAME=${owner}/" .env
  fi
  echo "-> identite ecrite : ${name}"
else
  echo "-> .env existe deja, on n'y touche pas"
fi

cat <<EOF

Installation terminee.

Etape suivante OBLIGATOIRE : edite .env et branche TON cerveau LLM
  LITELLM_BASE_URL, LITELLM_API_KEY, MODEL_PRIMARY

Lancer :
  .venv/bin/python3 moleck.py --text "bonjour"   # test sans micro
  .venv/bin/python3 moleck.py                     # voix + orbe

Voir GUIDE_INSTALLATION_MOLECK.pdf pour le detail.
EOF
