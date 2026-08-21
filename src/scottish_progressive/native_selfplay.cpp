#include "native_selfplay.hpp"

#include <algorithm>
#include <array>
#include <bit>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <optional>
#include <set>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <utility>
#include <vector>

namespace spc::native {
namespace {

constexpr bool BLACK = false;
constexpr bool WHITE = true;
constexpr std::int64_t MATE_SCORE = 1'000'000;

constexpr std::uint64_t ATTEMPT_DOMAIN = 0x415454454D505456ULL;
constexpr std::uint64_t SERIES_DOMAIN = 0x5345524945535631ULL;
constexpr std::uint64_t LANE_DOMAIN = 0x4C414E4553504331ULL;
constexpr std::uint16_t FULL_GAME_V2_REQUEST_HEADER_SIZE = 144;
constexpr std::uint16_t FULL_GAME_V2_RESPONSE_HEADER_SIZE = 80;
constexpr std::uint16_t FULL_GAME_V2_RECORD_HEADER_SIZE = 64;
constexpr std::uint64_t FULL_GAME_V2_PROFILE_SIZE = 72;
constexpr std::uint32_t FULL_GAME_V2_MAX_PROFILES = 4096;
constexpr std::uint16_t POLICY_BASIS_POINTS = 10'000;

struct FullGameV2Runtime {
    const FullGameBatchConfigV2& config;
    std::uint32_t white_profile_index;
    std::uint32_t black_profile_index;
};

[[nodiscard]] constexpr std::uint64_t splitmix64(
    std::uint64_t value
) noexcept {
    value += 0x9E3779B97F4A7C15ULL;
    value = (value ^ (value >> 30)) * 0xBF58476D1CE4E5B9ULL;
    value = (value ^ (value >> 27)) * 0x94D049BB133111EBULL;
    return value ^ (value >> 31);
}

[[nodiscard]] constexpr std::uint64_t counter_random(
    std::uint64_t seed,
    std::uint64_t attempt,
    std::uint64_t series,
    std::uint64_t lane
) noexcept {
    return splitmix64(
        seed
        ^ splitmix64(attempt ^ ATTEMPT_DOMAIN)
        ^ splitmix64(series ^ SERIES_DOMAIN)
        ^ splitmix64(lane ^ LANE_DOMAIN)
    );
}

[[nodiscard]] BoardState initial_board() noexcept {
    constexpr Bitboard WHITE_PAWNS = 0x000000000000FF00ULL;
    constexpr Bitboard BLACK_PAWNS = 0x00FF000000000000ULL;
    constexpr Bitboard WHITE_KNIGHTS = 0x0000000000000042ULL;
    constexpr Bitboard BLACK_KNIGHTS = 0x4200000000000000ULL;
    constexpr Bitboard WHITE_BISHOPS = 0x0000000000000024ULL;
    constexpr Bitboard BLACK_BISHOPS = 0x2400000000000000ULL;
    constexpr Bitboard WHITE_ROOKS = 0x0000000000000081ULL;
    constexpr Bitboard BLACK_ROOKS = 0x8100000000000000ULL;
    constexpr Bitboard WHITE_QUEEN = 0x0000000000000008ULL;
    constexpr Bitboard BLACK_QUEEN = 0x0800000000000000ULL;
    constexpr Bitboard WHITE_KING = 0x0000000000000010ULL;
    constexpr Bitboard BLACK_KING = 0x1000000000000000ULL;
    constexpr Bitboard WHITE_OCCUPIED = 0x000000000000FFFFULL;
    constexpr Bitboard BLACK_OCCUPIED = 0xFFFF000000000000ULL;
    constexpr Bitboard CASTLING_RIGHTS = 0x8100000000000081ULL;
    return BoardState{
        WHITE_PAWNS | BLACK_PAWNS,
        WHITE_KNIGHTS | BLACK_KNIGHTS,
        WHITE_BISHOPS | BLACK_BISHOPS,
        WHITE_ROOKS | BLACK_ROOKS,
        WHITE_QUEEN | BLACK_QUEEN,
        WHITE_KING | BLACK_KING,
        {BLACK_OCCUPIED, WHITE_OCCUPIED},
        0,
        CASTLING_RIGHTS,
        WHITE,
    };
}

[[nodiscard]] std::optional<std::uint16_t> pack_uci_move(
    const std::string& uci
) noexcept {
    if (
        (uci.size() != 4 && uci.size() != 5)
        || uci[0] < 'a' || uci[0] > 'h'
        || uci[1] < '1' || uci[1] > '8'
        || uci[2] < 'a' || uci[2] > 'h'
        || uci[3] < '1' || uci[3] > '8'
    ) {
        return std::nullopt;
    }
    const std::uint16_t from = static_cast<std::uint16_t>(
        (uci[1] - '1') * 8 + (uci[0] - 'a')
    );
    const std::uint16_t to = static_cast<std::uint16_t>(
        (uci[3] - '1') * 8 + (uci[2] - 'a')
    );
    std::uint16_t promotion = 0;
    if (uci.size() == 5) {
        switch (uci[4]) {
            case 'n': promotion = 2; break;
            case 'b': promotion = 3; break;
            case 'r': promotion = 4; break;
            case 'q': promotion = 5; break;
            default: return std::nullopt;
        }
    }
    return static_cast<std::uint16_t>(
        from | (to << 6) | (promotion << 12)
    );
}

[[nodiscard]] FullGameTerminal checkmate_terminal(
    bool mover,
    const CompleteSeriesCandidate& candidate
) noexcept {
    const bool winner = candidate.ended_by_check ? mover : !mover;
    return winner == WHITE
        ? FullGameTerminal::CheckmateWhite
        : FullGameTerminal::CheckmateBlack;
}

[[nodiscard]] std::size_t select_candidate(
    const std::vector<CompleteSeriesCandidate>& candidates,
    std::uint64_t seed,
    std::uint64_t attempt,
    std::uint64_t series,
    bool mover
) noexcept {
    for (std::size_t index = 0; index < candidates.size(); ++index) {
        const auto& candidate = candidates[index];
        if (
            candidate.outcome == CompleteSeriesOutcome::Checkmate
            && checkmate_terminal(mover, candidate)
                == (mover == WHITE
                    ? FullGameTerminal::CheckmateWhite
                    : FullGameTerminal::CheckmateBlack)
        ) {
            return index;
        }
    }

    const std::size_t count = candidates.size();
    const std::uint64_t bucket = counter_random(
        seed,
        attempt,
        series,
        0
    ) % 100;
    if (bucket < 80 || count == 1) {
        return 0;
    }
    if (bucket < 95) {
        if (count < 2) {
            return 0;
        }
        const std::size_t width = std::min<std::size_t>(3, count - 1);
        return 1 + static_cast<std::size_t>(counter_random(
            seed,
            attempt,
            series,
            1
        ) % width);
    }
    if (count < 5) {
        return count - 1;
    }
    return 4 + static_cast<std::size_t>(counter_random(
        seed,
        attempt,
        series,
        2
    ) % (count - 4));
}

[[nodiscard]] std::optional<std::size_t> returned_mate_for_mover(
    const std::vector<CompleteSeriesCandidate>& candidates,
    bool mover
) noexcept {
    for (std::size_t index = 0; index < candidates.size(); ++index) {
        const auto& candidate = candidates[index];
        if (
            candidate.outcome == CompleteSeriesOutcome::Checkmate
            && checkmate_terminal(mover, candidate)
                == (mover == WHITE
                    ? FullGameTerminal::CheckmateWhite
                    : FullGameTerminal::CheckmateBlack)
        ) {
            return index;
        }
    }
    return std::nullopt;
}

[[nodiscard]] std::size_t select_candidate_v2(
    const std::vector<CompleteSeriesCandidate>& candidates,
    const FullGameRankPolicy& policy,
    std::uint64_t seed,
    std::uint64_t attempt,
    std::uint64_t series,
    bool mover
) noexcept {
    if (policy.preserve_returned_mate) {
        const auto mate = returned_mate_for_mover(candidates, mover);
        if (mate.has_value()) {
            return *mate;
        }
    }

    const std::size_t count = candidates.size();
    if (policy.kind == FullGamePolicyKind::Uniform) {
        return static_cast<std::size_t>(counter_random(
            seed,
            attempt,
            series,
            0
        ) % count);
    }

    struct ActiveBand {
        std::size_t start;
        std::size_t width;
        std::uint16_t weight;
    };
    const std::size_t top_end = std::min<std::size_t>(
        policy.top_rank_count,
        count
    );
    const std::size_t near_end = std::min<std::size_t>(
        top_end + policy.near_rank_count,
        count
    );
    const std::array<ActiveBand, 3> possible = {{
        {0, top_end, policy.top_weight_basis_points},
        {top_end, near_end - top_end, policy.near_weight_basis_points},
        {near_end, count - near_end, policy.tail_weight_basis_points},
    }};
    std::array<ActiveBand, 3> active{};
    std::size_t active_count = 0;
    std::uint32_t active_weight = 0;
    for (const auto& band : possible) {
        if (band.width == 0 || band.weight == 0) {
            continue;
        }
        active[active_count++] = band;
        active_weight += band.weight;
    }

    std::uint64_t roll = counter_random(seed, attempt, series, 0)
        % active_weight;
    const ActiveBand* selected = &active[0];
    for (std::size_t index = 0; index < active_count; ++index) {
        if (roll < active[index].weight) {
            selected = &active[index];
            break;
        }
        roll -= active[index].weight;
    }
    return selected->start + static_cast<std::size_t>(counter_random(
        seed,
        attempt,
        series,
        1
    ) % selected->width);
}

[[nodiscard]] bool add_u64(
    std::uint64_t left,
    std::uint64_t right,
    std::uint64_t& result
) noexcept {
    if (left > std::numeric_limits<std::uint64_t>::max() - right) {
        return false;
    }
    result = left + right;
    return true;
}

[[nodiscard]] FullGameRecord reject_record(
    FullGameRecord record,
    FullGameReject reason
) {
    record.terminal = FullGameTerminal::None;
    record.reject = reason;
    record.series_ends.clear();
    record.moves.clear();
    return record;
}

[[nodiscard]] FullGameRecord generate_one(
    const FullGameBatchConfig& config,
    std::uint64_t attempt,
    const FullGameV2Runtime* v2_runtime = nullptr
) {
    FullGameRecord record;
    record.attempt_index = attempt;
    if (v2_runtime != nullptr) {
        record.white_profile_index = v2_runtime->white_profile_index;
        record.black_profile_index = v2_runtime->black_profile_index;
    }

    const FastWeights& initial_weights = v2_runtime == nullptr
        ? config.weights
        : v2_runtime->config.profiles[record.white_profile_index].weights;

    CompleteSeriesRequest request{
        initial_board(),
        0,
        1,
        1,
        0,
        {},
        {},
        config.max_frontier_states,
        config.max_positions_per_series,
        initial_weights,
        FinalSeriesScore{
            config.candidate_count,
            1,
            MATE_SCORE,
            initial_weights,
        },
        v2_runtime == nullptr
            ? PathCountOverflowMode::Reject
            : PathCountOverflowMode::Saturate,
    };
    std::uint64_t total_work = 0;

    while (true) {
        if (
            config.max_attempt_series != 0
            && record.series_ends.size() >= config.max_attempt_series
        ) {
            return reject_record(
                std::move(record),
                FullGameReject::TechnicalSeriesWatchdog
            );
        }
        if (
            request.series_number == std::numeric_limits<std::int64_t>::max()
            || request.quiet_series == std::numeric_limits<std::int64_t>::max()
        ) {
            return reject_record(std::move(record), FullGameReject::Overflow);
        }
        if (
            config.max_positions_per_game != 0
            && total_work >= config.max_positions_per_game
        ) {
            return reject_record(std::move(record), FullGameReject::WorkLimit);
        }

        std::uint64_t series_work_limit = config.max_positions_per_series;
        if (config.max_positions_per_game != 0) {
            series_work_limit = std::min(
                series_work_limit,
                config.max_positions_per_game - total_work
            );
        }
        request.max_positions = series_work_limit;
        const bool mover = request.board.white_to_move;
        if (v2_runtime != nullptr) {
            const std::uint32_t profile_index = mover == WHITE
                ? record.white_profile_index
                : record.black_profile_index;
            const FastWeights& weights =
                v2_runtime->config.profiles[profile_index].weights;
            request.frontier_weights = weights;
            request.final_series_score->weights = weights;
        }
        const CompleteSeriesResponse response = generate_complete_series(request);

        if (v2_runtime != nullptr) {
            std::uint64_t saturations = 0;
            if (!add_u64(
                    record.path_count_saturations,
                    response.stats.path_count_saturations,
                    saturations
                )) {
                record.path_count_saturations =
                    std::numeric_limits<std::uint64_t>::max();
            } else {
                record.path_count_saturations = saturations;
            }
        }

        std::uint64_t response_work = 0;
        if (!add_u64(
                response.stats.positions_visited,
                response.stats.frontier_score_positions,
                response_work
            ) || !add_u64(total_work, response_work, total_work)) {
            return reject_record(std::move(record), FullGameReject::Overflow);
        }
        record.logical_work = total_work;
        if (response.status == SeriesGenerationStatus::WorkLimit) {
            return reject_record(std::move(record), FullGameReject::WorkLimit);
        }
        if (response.status != SeriesGenerationStatus::Complete) {
            return reject_record(
                std::move(record),
                response.status == SeriesGenerationStatus::Unsupported
                    ? FullGameReject::Overflow
                    : FullGameReject::InternalError
            );
        }
        if (response.series.empty()) {
            return reject_record(
                std::move(record),
                FullGameReject::InternalError
            );
        }

        const std::size_t selected_index = v2_runtime == nullptr
            ? select_candidate(
                response.series,
                config.seed,
                attempt,
                static_cast<std::uint64_t>(request.series_number),
                mover
            )
            : select_candidate_v2(
                response.series,
                v2_runtime->config.policy,
                config.seed,
                attempt,
                static_cast<std::uint64_t>(request.series_number),
                mover
            );
        const CompleteSeriesCandidate& selected = response.series[selected_index];
        std::vector<std::uint16_t> packed;
        packed.reserve(selected.path.moves.size());
        for (const std::string& move : selected.path.moves) {
            const auto encoded = pack_uci_move(move);
            if (!encoded.has_value()) {
                return reject_record(
                    std::move(record),
                    FullGameReject::InternalError
                );
            }
            packed.push_back(*encoded);
        }
        if (
            packed.size()
            > record.moves.max_size() - record.moves.size()
        ) {
            return reject_record(std::move(record), FullGameReject::Overflow);
        }
        record.moves.insert(record.moves.end(), packed.begin(), packed.end());
        record.series_ends.push_back(
            static_cast<std::uint64_t>(record.moves.size())
        );

        if (selected.outcome == CompleteSeriesOutcome::Checkmate) {
            record.terminal = checkmate_terminal(mover, selected);
            return record;
        }
        if (selected.outcome == CompleteSeriesOutcome::Stalemate) {
            record.terminal = FullGameTerminal::Stalemate;
            return record;
        }
        if (selected.outcome == CompleteSeriesOutcome::TenSeriesDraw) {
            record.terminal = FullGameTerminal::TenSeriesDraw;
            return record;
        }
        if (selected.quiet_series >= 10) {
            return reject_record(
                std::move(record),
                FullGameReject::ManualProofRequired
            );
        }

        request.board = selected.board;
        request.halfmove_clock = selected.halfmove_clock;
        request.fullmove_number = selected.fullmove_number;
        request.series_number = selected.series_number;
        request.quiet_series = selected.quiet_series;
        request.ep_targets = selected.ep_targets;
        request.required_prefix.clear();
    }
}

void append_u8(std::vector<std::uint8_t>& output, std::uint8_t value) {
    output.push_back(value);
}

template <typename Unsigned>
void append_little_endian(
    std::vector<std::uint8_t>& output,
    Unsigned value
) {
    static_assert(std::is_unsigned_v<Unsigned>);
    for (std::size_t offset = 0; offset < sizeof(Unsigned); ++offset) {
        output.push_back(static_cast<std::uint8_t>(value & 0xff));
        value >>= 8;
    }
}

[[nodiscard]] std::uint64_t encoded_record_size(
    const FullGameRecord& record
) {
    constexpr std::uint64_t RECORD_HEADER_SIZE = 44;
    const auto series_count = static_cast<std::uint64_t>(
        record.series_ends.size()
    );
    const auto move_count = static_cast<std::uint64_t>(record.moves.size());
    if (
        series_count
            > (std::numeric_limits<std::uint64_t>::max() - RECORD_HEADER_SIZE) / 8
    ) {
        throw std::length_error("full-game series offsets overflow");
    }
    const std::uint64_t with_series = RECORD_HEADER_SIZE + series_count * 8;
    if (
        move_count
            > (std::numeric_limits<std::uint64_t>::max() - with_series) / 2
    ) {
        throw std::length_error("full-game packed moves overflow");
    }
    return with_series + move_count * 2;
}

class V2RequestReader {
public:
    explicit V2RequestReader(const std::vector<std::uint8_t>& input)
        : input_(input) {}

    template <typename Unsigned>
    [[nodiscard]] Unsigned read_little_endian() {
        static_assert(std::is_unsigned_v<Unsigned>);
        if (remaining() < sizeof(Unsigned)) {
            throw std::invalid_argument("truncated native full-game v2 request");
        }
        Unsigned value = 0;
        for (std::size_t byte = 0; byte < sizeof(Unsigned); ++byte) {
            value |= static_cast<Unsigned>(input_[offset_ + byte]) << (byte * 8);
        }
        offset_ += sizeof(Unsigned);
        return value;
    }

    template <std::size_t Size>
    [[nodiscard]] std::array<std::uint8_t, Size> read_array() {
        if (remaining() < Size) {
            throw std::invalid_argument("truncated native full-game v2 request");
        }
        std::array<std::uint8_t, Size> value{};
        std::copy_n(input_.begin() + static_cast<std::ptrdiff_t>(offset_), Size, value.begin());
        offset_ += Size;
        return value;
    }

    [[nodiscard]] std::size_t offset() const noexcept {
        return offset_;
    }

    [[nodiscard]] bool at_end() const noexcept {
        return offset_ == input_.size();
    }

private:
    [[nodiscard]] std::size_t remaining() const noexcept {
        return input_.size() - offset_;
    }

    const std::vector<std::uint8_t>& input_;
    std::size_t offset_ = 0;
};

template <std::size_t Size>
[[nodiscard]] bool any_nonzero(
    const std::array<std::uint8_t, Size>& value
) noexcept {
    return std::any_of(value.begin(), value.end(), [](std::uint8_t byte) {
        return byte != 0;
    });
}

[[nodiscard]] bool valid_profile_weights(const FastWeights& weights) noexcept {
    return weights.material >= 25 && weights.material <= 300
        && weights.king_space >= 25 && weights.king_space <= 300
        && weights.promotion_corridors >= 25
        && weights.promotion_corridors <= 300
        && weights.immediate_vulnerability >= 25
        && weights.immediate_vulnerability <= 300
        && weights.boundary_check >= 25 && weights.boundary_check <= 300;
}

[[nodiscard]] FullGameBatchConfigV2 parse_full_game_v2_request(
    const std::vector<std::uint8_t>& input
) {
    constexpr std::array<std::uint8_t, 8> MAGIC = {
        'S', 'P', 'C', 'F', 'G', 'R', '0', '2',
    };
    if (input.size() < FULL_GAME_V2_REQUEST_HEADER_SIZE) {
        throw std::invalid_argument("truncated native full-game v2 request");
    }
    V2RequestReader reader(input);
    if (reader.read_array<8>() != MAGIC) {
        throw std::invalid_argument("native full-game v2 request magic is invalid");
    }
    const std::uint16_t version = reader.read_little_endian<std::uint16_t>();
    const std::uint16_t header_size = reader.read_little_endian<std::uint16_t>();
    const std::uint32_t request_flags = reader.read_little_endian<std::uint32_t>();
    const std::uint64_t request_size = reader.read_little_endian<std::uint64_t>();

    FullGameBatchConfigV2 config;
    auto& common = config.common;
    common.first_attempt = reader.read_little_endian<std::uint64_t>();
    common.attempt_count = reader.read_little_endian<std::uint64_t>();
    common.seed = reader.read_little_endian<std::uint64_t>();
    common.max_attempt_series = reader.read_little_endian<std::uint64_t>();
    common.max_frontier_states = reader.read_little_endian<std::uint64_t>();
    common.max_positions_per_series = reader.read_little_endian<std::uint64_t>();
    common.max_positions_per_game = reader.read_little_endian<std::uint64_t>();
    common.candidate_count = reader.read_little_endian<std::uint32_t>();
    const std::uint32_t profile_count = reader.read_little_endian<std::uint32_t>();
    const std::uint16_t policy_kind = reader.read_little_endian<std::uint16_t>();
    const std::uint16_t schedule_kind = reader.read_little_endian<std::uint16_t>();
    const std::uint32_t policy_flags = reader.read_little_endian<std::uint32_t>();
    config.policy.top_weight_basis_points =
        reader.read_little_endian<std::uint16_t>();
    config.policy.near_weight_basis_points =
        reader.read_little_endian<std::uint16_t>();
    config.policy.tail_weight_basis_points =
        reader.read_little_endian<std::uint16_t>();
    config.policy.top_rank_count = reader.read_little_endian<std::uint16_t>();
    config.policy.near_rank_count = reader.read_little_endian<std::uint16_t>();
    const std::uint16_t policy_reserved =
        reader.read_little_endian<std::uint16_t>();
    config.semantic_config_digest = reader.read_array<32>();
    const std::uint32_t header_reserved =
        reader.read_little_endian<std::uint32_t>();

    if (
        version != FULL_GAME_BATCH_V2_VERSION
        || header_size != FULL_GAME_V2_REQUEST_HEADER_SIZE
        || request_flags != 0
        || policy_reserved != 0
        || header_reserved != 0
        || reader.offset() != FULL_GAME_V2_REQUEST_HEADER_SIZE
        || request_size != input.size()
        || !any_nonzero(config.semantic_config_digest)
        || common.attempt_count == 0
        || common.attempt_count > std::numeric_limits<std::uint32_t>::max()
        || common.max_frontier_states == 0
        || common.max_positions_per_series == 0
        || common.max_positions_per_game == 0
        || common.candidate_count == 0
        || common.candidate_count > common.max_frontier_states
        || (
            common.first_attempt
            > std::numeric_limits<std::uint64_t>::max()
                - (common.attempt_count - 1)
        )
        || profile_count == 0
        || profile_count > FULL_GAME_V2_MAX_PROFILES
        || (policy_flags & ~FULL_GAME_POLICY_PRESERVE_MATE) != 0
    ) {
        throw std::invalid_argument("invalid native full-game v2 request header");
    }

    const std::uint64_t expected_size = FULL_GAME_V2_REQUEST_HEADER_SIZE
        + static_cast<std::uint64_t>(profile_count) * FULL_GAME_V2_PROFILE_SIZE;
    if (request_size != expected_size) {
        throw std::invalid_argument("native full-game v2 request size is not canonical");
    }

    if (policy_kind == static_cast<std::uint16_t>(FullGamePolicyKind::Uniform)) {
        config.policy.kind = FullGamePolicyKind::Uniform;
        if (
            config.policy.top_weight_basis_points != 0
            || config.policy.near_weight_basis_points != 0
            || config.policy.tail_weight_basis_points != 0
            || config.policy.top_rank_count != 0
            || config.policy.near_rank_count != 0
        ) {
            throw std::invalid_argument("uniform v2 policy fields must be zero");
        }
    } else if (
        policy_kind
        == static_cast<std::uint16_t>(FullGamePolicyKind::RankMixtureBasisPoints)
    ) {
        config.policy.kind = FullGamePolicyKind::RankMixtureBasisPoints;
        const std::uint32_t total_weight =
            config.policy.top_weight_basis_points
            + config.policy.near_weight_basis_points
            + config.policy.tail_weight_basis_points;
        const std::uint32_t ranked_threshold = config.policy.top_rank_count
            + config.policy.near_rank_count;
        if (
            total_weight != POLICY_BASIS_POINTS
            || config.policy.top_weight_basis_points == 0
            || config.policy.top_rank_count == 0
            || ranked_threshold > common.candidate_count
            || (
                config.policy.near_rank_count == 0
                && config.policy.near_weight_basis_points != 0
            )
            || (
                ranked_threshold == common.candidate_count
                && config.policy.tail_weight_basis_points != 0
            )
        ) {
            throw std::invalid_argument("native full-game v2 rank policy is invalid");
        }
    } else {
        throw std::invalid_argument("native full-game v2 policy kind is invalid");
    }
    config.policy.preserve_returned_mate =
        (policy_flags & FULL_GAME_POLICY_PRESERVE_MATE) != 0;

    if (
        schedule_kind
        == static_cast<std::uint16_t>(FullGameProfileSchedule::SelfRoundRobin)
    ) {
        config.schedule = FullGameProfileSchedule::SelfRoundRobin;
    } else if (
        schedule_kind
        == static_cast<std::uint16_t>(
            FullGameProfileSchedule::OrderedPairRoundRobin
        )
    ) {
        config.schedule = FullGameProfileSchedule::OrderedPairRoundRobin;
    } else {
        throw std::invalid_argument("native full-game v2 profile schedule is invalid");
    }

    std::set<std::array<std::uint8_t, 32>> profile_digests;
    config.profiles.reserve(profile_count);
    for (std::uint32_t index = 0; index < profile_count; ++index) {
        FullGameProfile profile;
        profile.digest = reader.read_array<32>();
        profile.weights = FastWeights{
            std::bit_cast<std::int64_t>(
                reader.read_little_endian<std::uint64_t>()
            ),
            std::bit_cast<std::int64_t>(
                reader.read_little_endian<std::uint64_t>()
            ),
            std::bit_cast<std::int64_t>(
                reader.read_little_endian<std::uint64_t>()
            ),
            std::bit_cast<std::int64_t>(
                reader.read_little_endian<std::uint64_t>()
            ),
            std::bit_cast<std::int64_t>(
                reader.read_little_endian<std::uint64_t>()
            ),
        };
        if (
            !any_nonzero(profile.digest)
            || !profile_digests.insert(profile.digest).second
            || !valid_profile_weights(profile.weights)
        ) {
            throw std::invalid_argument("native full-game v2 profile pool is invalid");
        }
        config.profiles.push_back(profile);
    }
    if (!reader.at_end()) {
        throw std::invalid_argument("native full-game v2 request has trailing bytes");
    }
    common.weights = config.profiles.front().weights;
    return config;
}

[[nodiscard]] std::pair<std::uint32_t, std::uint32_t> profile_pair_for_attempt(
    const FullGameBatchConfigV2& config,
    std::uint64_t attempt
) noexcept {
    const std::uint64_t count = config.profiles.size();
    if (config.schedule == FullGameProfileSchedule::SelfRoundRobin) {
        const auto index = static_cast<std::uint32_t>(attempt % count);
        return {index, index};
    }
    const std::uint64_t seat = attempt % count;
    const std::uint64_t round = (attempt / count) % count;
    return {
        static_cast<std::uint32_t>(seat),
        static_cast<std::uint32_t>((seat + round) % count),
    };
}

[[nodiscard]] std::vector<FullGameRecord> generate_full_games_v2(
    const FullGameBatchConfigV2& config
) {
    std::vector<FullGameRecord> records;
    records.reserve(static_cast<std::size_t>(config.common.attempt_count));
    for (
        std::uint64_t offset = 0;
        offset < config.common.attempt_count;
        ++offset
    ) {
        const std::uint64_t attempt = config.common.first_attempt + offset;
        const auto [white, black] = profile_pair_for_attempt(config, attempt);
        const FullGameV2Runtime runtime{config, white, black};
        records.push_back(generate_one(config.common, attempt, &runtime));
    }
    return records;
}

[[nodiscard]] std::uint64_t encoded_record_size_v2(
    const FullGameRecord& record
) {
    const auto series_count = static_cast<std::uint64_t>(
        record.series_ends.size()
    );
    const auto move_count = static_cast<std::uint64_t>(record.moves.size());
    if (
        series_count
        > (
            std::numeric_limits<std::uint64_t>::max()
            - FULL_GAME_V2_RECORD_HEADER_SIZE
        ) / 8
    ) {
        throw std::length_error("full-game v2 series offsets overflow");
    }
    const std::uint64_t with_series = FULL_GAME_V2_RECORD_HEADER_SIZE
        + series_count * 8;
    if (
        move_count
        > (std::numeric_limits<std::uint64_t>::max() - with_series) / 2
    ) {
        throw std::length_error("full-game v2 packed moves overflow");
    }
    return with_series + move_count * 2;
}

void validate_v2_record(
    const FullGameBatchConfigV2& config,
    const FullGameRecord& record
) {
    if (
        record.white_profile_index >= config.profiles.size()
        || record.black_profile_index >= config.profiles.size()
    ) {
        throw std::logic_error("full-game v2 profile index is out of range");
    }
    if (record.accepted()) {
        if (record.series_ends.empty() || record.moves.empty()) {
            throw std::logic_error("accepted full-game v2 record has no trace");
        }
        std::uint64_t prior = 0;
        for (const std::uint64_t end : record.series_ends) {
            if (end <= prior || end > record.moves.size()) {
                throw std::logic_error("full-game v2 series offsets are invalid");
            }
            prior = end;
        }
        if (prior != record.moves.size()) {
            throw std::logic_error("full-game v2 final series offset is invalid");
        }
    } else if (
        record.terminal != FullGameTerminal::None
        || record.reject == FullGameReject::None
        || !record.series_ends.empty()
        || !record.moves.empty()
    ) {
        throw std::logic_error("rejected full-game v2 record contains a result or trace");
    }
}

[[nodiscard]] std::vector<std::uint8_t> encode_full_game_batch_v2(
    const FullGameBatchConfigV2& config,
    const std::vector<FullGameRecord>& records
) {
    constexpr std::array<std::uint8_t, 8> MAGIC = {
        'S', 'P', 'C', 'F', 'G', 'B', '0', '2',
    };
    if (records.size() != config.common.attempt_count) {
        throw std::invalid_argument("full-game v2 record count does not match request");
    }
    std::uint64_t total_size = FULL_GAME_V2_RESPONSE_HEADER_SIZE;
    std::uint64_t total_saturations = 0;
    for (const auto& record : records) {
        validate_v2_record(config, record);
        std::uint64_t next_size = 0;
        if (!add_u64(total_size, encoded_record_size_v2(record), next_size)) {
            throw std::length_error("full-game v2 batch payload overflow");
        }
        total_size = next_size;
        std::uint64_t next_saturations = 0;
        if (!add_u64(
                total_saturations,
                record.path_count_saturations,
                next_saturations
            )) {
            total_saturations = std::numeric_limits<std::uint64_t>::max();
        } else {
            total_saturations = next_saturations;
        }
    }
    if (total_size > std::numeric_limits<std::size_t>::max()) {
        throw std::length_error("full-game v2 batch exceeds addressable memory");
    }

    std::vector<std::uint8_t> output;
    output.reserve(static_cast<std::size_t>(total_size));
    output.insert(output.end(), MAGIC.begin(), MAGIC.end());
    append_little_endian(output, FULL_GAME_BATCH_V2_VERSION);
    append_little_endian(output, FULL_GAME_V2_RESPONSE_HEADER_SIZE);
    append_little_endian(output, FULL_GAME_V2_RECORD_HEADER_SIZE);
    append_little_endian(
        output,
        static_cast<std::uint16_t>(total_saturations == 0 ? 0 : 1)
    );
    append_little_endian(output, config.common.first_attempt);
    append_little_endian(output, config.common.attempt_count);
    output.insert(
        output.end(),
        config.semantic_config_digest.begin(),
        config.semantic_config_digest.end()
    );
    append_little_endian(
        output,
        static_cast<std::uint32_t>(config.profiles.size())
    );
    append_little_endian(
        output,
        static_cast<std::uint16_t>(config.policy.kind)
    );
    append_little_endian(
        output,
        static_cast<std::uint16_t>(config.schedule)
    );
    append_little_endian(output, total_saturations);

    for (const auto& record : records) {
        append_little_endian(output, encoded_record_size_v2(record));
        append_little_endian(output, record.attempt_index);
        append_u8(output, record.accepted() ? 0 : 1);
        append_u8(output, static_cast<std::uint8_t>(record.terminal));
        append_u8(output, static_cast<std::uint8_t>(record.reject));
        append_u8(output, 0);
        append_little_endian(
            output,
            static_cast<std::uint32_t>(
                record.path_count_saturations == 0 ? 0 : 1
            )
        );
        append_little_endian(output, record.white_profile_index);
        append_little_endian(output, record.black_profile_index);
        append_little_endian(
            output,
            static_cast<std::uint64_t>(record.series_ends.size())
        );
        append_little_endian(
            output,
            static_cast<std::uint64_t>(record.moves.size())
        );
        append_little_endian(output, record.logical_work);
        append_little_endian(output, record.path_count_saturations);
        for (const std::uint64_t end : record.series_ends) {
            append_little_endian(output, end);
        }
        for (const std::uint16_t move : record.moves) {
            append_little_endian(output, move);
        }
    }
    if (output.size() != total_size) {
        throw std::logic_error("full-game v2 batch encoder size mismatch");
    }
    return output;
}

}  // namespace

std::vector<FullGameRecord> generate_full_games(
    const FullGameBatchConfig& config
) {
    if (
        config.attempt_count > std::numeric_limits<std::uint32_t>::max()
        || config.max_frontier_states == 0
        || config.max_positions_per_series == 0
        || (
            config.max_positions_per_game == 0
            && config.max_attempt_series == 0
        )
        || config.candidate_count == 0
        || (
            config.attempt_count != 0
            && config.first_attempt > std::numeric_limits<std::uint64_t>::max()
                - (config.attempt_count - 1)
        )
    ) {
        throw std::invalid_argument("invalid native full-game batch configuration");
    }
    std::vector<FullGameRecord> records;
    records.reserve(static_cast<std::size_t>(config.attempt_count));
    for (std::uint64_t offset = 0; offset < config.attempt_count; ++offset) {
        records.push_back(generate_one(config, config.first_attempt + offset));
    }
    return records;
}

std::vector<std::uint8_t> encode_full_game_batch(
    const FullGameBatchConfig& config,
    const std::vector<FullGameRecord>& records
) {
    if (records.size() != config.attempt_count) {
        throw std::invalid_argument("full-game record count does not match request");
    }
    constexpr std::array<std::uint8_t, 8> MAGIC = {
        'S', 'P', 'C', 'F', 'G', 'B', '0', '1',
    };
    constexpr std::uint16_t HEADER_SIZE = 32;
    std::uint64_t total_size = HEADER_SIZE;
    for (const auto& record : records) {
        std::uint64_t next = 0;
        if (!add_u64(total_size, encoded_record_size(record), next)) {
            throw std::length_error("full-game batch payload overflow");
        }
        total_size = next;
    }
    if (total_size > std::numeric_limits<std::size_t>::max()) {
        throw std::length_error("full-game batch exceeds addressable memory");
    }

    std::vector<std::uint8_t> output;
    output.reserve(static_cast<std::size_t>(total_size));
    output.insert(output.end(), MAGIC.begin(), MAGIC.end());
    append_little_endian(output, FULL_GAME_BATCH_VERSION);
    append_little_endian(output, HEADER_SIZE);
    append_little_endian(
        output,
        static_cast<std::uint32_t>(records.size())
    );
    append_little_endian(output, config.first_attempt);
    append_little_endian(output, config.attempt_count);

    for (const auto& record : records) {
        const std::uint64_t record_size = encoded_record_size(record);
        append_little_endian(output, record_size);
        append_little_endian(output, record.attempt_index);
        append_u8(output, record.accepted() ? 0 : 1);
        append_u8(output, static_cast<std::uint8_t>(record.terminal));
        append_u8(output, static_cast<std::uint8_t>(record.reject));
        append_u8(output, 0);
        append_little_endian(
            output,
            static_cast<std::uint64_t>(record.series_ends.size())
        );
        append_little_endian(
            output,
            static_cast<std::uint64_t>(record.moves.size())
        );
        append_little_endian(output, record.logical_work);
        for (const std::uint64_t end : record.series_ends) {
            append_little_endian(output, end);
        }
        for (const std::uint16_t move : record.moves) {
            append_little_endian(output, move);
        }
    }
    if (output.size() != total_size) {
        throw std::logic_error("full-game batch encoder size mismatch");
    }
    return output;
}

std::vector<std::uint8_t> generate_full_game_batch_v2(
    const std::vector<std::uint8_t>& request
) {
    const FullGameBatchConfigV2 config = parse_full_game_v2_request(request);
    const auto records = generate_full_games_v2(config);
    return encode_full_game_batch_v2(config, records);
}

}  // namespace spc::native
