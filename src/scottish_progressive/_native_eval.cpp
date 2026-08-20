#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include "native_eval.hpp"

#ifndef SPC_NATIVE_SOURCE_IDENTITY
#define SPC_NATIVE_SOURCE_IDENTITY "unconfigured"
#endif

#include <algorithm>
#include <array>
#include <bit>
#include <cstdint>
#include <cstdlib>
#include <limits>
#include <new>
#include <set>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

namespace spc::native {
namespace {

constexpr bool BLACK = false;
constexpr bool WHITE = true;

constexpr int PAWN = 1;
constexpr int KNIGHT = 2;
constexpr int BISHOP = 3;
constexpr int ROOK = 4;
constexpr int QUEEN = 5;
constexpr int KING = 6;

struct Move {
    int from;
    int to;
    int promotion;
    int required_ep_square;
    bool castling;
};

constexpr std::array<int, 7> PIECE_VALUES = {0, 100, 325, 340, 525, 975, 0};
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

[[nodiscard]] constexpr bool inside(int file, int rank) noexcept {
    return file >= 0 && file < 8 && rank >= 0 && rank < 8;
}

[[nodiscard]] constexpr int square(int file, int rank) noexcept {
    return rank * 8 + file;
}

[[nodiscard]] constexpr Bitboard bit(int square_index) noexcept {
    return Bitboard{1} << square_index;
}

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

[[nodiscard]] int king_square(
    const Position& position,
    bool color
) noexcept {
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
    const int target_file = target & 7;
    const int target_rank = target >> 3;
    const Bitboard pawns = position.pawns & attacker_occupancy;
    const int pawn_source_rank = target_rank + (attacker == WHITE ? -1 : 1);
    if (inside(target_file - 1, pawn_source_rank)
        && (pawns & bit(square(target_file - 1, pawn_source_rank))) != 0) {
        return true;
    }
    if (inside(target_file + 1, pawn_source_rank)
        && (pawns & bit(square(target_file + 1, pawn_source_rank))) != 0) {
        return true;
    }

    const Bitboard knights = position.knights & attacker_occupancy;
    for (const auto& delta : KNIGHT_DELTAS) {
        const int file = target_file + delta[0];
        const int rank = target_rank + delta[1];
        if (inside(file, rank) && (knights & bit(square(file, rank))) != 0) {
            return true;
        }
    }

    const Bitboard kings = position.kings & attacker_occupancy;
    for (const auto& delta : KING_DELTAS) {
        const int file = target_file + delta[0];
        const int rank = target_rank + delta[1];
        if (inside(file, rank) && (kings & bit(square(file, rank))) != 0) {
            return true;
        }
    }

    for (const auto& delta : ORTHOGONAL) {
        int file = target_file + delta[0];
        int rank = target_rank + delta[1];
        while (inside(file, rank)) {
            const int source = square(file, rank);
            const Bitboard source_mask = bit(source);
            if ((occupancy & source_mask) != 0) {
                if ((attacker_occupancy & source_mask) != 0) {
                    const int type = piece_type_at(position, source);
                    if (type == ROOK || type == QUEEN) {
                        return true;
                    }
                }
                break;
            }
            file += delta[0];
            rank += delta[1];
        }
    }

    for (const auto& delta : DIAGONAL) {
        int file = target_file + delta[0];
        int rank = target_rank + delta[1];
        while (inside(file, rank)) {
            const int source = square(file, rank);
            const Bitboard source_mask = bit(source);
            if ((occupancy & source_mask) != 0) {
                if ((attacker_occupancy & source_mask) != 0) {
                    const int type = piece_type_at(position, source);
                    if (type == BISHOP || type == QUEEN) {
                        return true;
                    }
                }
                break;
            }
            file += delta[0];
            rank += delta[1];
        }
    }
    return false;
}

[[nodiscard]] bool is_check(const Position& position) noexcept {
    const bool mover = position.white_to_move;
    const int king = king_square(position, mover);
    if (king < 0) {
        return false;
    }
    return attacked_by(
        position,
        king,
        !mover,
        position.occupied[0] | position.occupied[1],
        position.occupied[(!mover) ? 1 : 0]
    );
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
        1,
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
    const int king_index = static_cast<int>(std::countr_zero(king));
    return !board_attacked_by(child, king_index, !mover);
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

void add_standard_castling(
    const BoardState& board,
    std::vector<Move>& moves
) {
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
        Move pass_move{king_from, pass, 0, -1, false};
        if (!legal_after_move(board, pass_move)) {
            return;
        }
        const int destination = square(king_side ? 6 : 2, rank);
        Move castle{king_from, destination, 0, -1, true};
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
    const Position position = evaluation_position(board);

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
        result.push_back(SYMBOLS[move.promotion]);
    }
    return result;
}

[[nodiscard]] int king_flight_squares(
    const Position& position,
    bool color
) noexcept {
    const int king = king_square(position, color);
    if (king < 0) {
        return 0;
    }
    const int king_file = king & 7;
    const int king_rank = king >> 3;
    const Bitboard friendly = position.occupied[color ? 1 : 0];
    const Bitboard enemy = position.occupied[(!color) ? 1 : 0];
    int count = 0;
    for (const auto& delta : KING_DELTAS) {
        const int file = king_file + delta[0];
        const int rank = king_rank + delta[1];
        if (!inside(file, rank)) {
            continue;
        }
        const int target = square(file, rank);
        const Bitboard target_mask = bit(target);
        if ((friendly & target_mask) != 0) {
            continue;
        }
        const Bitboard after_friendly = (friendly & ~bit(king)) | target_mask;
        const Bitboard after_enemy = enemy & ~target_mask;
        const Bitboard occupancy = after_friendly | after_enemy;
        if (!attacked_by(
                position,
                target,
                !color,
                occupancy,
                after_enemy
            )) {
            ++count;
        }
    }
    return count;
}

[[nodiscard]] int material(const Position& position) noexcept {
    int score = 0;
    const auto count_for = [&](Bitboard pieces, bool color) noexcept {
        return static_cast<int>(std::popcount(
            pieces & position.occupied[color ? 1 : 0]
        ));
    };
    score += (count_for(position.pawns, WHITE) - count_for(position.pawns, BLACK))
        * PIECE_VALUES[PAWN];
    score += (count_for(position.knights, WHITE) - count_for(position.knights, BLACK))
        * PIECE_VALUES[KNIGHT];
    score += (count_for(position.bishops, WHITE) - count_for(position.bishops, BLACK))
        * PIECE_VALUES[BISHOP];
    score += (count_for(position.rooks, WHITE) - count_for(position.rooks, BLACK))
        * PIECE_VALUES[ROOK];
    score += (count_for(position.queens, WHITE) - count_for(position.queens, BLACK))
        * PIECE_VALUES[QUEEN];
    return score;
}

[[nodiscard]] int promotion_distance(
    const Position& position,
    int pawn_square,
    bool color
) noexcept {
    const int rank = pawn_square >> 3;
    const int file = pawn_square & 7;
    const int direction = color == WHITE ? 1 : -1;
    const int target_rank = color == WHITE ? 7 : 0;
    int distance = std::abs(target_rank - rank);
    if (distance == 0) {
        return 0;
    }
    const Bitboard occupancy = position.occupied[0] | position.occupied[1];
    for (int next_rank = rank + direction;
         next_rank != target_rank + direction;
         next_rank += direction) {
        if ((occupancy & bit(square(file, next_rank))) != 0) {
            return -1;
        }
    }
    const int start_rank = color == WHITE ? 1 : 6;
    if (rank == start_rank && distance >= 2) {
        --distance;
    }
    return distance;
}

[[nodiscard]] bool checked_add(
    std::int64_t left,
    std::int64_t right,
    std::int64_t& result
) noexcept {
    if (
        (right > 0 && left > std::numeric_limits<std::int64_t>::max() - right)
        || (right < 0 && left < std::numeric_limits<std::int64_t>::min() - right)
    ) {
        return false;
    }
    result = left + right;
    return true;
}

[[nodiscard]] bool checked_subtract(
    std::int64_t left,
    std::int64_t right,
    std::int64_t& result
) noexcept {
    if (
        (right > 0 && left < std::numeric_limits<std::int64_t>::min() + right)
        || (right < 0 && left > std::numeric_limits<std::int64_t>::max() + right)
    ) {
        return false;
    }
    result = left - right;
    return true;
}

[[nodiscard]] bool checked_multiply(
    std::int64_t left,
    std::int64_t right,
    std::int64_t& result
) noexcept {
    constexpr auto MINIMUM = std::numeric_limits<std::int64_t>::min();
    constexpr auto MAXIMUM = std::numeric_limits<std::int64_t>::max();
    if (left > 0) {
        if (
            (right > 0 && left > MAXIMUM / right)
            || (right < 0 && right < MINIMUM / left)
        ) {
            return false;
        }
    } else if (left < 0) {
        if (
            (right > 0 && left < MINIMUM / right)
            || (right < 0 && left < MAXIMUM / right)
        ) {
            return false;
        }
    }
    result = left * right;
    return true;
}

[[nodiscard]] std::optional<std::int64_t> promotion_score(
    const Position& position,
    bool color
) noexcept {
    Bitboard pawns = position.pawns & position.occupied[color ? 1 : 0];
    int best = std::numeric_limits<int>::max();
    while (pawns != 0) {
        const int pawn = static_cast<int>(std::countr_zero(pawns));
        pawns &= pawns - 1;
        const int distance = promotion_distance(position, pawn, color);
        if (distance >= 0) {
            best = std::min(best, distance);
        }
    }
    if (best == std::numeric_limits<int>::max()) {
        return 0;
    }
    std::int64_t budget = 0;
    if (!checked_add(
            position.series_number,
            (position.white_to_move == color) ? 0 : 1,
            budget
        )) {
        return std::nullopt;
    }
    if (best <= budget) {
        std::int64_t distance_bonus = 0;
        std::int64_t scaled_bonus = 0;
        std::int64_t result = 0;
        if (
            !checked_subtract(budget, best, distance_bonus)
            || !checked_multiply(distance_bonus, 55, scaled_bonus)
            || !checked_add(650, scaled_bonus, result)
        ) {
            return std::nullopt;
        }
        return result;
    }
    const std::int64_t deficit = best - budget;
    return std::max<std::int64_t>(0, 180 - deficit * 45);
}

[[nodiscard]] int attacked_material(
    const Position& position,
    bool victim
) noexcept {
    const bool attacker = !victim;
    const Bitboard victim_occupancy = position.occupied[victim ? 1 : 0];
    const Bitboard attacker_occupancy = position.occupied[attacker ? 1 : 0];
    const Bitboard occupancy = victim_occupancy | attacker_occupancy;
    Bitboard pieces = victim_occupancy;
    int value = 0;
    while (pieces != 0) {
        const int target = static_cast<int>(std::countr_zero(pieces));
        pieces &= pieces - 1;
        if (attacked_by(
                position,
                target,
                attacker,
                occupancy,
                attacker_occupancy
            )) {
            value += PIECE_VALUES[piece_type_at(position, target)];
        }
    }
    return value;
}

[[nodiscard]] int floor_div(int numerator, int denominator) noexcept {
    int quotient = numerator / denominator;
    const int remainder = numerator % denominator;
    if (remainder != 0 && numerator < 0) {
        --quotient;
    }
    return quotient;
}

[[nodiscard]] std::optional<std::int64_t> bankers_scale(
    std::int64_t value,
    std::int64_t percentage
) noexcept {
    std::int64_t product = 0;
    if (!checked_multiply(value, percentage, product)) {
        return std::nullopt;
    }
    const bool negative = product < 0;
    const std::uint64_t magnitude = negative
        ? static_cast<std::uint64_t>(-(product + 1)) + 1
        : static_cast<std::uint64_t>(product);
    std::uint64_t quotient = magnitude / 100;
    const std::uint64_t remainder = magnitude % 100;
    if (remainder > 50 || (remainder == 50 && (quotient & 1U) != 0)) {
        ++quotient;
    }
    const auto signed_result = static_cast<std::int64_t>(quotient);
    return negative ? -signed_result : signed_result;
}

}  // namespace

std::optional<std::int64_t> fast_evaluate(
    const Position& position,
    const FastWeights& weights
) noexcept {
    std::int64_t score = 0;
    const auto add_scaled = [&score](
        std::int64_t raw,
        std::int64_t weight
    ) noexcept {
        const auto scaled = bankers_scale(raw, weight);
        return scaled.has_value() && checked_add(score, *scaled, score);
    };
    if (!add_scaled(material(position), weights.material)) {
        return std::nullopt;
    }
    if (!add_scaled(
        (king_flight_squares(position, WHITE)
         - king_flight_squares(position, BLACK)) * 20,
        weights.king_space
    )) {
        return std::nullopt;
    }
    const auto white_promotion = promotion_score(position, WHITE);
    const auto black_promotion = promotion_score(position, BLACK);
    std::int64_t promotion_difference = 0;
    if (
        !white_promotion.has_value()
        || !black_promotion.has_value()
        || !checked_subtract(
            *white_promotion,
            *black_promotion,
            promotion_difference
        )
        || !add_scaled(
        promotion_difference,
        weights.promotion_corridors
    )) {
        return std::nullopt;
    }
    if (!add_scaled(
        floor_div(
            attacked_material(position, BLACK)
            - attacked_material(position, WHITE),
            6
        ),
        weights.immediate_vulnerability
    )) {
        return std::nullopt;
    }
    if (
        is_check(position)
        && !add_scaled(
            position.white_to_move ? -140 : 140,
            weights.boundary_check
        )
    ) {
        return std::nullopt;
    }
    return score;
}

std::vector<ExpandedMove> expand_legal_move_variants(
    const BoardState& position,
    const std::vector<int>& ep_targets
) {
    std::vector<ExpandedMove> legal;
    const bool mover = position.white_to_move;
    const Bitboard enemy = position.occupied[(!mover) ? 1 : 0];
    const Position evaluation = evaluation_position(position);
    for (const Move& move : pseudo_moves(position, ep_targets)) {
        if (legal_after_move(position, move)) {
            const int moving_piece = piece_type_at(evaluation, move.from);
            const bool en_passant = move.required_ep_square >= 0
                && moving_piece == PAWN
                && move.to == move.required_ep_square
                && (position.occupied[0] & bit(move.to)) == 0
                && (position.occupied[1] & bit(move.to)) == 0;
            const bool is_capture = en_passant || (enemy & bit(move.to)) != 0;
            BoardState child = apply_move(position, move);
            const Bitboard opponent_king = child.kings
                & child.occupied[(!mover) ? 1 : 0];
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

std::vector<LegalMove> legal_move_variants(
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

bool has_legal_move(
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

namespace {

enum class NativeSeriesOutcome : std::uint8_t {
    None = 0,
    Checkmate = 1,
    Stalemate = 2,
    TenSeriesDraw = 3,
};

struct BoardIdentity {
    std::array<Bitboard, 9> words;
    bool white_to_move;

    bool operator==(const BoardIdentity&) const = default;
};

struct PartialIdentity {
    BoardIdentity board;
    Bitboard pending_ep_targets;
    bool made_progress;

    bool operator==(const PartialIdentity&) const = default;
};

struct CompleteIdentity {
    BoardIdentity board;
    Bitboard boundary_ep_targets;
    std::int64_t series_number;
    std::int64_t quiet_series;
    NativeSeriesOutcome outcome;
    bool ended_by_check;

    bool operator==(const CompleteIdentity&) const = default;
};

struct FrontierScoreIdentity {
    BoardIdentity board;
    std::int64_t halfmove_clock;
    std::int64_t fullmove_number;

    bool operator==(const FrontierScoreIdentity&) const = default;
};

void hash_word(std::size_t& seed, std::uint64_t value) noexcept {
    seed ^= std::hash<std::uint64_t>{}(value)
        + static_cast<std::size_t>(0x9e3779b97f4a7c15ULL)
        + (seed << 6)
        + (seed >> 2);
}

struct PartialIdentityHash {
    std::size_t operator()(const PartialIdentity& key) const noexcept {
        std::size_t seed = 0;
        for (const Bitboard word : key.board.words) {
            hash_word(seed, word);
        }
        hash_word(seed, key.board.white_to_move ? 1 : 0);
        hash_word(seed, key.pending_ep_targets);
        hash_word(seed, key.made_progress ? 1 : 0);
        return seed;
    }
};

struct CompleteIdentityHash {
    std::size_t operator()(const CompleteIdentity& key) const noexcept {
        std::size_t seed = 0;
        for (const Bitboard word : key.board.words) {
            hash_word(seed, word);
        }
        hash_word(seed, key.board.white_to_move ? 1 : 0);
        hash_word(seed, key.boundary_ep_targets);
        hash_word(seed, static_cast<std::uint64_t>(key.series_number));
        hash_word(seed, static_cast<std::uint64_t>(key.quiet_series));
        hash_word(seed, static_cast<std::uint64_t>(key.outcome));
        hash_word(seed, key.ended_by_check ? 1 : 0);
        return seed;
    }
};

struct FrontierScoreIdentityHash {
    std::size_t operator()(const FrontierScoreIdentity& key) const noexcept {
        std::size_t seed = 0;
        for (const Bitboard word : key.board.words) {
            hash_word(seed, word);
        }
        hash_word(seed, key.board.white_to_move ? 1 : 0);
        hash_word(seed, static_cast<std::uint64_t>(key.halfmove_clock));
        hash_word(seed, static_cast<std::uint64_t>(key.fullmove_number));
        return seed;
    }
};

struct NativeFrontierState {
    BoardState board;
    std::vector<std::string> moves;
    Bitboard pending_ep_targets = 0;
    bool made_progress = false;
    std::uint64_t path_count = 1;
    std::int64_t halfmove_clock = 0;
    std::int64_t fullmove_number = 1;
};

struct NativeCompletedSeries {
    BoardState board;
    std::vector<std::string> moves;
    std::vector<int> boundary_ep_targets;
    std::int64_t series_number;
    std::int64_t quiet_series;
    NativeSeriesOutcome outcome = NativeSeriesOutcome::None;
    bool ended_by_check = false;
    std::uint64_t path_count = 1;
};

struct NativeMergedSeries {
    NativeCompletedSeries representative;
    std::uint64_t path_count;
};

struct NativeGenerationContext {
    const CompleteSeriesRequest& request;
    CompleteSeriesResponse response;
    std::vector<NativeCompletedSeries> completed;
    std::unordered_map<
        FrontierScoreIdentity,
        std::int64_t,
        FrontierScoreIdentityHash
    > frontier_score_cache;

    bool unsupported(const char* message) {
        response.status = SeriesGenerationStatus::Unsupported;
        response.message = message;
        return false;
    }

    bool add(std::uint64_t& target, std::uint64_t amount) {
        if (target > std::numeric_limits<std::uint64_t>::max() - amount) {
            return unsupported("native series path counter overflow");
        }
        target += amount;
        return true;
    }

    bool charge_position() {
        auto& stats = response.stats;
        if (
            request.max_positions.has_value()
            && (
                stats.positions_visited >= *request.max_positions
                || stats.frontier_score_positions
                    >= *request.max_positions - stats.positions_visited
            )
        ) {
            stats.work_limit_reached = true;
            response.status = SeriesGenerationStatus::WorkLimit;
            return false;
        }
        return add(stats.positions_visited, 1);
    }

    bool charge_frontier_score() {
        auto& stats = response.stats;
        if (
            request.max_positions.has_value()
            && (
                stats.positions_visited >= *request.max_positions
                || stats.frontier_score_positions
                    >= *request.max_positions - stats.positions_visited
            )
        ) {
            stats.work_limit_reached = true;
            response.status = SeriesGenerationStatus::WorkLimit;
            return false;
        }
        return add(stats.frontier_score_positions, 1);
    }
};

[[nodiscard]] BoardIdentity board_identity(const BoardState& board) noexcept {
    return BoardIdentity{
        {
            board.pawns,
            board.knights,
            board.bishops,
            board.rooks,
            board.queens,
            board.kings,
            board.occupied[0],
            board.occupied[1],
            board.castling_rights,
        },
        board.white_to_move,
    };
}

[[nodiscard]] Bitboard target_bits(const std::vector<int>& targets) noexcept {
    Bitboard result = 0;
    for (const int target : targets) {
        if (target >= 0 && target < 64) {
            result |= bit(target);
        }
    }
    return result;
}

[[nodiscard]] bool board_in_check(const BoardState& board) noexcept {
    return is_check(evaluation_position(board));
}

bool update_frontier_clocks(
    NativeGenerationContext& context,
    NativeFrontierState& state,
    const ExpandedMove& expanded,
    bool mover
) {
    if (expanded.is_pawn_move || expanded.is_capture) {
        state.halfmove_clock = 0;
    } else if (
        state.halfmove_clock == std::numeric_limits<std::int64_t>::max()
    ) {
        return context.unsupported("native halfmove clock overflow");
    } else {
        ++state.halfmove_clock;
    }
    if (mover == BLACK) {
        if (state.fullmove_number == std::numeric_limits<std::int64_t>::max()) {
            return context.unsupported("native fullmove clock overflow");
        }
        ++state.fullmove_number;
    }
    return true;
}

[[nodiscard]] std::optional<std::int64_t> calculate_frontier_score(
    const CompleteSeriesRequest& request,
    const NativeFrontierState& state
) {
    if (!request.frontier_weights.has_value()) {
        return 0;
    }
    const BoardState& board = state.board;
    const Position position{
        board.pawns,
        board.knights,
        board.bishops,
        board.rooks,
        board.queens,
        board.kings,
        board.occupied,
        board.white_to_move,
        request.series_number,
    };
    auto score = fast_evaluate(position, *request.frontier_weights);
    if (!score.has_value()) {
        return std::nullopt;
    }

    std::int64_t checks = 0;
    std::int64_t immediate_mates = 0;
    std::int64_t captures = 0;
    std::int64_t promotions = 0;
    for (const auto& expanded : expand_legal_move_variants(board, {})) {
        checks += expanded.delivered_check ? 1 : 0;
        captures += expanded.is_capture ? 1 : 0;
        promotions += expanded.move.promotion != 0 ? 1 : 0;
        if (expanded.delivered_check) {
            std::vector<int> child_ep_targets;
            if (
                expanded.is_pawn_move
                && std::abs(
                    expanded.move.to_square - expanded.move.from_square
                ) == 16
            ) {
                child_ep_targets.push_back(
                    (expanded.move.from_square + expanded.move.to_square) / 2
                );
            }
            immediate_mates += !has_legal_move(
                expanded.child,
                child_ep_targets
            ) ? 1 : 0;
        }
    }

    std::int64_t tactical = 0;
    std::int64_t term = 0;
    if (
        !checked_multiply(immediate_mates, 5'000'000, term)
        || !checked_add(tactical, term, tactical)
        || !checked_multiply(checks, 50'000, term)
        || !checked_add(tactical, term, tactical)
        || !checked_multiply(promotions, 2'000, term)
        || !checked_add(tactical, term, tactical)
        || !checked_multiply(captures, 100, term)
        || !checked_add(tactical, term, tactical)
    ) {
        return std::nullopt;
    }
    std::int64_t combined = 0;
    if (
        board.white_to_move
            ? !checked_add(*score, tactical, combined)
            : !checked_subtract(*score, tactical, combined)
    ) {
        return std::nullopt;
    }
    return combined;
}

bool frontier_score(
    NativeGenerationContext& context,
    const NativeFrontierState& state,
    std::int64_t& score
) {
    if (!context.request.frontier_weights.has_value()) {
        score = 0;
        return true;
    }
    const FrontierScoreIdentity key{
        board_identity(state.board),
        state.halfmove_clock,
        state.fullmove_number,
    };
    const auto cached = context.frontier_score_cache.find(key);
    if (cached != context.frontier_score_cache.end()) {
        score = cached->second;
        return true;
    }
    if (!context.charge_frontier_score()) {
        return false;
    }
    const auto calculated = calculate_frontier_score(context.request, state);
    if (!calculated.has_value()) {
        return context.unsupported("native frontier score overflow");
    }
    score = *calculated;
    context.frontier_score_cache.emplace(key, score);
    return true;
}

bool order_frontier(
    NativeGenerationContext& context,
    std::vector<NativeFrontierState>& frontier
) {
    struct RankedState {
        NativeFrontierState state;
        std::int64_t score;
    };
    std::vector<RankedState> ranked;
    ranked.reserve(frontier.size());
    for (auto& item : frontier) {
        std::int64_t score = 0;
        if (!frontier_score(context, item, score)) {
            return false;
        }
        ranked.push_back(RankedState{std::move(item), score});
    }
    const bool mover = context.request.board.white_to_move;
    std::sort(
        ranked.begin(),
        ranked.end(),
        [mover](const RankedState& left, const RankedState& right) {
            if (left.score != right.score) {
                return mover == WHITE
                    ? left.score > right.score
                    : left.score < right.score;
            }
            return left.state.moves < right.state.moves;
        }
    );
    frontier.clear();
    frontier.reserve(ranked.size());
    for (auto& item : ranked) {
        frontier.push_back(std::move(item.state));
    }
    return true;
}

[[nodiscard]] bool side_has_insufficient_material(
    const BoardState& board,
    bool color
) noexcept {
    const Bitboard own = board.occupied[color ? 1 : 0];
    const Bitboard opponent = board.occupied[(!color) ? 1 : 0];
    if ((own & (board.pawns | board.rooks | board.queens)) != 0) {
        return false;
    }
    if ((own & board.knights) != 0) {
        return std::popcount(own) <= 2
            && (opponent & ~board.kings & ~board.queens) == 0;
    }
    if ((own & board.bishops) != 0) {
        bool dark = false;
        bool light = false;
        Bitboard bishops = board.bishops;
        while (bishops != 0) {
            const int bishop = static_cast<int>(std::countr_zero(bishops));
            bishops &= bishops - 1;
            if (((bishop & 7) + (bishop >> 3)) % 2 == 0) {
                dark = true;
            } else {
                light = true;
            }
        }
        return !(dark && light) && board.pawns == 0 && board.knights == 0;
    }
    return true;
}

[[nodiscard]] bool board_has_insufficient_material(
    const BoardState& board
) noexcept {
    return side_has_insufficient_material(board, WHITE)
        && side_has_insufficient_material(board, BLACK);
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

[[nodiscard]] NativeSeriesOutcome boundary_outcome(
    const BoardState& board,
    const std::vector<int>& ep_targets,
    std::int64_t quiet_series,
    bool delivered_check
) {
    const bool legal = has_legal_move(board, ep_targets);
    if (delivered_check && !legal) {
        return NativeSeriesOutcome::Checkmate;
    }
    if (!delivered_check && !legal) {
        return NativeSeriesOutcome::Stalemate;
    }
    if (quiet_series >= 10 && board_has_insufficient_material(board)) {
        return NativeSeriesOutcome::TenSeriesDraw;
    }
    return NativeSeriesOutcome::None;
}

bool record_completed(
    NativeGenerationContext& context,
    NativeCompletedSeries completed
) {
    auto& stats = context.response.stats;
    if (!context.add(stats.raw_series, completed.path_count)) {
        return false;
    }
    if (
        completed.ended_by_check
        && !context.add(stats.checking_series, completed.path_count)
    ) {
        return false;
    }
    if (
        completed.outcome == NativeSeriesOutcome::Checkmate
        && !context.add(stats.checkmates, completed.path_count)
    ) {
        return false;
    }
    if (
        completed.outcome == NativeSeriesOutcome::Stalemate
        && !context.add(stats.stalemates, completed.path_count)
    ) {
        return false;
    }
    context.completed.push_back(std::move(completed));
    return true;
}

[[nodiscard]] NativeCompletedSeries finish_series(
    const CompleteSeriesRequest& request,
    const NativeFrontierState& state,
    BoardState board,
    std::vector<std::string> moves,
    Bitboard pending_ep_targets,
    bool made_progress,
    bool delivered_check
) {
    const auto ep_targets = canonical_boundary_ep_targets(
        board,
        pending_ep_targets
    );
    const std::int64_t quiet_series = made_progress
        ? 0
        : request.quiet_series + 1;
    const auto outcome = boundary_outcome(
        board,
        ep_targets,
        quiet_series,
        delivered_check
    );
    return NativeCompletedSeries{
        board,
        std::move(moves),
        ep_targets,
        request.series_number + 1,
        quiet_series,
        outcome,
        delivered_check,
        state.path_count,
    };
}

[[nodiscard]] NativeCompletedSeries stuck_series(
    const CompleteSeriesRequest& request,
    const NativeFrontierState& state
) {
    return NativeCompletedSeries{
        state.board,
        state.moves,
        {},
        request.series_number,
        request.quiet_series,
        board_in_check(state.board)
            ? NativeSeriesOutcome::Checkmate
            : NativeSeriesOutcome::Stalemate,
        false,
        state.path_count,
    };
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
        if (
            std::abs(expanded.move.to_square - expanded.move.from_square) == 16
        ) {
            pending_ep_targets |= bit(
                (expanded.move.from_square + expanded.move.to_square) / 2
            );
        }
    }
}

[[nodiscard]] PartialIdentity partial_identity(
    const NativeFrontierState& state
) noexcept {
    return PartialIdentity{
        board_identity(state.board),
        state.pending_ep_targets,
        state.made_progress,
    };
}

[[nodiscard]] CompleteIdentity complete_identity(
    const NativeCompletedSeries& series
) noexcept {
    return CompleteIdentity{
        board_identity(series.board),
        target_bits(series.boundary_ep_targets),
        series.series_number,
        series.quiet_series,
        series.outcome,
        series.ended_by_check,
    };
}

bool bound_frontier(
    NativeGenerationContext& context,
    std::vector<NativeFrontierState>& frontier
) {
    auto& stats = context.response.stats;
    stats.peak_frontier_states = std::max(
        stats.peak_frontier_states,
        static_cast<std::uint64_t>(frontier.size())
    );
    if (!context.request.max_frontier_states.has_value()) {
        return true;
    }
    if (!order_frontier(context, frontier)) {
        return false;
    }
    const std::size_t cap = static_cast<std::size_t>(
        *context.request.max_frontier_states
    );
    if (frontier.size() <= cap) {
        return true;
    }

    struct Group {
        std::string move;
        std::vector<NativeFrontierState> states;
    };
    std::vector<Group> groups;
    std::unordered_map<std::string, std::size_t> group_indices;
    const std::size_t prefix_length = context.request.required_prefix.size();
    for (const auto& item : frontier) {
        const std::size_t group_index = std::min(
            prefix_length,
            item.moves.size() - 1
        );
        const std::string& group_move = item.moves[group_index];
        const auto [found, inserted] = group_indices.emplace(
            group_move,
            groups.size()
        );
        if (inserted) {
            groups.push_back(Group{group_move, {}});
        }
        groups[found->second].states.push_back(item);
    }

    const std::size_t quota = std::max<std::size_t>(1, cap / groups.size());
    std::vector<NativeFrontierState> selected;
    for (const auto& group : groups) {
        const std::size_t retained = std::min(quota, group.states.size());
        selected.insert(
            selected.end(),
            group.states.begin(),
            group.states.begin() + static_cast<std::ptrdiff_t>(retained)
        );
    }
    if (!order_frontier(context, selected)) {
        return false;
    }
    if (selected.size() > cap) {
        selected.resize(cap);
    }
    std::set<std::vector<std::string>> selected_moves;
    for (const auto& item : selected) {
        selected_moves.insert(item.moves);
    }
    if (selected.size() < cap) {
        for (const auto& item : frontier) {
            if (selected_moves.contains(item.moves)) {
                continue;
            }
            selected.push_back(item);
            selected_moves.insert(item.moves);
            if (selected.size() == cap) {
                break;
            }
        }
    }
    if (!order_frontier(context, selected)) {
        return false;
    }

    const std::uint64_t discarded_count = static_cast<std::uint64_t>(
        frontier.size() - selected.size()
    );
    std::uint64_t discarded_paths = 0;
    for (const auto& item : frontier) {
        if (
            !selected_moves.contains(item.moves)
            && !context.add(discarded_paths, item.path_count)
        ) {
            return false;
        }
    }
    if (
        !context.add(stats.frontier_prunes, 1)
        || !context.add(stats.frontier_states_pruned, discarded_count)
        || !context.add(stats.frontier_paths_pruned, discarded_paths)
    ) {
        return false;
    }
    frontier = std::move(selected);
    return true;
}

bool calculate_final_series_score(
    NativeGenerationContext& context,
    const NativeCompletedSeries& series,
    std::int64_t& score
) {
    const auto& selection = *context.request.final_series_score;
    if (series.outcome == NativeSeriesOutcome::Checkmate) {
        const bool mover = context.request.board.white_to_move;
        const bool winner = series.ended_by_check ? mover : !mover;
        const bool valid = winner == WHITE
            ? checked_subtract(selection.mate_score, selection.ply_from_root, score)
            : checked_subtract(selection.ply_from_root, selection.mate_score, score);
        return valid
            || context.unsupported("native final mate-distance score overflow");
    }
    if (
        series.outcome == NativeSeriesOutcome::Stalemate
        || series.outcome == NativeSeriesOutcome::TenSeriesDraw
    ) {
        score = 0;
        return true;
    }

    const BoardState& board = series.board;
    const Position position{
        board.pawns,
        board.knights,
        board.bishops,
        board.rooks,
        board.queens,
        board.kings,
        board.occupied,
        board.white_to_move,
        series.series_number,
    };
    const auto evaluated = fast_evaluate(position, selection.weights);
    if (!evaluated.has_value()) {
        return context.unsupported("native final static score overflow");
    }
    score = *evaluated;
    return true;
}

bool merge_complete_series(NativeGenerationContext& context) {
    std::vector<NativeMergedSeries> merged;
    std::unordered_map<
        CompleteIdentity,
        std::size_t,
        CompleteIdentityHash
    > indices;
    for (auto& completed : context.completed) {
        const CompleteIdentity key = complete_identity(completed);
        const auto [found, inserted] = indices.emplace(key, merged.size());
        if (inserted) {
            const std::uint64_t path_count = completed.path_count;
            merged.push_back(NativeMergedSeries{
                std::move(completed),
                path_count,
            });
            continue;
        }
        auto& incumbent = merged[found->second];
        if (!context.add(incumbent.path_count, completed.path_count)) {
            return false;
        }
        const bool prefer_candidate =
            completed.moves.size() > incumbent.representative.moves.size()
            || (
                completed.moves.size() == incumbent.representative.moves.size()
                && completed.moves < incumbent.representative.moves
            );
        if (prefer_candidate) {
            const std::uint64_t total_paths = incumbent.path_count;
            incumbent.representative = std::move(completed);
            incumbent.path_count = total_paths;
        }
    }

    auto& stats = context.response.stats;
    stats.unique_series = static_cast<std::uint64_t>(merged.size());
    stats.transpositions_merged = stats.raw_series - stats.unique_series;

    if (context.request.final_series_score.has_value()) {
        struct RankedSeries {
            NativeMergedSeries series;
            std::int64_t score;
        };
        std::vector<RankedSeries> ranked;
        ranked.reserve(merged.size());
        for (auto& item : merged) {
            std::int64_t score = 0;
            if (!calculate_final_series_score(
                    context,
                    item.representative,
                    score
                )) {
                return false;
            }
            ranked.push_back(RankedSeries{std::move(item), score});
        }
        const bool mover = context.request.board.white_to_move;
        std::sort(
            ranked.begin(),
            ranked.end(),
            [mover](const RankedSeries& left, const RankedSeries& right) {
                if (left.score != right.score) {
                    return mover == WHITE
                        ? left.score > right.score
                        : left.score < right.score;
                }
                return left.series.representative.moves
                    < right.series.representative.moves;
            }
        );
        if (
            ranked.size()
            > context.request.final_series_score->max_returned_series
        ) {
            ranked.resize(static_cast<std::size_t>(
                context.request.final_series_score->max_returned_series
            ));
        }
        context.response.series.reserve(ranked.size());
        for (auto& item : ranked) {
            context.response.series.push_back(CompleteSeriesPath{
                std::move(item.series.representative.moves),
                item.series.path_count,
            });
        }
        return true;
    }

    context.response.series.reserve(merged.size());
    for (auto& item : merged) {
        context.response.series.push_back(CompleteSeriesPath{
            std::move(item.representative.moves),
            item.path_count,
        });
    }
    std::sort(
        context.response.series.begin(),
        context.response.series.end(),
        [](const CompleteSeriesPath& left, const CompleteSeriesPath& right) {
            return left.moves < right.moves;
        }
    );
    return true;
}

bool replay_required_prefix(
    NativeGenerationContext& context,
    NativeFrontierState& root,
    bool& completed
) {
    const auto& request = context.request;
    auto& response = context.response;
    response.stats.required_prefix_moves = static_cast<std::uint64_t>(
        request.required_prefix.size()
    );
    if (
        request.required_prefix.size()
        > static_cast<std::uint64_t>(request.series_number)
    ) {
        response.status = SeriesGenerationStatus::InvalidPrefix;
        response.message = "required prefix exceeds the series budget";
        return false;
    }

    const bool mover = root.board.white_to_move;
    for (std::size_t index = 0; index < request.required_prefix.size(); ++index) {
        if (!context.charge_position()) {
            return false;
        }
        const auto expanded = expand_legal_move_variants(
            root.board,
            index == 0 ? request.ep_targets : std::vector<int>{}
        );
        const auto selected = std::find_if(
            expanded.begin(),
            expanded.end(),
            [&](const ExpandedMove& move) {
                return move.move.uci == request.required_prefix[index];
            }
        );
        if (selected == expanded.end()) {
            response.status = SeriesGenerationStatus::InvalidPrefix;
            response.message = "illegal required-prefix move";
            return false;
        }

        if (!update_frontier_clocks(context, root, *selected, mover)) {
            return false;
        }
        root.board = selected->child;
        update_pending_ep_targets(root.pending_ep_targets, *selected, mover);
        root.made_progress = root.made_progress
            || selected->is_pawn_move
            || selected->is_capture;
        root.moves.push_back(selected->move.uci);
        const bool series_finished = selected->delivered_check
            || root.moves.size() == static_cast<std::uint64_t>(request.series_number);
        if (series_finished) {
            if (index + 1 != request.required_prefix.size()) {
                response.status = SeriesGenerationStatus::InvalidPrefix;
                response.message = selected->delivered_check
                    ? "required prefix continues after check or series-budget completion"
                    : "required prefix continues after check or series-budget completion";
                return false;
            }
            completed = true;
            return record_completed(
                context,
                finish_series(
                    request,
                    root,
                    root.board,
                    root.moves,
                    root.pending_ep_targets,
                    root.made_progress,
                    selected->delivered_check
                )
            );
        }

        root.board.white_to_move = mover;
        if (!has_legal_move(root.board, {})) {
            if (index + 1 != request.required_prefix.size()) {
                response.status = SeriesGenerationStatus::InvalidPrefix;
                response.message = "required prefix continues after progressive stalemate";
                return false;
            }
            completed = true;
            return record_completed(context, stuck_series(request, root));
        }
    }
    return true;
}

}  // namespace

CompleteSeriesResponse generate_complete_series(
    const CompleteSeriesRequest& request
) {
    NativeGenerationContext context{request};
    if (
        request.series_number < 1
        || request.series_number == std::numeric_limits<std::int64_t>::max()
        || request.quiet_series < 0
        || request.quiet_series == std::numeric_limits<std::int64_t>::max()
        || request.halfmove_clock < 0
        || request.fullmove_number < 1
        || (
            request.max_frontier_states.has_value()
            && *request.max_frontier_states == 0
        )
        || (request.max_positions.has_value() && *request.max_positions == 0)
        || (
            request.final_series_score.has_value()
            && (
                request.final_series_score->max_returned_series == 0
                || request.final_series_score->ply_from_root < 0
                || request.final_series_score->mate_score < 1
            )
        )
    ) {
        context.response.status = SeriesGenerationStatus::Unsupported;
        context.response.message = "native complete-series request is out of range";
        return context.response;
    }

    NativeFrontierState root{
        request.board,
        {},
        0,
        false,
        1,
        request.halfmove_clock,
        request.fullmove_number,
    };
    bool prefix_completed = false;
    if (!replay_required_prefix(context, root, prefix_completed)) {
        return std::move(context.response);
    }

    std::vector<NativeFrontierState> frontier;
    if (!prefix_completed) {
        frontier.push_back(std::move(root));
    }
    const bool mover = request.board.white_to_move;
    while (!frontier.empty()) {
        std::vector<NativeFrontierState> following;
        std::unordered_map<
            PartialIdentity,
            std::size_t,
            PartialIdentityHash
        > indices;
        for (const auto& item : frontier) {
            if (!context.charge_position()) {
                return std::move(context.response);
            }
            const auto variants = expand_legal_move_variants(
                item.board,
                item.moves.empty() ? request.ep_targets : std::vector<int>{}
            );
            if (variants.empty()) {
                if (!record_completed(context, stuck_series(request, item))) {
                    return std::move(context.response);
                }
                continue;
            }

            for (const auto& expanded : variants) {
                NativeFrontierState candidate{
                    expanded.child,
                    item.moves,
                    item.pending_ep_targets,
                    item.made_progress || expanded.is_pawn_move || expanded.is_capture,
                    item.path_count,
                    item.halfmove_clock,
                    item.fullmove_number,
                };
                if (!update_frontier_clocks(context, candidate, expanded, mover)) {
                    return std::move(context.response);
                }
                candidate.moves.push_back(expanded.move.uci);
                update_pending_ep_targets(
                    candidate.pending_ep_targets,
                    expanded,
                    mover
                );
                if (
                    expanded.delivered_check
                    || candidate.moves.size()
                        == static_cast<std::uint64_t>(request.series_number)
                ) {
                    if (!record_completed(
                            context,
                            finish_series(
                                request,
                                candidate,
                                candidate.board,
                                candidate.moves,
                                candidate.pending_ep_targets,
                                candidate.made_progress,
                                expanded.delivered_check
                            )
                        )) {
                        return std::move(context.response);
                    }
                    continue;
                }

                candidate.board.white_to_move = mover;
                const PartialIdentity key = partial_identity(candidate);
                const auto [found, inserted] = indices.emplace(
                    key,
                    following.size()
                );
                if (inserted) {
                    following.push_back(std::move(candidate));
                    continue;
                }
                auto& incumbent = following[found->second];
                std::uint64_t total_paths = incumbent.path_count;
                if (!context.add(total_paths, candidate.path_count)) {
                    return std::move(context.response);
                }
                if (candidate.moves < incumbent.moves) {
                    candidate.path_count = total_paths;
                    incumbent = std::move(candidate);
                } else {
                    incumbent.path_count = total_paths;
                }
            }
        }
        if (!bound_frontier(context, following)) {
            return std::move(context.response);
        }
        frontier = std::move(following);
    }

    if (!merge_complete_series(context)) {
        return std::move(context.response);
    }
    return std::move(context.response);
}

}  // namespace spc::native

namespace {

bool parse_square_sequence(
    PyObject* object,
    std::vector<int>& squares,
    const char* label
) {
    PyObject* sequence = PySequence_Fast(object, label);
    if (sequence == nullptr) {
        return false;
    }
    const Py_ssize_t size = PySequence_Fast_GET_SIZE(sequence);
    try {
        squares.reserve(static_cast<std::size_t>(size));
        for (Py_ssize_t index = 0; index < size; ++index) {
            PyObject* item = PySequence_Fast_GET_ITEM(sequence, index);
            const long square_index = PyLong_AsLong(item);
            if (square_index == -1 && PyErr_Occurred()) {
                Py_DECREF(sequence);
                return false;
            }
            if (square_index < 0 || square_index >= 64) {
                Py_DECREF(sequence);
                PyErr_SetString(PyExc_ValueError, "square must be in [0, 63]");
                return false;
            }
            squares.push_back(static_cast<int>(square_index));
        }
    } catch (...) {
        Py_DECREF(sequence);
        throw;
    }
    Py_DECREF(sequence);
    return true;
}

bool parse_string_sequence(
    PyObject* object,
    std::vector<std::string>& strings,
    const char* label
) {
    PyObject* sequence = PySequence_Fast(object, label);
    if (sequence == nullptr) {
        return false;
    }
    const Py_ssize_t size = PySequence_Fast_GET_SIZE(sequence);
    try {
        strings.reserve(static_cast<std::size_t>(size));
        for (Py_ssize_t index = 0; index < size; ++index) {
            PyObject* item = PySequence_Fast_GET_ITEM(sequence, index);
            Py_ssize_t length = 0;
            const char* value = PyUnicode_AsUTF8AndSize(item, &length);
            if (value == nullptr) {
                Py_DECREF(sequence);
                return false;
            }
            strings.emplace_back(value, static_cast<std::size_t>(length));
        }
    } catch (...) {
        Py_DECREF(sequence);
        throw;
    }
    Py_DECREF(sequence);
    return true;
}

bool parse_optional_positive_u64(
    PyObject* object,
    std::optional<std::uint64_t>& value,
    const char* label
) {
    if (object == Py_None) {
        value.reset();
        return true;
    }
    const unsigned long long parsed = PyLong_AsUnsignedLongLong(object);
    if (parsed == static_cast<unsigned long long>(-1) && PyErr_Occurred()) {
        return false;
    }
    if (parsed == 0) {
        PyErr_SetString(PyExc_ValueError, label);
        return false;
    }
    value = static_cast<std::uint64_t>(parsed);
    return true;
}

bool parse_optional_frontier_weights(
    PyObject* object,
    std::optional<spc::native::FastWeights>& weights
) {
    if (object == Py_None) {
        weights.reset();
        return true;
    }
    PyObject* sequence = PySequence_Fast(
        object,
        "frontier_weights must be None or five signed integers"
    );
    if (sequence == nullptr) {
        return false;
    }
    if (PySequence_Fast_GET_SIZE(sequence) != 5) {
        Py_DECREF(sequence);
        PyErr_SetString(
            PyExc_ValueError,
            "frontier_weights must contain exactly five integers"
        );
        return false;
    }
    std::array<long long, 5> parsed{};
    for (Py_ssize_t index = 0; index < 5; ++index) {
        parsed[static_cast<std::size_t>(index)] = PyLong_AsLongLong(
            PySequence_Fast_GET_ITEM(sequence, index)
        );
        if (
            parsed[static_cast<std::size_t>(index)] == -1
            && PyErr_Occurred()
        ) {
            Py_DECREF(sequence);
            return false;
        }
    }
    Py_DECREF(sequence);
    weights = spc::native::FastWeights{
        parsed[0],
        parsed[1],
        parsed[2],
        parsed[3],
        parsed[4],
    };
    return true;
}

bool parse_optional_final_series_score(
    PyObject* object,
    std::optional<spc::native::FinalSeriesScore>& selection
) {
    if (object == Py_None) {
        selection.reset();
        return true;
    }
    PyObject* sequence = PySequence_Fast(
        object,
        "final_series_score must be None or eight integers"
    );
    if (sequence == nullptr) {
        return false;
    }
    if (PySequence_Fast_GET_SIZE(sequence) != 8) {
        Py_DECREF(sequence);
        PyErr_SetString(
            PyExc_ValueError,
            "final_series_score must contain exactly eight integers"
        );
        return false;
    }

    const unsigned long long cap = PyLong_AsUnsignedLongLong(
        PySequence_Fast_GET_ITEM(sequence, 0)
    );
    if (
        (cap == static_cast<unsigned long long>(-1) && PyErr_Occurred())
        || cap == 0
    ) {
        Py_DECREF(sequence);
        if (!PyErr_Occurred()) {
            PyErr_SetString(
                PyExc_ValueError,
                "final_series_score cap must be positive"
            );
        }
        return false;
    }
    std::array<long long, 7> parsed{};
    for (Py_ssize_t index = 0; index < 7; ++index) {
        parsed[static_cast<std::size_t>(index)] = PyLong_AsLongLong(
            PySequence_Fast_GET_ITEM(sequence, index + 1)
        );
        if (
            parsed[static_cast<std::size_t>(index)] == -1
            && PyErr_Occurred()
        ) {
            Py_DECREF(sequence);
            return false;
        }
    }
    Py_DECREF(sequence);
    selection = spc::native::FinalSeriesScore{
        static_cast<std::uint64_t>(cap),
        parsed[0],
        parsed[1],
        {
            parsed[2],
            parsed[3],
            parsed[4],
            parsed[5],
            parsed[6],
        },
    };
    return true;
}

PyObject* generation_stats_tuple(
    const spc::native::SeriesGenerationStats& stats
) {
    PyObject* result = PyTuple_New(14);
    if (result == nullptr) {
        return nullptr;
    }
    const std::array<std::uint64_t, 13> values = {
        stats.positions_visited,
        stats.frontier_score_positions,
        stats.raw_series,
        stats.unique_series,
        stats.transpositions_merged,
        stats.checking_series,
        stats.checkmates,
        stats.stalemates,
        stats.frontier_prunes,
        stats.frontier_states_pruned,
        stats.frontier_paths_pruned,
        stats.peak_frontier_states,
        stats.required_prefix_moves,
    };
    for (std::size_t index = 0; index < values.size(); ++index) {
        PyObject* value = PyLong_FromUnsignedLongLong(values[index]);
        if (value == nullptr) {
            Py_DECREF(result);
            return nullptr;
        }
        PyTuple_SET_ITEM(result, static_cast<Py_ssize_t>(index), value);
    }
    PyObject* work_limit = PyBool_FromLong(stats.work_limit_reached ? 1 : 0);
    if (work_limit == nullptr) {
        Py_DECREF(result);
        return nullptr;
    }
    PyTuple_SET_ITEM(result, 13, work_limit);
    return result;
}

PyObject* complete_series_tuple(
    const std::vector<spc::native::CompleteSeriesPath>& series
) {
    PyObject* result = PyTuple_New(static_cast<Py_ssize_t>(series.size()));
    if (result == nullptr) {
        return nullptr;
    }
    for (std::size_t index = 0; index < series.size(); ++index) {
        const auto& item = series[index];
        PyObject* moves = PyTuple_New(static_cast<Py_ssize_t>(item.moves.size()));
        if (moves == nullptr) {
            Py_DECREF(result);
            return nullptr;
        }
        for (std::size_t move_index = 0; move_index < item.moves.size(); ++move_index) {
            PyObject* move = PyUnicode_FromString(item.moves[move_index].c_str());
            if (move == nullptr) {
                Py_DECREF(moves);
                Py_DECREF(result);
                return nullptr;
            }
            PyTuple_SET_ITEM(moves, static_cast<Py_ssize_t>(move_index), move);
        }
        PyObject* count = PyLong_FromUnsignedLongLong(item.transposition_count);
        if (count == nullptr) {
            Py_DECREF(moves);
            Py_DECREF(result);
            return nullptr;
        }
        PyObject* entry = PyTuple_New(2);
        if (entry == nullptr) {
            Py_DECREF(moves);
            Py_DECREF(count);
            Py_DECREF(result);
            return nullptr;
        }
        PyTuple_SET_ITEM(entry, 0, moves);
        PyTuple_SET_ITEM(entry, 1, count);
        PyTuple_SET_ITEM(result, static_cast<Py_ssize_t>(index), entry);
    }
    return result;
}

PyObject* py_generate_complete_series(PyObject*, PyObject* arguments) {
    unsigned long long pawns = 0;
    unsigned long long knights = 0;
    unsigned long long bishops = 0;
    unsigned long long rooks = 0;
    unsigned long long queens = 0;
    unsigned long long kings = 0;
    unsigned long long white_occupied = 0;
    unsigned long long black_occupied = 0;
    unsigned long long promoted = 0;
    unsigned long long castling_rights = 0;
    int white_to_move = 0;
    long long halfmove_clock = 0;
    long long fullmove_number = 0;
    long long series_number = 0;
    long long quiet_series = 0;
    PyObject* ep_targets_object = nullptr;
    PyObject* required_prefix_object = nullptr;
    PyObject* max_frontier_states_object = nullptr;
    PyObject* max_positions_object = nullptr;
    PyObject* frontier_weights_object = nullptr;
    PyObject* final_series_score_object = nullptr;
    if (!PyArg_ParseTuple(
            arguments,
            "KKKKKKKKKKpLLLLOOOOOO:generate_complete_series",
            &pawns,
            &knights,
            &bishops,
            &rooks,
            &queens,
            &kings,
            &white_occupied,
            &black_occupied,
            &promoted,
            &castling_rights,
            &white_to_move,
            &halfmove_clock,
            &fullmove_number,
            &series_number,
            &quiet_series,
            &ep_targets_object,
            &required_prefix_object,
            &max_frontier_states_object,
            &max_positions_object,
            &frontier_weights_object,
            &final_series_score_object
        )) {
        return nullptr;
    }

    std::vector<int> ep_targets;
    std::vector<std::string> required_prefix;
    std::optional<std::uint64_t> max_frontier_states;
    std::optional<std::uint64_t> max_positions;
    std::optional<spc::native::FastWeights> frontier_weights;
    std::optional<spc::native::FinalSeriesScore> final_series_score;
    try {
        if (
            !parse_square_sequence(
                ep_targets_object,
                ep_targets,
                "ep_targets must be an iterable of squares"
            )
            || !parse_string_sequence(
                required_prefix_object,
                required_prefix,
                "required_prefix must be an iterable of UCI strings"
            )
            || !parse_optional_positive_u64(
                max_frontier_states_object,
                max_frontier_states,
                "max_frontier_states must be positive"
            )
            || !parse_optional_positive_u64(
                max_positions_object,
                max_positions,
                "max_positions must be positive"
            )
            || !parse_optional_frontier_weights(
                frontier_weights_object,
                frontier_weights
            )
            || !parse_optional_final_series_score(
                final_series_score_object,
                final_series_score
            )
        ) {
            return nullptr;
        }
    } catch (const std::bad_alloc&) {
        return PyErr_NoMemory();
    } catch (const std::length_error&) {
        return PyErr_NoMemory();
    } catch (...) {
        PyErr_SetString(
            PyExc_RuntimeError,
            "native complete-series argument parsing failed"
        );
        return nullptr;
    }

    const spc::native::CompleteSeriesRequest request{
        {
            pawns,
            knights,
            bishops,
            rooks,
            queens,
            kings,
            {black_occupied, white_occupied},
            promoted,
            castling_rights,
            white_to_move != 0,
        },
        halfmove_clock,
        fullmove_number,
        series_number,
        quiet_series,
        std::move(ep_targets),
        std::move(required_prefix),
        max_frontier_states,
        max_positions,
        frontier_weights,
        final_series_score,
    };

    spc::native::CompleteSeriesResponse response;
    try {
        response = spc::native::generate_complete_series(request);
    } catch (const std::bad_alloc&) {
        return PyErr_NoMemory();
    } catch (...) {
        PyErr_SetString(PyExc_RuntimeError, "native complete-series generation failed");
        return nullptr;
    }

    PyObject* stats = generation_stats_tuple(response.stats);
    if (stats == nullptr) {
        return nullptr;
    }
    PyObject* series = complete_series_tuple(response.series);
    if (series == nullptr) {
        Py_DECREF(stats);
        return nullptr;
    }
    PyObject* result = PyTuple_New(4);
    if (result == nullptr) {
        Py_DECREF(stats);
        Py_DECREF(series);
        return nullptr;
    }
    PyObject* status = PyLong_FromLong(static_cast<long>(response.status));
    PyObject* message = PyUnicode_FromString(response.message.c_str());
    if (status == nullptr || message == nullptr) {
        Py_XDECREF(status);
        Py_XDECREF(message);
        Py_DECREF(stats);
        Py_DECREF(series);
        Py_DECREF(result);
        return nullptr;
    }
    PyTuple_SET_ITEM(result, 0, status);
    PyTuple_SET_ITEM(result, 1, message);
    PyTuple_SET_ITEM(result, 2, stats);
    PyTuple_SET_ITEM(result, 3, series);
    return result;
}

PyObject* py_fast_evaluate(PyObject*, PyObject* arguments) {
    unsigned long long pawns = 0;
    unsigned long long knights = 0;
    unsigned long long bishops = 0;
    unsigned long long rooks = 0;
    unsigned long long queens = 0;
    unsigned long long kings = 0;
    unsigned long long white_occupied = 0;
    unsigned long long black_occupied = 0;
    int white_to_move = 0;
    long long series_number = 0;
    long long material_weight = 0;
    long long king_space_weight = 0;
    long long promotion_weight = 0;
    long long vulnerability_weight = 0;
    long long boundary_weight = 0;
    if (!PyArg_ParseTuple(
            arguments,
            "KKKKKKKKpLLLLLL:fast_evaluate",
            &pawns,
            &knights,
            &bishops,
            &rooks,
            &queens,
            &kings,
            &white_occupied,
            &black_occupied,
            &white_to_move,
            &series_number,
            &material_weight,
            &king_space_weight,
            &promotion_weight,
            &vulnerability_weight,
            &boundary_weight
        )) {
        return nullptr;
    }
    if (series_number < 1) {
        PyErr_SetString(PyExc_ValueError, "series_number must be at least 1");
        return nullptr;
    }
    const spc::native::Position position{
        pawns,
        knights,
        bishops,
        rooks,
        queens,
        kings,
        {black_occupied, white_occupied},
        white_to_move != 0,
        series_number,
    };
    const spc::native::FastWeights weights{
        material_weight,
        king_space_weight,
        promotion_weight,
        vulnerability_weight,
        boundary_weight,
    };
    const auto score = spc::native::fast_evaluate(position, weights);
    if (!score.has_value()) {
        PyErr_SetString(
            PyExc_OverflowError,
            "native fast evaluation exceeded signed 64-bit arithmetic"
        );
        return nullptr;
    }
    return PyLong_FromLongLong(*score);
}

PyObject* py_legal_move_variants(PyObject*, PyObject* arguments) {
    unsigned long long pawns = 0;
    unsigned long long knights = 0;
    unsigned long long bishops = 0;
    unsigned long long rooks = 0;
    unsigned long long queens = 0;
    unsigned long long kings = 0;
    unsigned long long white_occupied = 0;
    unsigned long long black_occupied = 0;
    unsigned long long promoted = 0;
    unsigned long long castling_rights = 0;
    int white_to_move = 0;
    PyObject* ep_targets_object = nullptr;
    if (!PyArg_ParseTuple(
            arguments,
            "KKKKKKKKKKpO:legal_move_variants",
            &pawns,
            &knights,
            &bishops,
            &rooks,
            &queens,
            &kings,
            &white_occupied,
            &black_occupied,
            &promoted,
            &castling_rights,
            &white_to_move,
            &ep_targets_object
        )) {
        return nullptr;
    }
    PyObject* ep_targets_sequence = PySequence_Fast(
        ep_targets_object,
        "ep_targets must be an iterable of squares"
    );
    if (ep_targets_sequence == nullptr) {
        return nullptr;
    }
    std::vector<int> ep_targets;
    const Py_ssize_t ep_count = PySequence_Fast_GET_SIZE(ep_targets_sequence);
    ep_targets.reserve(static_cast<std::size_t>(ep_count));
    for (Py_ssize_t index = 0; index < ep_count; ++index) {
        PyObject* item = PySequence_Fast_GET_ITEM(ep_targets_sequence, index);
        const long square_index = PyLong_AsLong(item);
        if (square_index == -1 && PyErr_Occurred()) {
            Py_DECREF(ep_targets_sequence);
            return nullptr;
        }
        if (square_index < 0 || square_index >= 64) {
            Py_DECREF(ep_targets_sequence);
            PyErr_SetString(PyExc_ValueError, "e.p. target must be in [0, 63]");
            return nullptr;
        }
        ep_targets.push_back(static_cast<int>(square_index));
    }
    Py_DECREF(ep_targets_sequence);

    const spc::native::BoardState position{
        pawns,
        knights,
        bishops,
        rooks,
        queens,
        kings,
        {black_occupied, white_occupied},
        promoted,
        castling_rights,
        white_to_move != 0,
    };
    const auto legal = spc::native::legal_move_variants(position, ep_targets);
    PyObject* result = PyTuple_New(static_cast<Py_ssize_t>(legal.size()));
    if (result == nullptr) {
        return nullptr;
    }
    for (std::size_t index = 0; index < legal.size(); ++index) {
        const auto& move = legal[index];
        PyObject* entry = Py_BuildValue(
            "(siiii)",
            move.uci.c_str(),
            move.from_square,
            move.to_square,
            move.promotion,
            move.required_ep_square
        );
        if (entry == nullptr) {
            Py_DECREF(result);
            return nullptr;
        }
        PyTuple_SET_ITEM(result, static_cast<Py_ssize_t>(index), entry);
    }
    return result;
}

PyObject* py_expand_legal_move_variants(PyObject*, PyObject* arguments) {
    unsigned long long pawns = 0;
    unsigned long long knights = 0;
    unsigned long long bishops = 0;
    unsigned long long rooks = 0;
    unsigned long long queens = 0;
    unsigned long long kings = 0;
    unsigned long long white_occupied = 0;
    unsigned long long black_occupied = 0;
    unsigned long long promoted = 0;
    unsigned long long castling_rights = 0;
    int white_to_move = 0;
    PyObject* ep_targets_object = nullptr;
    if (!PyArg_ParseTuple(
            arguments,
            "KKKKKKKKKKpO:expand_legal_move_variants",
            &pawns,
            &knights,
            &bishops,
            &rooks,
            &queens,
            &kings,
            &white_occupied,
            &black_occupied,
            &promoted,
            &castling_rights,
            &white_to_move,
            &ep_targets_object
        )) {
        return nullptr;
    }
    PyObject* ep_targets_sequence = PySequence_Fast(
        ep_targets_object,
        "ep_targets must be an iterable of squares"
    );
    if (ep_targets_sequence == nullptr) {
        return nullptr;
    }
    std::vector<int> ep_targets;
    const Py_ssize_t ep_count = PySequence_Fast_GET_SIZE(ep_targets_sequence);
    ep_targets.reserve(static_cast<std::size_t>(ep_count));
    for (Py_ssize_t index = 0; index < ep_count; ++index) {
        PyObject* item = PySequence_Fast_GET_ITEM(ep_targets_sequence, index);
        const long square_index = PyLong_AsLong(item);
        if (square_index == -1 && PyErr_Occurred()) {
            Py_DECREF(ep_targets_sequence);
            return nullptr;
        }
        if (square_index < 0 || square_index >= 64) {
            Py_DECREF(ep_targets_sequence);
            PyErr_SetString(PyExc_ValueError, "e.p. target must be in [0, 63]");
            return nullptr;
        }
        ep_targets.push_back(static_cast<int>(square_index));
    }
    Py_DECREF(ep_targets_sequence);

    const spc::native::BoardState position{
        pawns,
        knights,
        bishops,
        rooks,
        queens,
        kings,
        {black_occupied, white_occupied},
        promoted,
        castling_rights,
        white_to_move != 0,
    };
    const auto expanded = spc::native::expand_legal_move_variants(
        position,
        ep_targets
    );
    PyObject* result = PyTuple_New(static_cast<Py_ssize_t>(expanded.size()));
    if (result == nullptr) {
        return nullptr;
    }
    for (std::size_t index = 0; index < expanded.size(); ++index) {
        const auto& item = expanded[index];
        const auto& child = item.child;
        PyObject* entry = Py_BuildValue(
            "(siiiiKKKKKKKKKKiii)",
            item.move.uci.c_str(),
            item.move.from_square,
            item.move.to_square,
            item.move.promotion,
            item.move.required_ep_square,
            child.pawns,
            child.knights,
            child.bishops,
            child.rooks,
            child.queens,
            child.kings,
            child.occupied[1],
            child.occupied[0],
            child.promoted,
            child.castling_rights,
            item.is_pawn_move ? 1 : 0,
            item.is_capture ? 1 : 0,
            item.delivered_check ? 1 : 0
        );
        if (entry == nullptr) {
            Py_DECREF(result);
            return nullptr;
        }
        PyTuple_SET_ITEM(result, static_cast<Py_ssize_t>(index), entry);
    }
    return result;
}

PyObject* py_has_legal_move(PyObject*, PyObject* arguments) {
    unsigned long long pawns = 0;
    unsigned long long knights = 0;
    unsigned long long bishops = 0;
    unsigned long long rooks = 0;
    unsigned long long queens = 0;
    unsigned long long kings = 0;
    unsigned long long white_occupied = 0;
    unsigned long long black_occupied = 0;
    unsigned long long promoted = 0;
    unsigned long long castling_rights = 0;
    int white_to_move = 0;
    PyObject* ep_targets_object = nullptr;
    if (!PyArg_ParseTuple(
            arguments,
            "KKKKKKKKKKpO:has_legal_move",
            &pawns,
            &knights,
            &bishops,
            &rooks,
            &queens,
            &kings,
            &white_occupied,
            &black_occupied,
            &promoted,
            &castling_rights,
            &white_to_move,
            &ep_targets_object
        )) {
        return nullptr;
    }
    PyObject* ep_targets_sequence = PySequence_Fast(
        ep_targets_object,
        "ep_targets must be an iterable of squares"
    );
    if (ep_targets_sequence == nullptr) {
        return nullptr;
    }
    std::vector<int> ep_targets;
    const Py_ssize_t ep_count = PySequence_Fast_GET_SIZE(ep_targets_sequence);
    ep_targets.reserve(static_cast<std::size_t>(ep_count));
    for (Py_ssize_t index = 0; index < ep_count; ++index) {
        PyObject* item = PySequence_Fast_GET_ITEM(ep_targets_sequence, index);
        const long square_index = PyLong_AsLong(item);
        if (square_index == -1 && PyErr_Occurred()) {
            Py_DECREF(ep_targets_sequence);
            return nullptr;
        }
        if (square_index < 0 || square_index >= 64) {
            Py_DECREF(ep_targets_sequence);
            PyErr_SetString(PyExc_ValueError, "e.p. target must be in [0, 63]");
            return nullptr;
        }
        ep_targets.push_back(static_cast<int>(square_index));
    }
    Py_DECREF(ep_targets_sequence);
    const spc::native::BoardState position{
        pawns,
        knights,
        bishops,
        rooks,
        queens,
        kings,
        {black_occupied, white_occupied},
        promoted,
        castling_rights,
        white_to_move != 0,
    };
    return PyBool_FromLong(spc::native::has_legal_move(position, ep_targets));
}

PyMethodDef METHODS[] = {
    {
        "generate_complete_series",
        py_generate_complete_series,
        METH_VARARGS,
        PyDoc_STR("Bulk exact complete-series generation for supported frontiers.")
    },
    {
        "fast_evaluate",
        py_fast_evaluate,
        METH_VARARGS,
        PyDoc_STR("Exact compiled fast-ordering evaluation for one boundary board.")
    },
    {
        "legal_move_variants",
        py_legal_move_variants,
        METH_VARARGS,
        PyDoc_STR("Exact compiled legal move variants for one orthodox board.")
    },
    {
        "expand_legal_move_variants",
        py_expand_legal_move_variants,
        METH_VARARGS,
        PyDoc_STR("Exact compiled legal moves and post-move board transitions.")
    },
    {
        "has_legal_move",
        py_has_legal_move,
        METH_VARARGS,
        PyDoc_STR("Exact compiled existence test without materializing moves.")
    },
    {nullptr, nullptr, 0, nullptr},
};

PyModuleDef MODULE = {
    PyModuleDef_HEAD_INIT,
    "_native_eval",
    "C++20 acceleration for Scottish Progressive ordering evaluation.",
    -1,
    METHODS,
    nullptr,
    nullptr,
    nullptr,
    nullptr,
};

}  // namespace

PyMODINIT_FUNC PyInit__native_eval() {
    PyObject* module = PyModule_Create(&MODULE);
    if (module == nullptr) {
        return nullptr;
    }
    if (
        PyModule_AddStringConstant(
            module,
            "SOURCE_IDENTITY",
            SPC_NATIVE_SOURCE_IDENTITY
        ) < 0
    ) {
        Py_DECREF(module);
        return nullptr;
    }
    return module;
}
