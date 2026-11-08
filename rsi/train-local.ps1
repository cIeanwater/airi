[CmdletBinding()]
param(
    [string]$Dataset,
    [string]$Config = "rsi/train-config.example.json",
    [ValidateSet("Check", "Tokenize", "Train", "Merge")][string]$Action = "Check",
    [ValidateSet("lora", "qlora")][string]$Method,
    [string]$Python = "rsi/.venv/Scripts/python.exe",
    [string]$Output,
    [string]$Report,
    [string]$Adapter,
    [string]$ResumeCheckpoint
)

$ErrorActionPreference = "Stop"
$repository = Split-Path $PSScriptRoot -Parent
Push-Location -LiteralPath $repository
try {
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
        throw "The selected Python environment does not exist. Create an isolated environment and install the CUDA training dependencies first."
    }
    if ($env:CUDA_VISIBLE_DEVICES -and $env:CUDA_VISIBLE_DEVICES.Contains(",")) {
        throw "Select one GPU with CUDA_VISIBLE_DEVICES before starting this single-GPU runner."
    }
    if (-not $env:CUDA_VISIBLE_DEVICES) { $env:CUDA_VISIBLE_DEVICES = "0" }
    $arguments = @("-m", "rsi.finetune", "--config", $Config)
    if ($PSBoundParameters.ContainsKey("Method")) { $arguments += @("--method", $Method) }
    if ($Dataset) { $arguments += @("--dataset", $Dataset) }
    switch ($Action) {
        "Check" { $arguments += "--check" }
        "Tokenize" { $arguments += "--tokenize-only" }
        "Train" {
            if (-not $Output) { throw "Training requires -Output with a new run directory." }
            $arguments += "--train"
        }
        "Merge" {
            if (-not $Output -or -not $Adapter) { throw "Merging requires -Output and -Adapter." }
            $arguments += "--merge-only"
        }
    }
    if ($Output) { $arguments += @("--output", $Output) }
    if ($Report) { $arguments += @("--report", $Report) }
    if ($Adapter) { $arguments += @("--adapter", $Adapter) }
    if ($ResumeCheckpoint) { $arguments += @("--resume-from-checkpoint", $ResumeCheckpoint) }
    & $Python @arguments
    if ($LASTEXITCODE -ne 0) { throw "The local training command failed with exit code $LASTEXITCODE." }
}
finally {
    Pop-Location
}
