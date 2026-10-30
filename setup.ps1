# Sets up Ksoko for local development on Windows. Run from this folder:  .\setup.ps1
# If PowerShell refuses to run scripts:  Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
$ErrorActionPreference = "Stop"

function Step($text, [scriptblock]$cmd) {
    Write-Host "`n==> $text" -ForegroundColor Green
    & $cmd
    if ($LASTEXITCODE -ne 0) { throw "That step failed (exit code $LASTEXITCODE). Read the message above, fix it, and run .\setup.ps1 again." }
}

$py = if (Get-Command python -ErrorAction SilentlyContinue) { "python" } elseif (Get-Command py -ErrorAction SilentlyContinue) { "py" } else { throw "Python isn't installed. Get it from python.org and tick 'Add python.exe to PATH'." }

if (-not (Test-Path venv)) { Step "Creating a virtual environment" { & $py -m venv venv } }
. .\venv\Scripts\Activate.ps1

if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Write-Host "`nCreated .env from .env.example (development mode is on)." -ForegroundColor Green
}

Step "Installing packages"            { python -m pip install -r requirements.txt }
Step "Creating the database changes"  { python manage.py makemigrations market }
Step "Applying them"                  { python manage.py migrate }
Step "Adding the starter categories"  { python manage.py seed_categories }

Write-Host "`nAll set. Next:" -ForegroundColor Green
Write-Host "  python manage.py createsuperuser"
Write-Host "  python manage.py set_email YOURUSERNAME you@example.com"
Write-Host "  python manage.py runserver"
Write-Host "(Run '.\venv\Scripts\Activate.ps1' first in any new PowerShell window.)"
