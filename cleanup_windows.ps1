#requires -RunAsAdministrator

$ErrorActionPreference = "SilentlyContinue"

Write-Host "============================================" -ForegroundColor Cyan
Write-Host "        WINDOWS STORAGE CLEANUP" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

function Get-FreeGB {
    $drive = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='C:'"
    return [math]::Round($drive.FreeSpace / 1GB, 2)
}

$before = Get-FreeGB
Write-Host "Free space before cleanup: $before GB" -ForegroundColor Yellow
Write-Host ""

# --------------------------------------------------
# 1. User TEMP
# --------------------------------------------------
Write-Host "[1/8] Cleaning user temporary files..." -ForegroundColor Green

Get-ChildItem "$env:TEMP" -Force |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

# --------------------------------------------------
# 2. Windows TEMP
# --------------------------------------------------
Write-Host "[2/8] Cleaning Windows temporary files..." -ForegroundColor Green

Get-ChildItem "C:\Windows\Temp" -Force |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

# --------------------------------------------------
# 3. Windows Update download cache
# --------------------------------------------------
Write-Host "[3/8] Cleaning Windows Update download cache..." -ForegroundColor Green

Stop-Service wuauserv -Force -ErrorAction SilentlyContinue
Stop-Service bits -Force -ErrorAction SilentlyContinue

Get-ChildItem "C:\Windows\SoftwareDistribution\Download" -Force |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

Start-Service bits -ErrorAction SilentlyContinue
Start-Service wuauserv -ErrorAction SilentlyContinue

# --------------------------------------------------
# 4. Delivery Optimization cache
# --------------------------------------------------
Write-Host "[4/8] Cleaning Delivery Optimization cache..." -ForegroundColor Green

try {
    Delete-DeliveryOptimizationCache -Force
} catch {
    Remove-Item "C:\Windows\SoftwareDistribution\DeliveryOptimization" `
        -Recurse -Force -ErrorAction SilentlyContinue
}

# --------------------------------------------------
# 5. Recycle Bin
# --------------------------------------------------
Write-Host "[5/8] Emptying Recycle Bin..." -ForegroundColor Green

Clear-RecycleBin -Force -ErrorAction SilentlyContinue

# --------------------------------------------------
# 6. Windows Error Reporting
# --------------------------------------------------
Write-Host "[6/8] Cleaning Windows error-reporting files..." -ForegroundColor Green

$werPaths = @(
    "C:\ProgramData\Microsoft\Windows\WER\ReportArchive",
    "C:\ProgramData\Microsoft\Windows\WER\ReportQueue",
    "$env:LOCALAPPDATA\Microsoft\Windows\WER\ReportArchive",
    "$env:LOCALAPPDATA\Microsoft\Windows\WER\ReportQueue"
)

foreach ($path in $werPaths) {
    if (Test-Path $path) {
        Get-ChildItem $path -Force |
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# --------------------------------------------------
# 7. Windows component store cleanup
# --------------------------------------------------
Write-Host "[7/8] Cleaning old Windows components..." -ForegroundColor Green
Write-Host "This can take several minutes..." -ForegroundColor DarkYellow

DISM.exe /Online /Cleanup-Image /StartComponentCleanup /NoRestart

# --------------------------------------------------
# 8. Run Microsoft's built-in Disk Cleanup
# --------------------------------------------------
Write-Host "[8/8] Running Disk Cleanup..." -ForegroundColor Green

$cleanupKey = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\VolumeCaches"

$cleanupItems = @(
    "Active Setup Temp Folders",
    "BranchCache",
    "Delivery Optimization Files",
    "Device Driver Packages",
    "Downloaded Program Files",
    "Internet Cache Files",
    "Language Pack",
    "Offline Pages Files",
    "Previous Installations",
    "Recycle Bin",
    "Service Pack Cleanup",
    "Setup Log Files",
    "System error memory dump files",
    "System error minidump files",
    "Temporary Files",
    "Temporary Setup Files",
    "Thumbnail Cache",
    "Update Cleanup",
    "Upgrade Discarded Files",
    "Windows Defender",
    "Windows Error Reporting Files",
    "Windows Upgrade Log Files"
)

foreach ($item in $cleanupItems) {
    $key = Join-Path $cleanupKey $item

    if (Test-Path $key) {
        Set-ItemProperty -Path $key -Name StateFlags0011 -Type DWord -Value 2
    }
}

Start-Process cleanmgr.exe -ArgumentList "/sagerun:11" -Wait

# --------------------------------------------------
# Final space calculation
# --------------------------------------------------
$after = Get-FreeGB
$recovered = [math]::Round($after - $before, 2)

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "              CLEANUP COMPLETE" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Free space before : $before GB" -ForegroundColor Yellow
Write-Host "Free space after  : $after GB" -ForegroundColor Green
Write-Host "Space recovered   : $recovered GB" -ForegroundColor Green
Write-Host ""

Write-Host "Windows may have more space available after a restart." -ForegroundColor Cyan
Write-Host "Press Enter to exit..."
Read-Host