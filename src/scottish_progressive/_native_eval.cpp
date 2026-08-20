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
#include <string>
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

}  // namespace spc::native

namespace {

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
