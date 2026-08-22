param(
    [Parameter(Mandatory = $true)]
    [string]$EmPlusPlus,
    [string]$Output = "build/native-subtree-wasm/spc-start-kernel.js"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$package = Join-Path $root "src/scottish_progressive"
$absoluteOutput = Join-Path $root $Output
$outputDirectory = Split-Path -Parent $absoluteOutput
$requiredSources = @(
    "_native_eval.cpp",
    "native_eval.hpp",
    "native_subtree.cpp",
    "native_subtree.hpp",
    "native_subtree_wasm.cpp",
    "native_subtree_wasm.hpp"
)
$missingSources = @($requiredSources | Where-Object {
    -not (Test-Path -LiteralPath (Join-Path $package $_) -PathType Leaf)
})
if ($missingSources.Count -gt 0) {
    throw "WASM native-core dependency closure is missing: $($missingSources -join ', ')"
}
New-Item -ItemType Directory -Force -Path $outputDirectory | Out-Null

& $EmPlusPlus `
    (Join-Path $package "_native_eval.cpp") `
    (Join-Path $package "native_subtree.cpp") `
    (Join-Path $package "native_subtree_wasm.cpp") `
    -I $package `
    -std=c++20 `
    -O3 `
    -fexceptions `
    -DSPC_NATIVE_CORE_ONLY=1 `
    -sALLOW_MEMORY_GROWTH=1 `
    "-sENVIRONMENT=worker,node" `
    -sMODULARIZE=1 `
    -sEXPORT_ES6=1 `
    -sFILESYSTEM=0 `
    "-sEXPORTED_FUNCTIONS=_spc_start_kernel_search_json,_spc_boundary_kernel_search_json,_spc_boundary_prefix_json,_spc_boundary_prefix_contract_json,_spc_start_kernel_abi_version,_malloc,_free" `
    "-sEXPORTED_RUNTIME_METHODS=UTF8ToString,stringToNewUTF8" `
    -o $absoluteOutput

if ($LASTEXITCODE -ne 0) {
    throw "Emscripten build failed with exit code $LASTEXITCODE"
}

Write-Output $absoluteOutput
