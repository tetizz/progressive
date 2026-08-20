#pragma once

#include <array>
#include <cstdint>
#include <optional>
#include <string>
#include <vector>

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

struct BoardState {
    Bitboard pawns;
    Bitboard knights;
    Bitboard bishops;
    Bitboard rooks;
    Bitboard queens;
    Bitboard kings;
    std::array<Bitboard, 2> occupied;
    Bitboard promoted;
    Bitboard castling_rights;
    bool white_to_move;
};

struct LegalMove {
    std::string uci;
    int from_square;
    int to_square;
    int promotion;
    int required_ep_square;
};

struct ExpandedMove {
    LegalMove move;
    BoardState child;
    bool is_pawn_move;
    bool is_capture;
    bool delivered_check;
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

[[nodiscard]] std::vector<LegalMove> legal_move_variants(
    const BoardState& position,
    const std::vector<int>& ep_targets
);

[[nodiscard]] std::vector<ExpandedMove> expand_legal_move_variants(
    const BoardState& position,
    const std::vector<int>& ep_targets
);

[[nodiscard]] bool has_legal_move(
    const BoardState& position,
    const std::vector<int>& ep_targets
);

}  // namespace spc::native
