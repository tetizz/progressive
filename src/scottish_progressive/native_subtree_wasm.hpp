#pragma once

#include <cstdint>

#if defined(__EMSCRIPTEN__)
#include <emscripten/emscripten.h>
#define SPC_WASM_EXPORT EMSCRIPTEN_KEEPALIVE
#else
#define SPC_WASM_EXPORT
#endif

// Stable single-worker C ABI for the first browser vertical slice. The returned
// UTF-8 JSON pointer remains valid until the next call on this worker. This is
// intentionally a starting-position *kernel* API: safety_certified is always
// false until the exact root reply-mate screen/retry state machine is ported.
extern "C" {

SPC_WASM_EXPORT const char* spc_start_kernel_search_json(
    std::int32_t depth_series,
    std::uint32_t max_series_per_node,
    std::uint32_t max_work,
    std::uint32_t time_limit_ms
);

// General standard-chess boundary entry point. `fen` is the ordinary six-field
// FEN, `progressive_ep` is a comma-separated square list (or "-"), and
// `promoted_hex` is the exact python-chess promoted bitboard in hexadecimal.
// The two explicit fields preserve Progressive multi-EP and promoted provenance
// that an ordinary FEN cannot encode by itself. Chess960 castling is rejected.
SPC_WASM_EXPORT const char* spc_boundary_kernel_search_json(
    const char* fen,
    std::int32_t series_number,
    std::int32_t quiet_series,
    const char* progressive_ep,
    const char* promoted_hex,
    std::int32_t depth_series,
    std::uint32_t max_series_per_node,
    std::uint32_t max_work,
    std::uint32_t time_limit_ms
);

// Replays a slash-separated UCI micro-move prefix from an exact Progressive
// boundary and returns the current board, terminal handoff (if any), and every
// legal continuation. This is the browser replacement boundary for
// `/api/prefix`; it performs no engine search and therefore has no safety flag.
SPC_WASM_EXPORT const char* spc_boundary_prefix_json(
    const char* fen,
    std::int32_t series_number,
    std::int32_t quiet_series,
    const char* progressive_ep,
    const char* promoted_hex,
    const char* prefix_uci
);

SPC_WASM_EXPORT std::uint32_t spc_start_kernel_abi_version();

}

#undef SPC_WASM_EXPORT
