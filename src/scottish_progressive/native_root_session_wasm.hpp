#pragma once

#include <cstdint>

#if defined(__EMSCRIPTEN__)
#include <emscripten/emscripten.h>
#define SPC_ROOT_WASM_EXPORT EMSCRIPTEN_KEEPALIVE
#else
#define SPC_ROOT_WASM_EXPORT
#endif

// Persistent single-Worker retained-root ABI. Requests are caller-owned UTF-8
// JSON byte ranges (they need not be NUL terminated). Returned UTF-8 JSON is
// facade-owned and remains valid until the next root-session ABI call in this
// Worker. No returned pointer crosses an allocation/free boundary.
extern "C" {

SPC_ROOT_WASM_EXPORT const char* spc_root_session_contract_json();

SPC_ROOT_WASM_EXPORT const char* spc_root_session_create_json(
    const char* request_json,
    std::uint32_t request_length
);

SPC_ROOT_WASM_EXPORT const char* spc_root_session_enumerate_json(
    std::uint32_t session_id,
    const char* request_json,
    std::uint32_t request_length
);

SPC_ROOT_WASM_EXPORT const char* spc_root_session_import_json(
    std::uint32_t session_id,
    const char* request_json,
    std::uint32_t request_length
);

SPC_ROOT_WASM_EXPORT const char* spc_root_session_search_json(
    std::uint32_t session_id,
    const char* request_json,
    std::uint32_t request_length
);

// Idempotent: 1 means the named live session was destroyed, 0 means no such
// live session existed. Both outcomes leave the Worker with no matching state.
SPC_ROOT_WASM_EXPORT std::int32_t spc_root_session_destroy(
    std::uint32_t session_id
);

SPC_ROOT_WASM_EXPORT std::uint32_t spc_root_session_abi_version();

}

#undef SPC_ROOT_WASM_EXPORT
