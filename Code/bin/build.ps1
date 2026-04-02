# ============================================================
# WCB Firmware Build Script (PowerShell)
# Produces bin files for all WCB hardware versions
#
# Usage: run from anywhere, script locates itself
#   .\build.ps1
#   or
#   Code\bin\build.ps1
#
# Requirements:
#   - Arduino CLI installed and in PATH
#   - ESP32 platform installed at v3.3.4
#   - EspSoftwareSerial and Adafruit NeoPixel libraries installed
#
# Output:
#   Code\bin\WCB_<version>_<branch>_ESP32.bin    (v1.0, v2.1, v2.3, v2.4)
#   Code\bin\WCB_<version>_<branch>_ESP32S3.bin  (v3.1, v3.2)
# ============================================================

$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$SketchPath = Join-Path $ScriptDir "..\WCB"
$OutputDir  = $ScriptDir

# Get version string from WCB.ino - only match the variable declaration line
$VersionLine = (Select-String -Path "$SketchPath\WCB.ino" -Pattern 'String SoftwareVersion =' | Select-Object -First 1).Line
$Version = [regex]::Match($VersionLine, '"([^"]*)"').Groups[1].Value

# Get current git branch
$Branch = git -C $SketchPath rev-parse --abbrev-ref HEAD

Write-Host ""
Write-Host "========================================"
Write-Host " WCB Firmware Build"
Write-Host " Version : $Version"
Write-Host " Branch  : $Branch"
Write-Host " Sketch  : $SketchPath"
Write-Host " Output  : $OutputDir"
Write-Host "========================================"

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

$Pass = 0
$Fail = 0

# ----------------------------------------
# Build 1 - ESP32 (v1.0, v2.1, v2.3, v2.4)
# ----------------------------------------
Write-Host ""
Write-Host "Building ESP32 binary (v1.0, v2.1, v2.3, v2.4)..."

$TempEsp32 = "$env:TEMP\wcb_esp32"
arduino-cli compile `
    --fqbn esp32:esp32:esp32:PartitionScheme=default `
    --output-dir $TempEsp32 `
    $SketchPath

if ($LASTEXITCODE -eq 0) {
    $OutFile = Join-Path $OutputDir "WCB_${Version}_${Branch}_ESP32.bin"
    Copy-Item "$TempEsp32\WCB.ino.bin" $OutFile -Force
    Write-Host "✓ Built: $(Split-Path $OutFile -Leaf)"
    $Pass++
} else {
    Write-Host "✗ Failed: ESP32 build"
    $Fail++
}

Remove-Item -Recurse -Force $TempEsp32 -ErrorAction SilentlyContinue

# ----------------------------------------
# Build 2 - ESP32-S3 (v3.1, v3.2)
# ----------------------------------------
Write-Host ""
Write-Host "Building ESP32-S3 binary (v3.1, v3.2)..."

$TempS3 = "$env:TEMP\wcb_s3"
arduino-cli compile `
    --fqbn esp32:esp32:esp32s3:PartitionScheme=default `
    --output-dir $TempS3 `
    $SketchPath

if ($LASTEXITCODE -eq 0) {
    $OutFile = Join-Path $OutputDir "WCB_${Version}_${Branch}_ESP32S3.bin"
    Copy-Item "$TempS3\WCB.ino.bin" $OutFile -Force
    Write-Host "✓ Built: $(Split-Path $OutFile -Leaf)"
    $Pass++
} else {
    Write-Host "✗ Failed: ESP32-S3 build"
    $Fail++
}

Remove-Item -Recurse -Force $TempS3 -ErrorAction SilentlyContinue

# ----------------------------------------
Write-Host ""
Write-Host "========================================"
Write-Host " Build complete: $Pass passed, $Fail failed"
Write-Host " Binaries:"
Get-ChildItem "$OutputDir\*.bin" | ForEach-Object {
    Write-Host ("  {0,-50} {1,8} KB" -f $_.Name, [math]::Round($_.Length/1KB, 1))
}
Write-Host "========================================"