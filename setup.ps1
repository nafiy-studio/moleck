# Moleck - installateur Windows (PowerShell). (c) Nafiy Studio. Voir LICENSE.
# Usage : clic droit > Executer avec PowerShell, ou :  powershell -ExecutionPolicy Bypass -File setup.ps1
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

Write-Host "== Moleck : installation (Windows) =="

# 1. venv
if (-not (Test-Path ".venv")) {
  Write-Host "-> creation du venv (.venv)"
  python -m venv .venv
}
Write-Host "-> installation des dependances"
& .\.venv\Scripts\python.exe -m pip install --quiet --upgrade pip
& .\.venv\Scripts\python.exe -m pip install --quiet -r requirements.txt

# 2. .env
if (-not (Test-Path ".env")) {
  Copy-Item ".env.example" ".env"
  Write-Host "-> .env cree depuis .env.example"
  $name = Read-Host "Nom de l'assistante [Moleck]"
  if ([string]::IsNullOrWhiteSpace($name)) { $name = "Moleck" }
  $owner = Read-Host "Ton nom (proprietaire)"
  (Get-Content ".env") | ForEach-Object {
    if ($_ -match "^ASSISTANT_NAME=") { "ASSISTANT_NAME=$name" }
    elseif ($_ -match "^OWNER_NAME=" -and $owner) { "OWNER_NAME=$owner" }
    else { $_ }
  } | Set-Content ".env"
  Write-Host "-> identite ecrite : $name"
} else {
  Write-Host "-> .env existe deja, on n'y touche pas"
}

Write-Host ""
Write-Host "Installation terminee."
Write-Host "Etape suivante : edite .env et branche TON cerveau LLM"
Write-Host "  LITELLM_BASE_URL, LITELLM_API_KEY, MODEL_PRIMARY"
Write-Host ""
Write-Host "Lancer :"
Write-Host "  .\.venv\Scripts\python.exe moleck.py --text ""bonjour"""
Write-Host "  .\.venv\Scripts\python.exe moleck.py"
Write-Host "Voir GUIDE_INSTALLATION_MOLECK.pdf pour le detail."
