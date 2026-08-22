#pragma once

#include "native_subtree.hpp"

#include <cstdint>
#include <string>

namespace spc::wasm {

// Shared only by the two WASM facade translation units. Keeping exact boundary
// parsing and serialization here prevents the root-session ABI from drifting
// from the independently certified prefix ABI.
[[nodiscard]] bool parse_exact_boundary(
    const char* fen,
    std::int32_t series_number,
    std::int32_t quiet_series,
    const char* progressive_ep,
    const char* promoted_hex,
    native::SubtreeState& state,
    std::string& error
);

[[nodiscard]] std::string exact_boundary_json(
    const native::SubtreeState& state
);

}  // namespace spc::wasm
