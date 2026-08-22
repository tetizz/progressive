param(
    [Parameter(Mandatory = $true)]
    [string]$EmPlusPlus,
    [string]$Output = "build/native-subtree-wasm/spc-start-kernel.js",
    [int64]$InitialMemoryBytes = 67108864,
    [int64]$MaximumMemoryBytes = 268435456
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
    "native_root_session_wasm.cpp",
    "native_root_session_wasm.hpp",
    "native_subtree_wasm.cpp",
    "native_subtree_wasm.hpp",
    "native_subtree_wasm_support.hpp"
)
$missingSources = @($requiredSources | Where-Object {
    -not (Test-Path -LiteralPath (Join-Path $package $_) -PathType Leaf)
})
if ($missingSources.Count -gt 0) {
    throw "WASM native-core dependency closure is missing: $($missingSources -join ', ')"
}
New-Item -ItemType Directory -Force -Path $outputDirectory | Out-Null
if (
    $InitialMemoryBytes -le 0 -or
    $MaximumMemoryBytes -lt $InitialMemoryBytes -or
    ($InitialMemoryBytes % 65536) -ne 0 -or
    ($MaximumMemoryBytes % 65536) -ne 0 -or
    $MaximumMemoryBytes -gt 268435456
) {
    throw "WASM memory limits must be positive 64KiB multiples with initial <= maximum <= 256MiB"
}

& $EmPlusPlus `
    (Join-Path $package "_native_eval.cpp") `
    (Join-Path $package "native_subtree.cpp") `
    (Join-Path $package "native_subtree_wasm.cpp") `
    (Join-Path $package "native_root_session_wasm.cpp") `
    -I $package `
    -std=c++20 `
    -O3 `
    -fexceptions `
    -DSPC_NATIVE_CORE_ONLY=1 `
    -sALLOW_MEMORY_GROWTH=1 `
    "-sINITIAL_MEMORY=$InitialMemoryBytes" `
    "-sMAXIMUM_MEMORY=$MaximumMemoryBytes" `
    "-sENVIRONMENT=worker,node" `
    -sMODULARIZE=1 `
    -sEXPORT_ES6=1 `
    -sFILESYSTEM=0 `
    "-sEXPORTED_FUNCTIONS=_spc_start_kernel_search_json,_spc_boundary_kernel_search_json,_spc_boundary_prefix_json,_spc_boundary_prefix_contract_json,_spc_start_kernel_abi_version,_spc_root_session_contract_json,_spc_root_session_create_json,_spc_root_session_enumerate_json,_spc_root_session_import_json,_spc_root_session_search_json,_spc_root_session_destroy,_spc_root_session_abi_version,_malloc,_free" `
    "-sEXPORTED_RUNTIME_METHODS=UTF8ToString,stringToNewUTF8,HEAPU8" `
    -o $absoluteOutput

if ($LASTEXITCODE -ne 0) {
    throw "Emscripten build failed with exit code $LASTEXITCODE"
}

Write-Output $absoluteOutput
