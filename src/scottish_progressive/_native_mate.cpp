#define PY_SSIZE_T_CLEAN
#include <Python.h>

#ifndef SPC_NATIVE_MATE_SOURCE_IDENTITY
#define SPC_NATIVE_MATE_SOURCE_IDENTITY "unconfigured"
#endif

#include <algorithm>
#include <array>
#include <bit>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <exception>
#include <functional>
#include <limits>
#include <new>
#include <optional>
#include <queue>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

namespace spc::native_mate {
namespace {

using Bitboard = std::uint64_t;

constexpr bool BLACK = false;
constexpr bool WHITE = true;

constexpr int PAWN = 1;
constexpr int KNIGHT = 2;
constexpr int BISHOP = 3;
constexpr int ROOK = 4;
constexpr int QUEEN = 5;
constexpr int KING = 6;

struct Position {
    Bitboard pawns;
    Bitboard knights;
    Bitboard bishops;
    Bitboard rooks;
    Bitboard queens;
    Bitboard kings;
    std::array<Bitboard, 2> occupied;
    bool white_to_move;
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

struct Move {
    int from;
    int to;
    int promotion;
    int required_ep_square;
    bool castling;
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

enum class SeriesMateSearchStatus : std::uint8_t {
    Found = 0,
    Exhausted = 1,
    WorkLimit = 2,
    Deadline = 3,
    Unsupported = 4,
};

struct SeriesMateSearchStats {
    std::uint64_t positions_visited = 0;
    std::uint64_t moves_generated = 0;
    std::uint64_t transpositions_merged = 0;
    std::uint64_t checking_series = 0;
    std::uint64_t checkmates = 0;
    std::uint64_t peak_frontier = 0;
    std::uint64_t max_depth_reached = 0;
};

struct SeriesMateSearchRequest {
    BoardState board;
    std::int64_t series_number;
    std::vector<int> ep_targets;
    std::optional<std::uint64_t> max_positions;
    std::optional<std::chrono::steady_clock::time_point> deadline = std::nullopt;
    std::optional<std::uint64_t> max_work;
};

struct SeriesMateSearchResponse {
    SeriesMateSearchStatus status = SeriesMateSearchStatus::Exhausted;
    std::string message;
    SeriesMateSearchStats stats;
    std::vector<std::string> moves;
};

constexpr std::array<std::array<int, 2>, 8> KNIGHT_DELTAS = {{
    {{-2, -1}}, {{-2, 1}}, {{-1, -2}}, {{-1, 2}},
    {{1, -2}}, {{1, 2}}, {{2, -1}}, {{2, 1}},
}};
constexpr std::array<std::array<int, 2>, 8> KING_DELTAS = {{
    {{-1, -1}}, {{-1, 0}}, {{-1, 1}}, {{0, -1}},
    {{0, 1}}, {{1, -1}}, {{1, 0}}, {{1, 1}},
}};
constexpr std::array<std::array<int, 2>, 4> ORTHOGONAL = {{
    {{-1, 0}}, {{1, 0}}, {{0, -1}}, {{0, 1}},
}};
constexpr std::array<std::array<int, 2>, 4> DIAGONAL = {{
    {{-1, -1}}, {{-1, 1}}, {{1, -1}}, {{1, 1}},
}};
constexpr std::array<std::array<int, 2>, 8> RAY_DELTAS = {{
    {{-1, 0}}, {{1, 0}}, {{0, -1}}, {{0, 1}},
    {{-1, -1}}, {{-1, 1}}, {{1, -1}}, {{1, 1}},
}};
constexpr std::array<bool, 8> RAY_ASCENDING = {{
    false, true, false, true, false, true, false, true,
}};

[[nodiscard]] constexpr bool inside(int file, int rank) noexcept {
    return file >= 0 && file < 8 && rank >= 0 && rank < 8;
}

[[nodiscard]] constexpr int square(int file, int rank) noexcept {
    return rank * 8 + file;
}

[[nodiscard]] constexpr Bitboard bit(int square_index) noexcept {
    return Bitboard{1} << square_index;
}

[[nodiscard]] constexpr auto knight_attack_masks() noexcept {
    std::array<Bitboard, 64> masks{};
    for (int target = 0; target < 64; ++target) {
        const int target_file = target & 7;
        const int target_rank = target >> 3;
        for (const auto& delta : KNIGHT_DELTAS) {
            const int file = target_file + delta[0];
            const int rank = target_rank + delta[1];
            if (inside(file, rank)) {
                masks[static_cast<std::size_t>(target)] |= bit(square(file, rank));
            }
        }
    }
    return masks;
}

[[nodiscard]] constexpr auto king_attack_masks() noexcept {
    std::array<Bitboard, 64> masks{};
    for (int target = 0; target < 64; ++target) {
        const int target_file = target & 7;
        const int target_rank = target >> 3;
        for (const auto& delta : KING_DELTAS) {
            const int file = target_file + delta[0];
            const int rank = target_rank + delta[1];
            if (inside(file, rank)) {
                masks[static_cast<std::size_t>(target)] |= bit(square(file, rank));
            }
        }
    }
    return masks;
}

[[nodiscard]] constexpr auto pawn_attacker_masks() noexcept {
    std::array<std::array<Bitboard, 64>, 2> masks{};
    for (int color = 0; color < 2; ++color) {
        const bool attacker = color == 1;
        for (int target = 0; target < 64; ++target) {
            const int target_file = target & 7;
            const int target_rank = target >> 3;
            const int source_rank = target_rank + (attacker == WHITE ? -1 : 1);
            for (const int file_delta : {-1, 1}) {
                const int source_file = target_file + file_delta;
                if (inside(source_file, source_rank)) {
                    masks[static_cast<std::size_t>(color)]
                        [static_cast<std::size_t>(target)]
                        |= bit(square(source_file, source_rank));
                }
            }
        }
    }
    return masks;
}

[[nodiscard]] constexpr auto ray_masks() noexcept {
    std::array<std::array<Bitboard, 8>, 64> masks{};
    for (int target = 0; target < 64; ++target) {
        const int target_file = target & 7;
        const int target_rank = target >> 3;
        for (std::size_t direction = 0; direction < RAY_DELTAS.size(); ++direction) {
            const auto& delta = RAY_DELTAS[direction];
            int file = target_file + delta[0];
            int rank = target_rank + delta[1];
            while (inside(file, rank)) {
                masks[static_cast<std::size_t>(target)][direction]
                    |= bit(square(file, rank));
                file += delta[0];
                rank += delta[1];
            }
        }
    }
    return masks;
}

constexpr auto KNIGHT_ATTACK_MASKS = knight_attack_masks();
constexpr auto KING_ATTACK_MASKS = king_attack_masks();
constexpr auto PAWN_ATTACKER_MASKS = pawn_attacker_masks();
constexpr auto RAY_MASKS = ray_masks();

[[nodiscard]] int piece_type_at(
    const Position& position,
    int square_index
) noexcept {
    const Bitboard mask = bit(square_index);
    if ((position.pawns & mask) != 0) {
        return PAWN;
    }
    if ((position.knights & mask) != 0) {
        return KNIGHT;
    }
    if ((position.bishops & mask) != 0) {
        return BISHOP;
    }
    if ((position.rooks & mask) != 0) {
        return ROOK;
    }
    if ((position.queens & mask) != 0) {
        return QUEEN;
    }
    if ((position.kings & mask) != 0) {
        return KING;
    }
    return 0;
}

[[nodiscard]] int king_square(const Position& position, bool color) noexcept {
    const Bitboard king = position.kings & position.occupied[color ? 1 : 0];
    return king == 0 ? -1 : static_cast<int>(std::countr_zero(king));
}

[[nodiscard]] bool attacked_by(
    const Position& position,
    int target,
    bool attacker,
    Bitboard occupancy,
    Bitboard attacker_occupancy
) noexcept {
    const std::size_t target_index = static_cast<std::size_t>(target);
    const Bitboard pawns = position.pawns & attacker_occupancy;
    if ((pawns & PAWN_ATTACKER_MASKS[attacker ? 1 : 0][target_index]) != 0) {
        return true;
    }
    const Bitboard knights = position.knights & attacker_occupancy;
    if ((knights & KNIGHT_ATTACK_MASKS[target_index]) != 0) {
        return true;
    }
    const Bitboard kings = position.kings & attacker_occupancy;
    if ((kings & KING_ATTACK_MASKS[target_index]) != 0) {
        return true;
    }
    const Bitboard rook_attackers = attacker_occupancy
        & (position.rooks | position.queens);
    const Bitboard bishop_attackers = attacker_occupancy
        & (position.bishops | position.queens);
    for (std::size_t direction = 0; direction < RAY_MASKS[target_index].size(); ++direction) {
        const Bitboard blockers = occupancy & RAY_MASKS[target_index][direction];
        if (blockers == 0) {
            continue;
        }
        const int source = RAY_ASCENDING[direction]
            ? static_cast<int>(std::countr_zero(blockers))
            : 63 - static_cast<int>(std::countl_zero(blockers));
        const Bitboard attackers = direction < 4 ? rook_attackers : bishop_attackers;
        if ((attackers & bit(source)) != 0) {
            return true;
        }
    }
    return false;
}

[[nodiscard]] Position evaluation_position(const BoardState& board) noexcept {
    return Position{
        board.pawns,
        board.knights,
        board.bishops,
        board.rooks,
        board.queens,
        board.kings,
        board.occupied,
        board.white_to_move,
    };
}

[[nodiscard]] bool board_attacked_by(
    const BoardState& board,
    int target,
    bool attacker
) noexcept {
    const Position position = evaluation_position(board);
    return attacked_by(
        position,
        target,
        attacker,
        board.occupied[0] | board.occupied[1],
        board.occupied[attacker ? 1 : 0]
    );
}

void clear_piece(BoardState& board, int square_index) noexcept {
    const Bitboard mask = ~bit(square_index);
    board.pawns &= mask;
    board.knights &= mask;
    board.bishops &= mask;
    board.rooks &= mask;
    board.queens &= mask;
    board.kings &= mask;
    board.occupied[0] &= mask;
    board.occupied[1] &= mask;
    board.promoted &= mask;
}

void set_piece(
    BoardState& board,
    int square_index,
    int piece_type,
    bool color,
    bool promoted
) noexcept {
    const Bitboard mask = bit(square_index);
    switch (piece_type) {
        case PAWN: board.pawns |= mask; break;
        case KNIGHT: board.knights |= mask; break;
        case BISHOP: board.bishops |= mask; break;
        case ROOK: board.rooks |= mask; break;
        case QUEEN: board.queens |= mask; break;
        case KING: board.kings |= mask; break;
        default: return;
    }
    board.occupied[color ? 1 : 0] |= mask;
    if (promoted) {
        board.promoted |= mask;
    }
}

[[nodiscard]] BoardState apply_move(
    const BoardState& source,
    const Move& move
) noexcept {
    BoardState board = source;
    const bool mover = source.white_to_move;
    const int moving_piece = piece_type_at(evaluation_position(source), move.from);
    const bool was_promoted = (source.promoted & bit(move.from)) != 0;
    const bool en_passant = move.required_ep_square >= 0
        && moving_piece == PAWN
        && move.to == move.required_ep_square
        && (source.occupied[0] & bit(move.to)) == 0
        && (source.occupied[1] & bit(move.to)) == 0;
    const int capture_square = en_passant
        ? move.to + (mover == WHITE ? -8 : 8)
        : move.to;

    clear_piece(board, move.from);
    clear_piece(board, capture_square);
    set_piece(
        board,
        move.to,
        move.promotion != 0 ? move.promotion : moving_piece,
        mover,
        was_promoted || move.promotion != 0
    );

    if (move.castling) {
        const int rank = mover == WHITE ? 0 : 7;
        const bool king_side = (move.to & 7) == 6;
        const int rook_from = square(king_side ? 7 : 0, rank);
        const int rook_to = square(king_side ? 5 : 3, rank);
        const bool rook_promoted = (source.promoted & bit(rook_from)) != 0;
        clear_piece(board, rook_from);
        set_piece(board, rook_to, ROOK, mover, rook_promoted);
    }

    board.castling_rights &= ~bit(move.from);
    board.castling_rights &= ~bit(move.to);
    if (moving_piece == KING) {
        const int rank = mover == WHITE ? 0 : 7;
        board.castling_rights &= ~bit(square(0, rank));
        board.castling_rights &= ~bit(square(7, rank));
    }
    board.white_to_move = !mover;
    return board;
}

[[nodiscard]] bool legal_after_move(
    const BoardState& source,
    const Move& move
) noexcept {
    const bool mover = source.white_to_move;
    const BoardState child = apply_move(source, move);
    const Bitboard king = child.kings & child.occupied[mover ? 1 : 0];
    if (king == 0) {
        return false;
    }
    return !board_attacked_by(
        child,
        static_cast<int>(std::countr_zero(king)),
        !mover
    );
}

void add_promotions(
    std::vector<Move>& moves,
    int from,
    int to,
    int required_ep_square = -1
) {
    for (const int promotion : {QUEEN, ROOK, BISHOP, KNIGHT}) {
        moves.push_back(Move{from, to, promotion, required_ep_square, false});
    }
}

void add_standard_castling(const BoardState& board, std::vector<Move>& moves) {
    const bool mover = board.white_to_move;
    const int rank = mover == WHITE ? 0 : 7;
    const int king_from = square(4, rank);
    const Bitboard own = board.occupied[mover ? 1 : 0];
    const Bitboard occupancy = board.occupied[0] | board.occupied[1];
    if (
        (board.kings & own & bit(king_from)) == 0
        || board_attacked_by(board, king_from, !mover)
    ) {
        return;
    }

    const auto try_side = [&](bool king_side) {
        const int rook_from = square(king_side ? 7 : 0, rank);
        if (
            (board.castling_rights & bit(rook_from)) == 0
            || (board.rooks & own & bit(rook_from)) == 0
            || (board.promoted & bit(rook_from)) != 0
        ) {
            return;
        }
        const int first_file = king_side ? 5 : 1;
        const int last_file = king_side ? 6 : 3;
        for (int file = first_file; file <= last_file; ++file) {
            if ((occupancy & bit(square(file, rank))) != 0) {
                return;
            }
        }
        const int pass = square(king_side ? 5 : 3, rank);
        const Move pass_move{king_from, pass, 0, -1, false};
        if (!legal_after_move(board, pass_move)) {
            return;
        }
        const int destination = square(king_side ? 6 : 2, rank);
        const Move castle{king_from, destination, 0, -1, true};
        if (legal_after_move(board, castle)) {
            moves.push_back(castle);
        }
    };
    try_side(true);
    try_side(false);
}

[[nodiscard]] std::vector<Move> pseudo_moves(
    const BoardState& board,
    const std::vector<int>& ep_targets
) {
    std::vector<Move> moves;
    moves.reserve(64);
    const bool mover = board.white_to_move;
    const Bitboard own = board.occupied[mover ? 1 : 0];
    const Bitboard enemy = board.occupied[(!mover) ? 1 : 0];
    const Bitboard occupancy = own | enemy;

    Bitboard pawns = board.pawns & own;
    while (pawns != 0) {
        const int from = static_cast<int>(std::countr_zero(pawns));
        pawns &= pawns - 1;
        const int from_file = from & 7;
        const int from_rank = from >> 3;
        const int direction = mover == WHITE ? 1 : -1;
        const int next_rank = from_rank + direction;
        if (inside(from_file, next_rank)) {
            const int to = square(from_file, next_rank);
            if ((occupancy & bit(to)) == 0) {
                if (next_rank == 0 || next_rank == 7) {
                    add_promotions(moves, from, to);
                } else {
                    moves.push_back(Move{from, to, 0, -1, false});
                    const int start_rank = mover == WHITE ? 1 : 6;
                    const int double_rank = from_rank + direction * 2;
                    if (
                        from_rank == start_rank
                        && (occupancy & bit(square(from_file, double_rank))) == 0
                    ) {
                        moves.push_back(Move{
                            from,
                            square(from_file, double_rank),
                            0,
                            -1,
                            false,
                        });
                    }
                }
            }
        }
        for (const int file_delta : {-1, 1}) {
            const int to_file = from_file + file_delta;
            const int to_rank = from_rank + direction;
            if (!inside(to_file, to_rank)) {
                continue;
            }
            const int to = square(to_file, to_rank);
            if ((enemy & bit(to)) == 0) {
                continue;
            }
            if (to_rank == 0 || to_rank == 7) {
                add_promotions(moves, from, to);
            } else {
                moves.push_back(Move{from, to, 0, -1, false});
            }
        }
    }

    for (const int target : ep_targets) {
        if (target < 0 || target >= 64 || (occupancy & bit(target)) != 0) {
            continue;
        }
        const int target_file = target & 7;
        const int target_rank = target >> 3;
        const int expected_rank = mover == WHITE ? 5 : 2;
        const int source_rank = target_rank + (mover == WHITE ? -1 : 1);
        const int captured = target + (mover == WHITE ? -8 : 8);
        if (
            target_rank != expected_rank
            || captured < 0
            || captured >= 64
            || (board.pawns & enemy & bit(captured)) == 0
        ) {
            continue;
        }
        for (const int file_delta : {-1, 1}) {
            const int source_file = target_file + file_delta;
            if (!inside(source_file, source_rank)) {
                continue;
            }
            const int from = square(source_file, source_rank);
            if ((board.pawns & own & bit(from)) != 0) {
                moves.push_back(Move{from, target, 0, target, false});
            }
        }
    }

    Bitboard knights = board.knights & own;
    while (knights != 0) {
        const int from = static_cast<int>(std::countr_zero(knights));
        knights &= knights - 1;
        const int from_file = from & 7;
        const int from_rank = from >> 3;
        for (const auto& delta : KNIGHT_DELTAS) {
            const int file = from_file + delta[0];
            const int rank = from_rank + delta[1];
            if (inside(file, rank) && (own & bit(square(file, rank))) == 0) {
                moves.push_back(Move{from, square(file, rank), 0, -1, false});
            }
        }
    }

    const auto add_sliders = [&](Bitboard pieces, const auto& deltas) {
        while (pieces != 0) {
            const int from = static_cast<int>(std::countr_zero(pieces));
            pieces &= pieces - 1;
            const int from_file = from & 7;
            const int from_rank = from >> 3;
            for (const auto& delta : deltas) {
                int file = from_file + delta[0];
                int rank = from_rank + delta[1];
                while (inside(file, rank)) {
                    const int to = square(file, rank);
                    if ((own & bit(to)) != 0) {
                        break;
                    }
                    moves.push_back(Move{from, to, 0, -1, false});
                    if ((enemy & bit(to)) != 0) {
                        break;
                    }
                    file += delta[0];
                    rank += delta[1];
                }
            }
        }
    };
    add_sliders(board.bishops & own, DIAGONAL);
    add_sliders(board.rooks & own, ORTHOGONAL);
    add_sliders(board.queens & own, DIAGONAL);
    add_sliders(board.queens & own, ORTHOGONAL);

    Bitboard kings = board.kings & own;
    while (kings != 0) {
        const int from = static_cast<int>(std::countr_zero(kings));
        kings &= kings - 1;
        const int from_file = from & 7;
        const int from_rank = from >> 3;
        for (const auto& delta : KING_DELTAS) {
            const int file = from_file + delta[0];
            const int rank = from_rank + delta[1];
            if (inside(file, rank) && (own & bit(square(file, rank))) == 0) {
                moves.push_back(Move{from, square(file, rank), 0, -1, false});
            }
        }
    }
    add_standard_castling(board, moves);
    return moves;
}

[[nodiscard]] std::string move_uci(const Move& move) {
    std::string result;
    result.reserve(move.promotion == 0 ? 4 : 5);
    result.push_back(static_cast<char>('a' + (move.from & 7)));
    result.push_back(static_cast<char>('1' + (move.from >> 3)));
    result.push_back(static_cast<char>('a' + (move.to & 7)));
    result.push_back(static_cast<char>('1' + (move.to >> 3)));
    if (move.promotion != 0) {
        constexpr std::array<char, 7> SYMBOLS = {'\0', 'p', 'n', 'b', 'r', 'q', 'k'};
        result.push_back(SYMBOLS[static_cast<std::size_t>(move.promotion)]);
    }
    return result;
}

[[nodiscard]] std::vector<ExpandedMove> expand_legal_move_variants(
    const BoardState& position,
    const std::vector<int>& ep_targets
) {
    std::vector<ExpandedMove> legal;
    const bool mover = position.white_to_move;
    const Bitboard enemy = position.occupied[(!mover) ? 1 : 0];
    const Position evaluation = evaluation_position(position);
    for (const Move& move : pseudo_moves(position, ep_targets)) {
        BoardState child = apply_move(position, move);
        const Bitboard own_king = child.kings & child.occupied[mover ? 1 : 0];
        if (
            own_king == 0
            || board_attacked_by(
                child,
                static_cast<int>(std::countr_zero(own_king)),
                !mover
            )
        ) {
            continue;
        }
        const int moving_piece = piece_type_at(evaluation, move.from);
        const bool en_passant = move.required_ep_square >= 0
            && moving_piece == PAWN
            && move.to == move.required_ep_square
            && (position.occupied[0] & bit(move.to)) == 0
            && (position.occupied[1] & bit(move.to)) == 0;
        const bool is_capture = en_passant || (enemy & bit(move.to)) != 0;
        const Bitboard opponent_king = child.kings & child.occupied[(!mover) ? 1 : 0];
        const bool delivered_check = opponent_king != 0
            && board_attacked_by(
                child,
                static_cast<int>(std::countr_zero(opponent_king)),
                mover
            );
        legal.push_back(ExpandedMove{
            LegalMove{
                move_uci(move),
                move.from,
                move.to,
                move.promotion,
                move.required_ep_square,
            },
            child,
            moving_piece == PAWN,
            is_capture,
            delivered_check,
        });
    }
    std::sort(
        legal.begin(),
        legal.end(),
        [](const ExpandedMove& left, const ExpandedMove& right) {
            return left.move.uci < right.move.uci;
        }
    );
    legal.erase(
        std::unique(
            legal.begin(),
            legal.end(),
            [](const ExpandedMove& left, const ExpandedMove& right) {
                return left.move.uci == right.move.uci;
            }
        ),
        legal.end()
    );
    return legal;
}

[[nodiscard]] std::vector<LegalMove> legal_move_variants(
    const BoardState& position,
    const std::vector<int>& ep_targets
) {
    std::vector<LegalMove> legal;
    const auto expanded = expand_legal_move_variants(position, ep_targets);
    legal.reserve(expanded.size());
    for (const ExpandedMove& move : expanded) {
        legal.push_back(move.move);
    }
    return legal;
}

[[nodiscard]] bool has_legal_move(
    const BoardState& position,
    const std::vector<int>& ep_targets
) {
    for (const Move& move : pseudo_moves(position, ep_targets)) {
        if (legal_after_move(position, move)) {
            return true;
        }
    }
    return false;
}

void hash_word(std::size_t& seed, std::uint64_t value) noexcept {
    seed ^= std::hash<std::uint64_t>{}(value)
        + static_cast<std::size_t>(0x9e3779b97f4a7c15ULL)
        + (seed << 6)
        + (seed >> 2);
}

[[nodiscard]] std::vector<int> canonical_boundary_ep_targets(
    const BoardState& board,
    Bitboard pending_ep_targets
) {
    std::vector<int> targets;
    const Bitboard occupancy = board.occupied[0] | board.occupied[1];
    while (pending_ep_targets != 0) {
        const int target = static_cast<int>(std::countr_zero(pending_ep_targets));
        pending_ep_targets &= pending_ep_targets - 1;
        if ((occupancy & bit(target)) != 0) {
            continue;
        }
        const auto legal = legal_move_variants(board, {target});
        if (std::any_of(
                legal.begin(),
                legal.end(),
                [target](const LegalMove& move) {
                    return move.required_ep_square == target;
                }
            )) {
            targets.push_back(target);
        }
    }
    return targets;
}

void update_pending_ep_targets(
    Bitboard& pending_ep_targets,
    const ExpandedMove& expanded,
    bool mover
) noexcept {
    if (expanded.is_pawn_move) {
        const int prior_target = expanded.move.from_square + (mover == WHITE ? -8 : 8);
        if (prior_target >= 0 && prior_target < 64) {
            pending_ep_targets &= ~bit(prior_target);
        }
        if (std::abs(expanded.move.to_square - expanded.move.from_square) == 16) {
            pending_ep_targets |= bit(
                (expanded.move.from_square + expanded.move.to_square) / 2
            );
        }
    }
}

struct MateSearchIdentity {
    std::array<Bitboard, 10> words;
    bool white_to_move;
    Bitboard pending_ep_targets;

    bool operator==(const MateSearchIdentity&) const = default;
};

struct MateSearchIdentityHash {
    std::size_t operator()(const MateSearchIdentity& key) const noexcept {
        std::size_t seed = 0;
        for (const Bitboard word : key.words) {
            hash_word(seed, word);
        }
        hash_word(seed, key.white_to_move ? 1 : 0);
        hash_word(seed, key.pending_ep_targets);
        return seed;
    }
};

[[nodiscard]] MateSearchIdentity mate_search_identity(
    const BoardState& board,
    Bitboard pending_ep_targets
) noexcept {
    return MateSearchIdentity{
        {
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
        },
        board.white_to_move,
        pending_ep_targets,
    };
}

[[nodiscard]] int attack_count_to(
    const BoardState& board,
    int target,
    bool attacker
) noexcept {
    const Position position = evaluation_position(board);
    const Bitboard occupancy = board.occupied[0] | board.occupied[1];
    const Bitboard own = board.occupied[attacker ? 1 : 0];
    const std::size_t target_index = static_cast<std::size_t>(target);
    int result = 0;
    result += std::popcount(
        position.pawns & own & PAWN_ATTACKER_MASKS[attacker ? 1 : 0][target_index]
    );
    result += std::popcount(position.knights & own & KNIGHT_ATTACK_MASKS[target_index]);
    result += std::popcount(position.kings & own & KING_ATTACK_MASKS[target_index]);
    const Bitboard rook_attackers = own & (position.rooks | position.queens);
    const Bitboard bishop_attackers = own & (position.bishops | position.queens);
    for (std::size_t direction = 0; direction < RAY_MASKS[target_index].size(); ++direction) {
        const Bitboard blockers = occupancy & RAY_MASKS[target_index][direction];
        if (blockers == 0) {
            continue;
        }
        const int source = RAY_ASCENDING[direction]
            ? static_cast<int>(std::countr_zero(blockers))
            : 63 - static_cast<int>(std::countl_zero(blockers));
        const Bitboard attackers = direction < 4 ? rook_attackers : bishop_attackers;
        result += (attackers & bit(source)) != 0 ? 1 : 0;
    }
    return result;
}

[[nodiscard]] std::int64_t mate_covering_priority(
    const BoardState& board,
    bool mover,
    std::size_t depth
) noexcept {
    const Position position = evaluation_position(board);
    const int enemy_king = king_square(position, !mover);
    if (enemy_king < 0) {
        return std::numeric_limits<std::int64_t>::min() / 2;
    }
    int covered = 0;
    int pressure = 0;
    int occupied_ring = 0;
    Bitboard ring = KING_ATTACK_MASKS[static_cast<std::size_t>(enemy_king)];
    while (ring != 0) {
        const int target = static_cast<int>(std::countr_zero(ring));
        ring &= ring - 1;
        const int attacks = attack_count_to(board, target, mover);
        covered += attacks > 0 ? 1 : 0;
        pressure += attacks;
        occupied_ring += (board.occupied[(!mover) ? 1 : 0] & bit(target)) != 0
            ? 1
            : 0;
    }

    int near_pressure = 0;
    Bitboard pieces = board.occupied[mover ? 1 : 0] & ~board.kings;
    while (pieces != 0) {
        const int source = static_cast<int>(std::countr_zero(pieces));
        pieces &= pieces - 1;
        const int file_distance = std::abs((source & 7) - (enemy_king & 7));
        const int rank_distance = std::abs((source >> 3) - (enemy_king >> 3));
        near_pressure += std::max(0, 5 - std::max(file_distance, rank_distance));
    }

    int best_pawn_progress = 0;
    Bitboard pawns = board.pawns & board.occupied[mover ? 1 : 0];
    while (pawns != 0) {
        const int source = static_cast<int>(std::countr_zero(pawns));
        pawns &= pawns - 1;
        const int rank = source >> 3;
        best_pawn_progress = std::max(
            best_pawn_progress,
            mover == WHITE ? rank : 7 - rank
        );
    }
    const int promoted_attackers = std::popcount(
        board.promoted & board.occupied[mover ? 1 : 0]
    );

    return static_cast<std::int64_t>(covered) * 1'000
        + static_cast<std::int64_t>(pressure) * 100
        + static_cast<std::int64_t>(occupied_ring) * 50
        + static_cast<std::int64_t>(near_pressure) * 4
        + static_cast<std::int64_t>(best_pawn_progress) * 300
        + static_cast<std::int64_t>(promoted_attackers) * 800
        + static_cast<std::int64_t>(depth);
}

struct MateSearchNode {
    BoardState board;
    std::vector<std::string> moves;
    Bitboard pending_ep_targets = 0;
    std::int64_t priority = 0;
};

struct MateSearchNodeWorse {
    bool operator()(
        const MateSearchNode& left,
        const MateSearchNode& right
    ) const noexcept {
        if (left.priority != right.priority) {
            return left.priority < right.priority;
        }
        return left.moves > right.moves;
    }
};

[[nodiscard]] bool mate_search_deadline_reached(
    const SeriesMateSearchRequest& request
) noexcept {
    return request.deadline.has_value()
        && std::chrono::steady_clock::now() >= *request.deadline;
}

[[nodiscard]] bool mate_search_work_limit_reached(
    const SeriesMateSearchRequest& request,
    const SeriesMateSearchStats& stats
) noexcept {
    if (!request.max_work.has_value()) {
        return false;
    }
    const std::uint64_t limit = *request.max_work;
    return stats.positions_visited >= limit
        || stats.moves_generated >= limit - stats.positions_visited;
}

[[nodiscard]] SeriesMateSearchResponse find_series_mate(
    const SeriesMateSearchRequest& request
) {
    SeriesMateSearchResponse response;
    if (
        request.series_number < 1
        || request.series_number > 256
        || (request.max_positions.has_value() && *request.max_positions == 0)
        || king_square(evaluation_position(request.board), WHITE) < 0
        || king_square(evaluation_position(request.board), BLACK) < 0
    ) {
        response.status = SeriesMateSearchStatus::Unsupported;
        response.message = "native series-mate request is out of range";
        return response;
    }
    if (mate_search_deadline_reached(request)) {
        response.status = SeriesMateSearchStatus::Deadline;
        response.message = "native series-mate deadline reached";
        return response;
    }

    const bool mover = request.board.white_to_move;
    MateSearchNode root{
        request.board,
        {},
        0,
        mate_covering_priority(request.board, mover, 0),
    };
    std::priority_queue<
        MateSearchNode,
        std::vector<MateSearchNode>,
        MateSearchNodeWorse
    > open;
    open.push(std::move(root));
    std::unordered_map<
        MateSearchIdentity,
        std::size_t,
        MateSearchIdentityHash
    > minimum_depth;
    minimum_depth.emplace(mate_search_identity(request.board, 0), 0);
    response.stats.peak_frontier = 1;

    while (!open.empty()) {
        if (mate_search_deadline_reached(request)) {
            response.status = SeriesMateSearchStatus::Deadline;
            response.message = "native series-mate deadline reached";
            return response;
        }
        if (
            request.max_positions.has_value()
            && response.stats.positions_visited >= *request.max_positions
        ) {
            response.status = SeriesMateSearchStatus::WorkLimit;
            response.message = "native series-mate work limit reached";
            return response;
        }
        if (mate_search_work_limit_reached(request, response.stats)) {
            response.status = SeriesMateSearchStatus::WorkLimit;
            response.message = "native series-mate total work limit reached";
            return response;
        }

        MateSearchNode item = open.top();
        open.pop();
        const std::size_t depth = item.moves.size();
        const auto seen = minimum_depth.find(
            mate_search_identity(item.board, item.pending_ep_targets)
        );
        if (seen != minimum_depth.end() && depth > seen->second) {
            continue;
        }
        ++response.stats.positions_visited;
        response.stats.max_depth_reached = std::max(
            response.stats.max_depth_reached,
            static_cast<std::uint64_t>(depth)
        );
        const auto variants = expand_legal_move_variants(
            item.board,
            depth == 0 ? request.ep_targets : std::vector<int>{}
        );
        for (const auto& expanded : variants) {
            if (
                (response.stats.moves_generated & 63U) == 0
                && mate_search_deadline_reached(request)
            ) {
                response.status = SeriesMateSearchStatus::Deadline;
                response.message = "native series-mate deadline reached";
                return response;
            }
            if (mate_search_work_limit_reached(request, response.stats)) {
                response.status = SeriesMateSearchStatus::WorkLimit;
                response.message = "native series-mate total work limit reached";
                return response;
            }
            ++response.stats.moves_generated;
            std::vector<std::string> moves = item.moves;
            moves.push_back(expanded.move.uci);
            Bitboard pending_ep_targets = item.pending_ep_targets;
            update_pending_ep_targets(pending_ep_targets, expanded, mover);

            if (expanded.delivered_check) {
                ++response.stats.checking_series;
                const auto boundary_ep_targets = canonical_boundary_ep_targets(
                    expanded.child,
                    pending_ep_targets
                );
                if (!has_legal_move(expanded.child, boundary_ep_targets)) {
                    response.status = SeriesMateSearchStatus::Found;
                    response.message = "native series mate found";
                    response.stats.checkmates = 1;
                    response.stats.max_depth_reached = std::max(
                        response.stats.max_depth_reached,
                        static_cast<std::uint64_t>(moves.size())
                    );
                    response.moves = std::move(moves);
                    return response;
                }
                continue;
            }
            if (moves.size() >= static_cast<std::uint64_t>(request.series_number)) {
                continue;
            }

            BoardState child = expanded.child;
            child.white_to_move = mover;
            const auto identity = mate_search_identity(child, pending_ep_targets);
            const auto [found, inserted] = minimum_depth.emplace(identity, moves.size());
            if (!inserted && found->second <= moves.size()) {
                ++response.stats.transpositions_merged;
                continue;
            }
            if (!inserted) {
                found->second = moves.size();
            }
            open.push(MateSearchNode{
                child,
                std::move(moves),
                pending_ep_targets,
                mate_covering_priority(child, mover, depth + 1),
            });
            response.stats.peak_frontier = std::max(
                response.stats.peak_frontier,
                static_cast<std::uint64_t>(open.size())
            );
        }
    }

    response.status = SeriesMateSearchStatus::Exhausted;
    response.message = "native series-mate state space exhausted";
    return response;
}

enum class IntegerParseStatus : std::uint8_t {
    Ok,
    OutOfRange,
    Error,
};

[[nodiscard]] IntegerParseStatus parse_u64(
    PyObject* object,
    std::uint64_t& value
) {
    if (!PyLong_Check(object)) {
        PyErr_SetString(PyExc_TypeError, "native integer argument must be an int");
        return IntegerParseStatus::Error;
    }
    const unsigned long long parsed = PyLong_AsUnsignedLongLong(object);
    if (parsed == static_cast<unsigned long long>(-1) && PyErr_Occurred()) {
        if (PyErr_ExceptionMatches(PyExc_OverflowError)) {
            PyErr_Clear();
            return IntegerParseStatus::OutOfRange;
        }
        return IntegerParseStatus::Error;
    }
    value = static_cast<std::uint64_t>(parsed);
    return IntegerParseStatus::Ok;
}

[[nodiscard]] IntegerParseStatus parse_i64(
    PyObject* object,
    std::int64_t& value
) {
    if (!PyLong_Check(object)) {
        PyErr_SetString(PyExc_TypeError, "series_number must be an int");
        return IntegerParseStatus::Error;
    }
    const long long parsed = PyLong_AsLongLong(object);
    if (parsed == -1 && PyErr_Occurred()) {
        if (PyErr_ExceptionMatches(PyExc_OverflowError)) {
            PyErr_Clear();
            return IntegerParseStatus::OutOfRange;
        }
        return IntegerParseStatus::Error;
    }
    value = static_cast<std::int64_t>(parsed);
    return IntegerParseStatus::Ok;
}

IntegerParseStatus parse_square_sequence(
    PyObject* object,
    std::vector<int>& squares
) {
    PyObject* sequence = PySequence_Fast(
        object,
        "ep_targets must be an iterable of squares"
    );
    if (sequence == nullptr) {
        return IntegerParseStatus::Error;
    }
    const Py_ssize_t size = PySequence_Fast_GET_SIZE(sequence);
    try {
        squares.reserve(static_cast<std::size_t>(size));
        for (Py_ssize_t index = 0; index < size; ++index) {
            PyObject* item = PySequence_Fast_GET_ITEM(sequence, index);
            if (!PyLong_Check(item)) {
                Py_DECREF(sequence);
                PyErr_SetString(PyExc_TypeError, "square must be an int");
                return IntegerParseStatus::Error;
            }
            const long square_index = PyLong_AsLong(item);
            if (square_index == -1 && PyErr_Occurred()) {
                Py_DECREF(sequence);
                if (PyErr_ExceptionMatches(PyExc_OverflowError)) {
                    PyErr_Clear();
                    return IntegerParseStatus::OutOfRange;
                }
                return IntegerParseStatus::Error;
            }
            if (square_index < 0 || square_index >= 64) {
                Py_DECREF(sequence);
                PyErr_SetString(PyExc_ValueError, "square must be in [0, 63]");
                return IntegerParseStatus::Error;
            }
            squares.push_back(static_cast<int>(square_index));
        }
    } catch (...) {
        Py_DECREF(sequence);
        throw;
    }
    Py_DECREF(sequence);
    return IntegerParseStatus::Ok;
}

IntegerParseStatus parse_optional_u64(
    PyObject* object,
    std::optional<std::uint64_t>& value,
    bool positive,
    const char* name
) {
    if (object == Py_None) {
        value.reset();
        return IntegerParseStatus::Ok;
    }
    std::uint64_t parsed = 0;
    const auto status = parse_u64(object, parsed);
    if (status != IntegerParseStatus::Ok) {
        return status;
    }
    if (positive && parsed == 0) {
        PyErr_Format(PyExc_ValueError, "%s must be positive", name);
        return IntegerParseStatus::Error;
    }
    value = parsed;
    return IntegerParseStatus::Ok;
}

[[nodiscard]] PyObject* string_tuple(const std::vector<std::string>& values) {
    PyObject* result = PyTuple_New(static_cast<Py_ssize_t>(values.size()));
    if (result == nullptr) {
        return nullptr;
    }
    for (std::size_t index = 0; index < values.size(); ++index) {
        PyObject* value = PyUnicode_FromString(values[index].c_str());
        if (value == nullptr) {
            Py_DECREF(result);
            return nullptr;
        }
        PyTuple_SET_ITEM(result, static_cast<Py_ssize_t>(index), value);
    }
    return result;
}

[[nodiscard]] PyObject* series_mate_stats_tuple(
    const SeriesMateSearchStats& stats
) {
    const std::array<std::uint64_t, 7> values = {
        stats.positions_visited,
        stats.moves_generated,
        stats.transpositions_merged,
        stats.checking_series,
        stats.checkmates,
        stats.peak_frontier,
        stats.max_depth_reached,
    };
    PyObject* result = PyTuple_New(static_cast<Py_ssize_t>(values.size()));
    if (result == nullptr) {
        return nullptr;
    }
    for (std::size_t index = 0; index < values.size(); ++index) {
        PyObject* value = PyLong_FromUnsignedLongLong(values[index]);
        if (value == nullptr) {
            Py_DECREF(result);
            return nullptr;
        }
        PyTuple_SET_ITEM(result, static_cast<Py_ssize_t>(index), value);
    }
    return result;
}

[[nodiscard]] PyObject* series_mate_response_tuple(
    const SeriesMateSearchResponse& response
) {
    PyObject* status = PyLong_FromLong(static_cast<long>(response.status));
    PyObject* message = PyUnicode_FromString(response.message.c_str());
    PyObject* stats = series_mate_stats_tuple(response.stats);
    PyObject* moves = string_tuple(response.moves);
    if (status == nullptr || message == nullptr || stats == nullptr || moves == nullptr) {
        Py_XDECREF(status);
        Py_XDECREF(message);
        Py_XDECREF(stats);
        Py_XDECREF(moves);
        return nullptr;
    }
    PyObject* result = PyTuple_New(4);
    if (result == nullptr) {
        Py_DECREF(status);
        Py_DECREF(message);
        Py_DECREF(stats);
        Py_DECREF(moves);
        return nullptr;
    }
    PyTuple_SET_ITEM(result, 0, status);
    PyTuple_SET_ITEM(result, 1, message);
    PyTuple_SET_ITEM(result, 2, stats);
    PyTuple_SET_ITEM(result, 3, moves);
    return result;
}

[[nodiscard]] PyObject* unsupported_integer_response() {
    SeriesMateSearchResponse response;
    response.status = SeriesMateSearchStatus::Unsupported;
    response.message = "native series-mate integer argument is out of range";
    return series_mate_response_tuple(response);
}

PyObject* py_find_series_mate(PyObject*, PyObject* arguments) {
    PyObject* pawns_object = nullptr;
    PyObject* knights_object = nullptr;
    PyObject* bishops_object = nullptr;
    PyObject* rooks_object = nullptr;
    PyObject* queens_object = nullptr;
    PyObject* kings_object = nullptr;
    PyObject* white_occupied_object = nullptr;
    PyObject* black_occupied_object = nullptr;
    PyObject* promoted_object = nullptr;
    PyObject* castling_rights_object = nullptr;
    int white_to_move = 0;
    PyObject* series_number_object = nullptr;
    PyObject* ep_targets_object = nullptr;
    PyObject* max_positions_object = nullptr;
    PyObject* remaining_nanoseconds_object = nullptr;
    PyObject* max_work_object = Py_None;
    if (!PyArg_ParseTuple(
            arguments,
            "OOOOOOOOOOpOOOO|O:find_series_mate",
            &pawns_object,
            &knights_object,
            &bishops_object,
            &rooks_object,
            &queens_object,
            &kings_object,
            &white_occupied_object,
            &black_occupied_object,
            &promoted_object,
            &castling_rights_object,
            &white_to_move,
            &series_number_object,
            &ep_targets_object,
            &max_positions_object,
            &remaining_nanoseconds_object,
            &max_work_object
        )) {
        return nullptr;
    }

    std::array<std::uint64_t, 10> words{};
    const std::array<PyObject*, 10> word_objects = {
        pawns_object,
        knights_object,
        bishops_object,
        rooks_object,
        queens_object,
        kings_object,
        white_occupied_object,
        black_occupied_object,
        promoted_object,
        castling_rights_object,
    };
    for (std::size_t index = 0; index < word_objects.size(); ++index) {
        const auto status = parse_u64(word_objects[index], words[index]);
        if (status == IntegerParseStatus::OutOfRange) {
            return unsupported_integer_response();
        }
        if (status == IntegerParseStatus::Error) {
            return nullptr;
        }
    }
    std::int64_t series_number = 0;
    const auto series_status = parse_i64(series_number_object, series_number);
    if (series_status == IntegerParseStatus::OutOfRange) {
        return unsupported_integer_response();
    }
    if (series_status == IntegerParseStatus::Error) {
        return nullptr;
    }

    std::vector<int> ep_targets;
    std::optional<std::uint64_t> max_positions;
    std::optional<std::uint64_t> remaining_nanoseconds;
    std::optional<std::uint64_t> max_work;
    try {
        const auto squares_status = parse_square_sequence(
            ep_targets_object,
            ep_targets
        );
        if (squares_status == IntegerParseStatus::OutOfRange) {
            return unsupported_integer_response();
        }
        if (squares_status == IntegerParseStatus::Error) {
            return nullptr;
        }
        auto status = parse_optional_u64(
            max_positions_object,
            max_positions,
            true,
            "max_positions"
        );
        if (status == IntegerParseStatus::OutOfRange) {
            return unsupported_integer_response();
        }
        if (status == IntegerParseStatus::Error) {
            return nullptr;
        }
        status = parse_optional_u64(
            remaining_nanoseconds_object,
            remaining_nanoseconds,
            false,
            "remaining_nanoseconds"
        );
        if (status == IntegerParseStatus::OutOfRange) {
            return unsupported_integer_response();
        }
        if (status == IntegerParseStatus::Error) {
            return nullptr;
        }
        status = parse_optional_u64(
            max_work_object,
            max_work,
            true,
            "max_work"
        );
        if (status == IntegerParseStatus::OutOfRange) {
            return unsupported_integer_response();
        }
        if (status == IntegerParseStatus::Error) {
            return nullptr;
        }
    } catch (const std::bad_alloc&) {
        return PyErr_NoMemory();
    } catch (const std::length_error&) {
        return PyErr_NoMemory();
    } catch (...) {
        PyErr_SetString(
            PyExc_RuntimeError,
            "native series-mate argument parsing failed"
        );
        return nullptr;
    }

    SeriesMateSearchRequest request{
        {
            words[0],
            words[1],
            words[2],
            words[3],
            words[4],
            words[5],
            {words[7], words[6]},
            words[8],
            words[9],
            white_to_move != 0,
        },
        series_number,
        std::move(ep_targets),
        max_positions,
        std::nullopt,
        max_work,
    };
    if (remaining_nanoseconds.has_value()) {
        const auto now = std::chrono::steady_clock::now();
        const auto bounded = std::min<std::uint64_t>(
            *remaining_nanoseconds,
            static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max())
        );
        const auto requested = std::chrono::duration_cast<
            std::chrono::steady_clock::duration
        >(std::chrono::nanoseconds(static_cast<std::int64_t>(bounded)));
        const auto maximum = std::chrono::steady_clock::time_point::max() - now;
        request.deadline = now + std::min(requested, maximum);
    }

    SeriesMateSearchResponse response;
    std::exception_ptr failure;
    PyThreadState* saved_thread = PyEval_SaveThread();
    try {
        response = find_series_mate(request);
    } catch (...) {
        failure = std::current_exception();
    }
    PyEval_RestoreThread(saved_thread);
    try {
        if (failure != nullptr) {
            std::rethrow_exception(failure);
        }
    } catch (const std::bad_alloc&) {
        return PyErr_NoMemory();
    } catch (const std::length_error&) {
        return PyErr_NoMemory();
    } catch (...) {
        PyErr_SetString(PyExc_RuntimeError, "native series-mate search failed");
        return nullptr;
    }
    return series_mate_response_tuple(response);
}

PyMethodDef METHODS[] = {
    {
        "find_series_mate",
        py_find_series_mate,
        METH_VARARGS,
        PyDoc_STR(
            "Find one replayable progressive series mate with uncapped native best-first search."
        )
    },
    {nullptr, nullptr, 0, nullptr},
};

PyModuleDef MODULE = {
    PyModuleDef_HEAD_INIT,
    "_native_mate",
    "Isolated C++20 one-series mate search for Scottish Progressive Chess.",
    -1,
    METHODS,
    nullptr,
    nullptr,
    nullptr,
    nullptr,
};

}  // namespace
}  // namespace spc::native_mate

PyMODINIT_FUNC PyInit__native_mate() {
    PyObject* module = PyModule_Create(&spc::native_mate::MODULE);
    if (module == nullptr) {
        return nullptr;
    }
    if (
        PyModule_AddStringConstant(
            module,
            "SOURCE_IDENTITY",
            SPC_NATIVE_MATE_SOURCE_IDENTITY
        ) < 0
    ) {
        Py_DECREF(module);
        return nullptr;
    }
    return module;
}
