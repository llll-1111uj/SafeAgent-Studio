# Start the API in Docker and the Vite frontend on the host.
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Error "Docker is not on PATH. Start Docker Desktop, then run this script again."
}

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example. Fill in ANTHROPIC_API_KEY and MODEL_ID, then run .\start.ps1 again."
    exit 1
}

if (Get-Command docker-compose -ErrorAction SilentlyContinue) {
    & docker-compose up -d --build
} else {
    & docker compose up -d --build
}
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Set-Location (Join-Path $PSScriptRoot "frontend")
if (-not (Test-Path "node_modules")) {
    npm install
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}
npm run dev
