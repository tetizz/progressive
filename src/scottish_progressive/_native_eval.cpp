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

PyMethodDef METHODS[] = {
    {
        "fast_evaluate",
        py_fast_evaluate,
        METH_VARARGS,
        PyDoc_STR("Exact compiled fast-ordering evaluation for one boundary board.")
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
