#pragma once

#include "native_eval.hpp"

#include <array>
#include <cstdint>
#include <vector>

namespace spc::native {

inline constexpr std::uint16_t FULL_GAME_BATCH_VERSION = 1;
inline constexpr std::uint16_t FULL_GAME_BATCH_V2_VERSION = 2;

enum class FullGameTerminal : std::uint8_t {
    None = 0,
    CheckmateWhite = 1,
    CheckmateBlack = 2,
    Stalemate = 3,
    TenSeriesDraw = 4,
};

enum class FullGameReject : std::uint8_t {
    None = 0,
    ManualProofRequired = 1,
    WorkLimit = 2,
    // Value 3 belongs to the coordinator's discarded-range cancellation
    // record. The native kernel never emits it without a cooperative token.
    ReservedCoordinatorCancelled = 3,
    Overflow = 4,
    TechnicalSeriesWatchdog = 5,
    InternalError = 6,
};

struct FullGameBatchConfig {
    std::uint64_t first_attempt = 0;
    std::uint64_t attempt_count = 0;
    std::uint64_t seed = 0;
    std::uint64_t max_attempt_series = 0;
    std::uint64_t max_frontier_states = 0;
    std::uint64_t max_positions_per_series = 0;
    std::uint64_t max_positions_per_game = 0;
    std::uint32_t candidate_count = 0;
    FastWeights weights{};
};

struct FullGameRecord {
    std::uint64_t attempt_index = 0;
    FullGameTerminal terminal = FullGameTerminal::None;
    FullGameReject reject = FullGameReject::None;
    std::uint64_t logical_work = 0;
    std::uint32_t white_profile_index = 0;
    std::uint32_t black_profile_index = 0;
    std::uint64_t path_count_saturations = 0;
    std::vector<std::uint64_t> series_ends;
    std::vector<std::uint16_t> moves;

    [[nodiscard]] bool accepted() const noexcept {
        return terminal != FullGameTerminal::None
            && reject == FullGameReject::None;
    }
};

enum class FullGamePolicyKind : std::uint16_t {
    Uniform = 1,
    RankMixtureBasisPoints = 2,
};

enum class FullGameProfileSchedule : std::uint16_t {
    SelfRoundRobin = 1,
    OrderedPairRoundRobin = 2,
};

inline constexpr std::uint32_t FULL_GAME_POLICY_PRESERVE_MATE = 1U;

struct FullGameRankPolicy {
    FullGamePolicyKind kind = FullGamePolicyKind::Uniform;
    bool preserve_returned_mate = true;
    std::uint16_t top_weight_basis_points = 0;
    std::uint16_t near_weight_basis_points = 0;
    std::uint16_t tail_weight_basis_points = 0;
    std::uint16_t top_rank_count = 0;
    std::uint16_t near_rank_count = 0;
};

struct FullGameProfile {
    std::array<std::uint8_t, 32> digest{};
    FastWeights weights{};
};

struct FullGameBatchConfigV2 {
    FullGameBatchConfig common{};
    FullGameRankPolicy policy{};
    FullGameProfileSchedule schedule = FullGameProfileSchedule::SelfRoundRobin;
    std::array<std::uint8_t, 32> semantic_config_digest{};
    std::vector<FullGameProfile> profiles;
};

[[nodiscard]] std::vector<FullGameRecord> generate_full_games(
    const FullGameBatchConfig& config
);

[[nodiscard]] std::vector<std::uint8_t> encode_full_game_batch(
    const FullGameBatchConfig& config,
    const std::vector<FullGameRecord>& records
);

// Parse, generate, and encode one canonical SPCFGR02 request. This keeps the
// Python boundary at one immutable byte buffer and leaves v1 byte-for-byte
// available for existing checkpoint stores. Digest fields are low-level,
// caller-supplied binding tags, not native authentication: the production
// wrapper must recompute/bind them and pin preserve_returned_mate=true.
[[nodiscard]] std::vector<std::uint8_t> generate_full_game_batch_v2(
    const std::vector<std::uint8_t>& request
);

}  // namespace spc::native
