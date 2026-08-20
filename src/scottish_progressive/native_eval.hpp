#pragma once

#include <array>
#include <cstdint>
#include <optional>

namespace spc::native {

using Bitboard = std::uint64_t;

struct Position {
    Bitboard pawns;
    Bitboard knights;
    Bitboard bishops;
    Bitboard rooks;
    Bitboard queens;
    Bitboard kings;
    std::array<Bitboard, 2> occupied;
    bool white_to_move;
    std::int64_t series_number;
};

struct FastWeights {
    std::int64_t material;
    std::int64_t king_space;
    std::int64_t promotion_corridors;
    std::int64_t immediate_vulnerability;
    std::int64_t boundary_check;
};

[[nodiscard]] std::optional<std::int64_t> fast_evaluate(
    const Position& position,
    const FastWeights& weights
) noexcept;

}  // namespace spc::native
