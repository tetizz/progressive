#pragma once

#include <array>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <optional>
#include <string>
#include <string_view>
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
    std::int8_t from_square;
    std::int8_t to_square;
    std::int8_t promotion;
    std::int8_t required_ep_square;
};

static_assert(sizeof(LegalMove) == 4);

[[nodiscard]] std::uint16_t legal_move_uci_key(
    const LegalMove& move
) noexcept;

[[nodiscard]] std::string legal_move_uci(const LegalMove& move);

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

struct FullWeights {
    std::int64_t material;
    std::int64_t king_space;
    std::int64_t series_reach;
    std::int64_t promotion_corridors;
    std::int64_t immediate_vulnerability;
    std::int64_t useful_mobility;
    std::int64_t boundary_check;
};

struct ReachProbe {
    std::optional<std::int64_t> distance;
    std::uint64_t nodes = 0;
    bool complete = true;
};

struct FullEvaluation {
    std::int64_t total = 0;
    std::int64_t material = 0;
    std::int64_t king_space = 0;
    std::int64_t series_reach = 0;
    std::int64_t promotion_corridors = 0;
    std::int64_t immediate_vulnerability = 0;
    std::int64_t useful_mobility = 0;
    std::int64_t boundary_check = 0;
    ReachProbe white_reach;
    ReachProbe black_reach;
    std::uint64_t capture_reach_positions = 0;
    bool capture_reach_complete = true;
    bool tactical_unstable = false;
};

// Frozen Python contract in teacher_value_features.py.  Every supported
// model group is a prefix of this exact order (7, 14, 19, 38, 44, or 47).
// Keeping the values in the shared native core makes the same implementation
// available to CPython and the single-threaded WebAssembly build.
inline constexpr std::size_t TEACHER_VALUE_FEATURE_COUNT = 47;
inline constexpr std::int64_t DEEP_TEACHER_FIXED_POINT_SCALE = 1'000'000'000;

struct TeacherValueFeaturesV3 {
    std::array<std::int64_t, TEACHER_VALUE_FEATURE_COUNT> values{};
    ReachProbe white_reach;
    ReachProbe black_reach;
    // Exact generated legal variants used by the direct and two-move threat
    // suffix.  A search caller can account for this work before activation.
    std::uint64_t direct_move_variants = 0;
    std::uint64_t two_move_variants = 0;
};

struct DeepTeacherLinearModelV1 {
    std::array<std::int64_t, TEACHER_VALUE_FEATURE_COUNT> coefficients{};
    std::size_t feature_count = 0;
    std::int64_t fixed_point_scale = DEEP_TEACHER_FIXED_POINT_SCALE;
};

// Root-choice safety contract shared by CPython and the WebAssembly build.
// A candidate is adverse only when its proof interval is the exact opponent
// result. Unknown and partial intervals remain eligible. If every candidate is
// adverse, comparing them pairwise still falls back to the ordinary mover
// score and canonical notation order.
struct ProofAwareRootCandidateV1 {
    std::int64_t score = 0;
    std::array<int, 2> proof_bounds{-1, 1};
    std::string_view machine_notation;
};

[[nodiscard]] bool root_candidate_is_proven_adverse_v1(
    bool mover_white,
    const std::array<int, 2>& proof_bounds
) noexcept;

[[nodiscard]] bool proof_aware_root_precedes_v1(
    bool mover_white,
    const ProofAwareRootCandidateV1& left,
    const ProofAwareRootCandidateV1& right
) noexcept;

inline constexpr std::uint8_t S3_NEURAL_ORDERING_MODEL = 1;
inline constexpr std::int64_t S3_NEURAL_ORDERING_BLEND_PERCENT = 75;

struct FinalSeriesScore {
    std::uint64_t max_returned_series;
    std::int64_t ply_from_root;
    std::int64_t mate_score;
    FastWeights weights;
    // Optional frozen ordering model. A non-zero model identifier may alter
    // only the static ranking used by the final top-K cap; terminal outcomes
    // and all uncapped generation remain authoritative and unchanged.
    std::uint8_t neural_ordering_model = 0;
    std::int64_t neural_blend_percent = 0;
};

enum class SeriesGenerationStatus : std::uint8_t {
    Complete = 0,
    WorkLimit = 1,
    InvalidPrefix = 2,
    Unsupported = 3,
    Deadline = 4,
};

enum class PathCountOverflowMode : std::uint8_t {
    Reject = 0,
    Saturate = 1,
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
    std::uint64_t tactical_frontier_states_retained = 0;
    std::uint64_t tactical_frontier_reserve_drops = 0;
    std::uint64_t tactical_final_series_retained = 0;
    std::uint64_t tactical_final_reserve_drops = 0;
    std::uint64_t peak_frontier_states = 0;
    std::uint64_t required_prefix_moves = 0;
    std::uint64_t path_count_saturations = 0;
    bool work_limit_reached = false;
};

struct CompleteSeriesPath {
    std::vector<std::string> moves;
    std::uint64_t transposition_count = 1;
};

enum class CompleteSeriesOutcome : std::uint8_t {
    None = 0,
    Checkmate = 1,
    Stalemate = 2,
    TenSeriesDraw = 3,
};

struct CompleteSeriesCandidate {
    CompleteSeriesPath path;
    BoardState board;
    std::int64_t halfmove_clock = 0;
    std::int64_t fullmove_number = 1;
    std::int64_t series_number = 1;
    std::int64_t quiet_series = 0;
    std::vector<int> ep_targets;
    CompleteSeriesOutcome outcome = CompleteSeriesOutcome::None;
    bool ended_by_check = false;
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
    // Exact public complete-series calls retain the historical reject behavior.
    // The full-game v2 exploration kernel opts into saturation because path
    // multiplicity is evidence only and is not consumed by its move selector.
    PathCountOverflowMode path_count_overflow_mode = PathCountOverflowMode::Reject;
    // Saturating consumers may choose a lower transport-safe ceiling. Native
    // root sessions use JavaScript's largest exactly representable integer;
    // native-only consumers retain the full uint64_t range.
    std::uint64_t path_count_saturation_limit =
        std::numeric_limits<std::uint64_t>::max();
    // The binding converts a relative Python request budget to this process's
    // own steady-clock epoch. No cross-runtime clock-epoch assumption leaks
    // into the generation kernel.
    std::optional<std::chrono::steady_clock::time_point> deadline = std::nullopt;
    // Execution-only parallelism. Search/tournament callers default to one so
    // existing worker-level parallelism never oversubscribes implicitly.
    std::uint32_t worker_threads = 1;
    // Opt-in selective-search lane. Full-game corpus exploration keeps its
    // historical fixed-width policy unless it explicitly requests this.
    bool tactical_protection = false;
    // Bound-only alpha-beta calls may stop once the mover has a legal
    // delivered mate. Exact/full-window callers leave this false so their
    // canonical retained frontier and principal variation stay unchanged.
    bool stop_on_mover_mate = false;
};

struct CompleteSeriesResponse {
    SeriesGenerationStatus status = SeriesGenerationStatus::Complete;
    std::string message;
    SeriesGenerationStats stats;
    std::vector<CompleteSeriesCandidate> series;
    // True only when generation returned a partial frontier after the
    // request's bound-only mover-mate condition was satisfied.
    bool stopped_on_mover_mate = false;
};

[[nodiscard]] std::optional<std::int64_t> fast_evaluate(
    const Position& position,
    const FastWeights& weights
) noexcept;

[[nodiscard]] std::optional<FullEvaluation> full_evaluate(
    const BoardState& position,
    const std::vector<int>& ep_targets,
    std::int64_t series_number,
    std::uint64_t max_reach_positions,
    const FullWeights& weights
);

[[nodiscard]] std::optional<TeacherValueFeaturesV3>
teacher_value_features_v3(
    const BoardState& position,
    const std::vector<int>& ep_targets,
    std::int64_t series_number,
    std::uint64_t max_reach_positions = 256,
    std::size_t feature_count = TEACHER_VALUE_FEATURE_COUNT
);

// Returns the exact fixed-point dot product used by
// fit_deep_teacher_value.py.  It deliberately does not divide by the scale:
// doing so would introduce ranking ties that the frozen Python evaluator does
// not have.  Terminal mate/draw values remain the searcher's responsibility.
[[nodiscard]] std::optional<std::int64_t> deep_teacher_score_v1(
    const TeacherValueFeaturesV3& features,
    const DeepTeacherLinearModelV1& model
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

// These small rule helpers are part of the standalone native-core surface.
// They let a Python-free/WASM facade validate and replay exact boundaries
// without reaching into this translation unit's private move generator.
[[nodiscard]] bool is_in_check(const BoardState& position) noexcept;

[[nodiscard]] bool has_insufficient_material(
    const BoardState& position
) noexcept;

[[nodiscard]] std::vector<int> canonical_ep_targets(
    const BoardState& position,
    Bitboard pending_ep_targets
);

[[nodiscard]] CompleteSeriesResponse generate_complete_series(
    const CompleteSeriesRequest& request
);

}  // namespace spc::native
