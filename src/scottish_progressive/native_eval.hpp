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

struct FinalSeriesScore {
    std::uint64_t max_returned_series;
    std::int64_t ply_from_root;
    std::int64_t mate_score;
    FastWeights weights;
};

enum class SeriesGenerationStatus : std::uint8_t {
    Complete = 0,
    WorkLimit = 1,
    InvalidPrefix = 2,
    Unsupported = 3,
};

struct SeriesGenerationStats {
    std::uint64_t positions_visited = 0;
    std::uint64_t frontier_score_positions = 0;
    std::uint64_t raw_series = 0;
    std::uint64_t unique_series = 0;
    std::uint64_t transpositions_merged = 0;
    std::uint64_t checking_series = 0;
    std::uint64_t checkmates = 0;
    std::uint64_t stalemates = 0;
    std::uint64_t frontier_prunes = 0;
    std::uint64_t frontier_states_pruned = 0;
    std::uint64_t frontier_paths_pruned = 0;
    std::uint64_t peak_frontier_states = 0;
    std::uint64_t required_prefix_moves = 0;
    bool work_limit_reached = false;
};

struct CompleteSeriesPath {
    std::vector<std::string> moves;
    std::uint64_t transposition_count = 1;
};

struct CompleteSeriesRequest {
    BoardState board;
    std::int64_t halfmove_clock;
    std::int64_t fullmove_number;
    std::int64_t series_number;
    std::int64_t quiet_series;
    std::vector<int> ep_targets;
    std::vector<std::string> required_prefix;
    std::optional<std::uint64_t> max_frontier_states;
    std::optional<std::uint64_t> max_positions;
    std::optional<FastWeights> frontier_weights;
    std::optional<FinalSeriesScore> final_series_score;
};

struct CompleteSeriesResponse {
    SeriesGenerationStatus status = SeriesGenerationStatus::Complete;
    std::string message;
    SeriesGenerationStats stats;
    std::vector<CompleteSeriesPath> series;
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

[[nodiscard]] CompleteSeriesResponse generate_complete_series(
    const CompleteSeriesRequest& request
);

}  // namespace spc::native
