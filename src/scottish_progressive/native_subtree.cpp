#include "native_subtree.hpp"

#include <algorithm>
#include <array>
#include <bit>
#include <chrono>
#include <cstdint>
#include <iterator>
#include <limits>
#include <list>
#include <memory>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <unordered_map>
#include <utility>
#include <vector>

namespace spc::native {
namespace {

constexpr bool WHITE = true;
constexpr std::array<int, 2> UNKNOWN_PROOF_BOUNDS{-1, 1};
constexpr std::int64_t TACTICAL_DESCENDANT_PROMOTION_MAX_PLY = 2;
constexpr std::int64_t ROOT_TACTICAL_PROTECTION_MIN_SERIES = 5;
constexpr std::uint64_t MAX_TERMINAL_MATE_SCAN_WIDTH = 832;
constexpr std::size_t MAX_HORIZON_PROOF_SETS = 256;
static_assert(MAX_HORIZON_PROOF_SETS <= 256);
static_assert(RETAINED_ROOT_MAX_HORIZON_PROOFS <= 16);
constexpr std::int64_t DEEP_TEACHER_MATE_SCORE = 1'000'000;
constexpr std::int64_t DEEP_TEACHER_SCORE_LIMIT =
    DEEP_TEACHER_MATE_SCORE - 10'000 - 1;
constexpr std::uint64_t MAX_ORTHODOX_LEGAL_MOVE_VARIANTS = 218;
#ifdef __EMSCRIPTEN__
constexpr std::uint64_t ROOT_CONTRACT_PATH_COUNT_SATURATION_LIMIT =
    (std::uint64_t{1} << 53) - 1;
#else
constexpr std::uint64_t ROOT_CONTRACT_PATH_COUNT_SATURATION_LIMIT =
    std::numeric_limits<std::uint64_t>::max();
#endif

[[nodiscard]] std::string machine_notation(
    const std::vector<std::string>& moves
) {
    std::string result;
    for (std::size_t index = 0; index < moves.size(); ++index) {
        if (index != 0) {
            result.push_back('/');
        }
        result += moves[index];
    }
    return result;
}

void append_hex_word(std::string& target, std::uint64_t value) {
    constexpr char HEX[] = "0123456789abcdef";
    for (int shift = 60; shift >= 0; shift -= 4) {
        target.push_back(HEX[(value >> shift) & 0x0fU]);
    }
}

void append_text_field(std::string& target, const std::string& value) {
    target += std::to_string(value.size());
    target.push_back(':');
    target += value;
}

[[nodiscard]] bool bounded_identity(std::string_view value) noexcept {
    return !value.empty()
        && value.size() <= 256
        && std::all_of(value.begin(), value.end(), [](unsigned char item) {
            return item >= 0x21U && item <= 0x7eU;
        });
}

[[nodiscard]] bool lowercase_sha256(std::string_view value) noexcept {
    return value.size() == 64
        && std::all_of(value.begin(), value.end(), [](char item) {
            return (item >= '0' && item <= '9')
                || (item >= 'a' && item <= 'f');
        });
}

[[nodiscard]] bool supported_teacher_feature_count(
    std::size_t count
) noexcept {
    return count == 7
        || count == 14
        || count == 19
        || count == 38
        || count == 44
        || count == TEACHER_VALUE_FEATURE_COUNT;
}

[[nodiscard]] bool valid_teacher_model(
    const SubtreeDeepTeacherValueModel& model
) noexcept {
    if (
        !bounded_identity(model.base_profile_id)
        || !bounded_identity(model.variant_id)
        || !bounded_identity(model.model_id)
        || !lowercase_sha256(model.model_sha256)
        || !lowercase_sha256(model.native_source_identity)
        || model.linear.fixed_point_scale != DEEP_TEACHER_FIXED_POINT_SCALE
        || !supported_teacher_feature_count(model.linear.feature_count)
    ) {
        return false;
    }
    bool normalized = false;
    for (std::size_t index = 0; index < model.linear.feature_count; ++index) {
        const std::int64_t coefficient = model.linear.coefficients[index];
        if (
            coefficient < -DEEP_TEACHER_FIXED_POINT_SCALE
            || coefficient > DEEP_TEACHER_FIXED_POINT_SCALE
        ) {
            return false;
        }
        normalized = normalized
            || coefficient == DEEP_TEACHER_FIXED_POINT_SCALE
            || coefficient == -DEEP_TEACHER_FIXED_POINT_SCALE;
    }
    return normalized;
}

[[nodiscard]] std::uint64_t maximum_teacher_overlay_work(
    std::size_t feature_count
) noexcept {
    std::uint64_t result = 256;
    if (feature_count >= 44) {
        result += MAX_ORTHODOX_LEGAL_MOVE_VARIANTS;
    }
    if (feature_count == TEACHER_VALUE_FEATURE_COUNT) {
        result += MAX_ORTHODOX_LEGAL_MOVE_VARIANTS
            * MAX_ORTHODOX_LEGAL_MOVE_VARIANTS;
    }
    return result;
}

[[nodiscard]] std::optional<std::int64_t> rounded_fixed_point_score(
    std::int64_t raw_score,
    std::int64_t scale
) noexcept {
    if (scale <= 0) {
        return std::nullopt;
    }
    const bool negative = raw_score < 0;
    const std::uint64_t magnitude = negative
        ? static_cast<std::uint64_t>(-(raw_score + 1)) + 1
        : static_cast<std::uint64_t>(raw_score);
    const std::uint64_t divisor = static_cast<std::uint64_t>(scale);
    std::uint64_t quotient = magnitude / divisor;
    const std::uint64_t remainder = magnitude % divisor;
    if (remainder >= divisor - divisor / 2) {
        ++quotient;
    }
    if (
        quotient
        > static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max())
    ) {
        return std::nullopt;
    }
    const std::int64_t rounded = static_cast<std::int64_t>(quotient);
    return negative ? -rounded : rounded;
}

[[nodiscard]] bool structurally_valid_state(
    const SubtreeState& state,
    std::string& error
) {
    const BoardState& board = state.board;
    const Bitboard black = board.occupied[0];
    const Bitboard white = board.occupied[1];
    const Bitboard occupied = black | white;
    const std::array<Bitboard, 6> pieces = {
        board.pawns,
        board.knights,
        board.bishops,
        board.rooks,
        board.queens,
        board.kings,
    };
    if ((black & white) != 0) {
        error = "native subtree colors overlap";
        return false;
    }
    Bitboard seen = 0;
    for (const Bitboard piece : pieces) {
        if ((seen & piece) != 0) {
            error = "native subtree piece bitboards overlap";
            return false;
        }
        seen |= piece;
    }
    if (seen != occupied) {
        error = "native subtree occupancy does not match pieces";
        return false;
    }
    if (
        std::popcount(board.kings & white) != 1
        || std::popcount(board.kings & black) != 1
    ) {
        error = "native subtree boundary needs one king per side";
        return false;
    }
    constexpr Bitboard BACK_RANKS = 0xff000000000000ffULL;
    if ((board.pawns & BACK_RANKS) != 0) {
        error = "native subtree boundary has a pawn on a back rank";
        return false;
    }
    if (
        std::popcount(white) > 16
        || std::popcount(black) > 16
        || std::popcount(board.pawns & white) > 8
        || std::popcount(board.pawns & black) > 8
    ) {
        error = "native subtree boundary has too many pieces";
        return false;
    }
    if (
        (board.promoted & ~occupied) != 0
        || (board.promoted & (board.pawns | board.kings)) != 0
    ) {
        error = "native subtree promoted mask is invalid";
        return false;
    }
    constexpr Bitboard STANDARD_ROOK_SQUARES =
        (Bitboard{1} << 0) | (Bitboard{1} << 7)
        | (Bitboard{1} << 56) | (Bitboard{1} << 63);
    if (
        (board.castling_rights & ~STANDARD_ROOK_SQUARES) != 0
        || (board.castling_rights & ~board.rooks) != 0
    ) {
        error = "native subtree castling rights are invalid or Chess960";
        return false;
    }
    if (
        (board.castling_rights & 0x81ULL) != 0
        && (board.kings & white & (Bitboard{1} << 4)) == 0
    ) {
        error = "native subtree White castling king is absent";
        return false;
    }
    if (
        (board.castling_rights & 0x8100000000000000ULL) != 0
        && (board.kings & black & (Bitboard{1} << 60)) == 0
    ) {
        error = "native subtree Black castling king is absent";
        return false;
    }
    if (
        state.halfmove_clock < 0
        || state.fullmove_number < 1
        || state.series_number < 1
        || state.series_number == std::numeric_limits<std::int64_t>::max()
        || state.quiet_series < 0
        || state.quiet_series == std::numeric_limits<std::int64_t>::max()
    ) {
        error = "native subtree clocks or Progressive counters are invalid";
        return false;
    }
    if (board.white_to_move != (state.series_number % 2 == 1)) {
        error = "native subtree turn does not match Progressive series parity";
        return false;
    }
    if (!std::is_sorted(state.ep_targets.begin(), state.ep_targets.end())) {
        error = "native subtree e.p. targets are not canonical";
        return false;
    }
    if (
        std::adjacent_find(state.ep_targets.begin(), state.ep_targets.end())
        != state.ep_targets.end()
    ) {
        error = "native subtree e.p. targets are duplicated";
        return false;
    }
    Bitboard pending_ep = 0;
    for (const int target : state.ep_targets) {
        if (target < 0 || target >= 64) {
            error = "native subtree e.p. target is outside the board";
            return false;
        }
        pending_ep |= Bitboard{1} << target;
    }
    if (canonical_ep_targets(board, pending_ep) != state.ep_targets) {
        error = "native subtree e.p. targets are not legally canonical";
        return false;
    }
    BoardState opposite = board;
    opposite.white_to_move = !opposite.white_to_move;
    if (is_in_check(opposite)) {
        error = "native subtree side that just moved remains in check";
        return false;
    }
    return true;
}

[[nodiscard]] std::string state_identity_impl(const SubtreeState& state) {
    std::string result = "spc-state-v1";
    const BoardState& board = state.board;
    const std::array<Bitboard, 10> words = {
        board.pawns,
        board.knights,
        board.bishops,
        board.rooks,
        board.queens,
        board.kings,
        board.occupied[0],
        board.occupied[1],
        board.promoted,
        board.castling_rights,
    };
    for (const Bitboard word : words) {
        result.push_back('|');
        append_hex_word(result, word);
    }
    result += board.white_to_move ? "|w" : "|b";
    result += "|h" + std::to_string(state.halfmove_clock);
    result += "|f" + std::to_string(state.fullmove_number);
    result += "|s" + std::to_string(state.series_number);
    result += "|q" + std::to_string(state.quiet_series);
    result += "|ep" + std::to_string(state.ep_targets.size()) + ":";
    for (const int target : state.ep_targets) {
        result += std::to_string(target);
        result.push_back(',');
    }
    return result;
}

[[nodiscard]] bool same_board(
    const BoardState& left,
    const BoardState& right
) noexcept {
    return left.pawns == right.pawns
        && left.knights == right.knights
        && left.bishops == right.bishops
        && left.rooks == right.rooks
        && left.queens == right.queens
        && left.kings == right.kings
        && left.occupied == right.occupied
        && left.promoted == right.promoted
        && left.castling_rights == right.castling_rights
        && left.white_to_move == right.white_to_move;
}

[[nodiscard]] bool same_replayed_candidate(
    const CompleteSeriesCandidate& left,
    const CompleteSeriesCandidate& right
) noexcept {
    return left.path.moves == right.path.moves
        && same_board(left.board, right.board)
        && left.halfmove_clock == right.halfmove_clock
        && left.fullmove_number == right.fullmove_number
        && left.series_number == right.series_number
        && left.quiet_series == right.quiet_series
        && left.ep_targets == right.ep_targets
        && left.outcome == right.outcome
        && left.ended_by_check == right.ended_by_check;
}

[[nodiscard]] std::string candidate_identity_impl(
    const CompleteSeriesCandidate& candidate
) {
    std::string result = "spc-root-candidate-v1|moves";
    result += std::to_string(candidate.path.moves.size());
    result.push_back(':');
    for (const std::string& move : candidate.path.moves) {
        append_text_field(result, move);
    }
    result += "|count" + std::to_string(candidate.path.transposition_count);
    result += "|final";
    append_text_field(result, state_identity_impl(SubtreeState{
        candidate.board,
        candidate.halfmove_clock,
        candidate.fullmove_number,
        candidate.series_number,
        candidate.quiet_series,
        candidate.ep_targets,
    }));
    result += "|out" + std::to_string(static_cast<int>(candidate.outcome));
    result += candidate.ended_by_check ? "|check1" : "|check0";
    return result;
}

[[nodiscard]] std::string horizon_proof_identity_impl(
    const RetainedRootHorizonProof& proof
) {
    std::string result = "spc-horizon-proof-v1|path";
    result += std::to_string(proof.rooted_path.size());
    result.push_back(':');
    for (const CompleteSeriesCandidate& series : proof.rooted_path) {
        append_text_field(result, candidate_identity_impl(series));
    }
    result += "|mate";
    append_text_field(result, candidate_identity_impl(proof.mate_reply));
    return result;
}

[[nodiscard]] std::string horizon_proof_set_identity_impl(
    std::string_view candidate_identity,
    const std::vector<std::string>& proof_identities
) {
    std::string result = "spc-horizon-proof-set-v1|candidate";
    append_text_field(result, std::string{candidate_identity});
    result += "|proofs" + std::to_string(proof_identities.size()) + ":";
    for (const std::string& identity : proof_identities) {
        append_text_field(result, identity);
    }
    return result;
}

void append_weights(std::string& result, const SubtreeSearchConfig& config) {
    const std::array<std::int64_t, 12> weights = {
        config.fast_weights.material,
        config.fast_weights.king_space,
        config.fast_weights.promotion_corridors,
        config.fast_weights.immediate_vulnerability,
        config.fast_weights.boundary_check,
        config.full_weights.material,
        config.full_weights.king_space,
        config.full_weights.series_reach,
        config.full_weights.promotion_corridors,
        config.full_weights.immediate_vulnerability,
        config.full_weights.useful_mobility,
        config.full_weights.boundary_check,
    };
    for (const std::int64_t weight : weights) {
        result.push_back(',');
        result += std::to_string(weight);
    }
}

void append_deep_teacher_model(
    std::string& result,
    const SubtreeDeepTeacherValueModel& model
) {
    result += "|deep-teacher";
    append_text_field(result, model.base_profile_id);
    append_text_field(result, model.variant_id);
    append_text_field(result, model.model_id);
    append_text_field(result, model.model_sha256);
    append_text_field(result, model.native_source_identity);
    result += std::to_string(model.linear.feature_count);
    result.push_back(':');
    result += std::to_string(model.linear.fixed_point_scale);
    for (std::size_t index = 0; index < model.linear.feature_count; ++index) {
        result.push_back(',');
        result += std::to_string(model.linear.coefficients[index]);
    }
}

[[nodiscard]] std::string enumeration_identity_impl(
    const SubtreeState& state,
    const SubtreeSearchConfig& config,
    std::uint64_t requested_root_width,
    bool terminal_mate_scan,
    bool canonical_root_tactical_protection,
    const std::vector<std::string>& preferred_series,
    bool width_complete,
    const std::vector<RetainedRootCandidate>& candidates
) {
    std::string result = "spc-root-enumeration-v2|boundary";
    append_text_field(result, state_identity_impl(state));
    result += "|descendant-width"
        + std::to_string(config.max_series_per_node);
    result += "|root-width" + std::to_string(requested_root_width);
    result += terminal_mate_scan ? "|terminal-scan1" : "|terminal-scan0";
    result += "|maxwork";
    result += config.max_work.has_value()
        ? std::to_string(*config.max_work)
        : std::string{"none"};
    result += "|depth" + std::to_string(config.requested_depth);
    result += "|mate" + std::to_string(config.mate_score);
    result += "|cache" + std::to_string(config.series_cache_capacity);
    result += "|externalcache" + std::to_string(config.external_cache_weight);
    result += "|threads" + std::to_string(config.worker_threads);
    result += "|ttcap" + std::to_string(config.root_contract_tt_capacity);
    result += "|evalcap" + std::to_string(config.root_contract_eval_capacity);
    result += "|root-policycanonical-boundary-v1";
    if (
        !terminal_mate_scan
        && state.series_number == 2
        && !state.board.white_to_move
    ) {
        result += "|root-order-s3-neural-model"
            + std::to_string(S3_NEURAL_ORDERING_MODEL)
            + "-blend"
            + std::to_string(S3_NEURAL_ORDERING_BLEND_PERCENT);
    } else {
        result += "|root-order-hand-v1";
    }
    result += canonical_root_tactical_protection
        ? "|root-tactical1"
        : "|root-tactical0";
    result += "|weights";
    append_weights(result, config);
    if (config.deep_teacher_value_model.has_value()) {
        append_deep_teacher_model(
            result,
            *config.deep_teacher_value_model
        );
    }
    result += "|preferred" + std::to_string(preferred_series.size()) + ":";
    for (const std::string& move : preferred_series) {
        append_text_field(result, move);
    }
    result += width_complete ? "|complete1" : "|complete0";
    result += "|candidates" + std::to_string(candidates.size()) + ":";
    for (const RetainedRootCandidate& candidate : candidates) {
        result += std::to_string(candidate.order_index);
        result.push_back(':');
        append_text_field(result, candidate.candidate_identity);
    }
    return result;
}

[[nodiscard]] std::uint64_t subtract_counter(
    std::uint64_t after,
    std::uint64_t before
) {
    if (after < before) {
        throw std::logic_error("native subtree stats regressed");
    }
    return after - before;
}

[[nodiscard]] SubtreeSearchStats stats_delta(
    const SubtreeSearchStats& before,
    const SubtreeSearchStats& after
) {
    SubtreeSearchStats result;
#define SPC_SUBTREE_DELTA(field) \
    result.field = subtract_counter(after.field, before.field)
    SPC_SUBTREE_DELTA(nodes);
    SPC_SUBTREE_DELTA(leaf_evaluations);
    SPC_SUBTREE_DELTA(generated_raw_series);
    SPC_SUBTREE_DELTA(generated_unique_series);
    SPC_SUBTREE_DELTA(intra_series_transpositions);
    SPC_SUBTREE_DELTA(tt_hits);
    SPC_SUBTREE_DELTA(alpha_beta_cutoffs);
    SPC_SUBTREE_DELTA(pvs_zero_window_searches);
    SPC_SUBTREE_DELTA(pvs_researches);
    SPC_SUBTREE_DELTA(pvs_tt_writes_rolled_back);
    SPC_SUBTREE_DELTA(branch_caps);
    SPC_SUBTREE_DELTA(series_generation_positions);
    SPC_SUBTREE_DELTA(frontier_score_positions);
    SPC_SUBTREE_DELTA(static_evaluation_positions);
    SPC_SUBTREE_DELTA(evaluation_reach_positions);
    SPC_SUBTREE_DELTA(evaluation_capture_positions);
    SPC_SUBTREE_DELTA(incomplete_reach_evaluations);
    SPC_SUBTREE_DELTA(tactical_leaf_extensions);
    SPC_SUBTREE_DELTA(overlay_evaluations);
    SPC_SUBTREE_DELTA(overlay_reach_positions);
    SPC_SUBTREE_DELTA(overlay_direct_move_variants);
    SPC_SUBTREE_DELTA(overlay_two_move_variants);
    SPC_SUBTREE_DELTA(generation_positions);
    SPC_SUBTREE_DELTA(frontier_prunes);
    SPC_SUBTREE_DELTA(frontier_states_pruned);
    SPC_SUBTREE_DELTA(frontier_paths_pruned);
    SPC_SUBTREE_DELTA(tactical_frontier_states_retained);
    SPC_SUBTREE_DELTA(tactical_frontier_reserve_drops);
    SPC_SUBTREE_DELTA(tactical_final_series_retained);
    SPC_SUBTREE_DELTA(tactical_final_reserve_drops);
    result.peak_frontier_states = after.peak_frontier_states;
    SPC_SUBTREE_DELTA(generation_work_limit_hits);
    SPC_SUBTREE_DELTA(series_generation_cache_hits);
    SPC_SUBTREE_DELTA(series_generation_cache_evictions);
    result.series_generation_cache_peak = after.series_generation_cache_peak;
    result.series_generation_cache_entries_peak =
        after.series_generation_cache_entries_peak;
#undef SPC_SUBTREE_DELTA
    return result;
}

[[nodiscard]] std::uint64_t saturating_add(
    std::uint64_t left,
    std::uint64_t right
) noexcept {
    return left > std::numeric_limits<std::uint64_t>::max() - right
        ? std::numeric_limits<std::uint64_t>::max()
        : left + right;
}

[[nodiscard]] SubtreeWorkReceipt work_receipt(
    const SubtreeSearchStats& before,
    const SubtreeSearchStats& after,
    std::uint64_t external_work,
    std::optional<std::uint64_t> call_work_credit,
    std::uint64_t tt_entries,
    std::uint64_t tt_entries_peak,
    std::uint64_t tt_capacity,
    std::uint64_t eval_entries,
    std::uint64_t eval_entries_peak,
    std::uint64_t eval_capacity
) {
    const SubtreeSearchStats call_stats = stats_delta(before, after);
    return SubtreeWorkReceipt{
        after,
        call_stats,
        external_work,
        before.generation_positions,
        after.generation_positions,
        subtract_counter(after.generation_positions, before.generation_positions),
        saturating_add(external_work, after.generation_positions),
        call_work_credit,
        tt_entries,
        tt_entries_peak,
        tt_capacity,
        eval_entries,
        eval_entries_peak,
        eval_capacity,
    };
}

void hash_word(std::size_t& seed, std::uint64_t value) noexcept {
    seed ^= std::hash<std::uint64_t>{}(value)
        + static_cast<std::size_t>(0x9e3779b97f4a7c15ULL)
        + (seed << 6)
        + (seed >> 2);
}

struct PositionKey {
    std::array<Bitboard, 9> words{};
    bool white_to_move = false;
    std::uint64_t ep_targets = 0;
    std::int64_t series_number = 1;
    std::int64_t quiet_series = 0;

    bool operator==(const PositionKey&) const = default;
};

struct ExactStateKey {
    PositionKey position;
    Bitboard promoted = 0;
    std::int64_t halfmove_clock = 0;
    std::int64_t fullmove_number = 1;

    bool operator==(const ExactStateKey&) const = default;
};

struct ValidatedHorizonProof {
    ExactStateKey horizon_state;
    std::int64_t horizon_ply_from_root = 0;
    std::size_t request_index = 0;
    CompleteSeriesCandidate mate_reply;
    std::int64_t score = 0;
    std::array<int, 2> proof_bounds = UNKNOWN_PROOF_BOUNDS;
    std::string identity;
};

struct ValidatedHorizonProofSet {
    std::uint64_t namespace_id = 0;
    std::string identity;
    std::vector<ValidatedHorizonProof> proofs;
};

[[nodiscard]] bool same_validated_horizon_proof(
    const ValidatedHorizonProof& left,
    const ValidatedHorizonProof& right
) noexcept {
    return left.horizon_state == right.horizon_state
        && left.horizon_ply_from_root == right.horizon_ply_from_root
        && left.score == right.score
        && left.proof_bounds == right.proof_bounds
        && left.identity == right.identity
        && left.mate_reply.path.transposition_count
            == right.mate_reply.path.transposition_count
        && same_replayed_candidate(left.mate_reply, right.mate_reply);
}

struct TTKey {
    ExactStateKey state;
    // Selective frontier policy and mate-distance scores are root-ply
    // relative, so the same boundary reached at a different root ply is not
    // the same minimax problem.
    std::uint64_t ply_and_tactical;
    std::size_t hash_value;

    TTKey(
        ExactStateKey state_value,
        std::uint64_t ply_and_tactical_value,
        std::size_t hash_value_value
    ) noexcept
        : state(std::move(state_value)),
          ply_and_tactical(ply_and_tactical_value),
          hash_value(hash_value_value) {}

    bool operator==(const TTKey& other) const noexcept {
        return hash_value == other.hash_value
            && ply_and_tactical == other.ply_and_tactical
            && state == other.state;
    }
};

struct GenerationKey {
    ExactStateKey state;
    std::int64_t ply_from_root = 0;
    bool tactical_protection = false;
    std::uint64_t effective_width = 0;
    std::uint64_t path_count_saturation_limit = 0;
    bool s3_neural_ordering = false;

    bool operator==(const GenerationKey&) const = default;
};

static_assert(sizeof(TTKey) == 144);

[[nodiscard]] constexpr std::uint64_t pack_tt_ply_and_tactical(
    std::int64_t ply_from_root,
    bool root_tactical_protection
) noexcept {
    return (static_cast<std::uint64_t>(ply_from_root) << 1)
        | static_cast<std::uint64_t>(root_tactical_protection);
}

[[nodiscard]] constexpr std::uint64_t pack_tt_context(
    std::int64_t ply_from_root,
    bool root_tactical_protection,
    std::uint64_t proof_set_namespace
) noexcept {
    const std::uint64_t ordinary = pack_tt_ply_and_tactical(
        ply_from_root,
        root_tactical_protection
    );
    if (proof_set_namespace == 0) {
        // Namespace zero preserves the original context bits exactly.
        return ordinary;
    }
    constexpr std::uint64_t PROOF_MARKER = std::uint64_t{1} << 63;
    constexpr int PROOF_NAMESPACE_SHIFT = 55;
    return PROOF_MARKER
        | ((proof_set_namespace - 1) << PROOF_NAMESPACE_SHIFT)
        | ordinary;
}

struct PositionKeyHash {
    std::size_t operator()(const PositionKey& key) const noexcept {
        std::size_t seed = 0;
        for (const auto word : key.words) {
            hash_word(seed, word);
        }
        hash_word(seed, key.white_to_move ? 1 : 0);
        hash_word(seed, key.ep_targets);
        hash_word(seed, static_cast<std::uint64_t>(key.series_number));
        hash_word(seed, static_cast<std::uint64_t>(key.quiet_series));
        return seed;
    }
};

struct ExactStateKeyHash {
    std::size_t operator()(const ExactStateKey& key) const noexcept {
        std::size_t seed = PositionKeyHash{}(key.position);
        hash_word(seed, key.promoted);
        hash_word(seed, static_cast<std::uint64_t>(key.halfmove_clock));
        hash_word(seed, static_cast<std::uint64_t>(key.fullmove_number));
        return seed;
    }
};

[[nodiscard]] TTKey tt_key(
    ExactStateKey state,
    std::int64_t ply_from_root,
    bool root_tactical_protection,
    std::uint64_t proof_set_namespace = 0
) noexcept {
    std::size_t seed = ExactStateKeyHash{}(state);
    // Preserve the original hash sequence exactly so bucket and direct-map
    // placement do not change.
    hash_word(seed, static_cast<std::uint64_t>(ply_from_root));
    hash_word(seed, root_tactical_protection ? 1 : 0);
    if (proof_set_namespace != 0) {
        hash_word(seed, proof_set_namespace);
    }
    return TTKey{
        std::move(state),
        pack_tt_context(
            ply_from_root,
            root_tactical_protection,
            proof_set_namespace
        ),
        seed,
    };
}

struct TTKeyHash {
    std::size_t operator()(const TTKey& key) const noexcept {
        return key.hash_value;
    }
};

struct GenerationKeyHash {
    std::size_t operator()(const GenerationKey& key) const noexcept {
        std::size_t seed = ExactStateKeyHash{}(key.state);
        hash_word(seed, static_cast<std::uint64_t>(key.ply_from_root));
        hash_word(seed, key.tactical_protection ? 1 : 0);
        hash_word(seed, key.effective_width);
        hash_word(seed, key.path_count_saturation_limit);
        hash_word(seed, key.s3_neural_ordering ? 1 : 0);
        return seed;
    }
};

[[nodiscard]] std::uint64_t ep_bits(const std::vector<int>& targets) noexcept {
    std::uint64_t result = 0;
    for (const int target : targets) {
        if (target >= 0 && target < 64) {
            result |= std::uint64_t{1} << target;
        }
    }
    return result;
}

[[nodiscard]] PositionKey position_key(const SubtreeState& state) noexcept {
    return PositionKey{
        {
            state.board.pawns,
            state.board.knights,
            state.board.bishops,
            state.board.rooks,
            state.board.queens,
            state.board.kings,
            state.board.occupied[0],
            state.board.occupied[1],
            state.board.castling_rights,
        },
        state.board.white_to_move,
        ep_bits(state.ep_targets),
        state.series_number,
        state.quiet_series,
    };
}

[[nodiscard]] ExactStateKey exact_key(const SubtreeState& state) noexcept {
    return ExactStateKey{
        position_key(state),
        state.board.promoted,
        state.halfmove_clock,
        state.fullmove_number,
    };
}

[[nodiscard]] SubtreeState child_state(
    const CompleteSeriesCandidate& candidate
) {
    return SubtreeState{
        candidate.board,
        candidate.halfmove_clock,
        candidate.fullmove_number,
        candidate.series_number,
        candidate.quiet_series,
        candidate.ep_targets,
    };
}

[[nodiscard]] bool promotion_mate_eligible(const SubtreeState& state) noexcept {
    const Bitboard pawns = state.board.pawns
        & state.board.occupied[state.board.white_to_move ? 1 : 0];
    Bitboard remaining = pawns;
    while (remaining != 0) {
        const int square = static_cast<int>(std::countr_zero(remaining));
        remaining &= remaining - 1;
        const int rank = square / 8;
        const std::int64_t distance = state.board.white_to_move
            ? 7 - rank
            : rank;
        if (
            distance > 0
            && state.series_number - distance >= 2
        ) {
            return true;
        }
    }
    return false;
}

enum class TTBound : std::uint8_t {
    Exact,
    Lower,
    Upper,
};

struct NodeResult {
    std::int64_t score = 0;
    std::vector<CompleteSeriesCandidate> pv;
    std::array<int, 2> proof_bounds = UNKNOWN_PROOF_BOUNDS;
    bool canonical_pv = true;
};

struct LeafEvaluation {
    std::int64_t score = 0;
    bool tactical_unstable = false;
};

struct TTEntry {
    std::int64_t depth = 0;
    std::int64_t score = 0;
    TTBound bound = TTBound::Exact;
    // Canonical entries store their PV.  A non-canonical bound may store one
    // legal series in this existing vector solely as an ordering hint; every
    // result path masks it unless canonical_pv is true.
    std::vector<CompleteSeriesCandidate> pv;
    std::array<int, 2> proof_bounds = UNKNOWN_PROOF_BOUNDS;
    bool canonical_pv = true;
};

struct CutoffHint {
    TTKey key;
    CompleteSeriesCandidate candidate;
};

struct TransactionalBound {
    TTKey key;
    std::int64_t depth = 0;
    std::int64_t lower = 0;
    std::int64_t upper = 0;
    std::uint8_t mask = 0;
};

constexpr std::uint8_t TRANSACTIONAL_LOWER = 1;
constexpr std::uint8_t TRANSACTIONAL_UPPER = 2;

using CandidateSeries = std::vector<CompleteSeriesCandidate>;
// Search nodes keep an immutable shared snapshot. An LRU eviction can remove
// the map entry while a recursive child is running without invalidating the
// parent's active traversal, and cache hits no longer deep-copy every series.
using CandidateSeriesStorage = std::shared_ptr<const CandidateSeries>;

struct GeneratedSeries {
    CandidateSeriesStorage series;
    std::optional<std::size_t> preferred_index;
    bool width_complete = false;
    bool stopped_on_mover_mate = false;
    std::uint64_t checking_series = 0;
};

struct CacheEntry {
    CandidateSeriesStorage series;
    bool width_complete = false;
    std::uint64_t checking_series = 0;
    std::uint64_t weight = 0;
    std::list<GenerationKey>::iterator recency;
};

struct StopSearch final : std::exception {
    SubtreeSearchStatus status;
    std::string message;

    StopSearch(SubtreeSearchStatus status_value, std::string message_value)
        : status(status_value), message(std::move(message_value)) {}
};

[[nodiscard]] bool same_series(
    const CompleteSeriesCandidate& left,
    const CompleteSeriesCandidate& right
) noexcept {
    return left.path.moves == right.path.moves;
}

[[nodiscard]] std::optional<std::int64_t> terminal_score(
    const CompleteSeriesCandidate& result,
    bool mover,
    std::int64_t ply_from_root,
    std::int64_t mate_score
) noexcept {
    if (result.outcome == CompleteSeriesOutcome::Checkmate) {
        const bool winner = result.ended_by_check ? mover : !mover;
        return winner == WHITE
            ? mate_score - ply_from_root
            : -mate_score + ply_from_root;
    }
    if (
        result.outcome == CompleteSeriesOutcome::Stalemate
        || result.outcome == CompleteSeriesOutcome::TenSeriesDraw
    ) {
        return 0;
    }
    return std::nullopt;
}

[[nodiscard]] std::array<int, 2> terminal_proof_bounds(
    const CompleteSeriesCandidate& result,
    bool mover
) noexcept {
    if (result.outcome == CompleteSeriesOutcome::Checkmate) {
        const bool winner = result.ended_by_check ? mover : !mover;
        const int value = winner == WHITE ? 1 : -1;
        return {value, value};
    }
    if (
        result.outcome == CompleteSeriesOutcome::Stalemate
        || result.outcome == CompleteSeriesOutcome::TenSeriesDraw
    ) {
        return {0, 0};
    }
    return UNKNOWN_PROOF_BOUNDS;
}

struct ProofBoundsAccumulator {
    explicit ProofBoundsAccumulator(bool mover_value) noexcept
        : mover(mover_value),
          lower(mover == WHITE ? -1 : 1),
          upper(mover == WHITE ? -1 : 1) {}

    void push(const std::array<int, 2>& item) noexcept {
        if (mover == WHITE) {
            lower = std::max(lower, item[0]);
            upper = std::max(upper, item[1]);
        } else {
            lower = std::min(lower, item[0]);
            upper = std::min(upper, item[1]);
        }
        ++count;
    }

    void clear() noexcept {
        lower = mover == WHITE ? -1 : 1;
        upper = mover == WHITE ? -1 : 1;
        count = 0;
    }

    [[nodiscard]] std::array<int, 2> result(
        bool all_branches_visited
    ) const noexcept {
        if (count == 0) {
            return UNKNOWN_PROOF_BOUNDS;
        }
        int result_lower = lower;
        int result_upper = upper;
        if (!all_branches_visited) {
            if (mover == WHITE) {
                result_lower = std::max(
                    result_lower,
                    UNKNOWN_PROOF_BOUNDS[0]
                );
                result_upper = std::max(
                    result_upper,
                    UNKNOWN_PROOF_BOUNDS[1]
                );
            } else {
                result_lower = std::min(
                    result_lower,
                    UNKNOWN_PROOF_BOUNDS[0]
                );
                result_upper = std::min(
                    result_upper,
                    UNKNOWN_PROOF_BOUNDS[1]
                );
            }
        }
        return {result_lower, result_upper};
    }

    bool mover;
    int lower;
    int upper;
    std::size_t count = 0;
};

}  // namespace

bool root_tactical_protection_eligible(
    const SubtreeState& state
) noexcept {
    return state.series_number >= ROOT_TACTICAL_PROTECTION_MIN_SERIES
        || promotion_mate_eligible(state);
}

std::string subtree_state_identity(const SubtreeState& state) {
    return state_identity_impl(state);
}

class SubtreeSearchSession::Impl {
public:
    explicit Impl(SubtreeSearchConfig config_value)
        : config(std::move(config_value)),
          cutoff_hints(
              config.requested_depth >= 5
                  ? static_cast<std::size_t>(std::min<std::uint64_t>(
                      1'024,
                      config.root_contract_tt_capacity
                  ))
                  : 0
          ),
          transactional_bounds(
              config.requested_depth >= 5
                  ? static_cast<std::size_t>(std::min<std::uint64_t>(
                      32'768,
                      config.root_contract_tt_capacity
                  ))
                  : 0
          ) {
        if (
            config.max_series_per_node == 0
            || config.requested_depth < 1
            || config.requested_depth > 8
            || config.mate_score < 1
            || config.mate_score > std::numeric_limits<std::int64_t>::max() / 2
            || config.series_cache_capacity == 0
            || config.external_cache_weight > config.series_cache_capacity
            || config.worker_threads < 1
            || config.worker_threads > 64
            || config.root_contract_tt_capacity == 0
            || config.root_contract_eval_capacity == 0
            || config.deep_teacher_value_model.has_value()
                && (
                    config.mate_score != DEEP_TEACHER_MATE_SCORE
                    || !valid_teacher_model(*config.deep_teacher_value_model)
                )
        ) {
            throw std::invalid_argument("native subtree configuration is out of range");
        }
        external_cache_key.ply_from_root = -1;
        if (config.external_cache_weight != 0) {
            insert_external_cache(config.external_cache_weight);
        }
    }

    SubtreeSearchConfig config;
    SubtreeSearchStats stats;
    bool selective = false;
    bool evaluation_work_limit_reached = false;
    std::uint64_t external_work = 0;
    std::optional<std::chrono::steady_clock::time_point> deadline;
    std::uint64_t root_contract_external_work = 0;
    bool root_contract_has_external_work = false;
    std::optional<std::chrono::steady_clock::time_point> root_contract_deadline;
    std::optional<SubtreeState> retained_root_state;
    std::string retained_enumeration_identity;
    std::vector<std::string> retained_preferred_series;
    std::vector<RetainedRootCandidate> retained_root_candidates;
    bool retained_width_complete = false;
    std::optional<bool> retained_root_tactical_protection;
    bool root_contract_active = false;
    std::optional<std::uint64_t> root_call_work_credit;
    std::uint64_t root_call_work_start = 0;
    std::uint64_t tt_entries_peak = 0;
    std::uint64_t eval_entries_peak = 0;
    std::vector<ValidatedHorizonProofSet> horizon_proof_sets;
    const ValidatedHorizonProofSet* active_horizon_proof_set = nullptr;
    std::uint64_t active_horizon_proof_hits = 0;
    std::uint16_t active_horizon_proof_hit_mask = 0;

    std::unordered_map<TTKey, TTEntry, TTKeyHash> tt;
    // Transactional PVS probes roll their TT writes back, but a legal series
    // that caused an ordinary alpha-beta cutoff remains a safe move-ordering
    // witness.  Keeping that witness outside the score table lets a later
    // zero-window visit prove its bound before regenerating the full series
    // frontier. Hints never supply a score; a direct-map collision replaces
    // only an ordering hint, and every miss falls back to canonical generation.
    std::vector<std::optional<CutoffHint>> cutoff_hints;
    // PVS score-table writes are rolled back to keep later exact searches
    // canonical. Their fail-low/fail-high bounds remain mathematically valid,
    // though, so retain a compact score-only overlay for later one-point
    // probes. The overlay is never consulted by a full-window search and never
    // supplies a PV.
    std::vector<std::optional<TransactionalBound>> transactional_bounds;
    std::vector<std::vector<std::pair<TTKey, std::optional<TTEntry>>>>
        tt_transactions;
    std::unordered_map<PositionKey, LeafEvaluation, PositionKeyHash> eval_cache;
    std::unordered_map<GenerationKey, CacheEntry, GenerationKeyHash>
        generation_cache;
    std::list<GenerationKey> generation_recency;
    std::uint64_t generation_cache_weight = 0;
    GenerationKey external_cache_key{};

    [[nodiscard]] const CompleteSeriesCandidate* cutoff_hint(
        const TTKey& key
    ) const noexcept {
        if (cutoff_hints.empty()) {
            return nullptr;
        }
        const auto& slot = cutoff_hints[
            TTKeyHash{}(key) % cutoff_hints.size()
        ];
        return slot.has_value() && slot->key == key
            ? &slot->candidate
            : nullptr;
    }

    void remember_cutoff_hint(
        const TTKey& key,
        const CompleteSeriesCandidate& candidate
    ) {
        if (cutoff_hints.empty()) {
            return;
        }
        auto& slot = cutoff_hints[TTKeyHash{}(key) % cutoff_hints.size()];
        if (
            slot.has_value()
            && slot->key == key
            && same_series(slot->candidate, candidate)
        ) {
            return;
        }
        slot = CutoffHint{
            key,
            candidate,
        };
    }

    [[nodiscard]] const TransactionalBound* transactional_bound(
        const TTKey& key
    ) const noexcept {
        if (transactional_bounds.empty()) {
            return nullptr;
        }
        const std::size_t base = TTKeyHash{}(key)
            % transactional_bounds.size();
        const std::size_t probes = std::min<std::size_t>(
            4,
            transactional_bounds.size()
        );
        for (std::size_t probe = 0; probe < probes; ++probe) {
            const auto& slot = transactional_bounds[
                (base + probe) % transactional_bounds.size()
            ];
            if (slot.has_value() && slot->key == key) {
                return &*slot;
            }
        }
        return nullptr;
    }

    void remember_transactional_bound(
        const TTKey& key,
        const TTEntry& entry
    ) {
        if (
            transactional_bounds.empty()
            || entry.bound == TTBound::Exact
        ) {
            return;
        }
        const std::size_t base = TTKeyHash{}(key)
            % transactional_bounds.size();
        const std::size_t probes = std::min<std::size_t>(
            4,
            transactional_bounds.size()
        );
        std::optional<TransactionalBound>* selected = nullptr;
        for (std::size_t probe = 0; probe < probes; ++probe) {
            auto& candidate = transactional_bounds[
                (base + probe) % transactional_bounds.size()
            ];
            if (candidate.has_value() && candidate->key == key) {
                selected = &candidate;
                break;
            }
            if (!candidate.has_value()) {
                selected = &candidate;
                break;
            }
            if (
                selected == nullptr
                || candidate->depth < (*selected)->depth
            ) {
                selected = &candidate;
            }
        }
        if (selected == nullptr) {
            return;
        }
        auto& slot = *selected;
        if (slot.has_value() && slot->key == key) {
            if (slot->depth > entry.depth) {
                return;
            }
            if (slot->depth == entry.depth) {
                if (entry.bound == TTBound::Lower) {
                    slot->lower = (slot->mask & TRANSACTIONAL_LOWER) != 0
                        ? std::max(slot->lower, entry.score)
                        : entry.score;
                    slot->mask |= TRANSACTIONAL_LOWER;
                } else {
                    slot->upper = (slot->mask & TRANSACTIONAL_UPPER) != 0
                        ? std::min(slot->upper, entry.score)
                        : entry.score;
                    slot->mask |= TRANSACTIONAL_UPPER;
                }
                return;
            }
        }
        const std::uint8_t mask = entry.bound == TTBound::Lower
            ? TRANSACTIONAL_LOWER
            : TRANSACTIONAL_UPPER;
        slot = TransactionalBound{
            key,
            entry.depth,
            entry.bound == TTBound::Lower ? entry.score : 0,
            entry.bound == TTBound::Upper ? entry.score : 0,
            mask,
        };
    }

    void evict_for(std::uint64_t weight) {
        while (
            !generation_recency.empty()
            && generation_cache_weight
                > config.series_cache_capacity - weight
        ) {
            const GenerationKey evicted_key = generation_recency.front();
            const auto evicted = generation_cache.find(evicted_key);
            if (evicted == generation_cache.end()) {
                throw std::logic_error("native subtree cache LRU drift");
            }
            generation_cache_weight -= evicted->second.weight;
            generation_cache.erase(evicted);
            generation_recency.pop_front();
            ++stats.series_generation_cache_evictions;
        }
    }

    [[nodiscard]] bool has_external_cache() const {
        return generation_cache.contains(external_cache_key);
    }

    void touch_external_cache() {
        const auto found = generation_cache.find(external_cache_key);
        if (found == generation_cache.end()) {
            throw std::logic_error("native subtree external cache is absent");
        }
        generation_recency.splice(
            generation_recency.end(),
            generation_recency,
            found->second.recency
        );
    }

    void insert_external_cache(std::uint64_t weight) {
        if (
            weight == 0
            || weight > config.series_cache_capacity
            || has_external_cache()
        ) {
            throw std::logic_error("native subtree external cache insert is invalid");
        }
        evict_for(weight);
        generation_recency.push_back(external_cache_key);
        const auto recency = std::prev(generation_recency.end());
        generation_cache.emplace(
            external_cache_key,
            CacheEntry{{}, false, 0, weight, recency}
        );
        generation_cache_weight += weight;
        stats.series_generation_cache_peak = std::max(
            stats.series_generation_cache_peak,
            generation_cache_weight
        );
        stats.series_generation_cache_entries_peak = std::max<std::uint64_t>(
            stats.series_generation_cache_entries_peak,
            static_cast<std::uint64_t>(generation_cache.size())
        );
    }

    void check_deadline() const {
        if (
            deadline.has_value()
            && std::chrono::steady_clock::now() >= *deadline
        ) {
            throw StopSearch(
                SubtreeSearchStatus::Deadline,
                "native subtree deadline reached"
            );
        }
    }

    void configure_root_contract_call(
        std::uint64_t requested_external_work,
        std::optional<std::uint64_t> requested_call_work_credit,
        std::optional<std::chrono::steady_clock::time_point> requested_deadline
    ) {
        root_contract_active = true;
        if (
            tt.size() > config.root_contract_tt_capacity
            || eval_cache.size() > config.root_contract_eval_capacity
        ) {
            throw StopSearch(
                SubtreeSearchStatus::Unsupported,
                "native root session cache already exceeds its hard ceiling"
            );
        }
        if (
            root_contract_has_external_work
            && requested_external_work < root_contract_external_work
        ) {
            throw StopSearch(
                SubtreeSearchStatus::Unsupported,
                "native root external work regressed"
            );
        }
        root_contract_has_external_work = true;
        root_contract_external_work = requested_external_work;
        root_call_work_credit = requested_call_work_credit;
        root_call_work_start = stats.generation_positions;
        evaluation_work_limit_reached = false;
        if (requested_deadline.has_value()) {
            root_contract_deadline = root_contract_deadline.has_value()
                ? std::min(*root_contract_deadline, *requested_deadline)
                : requested_deadline;
        }
        external_work = requested_external_work;
        deadline = root_contract_deadline;
        if (
            config.max_work.has_value()
            && requested_external_work >= *config.max_work
        ) {
            ++stats.generation_work_limit_hits;
            throw StopSearch(
                SubtreeSearchStatus::WorkLimit,
                "native root external work exhausted the session cap"
            );
        }
        check_deadline();
    }

    void clear_retained_root() {
        retained_root_state.reset();
        retained_enumeration_identity.clear();
        retained_preferred_series.clear();
        retained_root_candidates.clear();
        retained_width_complete = false;
        retained_root_tactical_protection.reset();
        active_horizon_proof_set = nullptr;
        active_horizon_proof_hits = 0;
        active_horizon_proof_hit_mask = 0;
    }

    [[nodiscard]] RetainedRootCandidate make_root_candidate(
        CompleteSeriesCandidate candidate,
        bool mover,
        std::uint64_t order_index
    ) const {
        RetainedRootCandidate result;
        result.order_index = order_index;
        result.order_key = machine_notation(candidate.path.moves);
        result.candidate_identity = candidate_identity_impl(candidate);
        result.terminal_score = terminal_score(
            candidate,
            mover,
            1,
            config.mate_score
        );
        if (result.terminal_score.has_value()) {
            result.terminal_proof_bounds = terminal_proof_bounds(
                candidate,
                mover
            );
        }
        result.series = std::move(candidate);
        return result;
    }

    [[nodiscard]] std::optional<std::uint64_t> remaining_work() const {
        const std::uint64_t native_work = stats.generation_positions;
        std::optional<std::uint64_t> result;
        if (config.max_work.has_value()) {
            if (
                external_work >= *config.max_work
                || native_work >= *config.max_work - external_work
            ) {
                result = 0;
            } else {
                result = *config.max_work - external_work - native_work;
            }
        }
        if (root_contract_active && root_call_work_credit.has_value()) {
            const std::uint64_t used = native_work - root_call_work_start;
            const std::uint64_t call_remaining =
                used >= *root_call_work_credit
                ? 0
                : *root_call_work_credit - used;
            result = result.has_value()
                ? std::min(*result, call_remaining)
                : std::optional<std::uint64_t>{call_remaining};
        }
        return result;
    }

    [[nodiscard]] bool descendant_tactical_protection() const noexcept {
        return root_contract_active
            && retained_root_tactical_protection.has_value()
            ? *retained_root_tactical_protection
            : config.root_tactical_protection;
    }

    [[nodiscard]] bool tactical_protection(
        const SubtreeState& state,
        std::int64_t generated_ply
    ) const noexcept {
        const bool protect_descendants = descendant_tactical_protection();
        if (generated_ply == 1 || protect_descendants) {
            return true;
        }
        return generated_ply <= TACTICAL_DESCENDANT_PROMOTION_MAX_PLY
            && promotion_mate_eligible(state);
    }

    void record_generation(const SeriesGenerationStats& generation) {
        // Path multiplicity grows combinatorially on high-series repetition
        // boundaries. Individual generation calls fit in uint64_t, but their
        // session-wide telemetry can exceed it after several child searches.
        // These are monotonic counters exposed to Python, so wraparound would
        // violate the cumulative-stats contract and abort an otherwise valid
        // search. Clamp every accumulated generation counter instead.
        stats.generated_raw_series = saturating_add(
            stats.generated_raw_series,
            generation.raw_series
        );
        stats.generated_unique_series = saturating_add(
            stats.generated_unique_series,
            generation.unique_series
        );
        stats.intra_series_transpositions = saturating_add(
            stats.intra_series_transpositions,
            generation.transpositions_merged
        );
        stats.series_generation_positions = saturating_add(
            stats.series_generation_positions,
            generation.positions_visited
        );
        stats.frontier_score_positions = saturating_add(
            stats.frontier_score_positions,
            generation.frontier_score_positions
        );
        stats.generation_positions = saturating_add(
            stats.generation_positions,
            generation.positions_visited
        );
        stats.generation_positions = saturating_add(
            stats.generation_positions,
            generation.frontier_score_positions
        );
        stats.frontier_prunes = saturating_add(
            stats.frontier_prunes,
            generation.frontier_prunes
        );
        stats.frontier_states_pruned = saturating_add(
            stats.frontier_states_pruned,
            generation.frontier_states_pruned
        );
        stats.frontier_paths_pruned = saturating_add(
            stats.frontier_paths_pruned,
            generation.frontier_paths_pruned
        );
        stats.tactical_frontier_states_retained = saturating_add(
            stats.tactical_frontier_states_retained,
            generation.tactical_frontier_states_retained
        );
        stats.tactical_frontier_reserve_drops = saturating_add(
            stats.tactical_frontier_reserve_drops,
            generation.tactical_frontier_reserve_drops
        );
        stats.tactical_final_series_retained = saturating_add(
            stats.tactical_final_series_retained,
            generation.tactical_final_series_retained
        );
        stats.tactical_final_reserve_drops = saturating_add(
            stats.tactical_final_reserve_drops,
            generation.tactical_final_reserve_drops
        );
        stats.peak_frontier_states = std::max(
            stats.peak_frontier_states,
            generation.peak_frontier_states
        );
        if (generation.frontier_prunes != 0) {
            selective = true;
        }
        if (generation.work_limit_reached) {
            stats.generation_work_limit_hits = saturating_add(
                stats.generation_work_limit_hits,
                1
            );
        }
    }

    [[nodiscard]] GeneratedSeries generate(
        const SubtreeState& state,
        std::int64_t generated_ply,
        const std::vector<std::string>* preferred_series,
        bool stop_on_mover_mate = false,
        std::optional<std::uint64_t> width_override = std::nullopt
    ) {
        check_deadline();
        const bool tactical = tactical_protection(state, generated_ply);
        const std::uint64_t effective_width = width_override.value_or(
            config.max_series_per_node
        );
        const std::uint64_t path_count_saturation_limit =
            root_contract_active
                ? ROOT_CONTRACT_PATH_COUNT_SATURATION_LIMIT
                : std::numeric_limits<std::uint64_t>::max();
        // The accepted student was gated on root move ordering after each of
        // the 20 legal opening moves. Keep that evidence boundary exact: it is
        // enabled only for browser/root-contract enumeration of Black's
        // Series 2, and never changes descendant evaluation or minimax.
        const bool s3_neural_ordering = root_contract_active
            && generated_ply == 1
            && state.series_number == 2
            && !state.board.white_to_move
            && !stop_on_mover_mate;
        const GenerationKey key{
            exact_key(state),
            generated_ply,
            tactical,
            effective_width,
            path_count_saturation_limit,
            s3_neural_ordering,
        };
        auto cached = generation_cache.find(key);
        if (cached != generation_cache.end()) {
            generation_recency.splice(
                generation_recency.end(),
                generation_recency,
                cached->second.recency
            );
            ++stats.series_generation_cache_hits;
            return GeneratedSeries{
                cached->second.series,
                preferred_index(*cached->second.series, preferred_series),
                cached->second.width_complete,
                false,
                cached->second.checking_series,
            };
        }

        const auto remaining = remaining_work();
        if (remaining.has_value() && *remaining == 0) {
            ++stats.generation_work_limit_hits;
            throw StopSearch(
                SubtreeSearchStatus::WorkLimit,
                "native subtree work limit reached before generation"
            );
        }
        CompleteSeriesRequest request{
            state.board,
            state.halfmove_clock,
            state.fullmove_number,
            state.series_number,
            state.quiet_series,
            state.ep_targets,
            {},
            effective_width,
            remaining,
            config.fast_weights,
            FinalSeriesScore{
                effective_width,
                generated_ply,
                config.mate_score,
                config.fast_weights,
                s3_neural_ordering
                    ? S3_NEURAL_ORDERING_MODEL
                    : std::uint8_t{0},
                s3_neural_ordering
                    ? S3_NEURAL_ORDERING_BLEND_PERCENT
                    : 0,
            },
        };
        request.deadline = deadline;
        request.worker_threads = config.worker_threads;
        request.tactical_protection = tactical;
        request.stop_on_mover_mate = stop_on_mover_mate;
        // Equivalent move-order multiplicity is telemetry, never a search
        // score or ordering input. Let finite high-series searches continue
        // after that counter exceeds its transport representation. Browser
        // root sessions use a Number-safe ceiling; native Python sessions can
        // retain the full uint64_t range.
        request.path_count_overflow_mode = PathCountOverflowMode::Saturate;
        request.path_count_saturation_limit = path_count_saturation_limit;
        CompleteSeriesResponse response = generate_complete_series(request);
        record_generation(response.stats);
        if (response.status == SeriesGenerationStatus::WorkLimit) {
            throw StopSearch(
                SubtreeSearchStatus::WorkLimit,
                response.message.empty()
                    ? "native subtree generation work limit reached"
                    : response.message
            );
        }
        if (response.status == SeriesGenerationStatus::Deadline) {
            throw StopSearch(
                SubtreeSearchStatus::Deadline,
                response.message.empty()
                    ? "native subtree generation deadline reached"
                    : response.message
            );
        }
        if (response.status != SeriesGenerationStatus::Complete) {
            throw StopSearch(
                SubtreeSearchStatus::Unsupported,
                response.message.empty()
                    ? "native subtree generation is unsupported"
                    : response.message
            );
        }

        if (response.stats.unique_series > response.series.size()) {
            ++stats.branch_caps;
            selective = true;
        }
        const bool stopped_on_mover_mate = response.stopped_on_mover_mate;
        const bool width_complete = !stopped_on_mover_mate
            && response.stats.frontier_prunes == 0
            && response.stats.unique_series <= response.series.size();
        const std::uint64_t weight = std::max<std::uint64_t>(
            1,
            static_cast<std::uint64_t>(response.series.size())
        );
        CandidateSeriesStorage series =
            std::make_shared<const CandidateSeries>(
                std::move(response.series)
            );
        if (!stopped_on_mover_mate && weight <= config.series_cache_capacity) {
            evict_for(weight);
            generation_recency.push_back(key);
            auto recency = std::prev(generation_recency.end());
            generation_cache.emplace(
                key,
                CacheEntry{
                    series,
                    width_complete,
                    response.stats.checking_series,
                    weight,
                    recency,
                }
            );
            generation_cache_weight += weight;
            stats.series_generation_cache_peak = std::max(
                stats.series_generation_cache_peak,
                generation_cache_weight
            );
            stats.series_generation_cache_entries_peak = std::max<std::uint64_t>(
                stats.series_generation_cache_entries_peak,
                static_cast<std::uint64_t>(generation_cache.size())
            );
        }
        const auto preferred = preferred_index(*series, preferred_series);
        return GeneratedSeries{
            std::move(series),
            preferred,
            width_complete,
            stopped_on_mover_mate,
            response.stats.checking_series,
        };
    }

    [[nodiscard]] CompleteSeriesCandidate replay_imported_candidate(
        const SubtreeState& root,
        const CompleteSeriesCandidate& supplied
    ) {
        const auto remaining = remaining_work();
        if (remaining.has_value() && *remaining == 0) {
            ++stats.generation_work_limit_hits;
            throw StopSearch(
                SubtreeSearchStatus::WorkLimit,
                "native root import work limit reached before replay"
            );
        }
        CompleteSeriesRequest request{
            root.board,
            root.halfmove_clock,
            root.fullmove_number,
            root.series_number,
            root.quiet_series,
            root.ep_targets,
            supplied.path.moves,
            std::uint64_t{1},
            remaining,
            std::nullopt,
            std::nullopt,
        };
        request.deadline = deadline;
        request.worker_threads = 1;
        request.tactical_protection = false;
        CompleteSeriesResponse response = generate_complete_series(request);
        record_generation(response.stats);
        if (response.status == SeriesGenerationStatus::WorkLimit) {
            throw StopSearch(
                SubtreeSearchStatus::WorkLimit,
                response.message.empty()
                    ? "native root import work limit reached"
                    : response.message
            );
        }
        if (response.status == SeriesGenerationStatus::Deadline) {
            throw StopSearch(
                SubtreeSearchStatus::Deadline,
                response.message.empty()
                    ? "native root import deadline reached"
                    : response.message
            );
        }
        if (response.status != SeriesGenerationStatus::Complete) {
            throw StopSearch(
                SubtreeSearchStatus::Unsupported,
                response.message.empty()
                    ? "native root import replay is unsupported"
                    : response.message
            );
        }
        if (
            response.series.size() != 1
            || response.series.front().path.moves != supplied.path.moves
            || !same_replayed_candidate(response.series.front(), supplied)
        ) {
            throw StopSearch(
                SubtreeSearchStatus::Unsupported,
                "native root import candidate failed authoritative replay"
            );
        }
        return std::move(response.series.front());
    }

    [[nodiscard]] const ValidatedHorizonProofSet*
    validate_and_intern_horizon_proof_set(
        const RetainedRootCandidateRequest& request,
        const RetainedRootCandidate& retained_candidate
    ) {
        if (request.horizon_proofs.empty()) {
            return nullptr;
        }
        if (
            !retained_root_state.has_value()
            || request.horizon_proofs.size()
                > RETAINED_ROOT_MAX_HORIZON_PROOFS
            || request.tt_persistence != SubtreeTTPersistence::Commit
            || request.alpha != -config.mate_score * 2
            || request.beta != config.mate_score * 2
            || retained_candidate.terminal_score.has_value()
        ) {
            throw StopSearch(
                SubtreeSearchStatus::Unsupported,
                "native horizon proof re-search request is invalid"
            );
        }

        std::vector<ValidatedHorizonProof> validated;
        validated.reserve(request.horizon_proofs.size());
        for (std::size_t proof_index = 0;
             proof_index < request.horizon_proofs.size();
             ++proof_index) {
            const RetainedRootHorizonProof& supplied =
                request.horizon_proofs[proof_index];
            check_deadline();
            if (
                supplied.rooted_path.size()
                    != static_cast<std::size_t>(request.child_depth + 1)
                || supplied.rooted_path.empty()
                || supplied.rooted_path.size()
                    > RETAINED_ROOT_MAX_HORIZON_PROOF_PATH
                || supplied.mate_reply.path.transposition_count != 1
            ) {
                throw StopSearch(
                    SubtreeSearchStatus::Unsupported,
                    "native horizon proof path depth is invalid"
                );
            }

            SubtreeState cursor = *retained_root_state;
            RetainedRootHorizonProof canonical;
            canonical.rooted_path.reserve(supplied.rooted_path.size());
            for (std::size_t index = 0;
                 index < supplied.rooted_path.size();
                 ++index) {
                const CompleteSeriesCandidate& path_series =
                    supplied.rooted_path[index];
                CompleteSeriesCandidate replayed = replay_imported_candidate(
                    cursor,
                    path_series
                );
                replayed.path.transposition_count =
                    path_series.path.transposition_count;
                if (
                    replayed.outcome != CompleteSeriesOutcome::None
                    || (
                        index == 0
                        && (
                            !same_replayed_candidate(
                                replayed,
                                retained_candidate.series
                            )
                            || replayed.path.transposition_count
                                != retained_candidate.series.path
                                    .transposition_count
                        )
                    )
                ) {
                    throw StopSearch(
                        SubtreeSearchStatus::Unsupported,
                        "native horizon proof is not rooted at the selected candidate"
                    );
                }
                cursor = child_state(replayed);
                canonical.rooted_path.push_back(std::move(replayed));
            }

            const std::int64_t horizon_ply = static_cast<std::int64_t>(
                canonical.rooted_path.size()
            );
            const std::int64_t mate_ply = horizon_ply + 1;
            if (
                !canonical.rooted_path.back().ended_by_check
                || !is_in_check(cursor.board)
                || cursor.board.white_to_move
                    == retained_root_state->board.white_to_move
                || mate_ply % 2 != 0
            ) {
                throw StopSearch(
                    SubtreeSearchStatus::Unsupported,
                    "native horizon proof boundary is not an adverse checked horizon"
                );
            }

            canonical.mate_reply = replay_imported_candidate(
                cursor,
                supplied.mate_reply
            );
            // Mate search proves one exact reply path; unlike full series
            // enumeration it has no transposition multiplicity to preserve.
            canonical.mate_reply.path.transposition_count = 1;
            if (
                canonical.mate_reply.outcome
                    != CompleteSeriesOutcome::Checkmate
                || !canonical.mate_reply.ended_by_check
            ) {
                throw StopSearch(
                    SubtreeSearchStatus::Unsupported,
                    "native horizon proof reply is not checkmate"
                );
            }
            const auto score = terminal_score(
                canonical.mate_reply,
                cursor.board.white_to_move,
                mate_ply,
                config.mate_score
            );
            const bool root_mover = retained_root_state->board.white_to_move;
            if (
                !score.has_value()
                || (root_mover == WHITE ? *score >= 0 : *score <= 0)
            ) {
                throw StopSearch(
                    SubtreeSearchStatus::Unsupported,
                    "native horizon proof mate is not adverse to the root mover"
                );
            }

            const std::string identity = horizon_proof_identity_impl(canonical);
            const ExactStateKey horizon_state = exact_key(cursor);
            const std::array<int, 2> proof_bounds = terminal_proof_bounds(
                canonical.mate_reply,
                cursor.board.white_to_move
            );
            validated.push_back(ValidatedHorizonProof{
                horizon_state,
                horizon_ply,
                proof_index,
                std::move(canonical.mate_reply),
                *score,
                proof_bounds,
                identity,
            });
        }

        std::sort(
            validated.begin(),
            validated.end(),
            [](const ValidatedHorizonProof& left,
               const ValidatedHorizonProof& right) {
                return left.identity < right.identity;
            }
        );
        std::vector<std::string> identities;
        identities.reserve(validated.size());
        for (std::size_t index = 0; index < validated.size(); ++index) {
            const ValidatedHorizonProof& proof = validated[index];
            if (index > 0 && validated[index - 1].identity == proof.identity) {
                throw StopSearch(
                    SubtreeSearchStatus::Unsupported,
                    "native horizon proofs duplicate an identity"
                );
            }
            if (std::any_of(
                    validated.begin(),
                    validated.begin() + static_cast<std::ptrdiff_t>(index),
                    [&proof](const ValidatedHorizonProof& prior) {
                        return prior.horizon_ply_from_root
                                == proof.horizon_ply_from_root
                            && prior.horizon_state == proof.horizon_state;
                    }
                )) {
                throw StopSearch(
                    SubtreeSearchStatus::Unsupported,
                    "native horizon proofs duplicate an exact leaf"
                );
            }
            identities.push_back(proof.identity);
        }

        const std::string set_identity = horizon_proof_set_identity_impl(
            retained_candidate.candidate_identity,
            identities
        );
        const auto existing = std::find_if(
            horizon_proof_sets.begin(),
            horizon_proof_sets.end(),
            [&set_identity](const ValidatedHorizonProofSet& item) {
                return item.identity == set_identity;
            }
        );
        if (existing != horizon_proof_sets.end()) {
            const bool exact_match = existing->proofs.size() == validated.size()
                && std::equal(
                    existing->proofs.begin(),
                    existing->proofs.end(),
                    validated.begin(),
                    same_validated_horizon_proof
                );
            if (!exact_match) {
                throw std::logic_error(
                    "native horizon proof identity collision"
                );
            }
            for (std::size_t index = 0; index < validated.size(); ++index) {
                existing->proofs[index].request_index =
                    validated[index].request_index;
            }
            return &*existing;
        }
        if (horizon_proof_sets.size() >= MAX_HORIZON_PROOF_SETS) {
            throw StopSearch(
                SubtreeSearchStatus::Unsupported,
                "native horizon proof-set capacity reached"
            );
        }
        horizon_proof_sets.push_back(ValidatedHorizonProofSet{
            static_cast<std::uint64_t>(horizon_proof_sets.size() + 1),
            set_identity,
            std::move(validated),
        });
        return &horizon_proof_sets.back();
    }

    void activate_horizon_proof_set(
        const ValidatedHorizonProofSet* proof_set
    ) noexcept {
        active_horizon_proof_set = proof_set;
        active_horizon_proof_hits = 0;
        active_horizon_proof_hit_mask = 0;
    }

    [[nodiscard]] const ValidatedHorizonProof* active_horizon_proof(
        const SubtreeState& state,
        std::int64_t ply_from_root
    ) noexcept {
        if (active_horizon_proof_set == nullptr) {
            return nullptr;
        }
        const ExactStateKey state_key = exact_key(state);
        const auto found = std::find_if(
            active_horizon_proof_set->proofs.begin(),
            active_horizon_proof_set->proofs.end(),
            [&state_key, ply_from_root](const ValidatedHorizonProof& proof) {
                return proof.horizon_ply_from_root == ply_from_root
                    && proof.horizon_state == state_key;
            }
        );
        if (found == active_horizon_proof_set->proofs.end()) {
            return nullptr;
        }
        ++active_horizon_proof_hits;
        active_horizon_proof_hit_mask |= static_cast<std::uint16_t>(
            std::uint16_t{1} << found->request_index
        );
        return &*found;
    }

    [[nodiscard]] static std::optional<std::size_t> preferred_index(
        const CandidateSeries& series,
        const std::vector<std::string>* preferred_series
    ) {
        if (preferred_series == nullptr) {
            return std::nullopt;
        }
        const auto found = std::find_if(
            series.begin(),
            series.end(),
            [preferred_series](const CompleteSeriesCandidate& candidate) {
                return candidate.path.moves == *preferred_series;
            }
        );
        return found == series.end()
            ? std::nullopt
            : std::optional<std::size_t>{
                static_cast<std::size_t>(found - series.begin())
            };
    }

    [[nodiscard]] static std::size_t ordered_index(
        std::size_t ordinal,
        std::optional<std::size_t> preferred
    ) noexcept {
        if (!preferred.has_value() || *preferred == 0) {
            return ordinal;
        }
        if (ordinal == 0) {
            return *preferred;
        }
        return ordinal <= *preferred ? ordinal - 1 : ordinal;
    }

    [[nodiscard]] LeafEvaluation evaluate(const SubtreeState& state) {
        const PositionKey key = position_key(state);
        const auto cached = eval_cache.find(key);
        if (cached != eval_cache.end()) {
            return cached->second;
        }
        if (
            root_contract_active
            && eval_cache.size() >= config.root_contract_eval_capacity
        ) {
            throw StopSearch(
                SubtreeSearchStatus::Unsupported,
                "native root evaluation cache capacity reached"
            );
        }
        const auto before = remaining_work();
        if (before.has_value() && *before == 0) {
            if (!evaluation_work_limit_reached) {
                ++stats.generation_work_limit_hits;
            }
            evaluation_work_limit_reached = true;
            if (!root_contract_active) {
                selective = true;
            }
            throw StopSearch(
                SubtreeSearchStatus::WorkLimit,
                "native subtree work limit reached before evaluation"
            );
        }
        ++stats.static_evaluation_positions;
        ++stats.generation_positions;
        const auto after = remaining_work();
        const std::uint64_t reach_limit = after.has_value() ? *after : 512;
        std::optional<FullEvaluation> evaluated = full_evaluate(
            state.board,
            state.ep_targets,
            state.series_number,
            reach_limit,
            config.full_weights
        );
        if (!evaluated.has_value()) {
            throw StopSearch(
                SubtreeSearchStatus::Unsupported,
                "native subtree full evaluation overflowed"
            );
        }
        const std::uint64_t check_reach_positions = evaluated->white_reach.nodes
            + evaluated->black_reach.nodes;
        const std::uint64_t probe_positions = saturating_add(
            check_reach_positions,
            evaluated->capture_reach_positions
        );
        stats.evaluation_reach_positions = saturating_add(
            stats.evaluation_reach_positions,
            check_reach_positions
        );
        stats.evaluation_capture_positions = saturating_add(
            stats.evaluation_capture_positions,
            evaluated->capture_reach_positions
        );
        stats.generation_positions = saturating_add(
            stats.generation_positions,
            probe_positions
        );
        const bool reach_complete = evaluated->white_reach.complete
            && evaluated->black_reach.complete;
        if (!reach_complete) {
            ++stats.incomplete_reach_evaluations;
            const bool constrained_by_remaining_work =
                after.has_value()
                && probe_positions >= *after;
            if (constrained_by_remaining_work) {
                if (!evaluation_work_limit_reached) {
                    ++stats.generation_work_limit_hits;
                }
                evaluation_work_limit_reached = true;
                if (!root_contract_active) {
                    selective = true;
                }
            }
            if (root_contract_active && constrained_by_remaining_work) {
                throw StopSearch(
                    SubtreeSearchStatus::WorkLimit,
                    "native root evaluation did not complete within its work credit"
                );
            }
        }
        std::int64_t total = evaluated->total;
        if (config.deep_teacher_value_model.has_value()) {
            const auto overlay_remaining = remaining_work();
            // Root-session receipts promise never to exceed a per-call credit.
            // The extractor charges every generated legal variant only after
            // it has produced the feature vector, so reserve its orthodox
            // worst case before starting. Legacy/Python calls keep the exact
            // historical partial-budget behavior; certified root calls fail
            // closed without consuming unreceipted work.
            if (
                root_contract_active
                && overlay_remaining.has_value()
                && *overlay_remaining < maximum_teacher_overlay_work(
                    config.deep_teacher_value_model->linear.feature_count
                )
            ) {
                if (!evaluation_work_limit_reached) {
                    ++stats.generation_work_limit_hits;
                }
                evaluation_work_limit_reached = true;
                throw StopSearch(
                    SubtreeSearchStatus::WorkLimit,
                    "native root deep-teacher reserve exceeds its work credit"
                );
            }
            const std::uint64_t overlay_reach_limit = std::min<std::uint64_t>(
                256,
                overlay_remaining.value_or(256)
            );
            const auto features = teacher_value_features_v3(
                state.board,
                state.ep_targets,
                state.series_number,
                overlay_reach_limit,
                config.deep_teacher_value_model->linear.feature_count
            );
            if (!features.has_value()) {
                throw StopSearch(
                    SubtreeSearchStatus::Unsupported,
                    "native deep-teacher feature evaluation overflowed"
                );
            }
            const std::uint64_t overlay_reach_positions = saturating_add(
                features->white_reach.nodes,
                features->black_reach.nodes
            );
            const std::uint64_t overlay_variant_positions = saturating_add(
                features->direct_move_variants,
                features->two_move_variants
            );
            const std::uint64_t overlay_work = saturating_add(
                overlay_reach_positions,
                overlay_variant_positions
            );
            ++stats.overlay_evaluations;
            stats.overlay_reach_positions = saturating_add(
                stats.overlay_reach_positions,
                overlay_reach_positions
            );
            stats.overlay_direct_move_variants = saturating_add(
                stats.overlay_direct_move_variants,
                features->direct_move_variants
            );
            stats.overlay_two_move_variants = saturating_add(
                stats.overlay_two_move_variants,
                features->two_move_variants
            );
            stats.evaluation_reach_positions = saturating_add(
                stats.evaluation_reach_positions,
                overlay_reach_positions
            );
            stats.generation_positions = saturating_add(
                stats.generation_positions,
                overlay_work
            );
            const bool overlay_reach_complete =
                overlay_reach_limit == 256
                || (
                    features->white_reach.complete
                    && features->black_reach.complete
                )
                || overlay_reach_positions < overlay_reach_limit;
            const bool overlay_complete = overlay_reach_complete
                && (
                    !overlay_remaining.has_value()
                    || overlay_work <= *overlay_remaining
                );
            if (!overlay_complete) {
                if (!evaluation_work_limit_reached) {
                    ++stats.generation_work_limit_hits;
                }
                evaluation_work_limit_reached = true;
                selective = true;
                throw StopSearch(
                    SubtreeSearchStatus::WorkLimit,
                    "native deep-teacher evaluation did not complete within its work credit"
                );
            }
            const auto raw_score = deep_teacher_score_v1(
                *features,
                config.deep_teacher_value_model->linear
            );
            const auto rounded_score = raw_score.has_value()
                ? rounded_fixed_point_score(
                    *raw_score,
                    config.deep_teacher_value_model->linear.fixed_point_scale
                )
                : std::nullopt;
            if (!rounded_score.has_value()) {
                throw StopSearch(
                    SubtreeSearchStatus::Unsupported,
                    "native deep-teacher score overflowed"
                );
            }
            total = std::clamp(
                *rounded_score,
                -DEEP_TEACHER_SCORE_LIMIT,
                DEEP_TEACHER_SCORE_LIMIT
            );
        }
        const LeafEvaluation leaf{total, evaluated->tactical_unstable};
        eval_cache.emplace(key, leaf);
        eval_entries_peak = std::max<std::uint64_t>(
            eval_entries_peak,
            static_cast<std::uint64_t>(eval_cache.size())
        );
        ++stats.leaf_evaluations;
        return leaf;
    }

    [[nodiscard]] NodeResult tactical_leaf_extension(
        const SubtreeState& state,
        std::int64_t alpha,
        std::int64_t beta,
        std::int64_t ply_from_root,
        std::int64_t static_score
    ) {
        ++stats.tactical_leaf_extensions;
        const bool mover = state.board.white_to_move;
        GeneratedSeries generated = generate(
            state,
            ply_from_root + 1,
            nullptr
        );
        if (generated.series->empty()) {
            return NodeResult{static_score, {}, UNKNOWN_PROOF_BOUNDS};
        }

        std::int64_t best_score = mover == WHITE
            ? -config.mate_score * 2
            : config.mate_score * 2;
        for (const CompleteSeriesCandidate& candidate : *generated.series) {
            check_deadline();
            ++stats.nodes;
            const auto terminal = terminal_score(
                candidate,
                mover,
                ply_from_root + 1,
                config.mate_score
            );
            std::int64_t score = 0;
            if (terminal.has_value()) {
                score = *terminal;
            } else {
                const SubtreeState child = child_state(candidate);
                if (child.quiet_series >= 10) {
                    throw StopSearch(
                        SubtreeSearchStatus::AdjudicationPending,
                        "native tactical extension reached quiet adjudication"
                    );
                }
                score = evaluate(child).score;
            }
            if (
                (mover == WHITE && score > best_score)
                || (mover != WHITE && score < best_score)
            ) {
                best_score = score;
            }

            if (mover == WHITE) {
                alpha = std::max(alpha, best_score);
            } else {
                beta = std::min(beta, best_score);
            }
            if (alpha >= beta) {
                ++stats.alpha_beta_cutoffs;
                break;
            }
        }
        return NodeResult{best_score, {}, UNKNOWN_PROOF_BOUNDS};
    }

    void begin_transaction() {
        tt_transactions.emplace_back();
    }

    [[nodiscard]] std::uint64_t rollback_transaction() {
        if (tt_transactions.empty()) {
            throw std::logic_error("native subtree TT transaction stack is empty");
        }
        auto journal = std::move(tt_transactions.back());
        tt_transactions.pop_back();
        for (const auto& item : journal) {
            const auto found = tt.find(item.first);
            if (found != tt.end()) {
                remember_transactional_bound(item.first, found->second);
            }
        }
        for (auto item = journal.rbegin(); item != journal.rend(); ++item) {
            if (item->second.has_value()) {
                tt[item->first] = std::move(*item->second);
            } else {
                tt.erase(item->first);
            }
        }
        return static_cast<std::uint64_t>(journal.size());
    }

    void write_tt(const TTKey& key, TTEntry entry) {
        const auto found = tt.find(key);
        if (
            root_contract_active
            && found == tt.end()
            && tt.size() >= config.root_contract_tt_capacity
        ) {
            throw StopSearch(
                SubtreeSearchStatus::Unsupported,
                "native root TT capacity reached"
            );
        }
        if (!tt_transactions.empty()) {
            tt_transactions.back().push_back({
                key,
                found == tt.end()
                    ? std::optional<TTEntry>{}
                    : std::optional<TTEntry>{found->second},
            });
        }
        if (found == tt.end()) {
            tt.emplace(key, std::move(entry));
        } else {
            found->second = std::move(entry);
        }
        tt_entries_peak = std::max<std::uint64_t>(
            tt_entries_peak,
            static_cast<std::uint64_t>(tt.size())
        );
    }

    [[nodiscard]] NodeResult search_child_with_pvs(
        const SubtreeState& state,
        std::int64_t depth,
        std::int64_t alpha,
        std::int64_t beta,
        std::int64_t ply_from_root,
        bool parent_mover,
        bool has_prior_child
    ) {
        if (!has_prior_child || depth < 2 || beta - alpha <= 1) {
            return minimax(state, depth, alpha, beta, ply_from_root);
        }
        ++stats.pvs_zero_window_searches;
        begin_transaction();
        NodeResult probe;
        bool needs_research = false;
        try {
            probe = parent_mover == WHITE
                ? minimax(state, depth, alpha, alpha + 1, ply_from_root)
                : minimax(state, depth, beta - 1, beta, ply_from_root);
            needs_research = alpha < probe.score && probe.score < beta;
        } catch (...) {
            stats.pvs_tt_writes_rolled_back += rollback_transaction();
            throw;
        }
        stats.pvs_tt_writes_rolled_back += rollback_transaction();
        if (needs_research) {
            ++stats.pvs_researches;
            return minimax(state, depth, alpha, beta, ply_from_root);
        }
        return probe;
    }

    [[nodiscard]] NodeResult minimax(
        const SubtreeState& state,
        std::int64_t depth,
        std::int64_t alpha,
        std::int64_t beta,
        std::int64_t ply_from_root
    ) {
        check_deadline();
        ++stats.nodes;
        if (state.quiet_series >= 10) {
            throw StopSearch(
                SubtreeSearchStatus::AdjudicationPending,
                "native subtree reached quiet adjudication"
            );
        }
        if (depth == 0) {
            if (const auto* proof = active_horizon_proof(
                    state,
                    ply_from_root
                )) {
                return NodeResult{
                    proof->score,
                    {proof->mate_reply},
                    proof->proof_bounds,
                    true,
                };
            }
            const LeafEvaluation leaf = evaluate(state);
            if (leaf.tactical_unstable) {
                return tactical_leaf_extension(
                    state,
                    alpha,
                    beta,
                    ply_from_root,
                    leaf.score
                );
            }
            return NodeResult{leaf.score, {}, UNKNOWN_PROOF_BOUNDS};
        }

        const TTKey key = tt_key(
            exact_key(state),
            ply_from_root,
            descendant_tactical_protection(),
            active_horizon_proof_set == nullptr
                ? 0
                : active_horizon_proof_set->namespace_id
        );
        auto entry = tt.find(key);
        const bool had_entry = entry != tt.end();
        const std::int64_t existing_depth = had_entry
            ? entry->second.depth
            : -1;
        const TTBound existing_bound = had_entry
            ? entry->second.bound
            : TTBound::Upper;
        const std::int64_t original_alpha = alpha;
        const std::int64_t original_beta = beta;
        if (entry != tt.end() && entry->second.depth >= depth) {
            ++stats.tt_hits;
            if (entry->second.bound == TTBound::Exact) {
                if (!entry->second.canonical_pv) {
                    throw std::logic_error(
                        "native subtree TT contains a non-canonical exact entry"
                    );
                }
                return NodeResult{
                    entry->second.score,
                    entry->second.pv,
                    entry->second.proof_bounds,
                    entry->second.canonical_pv,
                };
            }
            if (entry->second.bound == TTBound::Lower) {
                alpha = std::max(alpha, entry->second.score);
            } else {
                beta = std::min(beta, entry->second.score);
            }
            if (alpha >= beta) {
                return NodeResult{
                    entry->second.score,
                    entry->second.canonical_pv
                        ? entry->second.pv
                        : std::vector<CompleteSeriesCandidate>{},
                    entry->second.proof_bounds,
                    entry->second.canonical_pv,
                };
            }
        }

        if (
            original_beta - original_alpha == 1
            && cutoff_hint(key) == nullptr
        ) {
            const auto* bound = transactional_bound(key);
            if (bound != nullptr && bound->depth >= depth) {
                const bool lower_cutoff =
                    (bound->mask & TRANSACTIONAL_LOWER) != 0
                    && bound->lower >= original_beta;
                const bool upper_cutoff =
                    (bound->mask & TRANSACTIONAL_UPPER) != 0
                    && bound->upper <= original_alpha;
                if (lower_cutoff || upper_cutoff) {
                    ++stats.tt_hits;
                    return NodeResult{
                        lower_cutoff ? bound->lower : bound->upper,
                        {},
                        UNKNOWN_PROOF_BOUNDS,
                        false,
                    };
                }
            }
        }

        const bool mover = state.board.white_to_move;
        std::optional<CompleteSeriesCandidate> preferred_candidate;
        std::optional<std::vector<std::string>> preferred_moves;
        const std::vector<std::string>* preferred_series = nullptr;
        bool proof_only_ordering = false;
        if (entry != tt.end() && !entry->second.pv.empty()) {
            if (entry->second.canonical_pv) {
                if (
                    config.requested_depth >= 4
                    && entry->second.depth < depth
                ) {
                    preferred_candidate = entry->second.pv.front();
                    preferred_series = &preferred_candidate->path.moves;
                } else {
                    preferred_moves = entry->second.pv.front().path.moves;
                    preferred_series = &*preferred_moves;
                }
            } else if (original_beta - original_alpha <= 1) {
                proof_only_ordering = true;
                // A zero-width integer window cannot return an exact score.
                // It is therefore safe to reuse a proof-only ordering hint
                // here, while exact searches retain canonical generation
                // order and tie-breaking semantics.
                if (
                    config.requested_depth >= 4
                    && entry->second.depth < depth
                ) {
                    preferred_candidate = entry->second.pv.front();
                    preferred_series = &preferred_candidate->path.moves;
                } else {
                    preferred_moves = entry->second.pv.front().path.moves;
                    preferred_series = &*preferred_moves;
                }
            } else {
                // A proof-only vector is never an ordering source for a full
                // window because it could change canonical equal-score ties.
            }
        }
        if (
            preferred_series == nullptr
            && config.requested_depth >= 5
            && original_beta - original_alpha == 1
        ) {
            const auto* hinted = cutoff_hint(key);
            if (hinted != nullptr) {
                // This witness came from an earlier zero-window cutoff whose
                // TT writes were rolled back. It is legal for ordering and
                // re-proving the same bound, but it is not a canonical PV.
                proof_only_ordering = true;
                preferred_candidate = *hinted;
                preferred_series = &preferred_candidate->path.moves;
            }
        }

        const std::int64_t search_alpha = alpha;
        const std::int64_t search_beta = beta;
        std::int64_t best_score = mover == WHITE
            ? -config.mate_score * 2
            : config.mate_score * 2;
        std::optional<CompleteSeriesCandidate> best_candidate;
        std::vector<CompleteSeriesCandidate> best_child_pv;
        bool best_child_pv_canonical = true;
        ProofBoundsAccumulator child_bounds(mover);
        bool cutoff_before_generation = false;
        const std::vector<std::string>* previsited_series = nullptr;

        if (preferred_candidate.has_value()) {
            check_deadline();
            NodeResult child;
            const auto terminal = terminal_score(
                *preferred_candidate,
                mover,
                ply_from_root + 1,
                config.mate_score
            );
            if (terminal.has_value()) {
                child.score = *terminal;
                child.proof_bounds = terminal_proof_bounds(
                    *preferred_candidate,
                    mover
                );
            } else {
                child = minimax(
                    child_state(*preferred_candidate),
                    depth - 1,
                    alpha,
                    beta,
                    ply_from_root + 1
                );
            }
            child_bounds.push(child.proof_bounds);
            best_score = child.score;
            best_candidate.emplace(std::move(*preferred_candidate));
            best_child_pv = std::move(child.pv);
            best_child_pv_canonical = child.canonical_pv;
            preferred_series = &best_candidate->path.moves;
            previsited_series = preferred_series;

            const std::int64_t immediate_mate_score =
                config.mate_score - (ply_from_root + 1);
            if (
                (mover == WHITE && best_score == immediate_mate_score)
                || (mover != WHITE && best_score == -immediate_mate_score)
            ) {
                cutoff_before_generation = true;
            } else {
                if (mover == WHITE) {
                    alpha = std::max(alpha, best_score);
                } else {
                    beta = std::min(beta, best_score);
                }
                if (alpha >= beta) {
                    ++stats.alpha_beta_cutoffs;
                    cutoff_before_generation = true;
                }
            }
        }

        bool width_complete = false;
        std::size_t series_count = 0;
        bool stopped_on_mover_mate = false;
        bool ordinary_cutoff_after_generation = false;
        if (!cutoff_before_generation) {
            const std::int64_t immediate_mate_score =
                config.mate_score - (ply_from_root + 1);
            // Partial generation pays off in deep searches; at shallower
            // requested depths, retaining the complete generation cache is
            // measurably cheaper for later calls in the same session.
            const bool mate_proves_caller_bound = config.requested_depth >= 5
                && (
                    mover == WHITE
                        ? immediate_mate_score >= original_beta
                        : -immediate_mate_score <= original_alpha
                );
            GeneratedSeries generated = generate(
                state,
                ply_from_root + 1,
                preferred_series,
                mate_proves_caller_bound
            );
            width_complete = generated.width_complete;
            stopped_on_mover_mate = generated.stopped_on_mover_mate;
            series_count = generated.series->size();
            if (generated.series->empty()) {
                return NodeResult{0, {}, UNKNOWN_PROOF_BOUNDS};
            }
            if (
                previsited_series != nullptr
                && !generated.preferred_index.has_value()
            ) {
                previsited_series = nullptr;
                alpha = search_alpha;
                beta = search_beta;
                best_score = mover == WHITE
                    ? -config.mate_score * 2
                    : config.mate_score * 2;
                best_candidate.reset();
                best_child_pv.clear();
                child_bounds.clear();
            }

            for (std::size_t ordinal = 0; ordinal < series_count; ++ordinal) {
                const auto& candidate = (*generated.series)[ordered_index(
                    ordinal,
                    generated.preferred_index
                )];
                if (
                    previsited_series != nullptr
                    && candidate.path.moves == *previsited_series
                ) {
                    continue;
                }
                check_deadline();
                NodeResult child;
                const auto terminal = terminal_score(
                    candidate,
                    mover,
                    ply_from_root + 1,
                    config.mate_score
                );
                if (terminal.has_value()) {
                    child.score = *terminal;
                    child.proof_bounds = terminal_proof_bounds(candidate, mover);
                } else {
                    child = search_child_with_pvs(
                        child_state(candidate),
                        depth - 1,
                        alpha,
                        beta,
                        ply_from_root + 1,
                        mover,
                        best_candidate.has_value()
                    );
                }
                child_bounds.push(child.proof_bounds);
                if (
                    (mover == WHITE && child.score > best_score)
                    || (mover != WHITE && child.score < best_score)
                ) {
                    best_score = child.score;
                    best_candidate = candidate;
                    best_child_pv = std::move(child.pv);
                    best_child_pv_canonical = child.canonical_pv;
                }

                const std::int64_t immediate_mate_score =
                    config.mate_score - (ply_from_root + 1);
                if (
                    (mover == WHITE && best_score == immediate_mate_score)
                    || (mover != WHITE && best_score == -immediate_mate_score)
                ) {
                    break;
                }
                if (mover == WHITE) {
                    alpha = std::max(alpha, best_score);
                } else {
                    beta = std::min(beta, best_score);
                }
                if (alpha >= beta) {
                    ++stats.alpha_beta_cutoffs;
                    ordinary_cutoff_after_generation = true;
                    break;
                }
            }
        }

        if (
            config.requested_depth >= 5
            && !tt_transactions.empty()
            && ordinary_cutoff_after_generation
            && best_candidate.has_value()
            && best_candidate->outcome == CompleteSeriesOutcome::None
        ) {
            // A committed TT entry already retains its own PV or proof-only
            // hint.  Keep a second witness only while a transactional PVS
            // probe is active and its score-table writes will be rolled back.
            remember_cutoff_hint(key, *best_candidate);
        }

        const bool canonical_pv = !proof_only_ordering
            && !stopped_on_mover_mate
            && best_child_pv_canonical;
        std::vector<CompleteSeriesCandidate> best_pv;
        if (best_candidate.has_value() && canonical_pv) {
            best_pv.reserve(1 + best_child_pv.size());
            best_pv.push_back(*best_candidate);
            best_pv.insert(
                best_pv.end(),
                best_child_pv.begin(),
                best_child_pv.end()
            );
        }
        TTBound bound = TTBound::Exact;
        if (best_score <= original_alpha) {
            bound = TTBound::Upper;
        } else if (best_score >= original_beta) {
            bound = TTBound::Lower;
        }
        if (!canonical_pv && bound == TTBound::Exact) {
            throw std::logic_error(
                "native bound-only mate exit produced an exact result"
            );
        }
        const auto proof_bounds = child_bounds.result(
            !cutoff_before_generation
                && !stopped_on_mover_mate
                && width_complete
                && child_bounds.count == series_count
        );
        if (
            !had_entry
            || depth > existing_depth
            || (
                depth == existing_depth
                && bound == TTBound::Exact
                && existing_bound != TTBound::Exact
            )
        ) {
            std::vector<CompleteSeriesCandidate> stored_pv = best_pv;
            if (
                !canonical_pv
                && bound != TTBound::Exact
                && best_candidate.has_value()
            ) {
                // Non-canonical bounds originate at a partial mover-mate exit
                // or propagate that proof through an ancestor.  Retain only
                // the legal series at this node, never the unproven child line.
                stored_pv.push_back(*best_candidate);
            }
            TTEntry replacement{
                depth,
                best_score,
                bound,
                std::move(stored_pv),
                proof_bounds,
                canonical_pv,
            };
            write_tt(key, std::move(replacement));
        }
        return NodeResult{
            best_score,
            std::move(best_pv),
            proof_bounds,
            canonical_pv,
        };
    }
};

SubtreeSearchSession::SubtreeSearchSession(SubtreeSearchConfig config)
    : impl_(std::make_unique<Impl>(std::move(config))) {}

SubtreeSearchSession::~SubtreeSearchSession() = default;

SubtreeSearchResult SubtreeSearchSession::search(
    const SubtreeState& state,
    std::int64_t depth,
    std::int64_t alpha,
    std::int64_t beta,
    std::int64_t ply_from_root,
    std::uint64_t external_work,
    std::optional<std::chrono::steady_clock::time_point> deadline
) {
    impl_->external_work = external_work;
    impl_->deadline = deadline;
    SubtreeSearchResult result;
    try {
        std::string validation_error;
        if (
            !structurally_valid_state(state, validation_error)
            || depth < 0
            || depth > impl_->config.requested_depth
            || alpha >= beta
            || ply_from_root < 0
            || ply_from_root > impl_->config.requested_depth + 1
        ) {
            throw StopSearch(
                SubtreeSearchStatus::Unsupported,
                validation_error.empty()
                    ? "native subtree request is out of range"
                    : validation_error
            );
        }
        NodeResult node = impl_->minimax(
            state,
            depth,
            alpha,
            beta,
            ply_from_root
        );
        result.score = node.score;
        result.principal_variation = std::move(node.pv);
        result.proof_bounds = node.proof_bounds;
    } catch (const StopSearch& stopped) {
        result.status = stopped.status;
        result.message = stopped.message;
    }
    result.stats = impl_->stats;
    result.selective = impl_->selective;
    result.evaluation_work_limit_reached =
        impl_->evaluation_work_limit_reached;
    return result;
}

RetainedRootEnumerationResult SubtreeSearchSession::enumerate_retained_root(
    const SubtreeState& state,
    const std::vector<std::string>& preferred_series,
    std::uint64_t requested_width,
    bool terminal_mate_scan,
    std::uint64_t external_work,
    std::optional<std::uint64_t> call_work_credit,
    std::optional<std::chrono::steady_clock::time_point> deadline
) {
    const SubtreeSearchStats before = impl_->stats;
    RetainedRootEnumerationResult result;
    result.root_white_to_move = state.board.white_to_move;
    result.requested_width = requested_width;
    result.terminal_mate_scan = terminal_mate_scan;
    result.preferred_series = preferred_series;
    impl_->clear_retained_root();
    try {
        impl_->configure_root_contract_call(
            external_work,
            call_work_credit,
            deadline
        );
        std::string validation_error;
        if (!structurally_valid_state(state, validation_error)) {
            throw StopSearch(
                SubtreeSearchStatus::Unsupported,
                std::move(validation_error)
            );
        }
        if (
            requested_width == 0
            || (
                !terminal_mate_scan
                && requested_width != impl_->config.max_series_per_node
            )
            || (
                terminal_mate_scan
                && (
                    requested_width < impl_->config.max_series_per_node
                    || requested_width > MAX_TERMINAL_MATE_SCAN_WIDTH
                    || !preferred_series.empty()
                )
            )
        ) {
            throw StopSearch(
                SubtreeSearchStatus::Unsupported,
                "native root enumeration width or mode is invalid"
            );
        }
        result.canonical_root_tactical_protection =
            root_tactical_protection_eligible(state);
        impl_->retained_root_tactical_protection =
            result.canonical_root_tactical_protection;
        if (state.quiet_series >= 10) {
            throw StopSearch(
                SubtreeSearchStatus::AdjudicationPending,
                "native root quiet adjudication remains Python/server-owned"
            );
        }
        if (!terminal_mate_scan && promotion_mate_eligible(state)) {
            throw StopSearch(
                SubtreeSearchStatus::Unsupported,
                "native root promotion-mate lane is not implemented"
            );
        }
        GeneratedSeries generated = impl_->generate(
            state,
            1,
            preferred_series.empty() ? nullptr : &preferred_series,
            terminal_mate_scan,
            requested_width
        );
        result.width_complete = generated.width_complete;
        result.generation_checking_series = generated.checking_series;
        result.candidates.reserve(generated.series->size());
        for (
            std::size_t ordinal = 0;
            ordinal < generated.series->size();
            ++ordinal
        ) {
            const std::size_t index = impl_->ordered_index(
                ordinal,
                generated.preferred_index
            );
            RetainedRootCandidate candidate = impl_->make_root_candidate(
                (*generated.series)[index],
                state.board.white_to_move,
                static_cast<std::uint64_t>(ordinal)
            );
            if (
                !terminal_mate_scan
                || (
                    candidate.series.outcome
                        == CompleteSeriesOutcome::Checkmate
                    && candidate.series.ended_by_check
                )
            ) {
                // A terminal-scan result is a compact proof witness, not a
                // broadened candidate manifest. Reindex the filtered output
                // so its transport remains canonical and gap-free.
                candidate.order_index = static_cast<std::uint64_t>(
                    result.candidates.size()
                );
                result.candidates.push_back(std::move(candidate));
            }
        }
        result.retained_count = static_cast<std::uint64_t>(
            result.candidates.size()
        );
        if (!terminal_mate_scan && result.candidates.empty()) {
            throw StopSearch(
                SubtreeSearchStatus::Unsupported,
                "native root boundary has no complete candidate series"
            );
        }
        result.enumeration_identity = enumeration_identity_impl(
            state,
            impl_->config,
            requested_width,
            terminal_mate_scan,
            result.canonical_root_tactical_protection,
            preferred_series,
            result.width_complete,
            result.candidates
        );
        if (!terminal_mate_scan) {
            impl_->retained_root_state = state;
            impl_->retained_enumeration_identity = result.enumeration_identity;
            impl_->retained_preferred_series = preferred_series;
            impl_->retained_root_candidates = result.candidates;
            impl_->retained_width_complete = result.width_complete;
        }
    } catch (const StopSearch& stopped) {
        impl_->clear_retained_root();
        result.status = stopped.status;
        result.message = stopped.message;
        result.enumeration_identity.clear();
        result.candidates.clear();
        result.retained_count = 0;
        result.width_complete = false;
    }
    result.work = work_receipt(
        before,
        impl_->stats,
        external_work,
        call_work_credit,
        static_cast<std::uint64_t>(impl_->tt.size()),
        impl_->tt_entries_peak,
        impl_->config.root_contract_tt_capacity,
        static_cast<std::uint64_t>(impl_->eval_cache.size()),
        impl_->eval_entries_peak,
        impl_->config.root_contract_eval_capacity
    );
    result.selective = impl_->selective;
    result.evaluation_work_limit_reached =
        impl_->evaluation_work_limit_reached;
    return result;
}

RetainedRootEnumerationResult SubtreeSearchSession::import_retained_root(
    const RetainedRootImportRequest& request
) {
    const SubtreeSearchStats before = impl_->stats;
    // Peer import is transactional with respect to the retained manifest. A
    // malformed, interrupted, or over-credit replacement must not strand a
    // persistent Worker without the last authoritatively verified candidate
    // set. Verification builds a local canonical set and assigns retained_*
    // only after every candidate and the enumeration identity pass. Search and
    // evaluation cache work remains cumulative and receipted.
    RetainedRootEnumerationResult result;
    result.root_white_to_move = request.root_white_to_move;
    result.requested_width = request.requested_width;
    result.width_complete = request.width_complete;
    result.preferred_series = request.preferred_series;
    try {
        impl_->configure_root_contract_call(
            request.external_work,
            request.call_work_credit,
            request.deadline
        );
        std::string validation_error;
        if (!structurally_valid_state(request.boundary, validation_error)) {
            throw StopSearch(
                SubtreeSearchStatus::Unsupported,
                std::move(validation_error)
            );
        }
        const bool canonical_root_tactical_protection =
            root_tactical_protection_eligible(request.boundary);
        result.canonical_root_tactical_protection =
            canonical_root_tactical_protection;
        if (
            request.boundary.quiet_series >= 10
            || promotion_mate_eligible(request.boundary)
        ) {
            throw StopSearch(
                request.boundary.quiet_series >= 10
                    ? SubtreeSearchStatus::AdjudicationPending
                    : SubtreeSearchStatus::Unsupported,
                request.boundary.quiet_series >= 10
                    ? "native root quiet adjudication remains Python/server-owned"
                    : "native root promotion-mate lane is not implemented"
            );
        }
        if (
            request.root_white_to_move
                != request.boundary.board.white_to_move
            || request.requested_width != impl_->config.max_series_per_node
            || request.candidates.empty()
            || request.candidates.size() > request.requested_width
        ) {
            throw StopSearch(
                SubtreeSearchStatus::Unsupported,
                "native root import manifest is inconsistent"
            );
        }
        std::vector<RetainedRootCandidate> canonical;
        canonical.reserve(request.candidates.size());
        for (std::size_t index = 0; index < request.candidates.size(); ++index) {
            impl_->check_deadline();
            const RetainedRootCandidate& supplied = request.candidates[index];
            if (
                supplied.order_index != index
                || supplied.order_key
                    != machine_notation(supplied.series.path.moves)
                || supplied.candidate_identity
                    != candidate_identity_impl(supplied.series)
            ) {
                throw StopSearch(
                    SubtreeSearchStatus::Unsupported,
                    "native root import candidate identity is invalid"
                );
            }
            CompleteSeriesCandidate replayed =
                impl_->replay_imported_candidate(
                    request.boundary,
                    supplied.series
                );
            replayed.path.transposition_count =
                supplied.series.path.transposition_count;
            RetainedRootCandidate verified = impl_->make_root_candidate(
                std::move(replayed),
                request.root_white_to_move,
                static_cast<std::uint64_t>(index)
            );
            if (
                verified.candidate_identity != supplied.candidate_identity
                || verified.order_key != supplied.order_key
                || verified.terminal_score != supplied.terminal_score
                || verified.terminal_proof_bounds
                    != supplied.terminal_proof_bounds
            ) {
                throw StopSearch(
                    SubtreeSearchStatus::Unsupported,
                    "native root import terminal metadata is invalid"
                );
            }
            canonical.push_back(std::move(verified));
        }
        const std::string identity = enumeration_identity_impl(
            request.boundary,
            impl_->config,
            request.requested_width,
            false,
            canonical_root_tactical_protection,
            request.preferred_series,
            request.width_complete,
            canonical
        );
        if (identity != request.enumeration_identity) {
            throw StopSearch(
                SubtreeSearchStatus::Unsupported,
                "native root import enumeration identity is invalid"
            );
        }
        result.enumeration_identity = identity;
        result.candidates = canonical;
        result.retained_count = static_cast<std::uint64_t>(canonical.size());
        impl_->retained_root_state = request.boundary;
        impl_->retained_enumeration_identity = identity;
        impl_->retained_preferred_series = request.preferred_series;
        impl_->retained_root_candidates = std::move(canonical);
        impl_->retained_width_complete = request.width_complete;
        impl_->retained_root_tactical_protection =
            canonical_root_tactical_protection;
    } catch (const StopSearch& stopped) {
        result.status = stopped.status;
        result.message = stopped.message;
        result.enumeration_identity.clear();
        result.candidates.clear();
        result.retained_count = 0;
        result.width_complete = false;
    }
    result.work = work_receipt(
        before,
        impl_->stats,
        request.external_work,
        request.call_work_credit,
        static_cast<std::uint64_t>(impl_->tt.size()),
        impl_->tt_entries_peak,
        impl_->config.root_contract_tt_capacity,
        static_cast<std::uint64_t>(impl_->eval_cache.size()),
        impl_->eval_entries_peak,
        impl_->config.root_contract_eval_capacity
    );
    result.selective = impl_->selective;
    result.evaluation_work_limit_reached =
        impl_->evaluation_work_limit_reached;
    return result;
}

RetainedRootCandidateResult
SubtreeSearchSession::search_retained_root_candidate(
    const RetainedRootCandidateRequest& request
) {
    const SubtreeSearchStats before = impl_->stats;
    RetainedRootCandidateResult result;
    result.enumeration_identity = request.enumeration_identity;
    result.candidate_identity = request.candidate_identity;
    bool transaction_open = false;
    impl_->activate_horizon_proof_set(nullptr);
    try {
        impl_->configure_root_contract_call(
            request.external_work,
            request.call_work_credit,
            request.deadline
        );
        if (
            !impl_->retained_root_state.has_value()
            || request.enumeration_identity.empty()
            || request.enumeration_identity
                != impl_->retained_enumeration_identity
            || request.child_depth < 0
            || request.child_depth >= impl_->config.requested_depth
            || request.alpha >= request.beta
            || request.alpha < -impl_->config.mate_score * 2
            || request.beta > impl_->config.mate_score * 2
            || (
                request.tt_persistence != SubtreeTTPersistence::Commit
                && request.tt_persistence != SubtreeTTPersistence::Rollback
            )
        ) {
            throw StopSearch(
                SubtreeSearchStatus::Unsupported,
                "native retained-root candidate request is invalid or stale"
            );
        }
        const auto found = std::find_if(
            impl_->retained_root_candidates.begin(),
            impl_->retained_root_candidates.end(),
            [&request](const RetainedRootCandidate& candidate) {
                return candidate.candidate_identity
                    == request.candidate_identity;
            }
        );
        if (found == impl_->retained_root_candidates.end()) {
            throw StopSearch(
                SubtreeSearchStatus::Unsupported,
                "native retained-root candidate identity is unknown"
            );
        }
        result.order_index = found->order_index;
        result.root_series = found->series;
        const ValidatedHorizonProofSet* horizon_proof_set =
            impl_->validate_and_intern_horizon_proof_set(request, *found);
        if (found->terminal_score.has_value()) {
            result.terminal = true;
            result.bound = SubtreeBoundKind::Exact;
            result.score = *found->terminal_score;
            result.proof_bounds = found->terminal_proof_bounds;
        } else {
            if (request.tt_persistence == SubtreeTTPersistence::Rollback) {
                impl_->begin_transaction();
                transaction_open = true;
            }
            NodeResult node;
            try {
                impl_->activate_horizon_proof_set(horizon_proof_set);
                node = impl_->minimax(
                    child_state(found->series),
                    request.child_depth,
                    request.alpha,
                    request.beta,
                    1
                );
            } catch (...) {
                impl_->activate_horizon_proof_set(nullptr);
                if (transaction_open) {
                    result.tt_writes_rolled_back =
                        impl_->rollback_transaction();
                    transaction_open = false;
                }
                throw;
            }
            const std::uint64_t horizon_proof_hits =
                impl_->active_horizon_proof_hits;
            const std::uint16_t horizon_proof_hit_mask =
                impl_->active_horizon_proof_hit_mask;
            impl_->activate_horizon_proof_set(nullptr);
            if (transaction_open) {
                result.tt_writes_rolled_back = impl_->rollback_transaction();
                transaction_open = false;
            }
            result.score = node.score;
            result.child_principal_variation = std::move(node.pv);
            result.proof_bounds = node.proof_bounds;
            if (horizon_proof_set != nullptr) {
                result.horizon_proof_set_identity =
                    horizon_proof_set->identity;
                result.horizon_proofs_validated = static_cast<std::uint64_t>(
                    horizon_proof_set->proofs.size()
                );
                result.horizon_proof_hits = horizon_proof_hits;
                result.horizon_proof_hit_mask = horizon_proof_hit_mask;
            }
            result.bound = node.score <= request.alpha
                ? SubtreeBoundKind::Upper
                : node.score >= request.beta
                    ? SubtreeBoundKind::Lower
                    : SubtreeBoundKind::Exact;
        }
    } catch (const StopSearch& stopped) {
        impl_->activate_horizon_proof_set(nullptr);
        if (transaction_open) {
            result.tt_writes_rolled_back = impl_->rollback_transaction();
        }
        result.status = stopped.status;
        result.message = stopped.message;
        result.bound = SubtreeBoundKind::Unknown;
        result.child_principal_variation.clear();
        result.horizon_proof_set_identity.clear();
        result.horizon_proofs_validated = 0;
        result.horizon_proof_hits = 0;
        result.horizon_proof_hit_mask = 0;
    }
    result.work = work_receipt(
        before,
        impl_->stats,
        request.external_work,
        request.call_work_credit,
        static_cast<std::uint64_t>(impl_->tt.size()),
        impl_->tt_entries_peak,
        impl_->config.root_contract_tt_capacity,
        static_cast<std::uint64_t>(impl_->eval_cache.size()),
        impl_->eval_entries_peak,
        impl_->config.root_contract_eval_capacity
    );
    result.selective = impl_->selective;
    result.evaluation_work_limit_reached =
        impl_->evaluation_work_limit_reached;
    return result;
}

void SubtreeSearchSession::begin_tt_transaction() {
    impl_->begin_transaction();
}

std::uint64_t SubtreeSearchSession::rollback_tt_transaction() {
    return impl_->rollback_transaction();
}

bool SubtreeSearchSession::external_cache_present() const {
    return impl_->has_external_cache();
}

void SubtreeSearchSession::touch_external_cache() {
    impl_->touch_external_cache();
}

void SubtreeSearchSession::insert_external_cache(std::uint64_t weight) {
    impl_->insert_external_cache(weight);
}

}  // namespace spc::native
