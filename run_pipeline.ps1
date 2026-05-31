# Store Intelligence Pipeline — Full Run Script
# Processes all 5 CCTV cameras and ingests events into the API
# Usage: .\run_pipeline.ps1 [clips_dir]

param(
    [string]$ClipsDir = "C:\Users\RajKumar5\Pictures\Screenshots\purpel Assignment\CCTV Footage-20260529T160731Z-3-00144614ea\CCTV Footage",
    [string]$StoreId = "STORE_BLR_002",
    [string]$ApiUrl = "http://localhost:8000",
    [string]$StartTime = "2026-03-03T09:00:00Z"
)

$ProjectDir = $PSScriptRoot
$OutputDir = "$ProjectDir\output\$StoreId"
$LayoutFile = "$ProjectDir\data\sample_store_layout.json"

Write-Host "=============================================" -ForegroundColor Cyan
Write-Host " Store Intelligence Pipeline" -ForegroundColor Cyan
Write-Host " Store: $StoreId | API: $ApiUrl" -ForegroundColor Cyan
Write-Host "=============================================" -ForegroundColor Cyan

# Create output directory
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$EventsFile = "$OutputDir\events_all.jsonl"
if (Test-Path $EventsFile) { Remove-Item $EventsFile }

# Camera → clip mapping
$Cameras = @{
    "CAM_ENTRY_01"   = "CAM 1.mp4"
    "CAM_FLOOR_01"   = "CAM 2.mp4"
    "CAM_FLOOR_02"   = "CAM 3.mp4"
    "CAM_BILLING_01" = "CAM 4.mp4"
    "CAM_FLOOR_03"   = "CAM 5.mp4"
}

# Build pipeline image once
Write-Host "`n[1/4] Building pipeline Docker image..." -ForegroundColor Yellow
docker build -t store-pipeline -f "$ProjectDir\Dockerfile.pipeline" $ProjectDir
if ($LASTEXITCODE -ne 0) { Write-Host "Build failed!" -ForegroundColor Red; exit 1 }
Write-Host "  Pipeline image built OK" -ForegroundColor Green

# Process each camera
Write-Host "`n[2/4] Processing CCTV clips..." -ForegroundColor Yellow
$camIndex = 0
foreach ($entry in $Cameras.GetEnumerator()) {
    $CameraId = $entry.Key
    $ClipFile = $entry.Value
    $ClipPath = "$ClipsDir\$ClipFile"

    if (-not (Test-Path $ClipPath)) {
        Write-Host "  WARNING: $ClipFile not found, skipping" -ForegroundColor DarkYellow
        continue
    }

    $CamOutput = "$OutputDir\events_$CameraId.jsonl"
    # Offset start time by 1 min per camera to simulate different start times
    $CamStart = [datetime]::Parse($StartTime).AddMinutes($camIndex * 2).ToString("yyyy-MM-ddTHH:mm:ssZ")

    Write-Host "  Processing $CameraId ($ClipFile)..." -ForegroundColor White

    docker run --rm `
        -v "${ClipsDir}:/clips" `
        -v "${ProjectDir}/data:/app/data" `
        -v "${OutputDir}:/app/output" `
        store-pipeline `
        --clip "/clips/$ClipFile" `
        --store-id $StoreId `
        --camera-id $CameraId `
        --layout /app/data/sample_store_layout.json `
        --output "/app/output/events_$CameraId.jsonl" `
        --start-time $CamStart `
        --model yolov8n.pt `
        --conf 0.35 `
        --device cpu

    if ($LASTEXITCODE -eq 0 -and (Test-Path $CamOutput)) {
        $count = (Get-Content $CamOutput | Measure-Object -Line).Lines
        Write-Host "    OK — $count events written to events_$CameraId.jsonl" -ForegroundColor Green
        Get-Content $CamOutput | Add-Content $EventsFile
    } else {
        Write-Host "    WARNING: No events from $CameraId" -ForegroundColor DarkYellow
    }

    $camIndex++
}

$totalEvents = if (Test-Path $EventsFile) { (Get-Content $EventsFile | Measure-Object -Line).Lines } else { 0 }
Write-Host "`n  Total events generated: $totalEvents" -ForegroundColor Cyan

# Validate events
Write-Host "`n[3/4] Validating event schema..." -ForegroundColor Yellow
docker run --rm `
    -v "${OutputDir}:/output" `
    -v "${ProjectDir}:/app" `
    python:3.11-slim `
    python /app/scripts/validate_events.py /output/events_all.jsonl 2>&1

# Ingest into API
Write-Host "`n[4/4] Ingesting events into API ($ApiUrl)..." -ForegroundColor Yellow
if ($totalEvents -gt 0) {
    docker run --rm `
        -v "${OutputDir}:/output" `
        -v "${ProjectDir}:/app" `
        --network host `
        python:3.11-slim `
        sh -c "pip install requests -q && python /app/pipeline/ingest_to_api.py --events /output/events_all.jsonl --api $ApiUrl"
} else {
    Write-Host "  No events to ingest" -ForegroundColor DarkYellow
}

Write-Host "`n=============================================" -ForegroundColor Cyan
Write-Host " Pipeline complete!" -ForegroundColor Green
Write-Host " Check metrics: $ApiUrl/stores/$StoreId/metrics" -ForegroundColor Cyan
Write-Host "=============================================" -ForegroundColor Cyan
