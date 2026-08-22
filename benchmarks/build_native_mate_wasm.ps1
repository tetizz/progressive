param(
    [Parameter(Mandatory = $true)]
    [string]$EmPlusPlus,
    [string]$Output = "build/mate-wasm/spc-series-mate.js"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$package = Join-Path $root "src/scottish_progressive"
$absoluteOutput = Join-Path $root $Output
$outputDirectory = Split-Path -Parent $absoluteOutput
New-Item -ItemType Directory -Force -Path $outputDirectory | Out-Null

& $EmPlusPlus `
    (Join-Path $package "_native_mate.cpp") `
    -I $package `
    -std=c++20 `
    -O3 `
    -fexceptions `
    -DSPC_NATIVE_MATE_CORE_ONLY=1 `
    -sALLOW_MEMORY_GROWTH=1 `
    "-sENVIRONMENT=worker,node" `
    -sMODULARIZE=1 `
    -sEXPORT_ES6=1 `
    -sFILESYSTEM=0 `
    "-sEXPORTED_FUNCTIONS=_spc_series_mate_search_json,_spc_series_mate_abi_version,_malloc,_free" `
    "-sEXPORTED_RUNTIME_METHODS=UTF8ToString,stringToNewUTF8" `
    -o $absoluteOutput

if ($LASTEXITCODE -ne 0) {
    throw "Emscripten mate build failed with exit code $LASTEXITCODE"
}

Write-Output $absoluteOutput
