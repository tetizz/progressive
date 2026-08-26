#if defined(SPC_WASM_CORE) && !defined(SPC_NATIVE_CORE_ONLY)
#define SPC_NATIVE_CORE_ONLY 1
#endif
#if defined(SPC_NATIVE_CORE_ONLY) && !defined(SPC_NATIVE_CORE_PTHREADS)
#define SPC_NATIVE_SERIAL_POOL 1
#endif

#ifndef SPC_NATIVE_CORE_ONLY
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#endif

#include "native_eval.hpp"
#ifndef SPC_NATIVE_CORE_ONLY
#include "native_selfplay.hpp"
#include "native_subtree.hpp"
#endif

#ifndef SPC_NATIVE_SOURCE_IDENTITY
#define SPC_NATIVE_SOURCE_IDENTITY "unconfigured"
#endif

#include <algorithm>
#include <array>
#include <atomic>
#include <bit>
#include <cstdint>
#include <cstdlib>
#include <exception>
#include <functional>
#include <limits>
#include <map>
#include <memory>
#include <new>
#include <set>
#include <stdexcept>
#include <string>
#ifndef SPC_NATIVE_SERIAL_POOL
#include <condition_variable>
#include <mutex>
#include <thread>
#endif
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
constexpr std::uint64_t CAPTURE_REACH_POSITION_LIMIT = 256;

#ifdef SPC_NATIVE_SERIAL_POOL
class BoundedNativePool {
public:
    static BoundedNativePool& instance() {
        static BoundedNativePool pool;
        return pool;
    }

    void run(
        std::size_t count,
        std::uint32_t,
        const std::function<void(std::size_t)>& task
    ) {
        for (std::size_t index = 0; index < count; ++index) {
            task(index);
        }
    }
};
#else
class BoundedNativePool {
public:
    BoundedNativePool(const BoundedNativePool&) = delete;
    BoundedNativePool& operator=(const BoundedNativePool&) = delete;

    static BoundedNativePool& instance() {
        static BoundedNativePool pool;
        return pool;
    }

    void run(
        std::size_t count,
        std::uint32_t requested_threads,
        const std::function<void(std::size_t)>& task
    ) {
        if (count == 0) {
            return;
        }
        if (requested_threads <= 1 || workers_.empty()) {
            for (std::size_t index = 0; index < count; ++index) {
                task(index);
            }
            return;
        }

        // Only one opt-in parallel native request owns the bounded pool at a
        // time. Default-one tournament/full-game calls bypass it completely,
        // preventing nested worker oversubscription.
        std::unique_lock<std::mutex> execution_lock(execution_mutex_);
        {
            std::lock_guard<std::mutex> state_lock(state_mutex_);
            task_ = task;
            count_ = count;
            next_index_.store(0, std::memory_order_relaxed);
            failed_.store(false, std::memory_order_relaxed);
            failure_ = nullptr;
            active_workers_ = std::min<std::size_t>(
                static_cast<std::size_t>(requested_threads - 1),
                workers_.size()
            );
            remaining_workers_ = active_workers_;
            ++generation_;
        }
        start_cv_.notify_all();

        execute_job();
        {
            std::unique_lock<std::mutex> state_lock(state_mutex_);
            finish_cv_.wait(state_lock, [this]() {
                return remaining_workers_ == 0;
            });
            task_ = {};
        }
        if (failure_ != nullptr) {
            std::rethrow_exception(failure_);
        }
    }

private:
    BoundedNativePool() {
        const auto hardware = std::max(1U, std::thread::hardware_concurrency());
        const auto worker_count = std::min<unsigned int>(hardware - 1, 63U);
        workers_.reserve(worker_count);
        for (unsigned int index = 0; index < worker_count; ++index) {
            workers_.emplace_back([this, index]() { worker_loop(index); });
        }
    }

    ~BoundedNativePool() {
        {
            std::lock_guard<std::mutex> state_lock(state_mutex_);
            stopping_ = true;
            ++generation_;
        }
        start_cv_.notify_all();
        for (auto& worker : workers_) {
            if (worker.joinable()) {
                worker.join();
            }
        }
    }

    void execute_job() noexcept {
        while (!failed_.load(std::memory_order_relaxed)) {
            const std::size_t index = next_index_.fetch_add(
                1,
                std::memory_order_relaxed
            );
            if (index >= count_) {
                return;
            }
            try {
                task_(index);
            } catch (...) {
                bool expected = false;
                if (failed_.compare_exchange_strong(
                        expected,
                        true,
                        std::memory_order_relaxed
                    )) {
                    std::lock_guard<std::mutex> failure_lock(failure_mutex_);
                    failure_ = std::current_exception();
                }
                return;
            }
        }
    }

    void worker_loop(std::size_t worker_index) noexcept {
        std::uint64_t observed_generation = 0;
        while (true) {
            std::unique_lock<std::mutex> state_lock(state_mutex_);
            start_cv_.wait(state_lock, [this, observed_generation]() {
                return stopping_ || generation_ != observed_generation;
            });
            if (stopping_) {
                return;
            }
            observed_generation = generation_;
            const bool active = worker_index < active_workers_;
            state_lock.unlock();
            if (!active) {
                continue;
            }
            execute_job();
            state_lock.lock();
            if (--remaining_workers_ == 0) {
                finish_cv_.notify_one();
            }
        }
    }

    std::mutex execution_mutex_;
    std::mutex state_mutex_;
    std::mutex failure_mutex_;
    std::condition_variable start_cv_;
    std::condition_variable finish_cv_;
    std::vector<std::thread> workers_;
    std::function<void(std::size_t)> task_;
    std::atomic<std::size_t> next_index_{0};
    std::atomic<bool> failed_{false};
    std::exception_ptr failure_;
    std::size_t count_ = 0;
    std::size_t active_workers_ = 0;
    std::size_t remaining_workers_ = 0;
    std::uint64_t generation_ = 0;
    bool stopping_ = false;
};
#endif

struct Move {
    std::int8_t from;
    std::int8_t to;
    std::int8_t promotion;
    std::int8_t required_ep_square;
    bool castling = false;

    Move() = default;

    constexpr Move(
        int from_square,
        int to_square,
        int promotion_piece,
        int ep_square,
        bool is_castling
    ) noexcept
        : from(static_cast<std::int8_t>(from_square)),
          to(static_cast<std::int8_t>(to_square)),
          promotion(static_cast<std::int8_t>(promotion_piece)),
          required_ep_square(static_cast<std::int8_t>(ep_square)),
          castling(is_castling) {}
};

static_assert(sizeof(Move) == 5);

class MoveList {
public:
    static constexpr std::size_t INLINE_CAPACITY = 64;

    MoveList() = default;
    MoveList(const MoveList&) = delete;
    MoveList& operator=(const MoveList&) = delete;

    MoveList(MoveList&& other) noexcept
        : overflow_moves_(std::move(other.overflow_moves_)),
          size_(other.size_) {
        if (size_ <= INLINE_CAPACITY) {
            std::copy_n(
                other.inline_moves_.begin(),
                size_,
                inline_moves_.begin()
            );
        }
        other.size_ = 0;
    }

    MoveList& operator=(MoveList&&) = delete;

    void push_back(const Move& move) {
        if (size_ < INLINE_CAPACITY) {
            inline_moves_[size_] = move;
            ++size_;
            return;
        }
        if (size_ == INLINE_CAPACITY) {
            overflow_moves_.reserve(INLINE_CAPACITY * 2);
            overflow_moves_.insert(
                overflow_moves_.end(),
                inline_moves_.begin(),
                inline_moves_.end()
            );
        }
        overflow_moves_.push_back(move);
        ++size_;
    }

    [[nodiscard]] const Move* begin() const noexcept {
        return data();
    }

    [[nodiscard]] Move* begin() noexcept {
        return data();
    }

    [[nodiscard]] const Move* end() const noexcept {
        return data() + size_;
    }

    [[nodiscard]] Move* end() noexcept {
        return data() + size_;
    }

    [[nodiscard]] std::size_t size() const noexcept {
        return size_;
    }

private:
    [[nodiscard]] Move* data() noexcept {
        return size_ <= INLINE_CAPACITY
            ? inline_moves_.data()
            : overflow_moves_.data();
    }

    [[nodiscard]] const Move* data() const noexcept {
        return size_ <= INLINE_CAPACITY
            ? inline_moves_.data()
            : overflow_moves_.data();
    }

    std::array<Move, INLINE_CAPACITY> inline_moves_;
    std::vector<Move> overflow_moves_;
    std::size_t size_ = 0;
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

[[nodiscard]] constexpr auto knight_attack_masks() noexcept {
    std::array<Bitboard, 64> masks{};
    for (int target = 0; target < 64; ++target) {
        const int target_file = target & 7;
        const int target_rank = target >> 3;
        for (const auto& delta : KNIGHT_DELTAS) {
            const int file = target_file + delta[0];
            const int rank = target_rank + delta[1];
            if (inside(file, rank)) {
                masks[static_cast<std::size_t>(target)]
                    |= bit(square(file, rank));
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
                masks[static_cast<std::size_t>(target)]
                    |= bit(square(file, rank));
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

constexpr std::array<std::array<int, 2>, 8> RAY_DELTAS = {{
    {{-1, 0}}, {{1, 0}}, {{0, -1}}, {{0, 1}},
    {{-1, -1}}, {{-1, 1}}, {{1, -1}}, {{1, 1}},
}};
constexpr std::array<bool, 8> RAY_ASCENDING = {{
    false, true, false, true, false, true, false, true,
}};

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
    const Bitboard pawns = position.pawns & attacker_occupancy;
    const std::size_t target_index = static_cast<std::size_t>(target);
    if (
        (pawns & PAWN_ATTACKER_MASKS[attacker ? 1 : 0][target_index])
        != 0
    ) {
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
    for (
        std::size_t direction = 0;
        direction < RAY_MASKS[target_index].size();
        ++direction
    ) {
        const Bitboard blockers = occupancy
            & RAY_MASKS[target_index][direction];
        if (blockers == 0) {
            continue;
        }
        const int source = RAY_ASCENDING[direction]
            ? static_cast<int>(std::countr_zero(blockers))
            : 63 - static_cast<int>(std::countl_zero(blockers));
        const Bitboard attackers = direction < 4
            ? rook_attackers
            : bishop_attackers;
        if ((attackers & bit(source)) != 0) {
            return true;
        }
    }
    return false;
}

[[nodiscard]] Bitboard ray_attacks_from(
    int source,
    std::size_t direction,
    Bitboard occupancy
) noexcept {
    Bitboard attacks = RAY_MASKS[static_cast<std::size_t>(source)][direction];
    const Bitboard blockers = attacks & occupancy;
    if (blockers == 0) {
        return attacks;
    }
    const int blocker = RAY_ASCENDING[direction]
        ? static_cast<int>(std::countr_zero(blockers))
        : 63 - static_cast<int>(std::countl_zero(blockers));
    return attacks
        & ~RAY_MASKS[static_cast<std::size_t>(blocker)][direction];
}

[[nodiscard]] Bitboard attacks_from(
    const Position& position,
    int source,
    int piece_type,
    bool color
) noexcept {
    const std::size_t source_index = static_cast<std::size_t>(source);
    if (piece_type == KNIGHT) {
        return KNIGHT_ATTACK_MASKS[source_index];
    }
    if (piece_type == KING) {
        return KING_ATTACK_MASKS[source_index];
    }
    if (piece_type == PAWN) {
        Bitboard attacks = 0;
        const int file = source & 7;
        const int rank = source >> 3;
        const int target_rank = rank + (color == WHITE ? 1 : -1);
        for (const int file_delta : {-1, 1}) {
            const int target_file = file + file_delta;
            if (inside(target_file, target_rank)) {
                attacks |= bit(square(target_file, target_rank));
            }
        }
        return attacks;
    }

    const Bitboard occupancy = position.occupied[0] | position.occupied[1];
    const std::size_t first_direction =
        piece_type == BISHOP ? 4 : 0;
    const std::size_t final_direction =
        piece_type == ROOK ? 4 : 8;
    Bitboard attacks = 0;
    for (
        std::size_t direction = first_direction;
        direction < final_direction;
        ++direction
    ) {
        attacks |= ray_attacks_from(source, direction, occupancy);
    }
    return attacks;
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
    MoveList& moves,
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
    MoveList& moves
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

[[nodiscard]] MoveList pseudo_moves(
    const BoardState& board,
    const std::vector<int>& ep_targets
) {
    MoveList moves;
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

// Frozen final-boundary ordering student accepted by the isolated Series-3
// gate. The inference core is model-shaped rather than search-shaped: it can
// evaluate an immutable fixed-point network view, while activation remains an
// explicit request opt-in and is validated to the trained boundary entering
// Series 3 after Black's Series 2.
constexpr const char* S3_NEURAL_ARTIFACT_ID =
    "spc-nnue-955ab36e1657870a31ee1130";
constexpr const char* S3_NEURAL_ARTIFACT_SHA256 =
    "4bbab0180470439a17441882b0e5a24870a4a004b4b0fded71646dd305bbcab8";
constexpr const char* S3_NEURAL_INFERENCE_SCOPE =
    "complete-boundaries-entering-series-3-only-v1";

constexpr std::uint64_t NEURAL_PIECE_SQUARE_OFFSET = 0;
constexpr std::uint64_t NEURAL_PIECE_SQUARE_COUNT = 2 * 6 * 64;
constexpr std::uint64_t NEURAL_PROMOTED_OFFSET =
    NEURAL_PIECE_SQUARE_OFFSET + NEURAL_PIECE_SQUARE_COUNT;
constexpr std::uint64_t NEURAL_PROMOTED_COUNT = 2 * 64;
constexpr std::uint64_t NEURAL_MOVER_OFFSET =
    NEURAL_PROMOTED_OFFSET + NEURAL_PROMOTED_COUNT;
constexpr std::uint64_t NEURAL_SERIES_OFFSET = NEURAL_MOVER_OFFSET + 2;
constexpr std::uint64_t NEURAL_MOVES_REMAINING_OFFSET =
    NEURAL_SERIES_OFFSET + 17;
constexpr std::uint64_t NEURAL_QUIET_OFFSET =
    NEURAL_MOVES_REMAINING_OFFSET + 18;
constexpr std::uint64_t NEURAL_CHECK_OFFSET = NEURAL_QUIET_OFFSET + 12;
constexpr std::uint64_t NEURAL_CASTLING_OFFSET = NEURAL_CHECK_OFFSET + 1;
constexpr std::uint64_t NEURAL_PROGRESSIVE_EP_OFFSET =
    NEURAL_CASTLING_OFFSET + 4;
constexpr std::uint64_t NEURAL_FEATURE_COUNT =
    NEURAL_PROGRESSIVE_EP_OFFSET + 64;
static_assert(NEURAL_FEATURE_COUNT == 1'014);

constexpr std::array<std::int16_t, NEURAL_FEATURE_COUNT> S3_INPUT_WEIGHTS = {
    0, 0, 0, 0, 0, 0, 0, 0, -600, -304, 654, -167, -385, -427, 704, 818,
    446, -163, -814, 256, 364, 401, -782, -138, 788, -190, -576, 410, 149, 232, -820, -964,
    1342, 416, -626, 451, 352, 991, 535, -256, 508, -418, 588, 623, -453, 838, 81, -983,
    451, -533, -643, 334, -1469, 1154, -1395, -1469, 0, 0, 0, 0, 0, 0, 0, 0,
    0, -191, 0, 0, 0, 0, 528, 0, 0, 0, 0, -1469, 0, 0, 0, 0,
    -26, 1652, -3, 0, 0, -759, 0, 259, 1401, 339, -302, 881, 735, 1092, 1543, 1587,
    1287, 279, -942, -216, -452, -1084, -247, -665, 931, -689, -1114, 386, 52, 422, -1029, -790,
    56, -219, -130, -1137, -644, -877, 971, -1080, 277, 0, -877, 1131, 0, -564, -1401, -908,
    0, 0, -349, 0, 0, 89, 0, 0, 0, 416, 0, 843, 606, 0, -436, 0,
    474, -326, -641, 1612, 669, 262, 1379, -538, -402, 596, 253, 287, -1059, 436, -1228, 1603,
    931, 681, -278, -398, -406, 667, 303, -1469, 602, 755, -996, -430, -922, -197, -458, 1165,
    91, -441, -348, -798, -362, 1059, -67, -675, -343, -181, -693, 968, 0, 715, 587, 228,
    -944, 1002, 1363, 0, 0, 0, -336, 756, 1521, 0, 0, 0, 0, 0, 0, 0,
    1517, 0, 0, 0, 0, 0, 0, 931, 1587, 931, 0, 0, 0, 0, 0, 0,
    -669, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, -377,
    800, 931, 0, 0, 0, 0, 0, -1021, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 1419, -80, 209, 0, 0, 0, 0, 0, 0, -198, 143, 1524, 0, 0, 0,
    -809, 1422, 88, 20, -1136, 78, 1569, 856, -224, -9, -355, -415, -825, 1453, 585, 972,
    616, -751, 84, -825, 1060, 150, 58, 789, -569, -741, -888, -557, -79, 95, -813, 77,
    -710, -625, -891, -1018, -677, -105, -236, -338, 351, -558, 358, 1538, -990, 274, -564, 524,
    0, 0, 0, -343, -923, -1136, 0, 0, 0, 0, 0, 1430, 1101, 1296, 0, 0,
    0, 0, -1469, 0, -1469, -1469, -1469, 0, 0, 1608, 0, 1532, -1469, 1572, 1559, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, -676, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 357, -1196, 0, -997, 1615, -1108, -949, 0,
    -198, -334, 439, -22, 557, -293, -542, -867, 339, 318, 792, -73, 455, 984, 429, -225,
    213, 647, 283, 983, 316, -46, 29, 598, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, -574, 0, -1104, 0, 0, 0, 0,
    0, 0, 0, -912, -733, -646, 0, 0, -551, 0, 221, 0, 0, -993, 0, -970,
    0, 0, 0, 1235, 529, 0, 0, 0, 0, 279, 0, 0, 0, 0, 122, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    -360, 0, 2165, 0, 0, 0, 0, -969, 0, 2463, 0, 0, 0, 0, -1374, 0,
    0, 0, -841, 0, 0, -934, 0, 0, -1259, 0, 0, 151, -193, 0, 0, -1245,
    0, -114, 0, -1079, -1377, 0, -1290, 0, 0, 0, 68, 0, 0, -701, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, -1281, 0, 0, 0, 0, 0, 0, 0,
    -1048, 0, 0, 0, 0, 0, 0, -1030, -163, 1273, 0, 0, 0, 0, 24, -368,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, -1425,
    -1248, 0, 0, 0, 0, 0, -1158, 0, 0, 0, 0, -1372, 0, -1338, 0, 0,
    0, 0, -1226, -1366, -1339, 0, 0, 0, 0, 0, 0, -1073, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 230, 77, 762, 0, 0, 0, 0, 0, 0, -2, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    917, 0, 0, 0, 0, 0, 917, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 917, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, -49, 108, 33, -452, 0, 0, 0, 0, 0, 0, 0,
    0, 38, -888, -927, -359, -192, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 931, 0, -1100, 0, 272, -1469, 0, -1469, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0,
};

constexpr std::array<std::int32_t, 1> S3_HIDDEN_BIAS = {9'002};
constexpr std::array<std::int32_t, 1> S3_OUTPUT_WEIGHTS = {256};

struct FixedPointNetworkView {
    const std::int16_t* input_weights;
    const std::int32_t* hidden_bias;
    const std::int32_t* output_weights;
    std::uint64_t feature_count;
    std::uint64_t hidden_size;
    std::int64_t output_bias;
    std::int64_t output_denominator;
    std::int32_t activation_clip;
    std::int64_t score_clip;
};

constexpr FixedPointNetworkView S3_NETWORK = {
    S3_INPUT_WEIGHTS.data(),
    S3_HIDDEN_BIAS.data(),
    S3_OUTPUT_WEIGHTS.data(),
    NEURAL_FEATURE_COUNT,
    1,
    -8'947'456,
    2'048,
    32'767,
    500'000,
};

[[nodiscard]] std::uint64_t neural_color_index(bool color) noexcept {
    return color == WHITE ? 0 : 1;
}

[[nodiscard]] bool valid_neural_board_bitboards(
    const BoardState& board
) noexcept {
    const Bitboard black = board.occupied[0];
    const Bitboard white = board.occupied[1];
    if ((black & white) != 0) {
        return false;
    }

    const std::array<Bitboard, 6> piece_masks = {
        board.pawns,
        board.knights,
        board.bishops,
        board.rooks,
        board.queens,
        board.kings,
    };
    Bitboard typed_occupancy = 0;
    for (const Bitboard piece_mask : piece_masks) {
        if ((typed_occupancy & piece_mask) != 0) {
            return false;
        }
        typed_occupancy |= piece_mask;
    }
    const Bitboard occupied = black | white;
    return typed_occupancy == occupied
        && (board.promoted & ~occupied) == 0;
}

struct ActiveNeuralFeatures {
    std::array<std::uint16_t, 192> values{};
    std::size_t size = 0;
    bool valid = true;

    void push(std::uint64_t value) noexcept {
        if (
            !valid
            || size >= values.size()
            || value >= NEURAL_FEATURE_COUNT
        ) {
            valid = false;
            return;
        }
        values[size++] = static_cast<std::uint16_t>(value);
    }

    [[nodiscard]] const std::uint16_t* begin() const noexcept {
        return values.data();
    }

    [[nodiscard]] const std::uint16_t* end() const noexcept {
        return values.data() + size;
    }
};

[[nodiscard]] ActiveNeuralFeatures extract_neural_features(
    const BoardState& board,
    std::int64_t series_number,
    std::int64_t quiet_series,
    std::int64_t moves_remaining,
    Bitboard progressive_ep_targets,
    bool known_in_check
) noexcept {
    ActiveNeuralFeatures active;
    const Position position = evaluation_position(board);
    Bitboard occupied = board.occupied[0] | board.occupied[1];
    while (occupied != 0) {
        const int square_index = static_cast<int>(std::countr_zero(occupied));
        occupied &= occupied - 1;
        const Bitboard square_mask = bit(square_index);
        const int piece_type = piece_type_at(position, square_index);
        const bool color = (board.occupied[1] & square_mask) != 0;
        active.push(
            NEURAL_PIECE_SQUARE_OFFSET
            + ((neural_color_index(color) * 6
                + static_cast<std::uint64_t>(piece_type - 1)) * 64)
            + static_cast<std::uint64_t>(square_index)
        );
        if ((board.promoted & square_mask) != 0) {
            active.push(
                NEURAL_PROMOTED_OFFSET
                + neural_color_index(color) * 64
                + static_cast<std::uint64_t>(square_index)
            );
        }
    }
    active.push(
        NEURAL_MOVER_OFFSET + neural_color_index(board.white_to_move)
    );
    active.push(
        NEURAL_SERIES_OFFSET
        + static_cast<std::uint64_t>(std::min<std::int64_t>(series_number, 17))
        - 1
    );
    active.push(
        NEURAL_MOVES_REMAINING_OFFSET
        + static_cast<std::uint64_t>(
            std::min<std::int64_t>(moves_remaining, 17)
        )
    );
    active.push(
        NEURAL_QUIET_OFFSET
        + static_cast<std::uint64_t>(std::min<std::int64_t>(quiet_series, 11))
    );
    if (known_in_check) {
        active.push(NEURAL_CHECK_OFFSET);
    }
    constexpr std::array<int, 4> CASTLING_ROOK_SQUARES = {7, 0, 63, 56};
    for (std::size_t index = 0; index < CASTLING_ROOK_SQUARES.size(); ++index) {
        if ((board.castling_rights & bit(CASTLING_ROOK_SQUARES[index])) != 0) {
            active.push(NEURAL_CASTLING_OFFSET + index);
        }
    }
    while (progressive_ep_targets != 0) {
        const int target = static_cast<int>(
            std::countr_zero(progressive_ep_targets)
        );
        progressive_ep_targets &= progressive_ep_targets - 1;
        active.push(
            NEURAL_PROGRESSIVE_EP_OFFSET + static_cast<std::uint64_t>(target)
        );
    }
    std::sort(active.values.begin(), active.values.begin() + active.size);
    active.size = static_cast<std::size_t>(
        std::unique(
            active.values.begin(),
            active.values.begin() + active.size
        ) - active.values.begin()
    );
    return active;
}

[[nodiscard]] std::optional<std::int64_t> fixed_point_predict(
    const FixedPointNetworkView& network,
    const ActiveNeuralFeatures& active
) noexcept {
    std::array<std::int64_t, 128> hidden;
    const std::size_t width = static_cast<std::size_t>(network.hidden_size);
    if (
        !active.valid
        || network.input_weights == nullptr
        || network.hidden_bias == nullptr
        || network.output_weights == nullptr
        || width == 0
        || width > hidden.size()
        || network.output_denominator <= 0
        || network.activation_clip < 0
        || network.score_clip < 0
    ) {
        return std::nullopt;
    }
    for (std::size_t index = 0; index < width; ++index) {
        hidden[index] = network.hidden_bias[index];
    }
    for (const std::uint16_t feature : active) {
        if (static_cast<std::uint64_t>(feature) >= network.feature_count) {
            return std::nullopt;
        }
        const std::size_t offset = static_cast<std::size_t>(feature) * width;
        for (std::size_t index = 0; index < width; ++index) {
            hidden[index] += network.input_weights[offset + index];
        }
    }
    std::int64_t accumulator = network.output_bias;
    for (std::size_t index = 0; index < width; ++index) {
        const std::int64_t activated = std::clamp<std::int64_t>(
            hidden[index],
            0,
            network.activation_clip
        );
        std::int64_t term = 0;
        if (
            !checked_multiply(activated, network.output_weights[index], term)
            || !checked_add(accumulator, term, accumulator)
        ) {
            return std::nullopt;
        }
    }
    const std::int64_t magnitude = accumulator < 0 ? -accumulator : accumulator;
    std::int64_t adjusted = 0;
    if (!checked_add(magnitude, network.output_denominator / 2, adjusted)) {
        return std::nullopt;
    }
    const std::int64_t divided = adjusted / network.output_denominator;
    const std::int64_t score = accumulator < 0 ? -divided : divided;
    return std::clamp<std::int64_t>(
        score,
        -network.score_clip,
        network.score_clip
    );
}

[[nodiscard]] std::optional<std::int64_t> blend_neural_score(
    std::int64_t hand_score,
    std::int64_t neural_score,
    std::int64_t blend_percent
) noexcept {
    std::int64_t hand_term = 0;
    std::int64_t neural_term = 0;
    std::int64_t numerator = 0;
    if (
        !checked_multiply(hand_score, 100 - blend_percent, hand_term)
        || !checked_multiply(neural_score, blend_percent, neural_term)
        || !checked_add(hand_term, neural_term, numerator)
    ) {
        return std::nullopt;
    }
    const std::int64_t magnitude = numerator < 0 ? -numerator : numerator;
    std::int64_t adjusted = 0;
    if (!checked_add(magnitude, 50, adjusted)) {
        return std::nullopt;
    }
    const std::int64_t divided = adjusted / 100;
    return numerator < 0 ? -divided : divided;
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
        if (!checked_subtract(budget, best, distance_bonus)) {
            return std::nullopt;
        }
        distance_bonus = std::min<std::int64_t>(4, distance_bonus);
        if (
            !checked_multiply(distance_bonus, 55, scaled_bonus)
            || !checked_add(650, scaled_bonus, result)
        ) {
            return std::nullopt;
        }
        return result;
    }
    const std::int64_t deficit = best - budget;
    return std::max<std::int64_t>(0, 180 - deficit * 45);
}

[[nodiscard]] Bitboard direct_capture_targets(
    const Position& position,
    const BoardState& board,
    bool validate_check_evasions
) noexcept {
    const bool attacker = board.white_to_move;
    const bool victim = !attacker;
    const Bitboard victim_occupancy = board.occupied[victim ? 1 : 0];
    const Bitboard attacker_occupancy = board.occupied[attacker ? 1 : 0];
    const int attacker_king = king_square(position, attacker);
    if (attacker_king < 0) {
        return 0;
    }
    const bool attacker_in_check = validate_check_evasions
        && board_attacked_by(board, attacker_king, victim);
    Bitboard captured = 0;
    Bitboard pieces = attacker_occupancy;
    while (pieces != 0) {
        const int source = static_cast<int>(std::countr_zero(pieces));
        pieces &= pieces - 1;
        const int piece_type = piece_type_at(position, source);
        Bitboard targets = attacks_from(
            position,
            source,
            piece_type,
            attacker
        ) & victim_occupancy;
        if (targets == 0) {
            continue;
        }
        bool requires_legal_replay = attacker_in_check
            || source == attacker_king;
        if (!requires_legal_replay) {
            const int file_delta = std::abs(
                (source & 7) - (attacker_king & 7)
            );
            const int rank_delta = std::abs(
                (source >> 3) - (attacker_king >> 3)
            );
            const bool could_be_pinned = file_delta == 0
                || rank_delta == 0
                || file_delta == rank_delta;
            if (could_be_pinned) {
                BoardState without_source = board;
                clear_piece(without_source, source);
                requires_legal_replay = board_attacked_by(
                    without_source,
                    attacker_king,
                    victim
                );
            }
        }
        if (!requires_legal_replay) {
            captured |= targets;
            continue;
        }
        while (targets != 0) {
            const int target = static_cast<int>(
                std::countr_zero(targets)
            );
            targets &= targets - 1;
            const int promotion = piece_type == PAWN
                    && ((target >> 3) == 0 || (target >> 3) == 7)
                ? QUEEN
                : 0;
            if (legal_after_move(
                    board,
                    Move{source, target, promotion, -1, false}
                )) {
                captured |= bit(target);
            }
        }
    }
    return captured;
}

[[nodiscard]] int attacked_material(
    const Position& position,
    bool victim
) noexcept {
    const bool attacker = !victim;
    const Bitboard victim_occupancy = position.occupied[victim ? 1 : 0];
    const Bitboard attacker_occupancy = position.occupied[attacker ? 1 : 0];
    const BoardState board{
        position.pawns,
        position.knights,
        position.bishops,
        position.rooks,
        position.queens,
        position.kings,
        position.occupied,
        0,
        0,
        attacker,
    };
    const int attacker_king = king_square(position, attacker);
    if (
        attacker_king >= 0
        && attacked_by(
            position,
            attacker_king,
            victim,
            victim_occupancy | attacker_occupancy,
            victim_occupancy
        )
    ) {
        Bitboard raw_targets = 0;
        Bitboard pieces = attacker_occupancy;
        while (pieces != 0) {
            const int source = static_cast<int>(
                std::countr_zero(pieces)
            );
            pieces &= pieces - 1;
            raw_targets |= attacks_from(
                position,
                source,
                piece_type_at(position, source),
                attacker
            ) & victim_occupancy;
        }
        int raw_value = 0;
        while (raw_targets != 0) {
            const int target = static_cast<int>(
                std::countr_zero(raw_targets)
            );
            raw_targets &= raw_targets - 1;
            raw_value += PIECE_VALUES[piece_type_at(position, target)];
        }
        return raw_value;
    }
    Bitboard capturable_targets = direct_capture_targets(
        position,
        board,
        false
    );

    int value = 0;
    while (capturable_targets != 0) {
        const int target = static_cast<int>(
            std::countr_zero(capturable_targets)
        );
        capturable_targets &= capturable_targets - 1;
        value += PIECE_VALUES[piece_type_at(position, target)];
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

namespace {

struct ReachIdentity {
    std::array<Bitboard, 9> words;
    bool white_to_move;
    Bitboard ep_targets;

    bool operator==(const ReachIdentity&) const = default;
};

void hash_reach_word(std::size_t& seed, std::uint64_t value) noexcept {
    seed ^= std::hash<std::uint64_t>{}(value)
        + static_cast<std::size_t>(0x9e3779b97f4a7c15ULL)
        + (seed << 6)
        + (seed >> 2);
}

struct ReachIdentityHash {
    std::size_t operator()(const ReachIdentity& key) const noexcept {
        std::size_t seed = 0;
        for (const Bitboard word : key.words) {
            hash_reach_word(seed, word);
        }
        hash_reach_word(seed, key.white_to_move ? 1 : 0);
        hash_reach_word(seed, key.ep_targets);
        return seed;
    }
};

[[nodiscard]] Bitboard reach_target_bits(
    const std::vector<int>& targets
) noexcept {
    Bitboard result = 0;
    for (const int target : targets) {
        if (target >= 0 && target < 64) {
            result |= bit(target);
        }
    }
    return result;
}

[[nodiscard]] ReachIdentity reach_identity(
    const BoardState& board,
    const std::vector<int>& ep_targets
) noexcept {
    return ReachIdentity{
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
        reach_target_bits(ep_targets),
    };
}

[[nodiscard]] bool reach_move_precedes(
    const ExpandedMove& left,
    const ExpandedMove& right
) noexcept {
    if (left.delivered_check != right.delivered_check) {
        return left.delivered_check;
    }
    const bool left_promotion = left.move.promotion != 0;
    const bool right_promotion = right.move.promotion != 0;
    if (left_promotion != right_promotion) {
        return left_promotion;
    }
    if (left.is_capture != right.is_capture) {
        return left.is_capture;
    }
    return legal_move_uci_key(left.move) < legal_move_uci_key(right.move);
}

[[nodiscard]] ReachProbe probe_series_reach_native(
    const BoardState& boundary,
    const std::vector<int>& boundary_ep_targets,
    bool color,
    int max_moves,
    std::uint64_t node_limit
) {
    const Position initial = evaluation_position(boundary);
    const int enemy_king = king_square(initial, !color);
    if (
        enemy_king < 0
        || board_attacked_by(boundary, enemy_king, color)
    ) {
        return ReachProbe{0, 0, true};
    }

    BoardState root = boundary;
    root.white_to_move = color;
    std::vector<BoardState> frontier{root};
    std::unordered_map<ReachIdentity, bool, ReachIdentityHash> seen;
    seen.emplace(reach_identity(root, boundary_ep_targets), true);
    std::uint64_t nodes = 0;
    for (int distance = 1; distance <= max_moves; ++distance) {
        std::vector<BoardState> following;
        for (const BoardState& position : frontier) {
            const bool first = distance == 1;
            auto variants = expand_legal_move_variants(
                position,
                first ? boundary_ep_targets : std::vector<int>{}
            );
            std::sort(variants.begin(), variants.end(), reach_move_precedes);
            for (const ExpandedMove& expanded : variants) {
                if (nodes >= node_limit) {
                    return ReachProbe{std::nullopt, nodes, false};
                }
                ++nodes;
                if (expanded.delivered_check) {
                    return ReachProbe{distance, nodes, true};
                }
                BoardState child = expanded.child;
                child.white_to_move = color;
                const ReachIdentity key = reach_identity(child, {});
                if (seen.emplace(key, true).second) {
                    following.push_back(std::move(child));
                }
            }
        }
        frontier = std::move(following);
        if (frontier.empty()) {
            break;
        }
    }
    return ReachProbe{std::nullopt, nodes, true};
}

[[nodiscard]] int useful_mobility_native(
    const BoardState& boundary,
    const std::vector<int>& boundary_ep_targets,
    bool color
) {
    BoardState board = boundary;
    board.white_to_move = color;
    const auto variants = expand_legal_move_variants(
        board,
        color == boundary.white_to_move
            ? boundary_ep_targets
            : std::vector<int>{}
    );
    int useful = 0;
    for (const ExpandedMove& expanded : variants) {
        useful += (
            expanded.delivered_check
            || expanded.is_capture
            || expanded.move.promotion != 0
        ) ? 3 : 1;
    }
    return useful;
}

struct CaptureReach {
    int value = 0;
    Bitboard targets = 0;
    std::uint64_t positions = 0;
    bool complete = true;
};

[[nodiscard]] CaptureReach immediate_capture_reach_native(
    const BoardState& boundary,
    const std::vector<int>& boundary_ep_targets,
    const Position& position
) {
    const bool attacker = boundary.white_to_move;
    const bool victim = !boundary.white_to_move;
    Bitboard captured = direct_capture_targets(position, boundary, true);
    const Bitboard attacker_occupancy = boundary.occupied[attacker ? 1 : 0];
    const Bitboard victim_occupancy = boundary.occupied[victim ? 1 : 0];
    const Bitboard occupancy = attacker_occupancy | victim_occupancy;
    for (const int target : boundary_ep_targets) {
        if (target < 0 || target >= 64 || (occupancy & bit(target)) != 0) {
            continue;
        }
        const int target_file = target & 7;
        const int target_rank = target >> 3;
        const int expected_rank = attacker == WHITE ? 5 : 2;
        const int source_rank = target_rank + (attacker == WHITE ? -1 : 1);
        const int capture_square = target + (attacker == WHITE ? -8 : 8);
        if (
            target_rank != expected_rank
            || capture_square < 0
            || capture_square >= 64
            || (boundary.pawns & victim_occupancy & bit(capture_square)) == 0
        ) {
            continue;
        }
        for (const int file_delta : {-1, 1}) {
            const int source_file = target_file + file_delta;
            if (!inside(source_file, source_rank)) {
                continue;
            }
            const int source = square(source_file, source_rank);
            if (
                (boundary.pawns & attacker_occupancy & bit(source)) != 0
                && legal_after_move(
                    boundary,
                    Move{source, target, 0, target, false}
                )
            ) {
                captured |= bit(capture_square);
            }
        }
    }
    int value = 0;
    Bitboard remaining = captured;
    while (remaining != 0) {
        const int target = static_cast<int>(std::countr_zero(remaining));
        remaining &= remaining - 1;
        value += PIECE_VALUES[piece_type_at(position, target)];
    }
    return CaptureReach{value, captured, 0, true};
}

[[nodiscard]] int expanded_capture_target(
    const BoardState& board,
    const ExpandedMove& expanded
) noexcept {
    if (!expanded.is_capture) {
        return -1;
    }
    const int target = expanded.move.to_square;
    const Bitboard occupancy = board.occupied[0] | board.occupied[1];
    if (
        expanded.move.required_ep_square >= 0
        && (occupancy & bit(target)) == 0
    ) {
        return target + (board.white_to_move == WHITE ? -8 : 8);
    }
    return target;
}

[[nodiscard]] int expanded_capture_value(
    const BoardState& board,
    const ExpandedMove& expanded
) noexcept {
    const int target = expanded_capture_target(board, expanded);
    if (target < 0 || target >= 64) {
        return 0;
    }
    return PIECE_VALUES[static_cast<std::size_t>(piece_type_at(
        evaluation_position(board),
        target
    ))];
}

[[nodiscard]] CaptureReach two_move_capture_reach_native(
    const BoardState& boundary,
    const std::vector<int>& ep_targets,
    std::int64_t series_number,
    std::uint64_t position_limit
) {
    if (series_number < 2) {
        return {};
    }
    const bool mover = boundary.white_to_move;
    int best = 0;
    Bitboard targets = 0;
    const auto first_variants = expand_legal_move_variants(
        boundary,
        ep_targets
    );
    const std::uint64_t first_count = first_variants.size();
    if (first_count > position_limit) {
        return CaptureReach{0, 0, position_limit, false};
    }
    std::uint64_t positions = first_count;
    for (const ExpandedMove& first : first_variants) {
        if (first.delivered_check) {
            continue;
        }
        BoardState same_mover = first.child;
        same_mover.white_to_move = mover;
        const Position child_position = evaluation_position(same_mover);
        Bitboard child_targets = direct_capture_targets(
            child_position,
            same_mover,
            true
        );
        targets |= child_targets;
        while (child_targets != 0) {
            const int target = static_cast<int>(
                std::countr_zero(child_targets)
            );
            child_targets &= child_targets - 1;
            best = std::max(
                best,
                PIECE_VALUES[static_cast<std::size_t>(piece_type_at(
                    child_position,
                    target
                ))]
            );
        }
    }
    return CaptureReach{best, targets, positions, true};
}

[[nodiscard]] bool promotable_pawn_is_reachable(
    const Position& position,
    const BoardState& board,
    Bitboard targets,
    std::int64_t series_number
) noexcept {
    const bool victim = !board.white_to_move;
    const Bitboard victim_pawns = position.pawns
        & position.occupied[victim ? 1 : 0];
    targets &= victim_pawns;
    const std::int64_t next_budget = series_number + 1;
    while (targets != 0) {
        const int target = static_cast<int>(std::countr_zero(targets));
        targets &= targets - 1;
        const int distance = promotion_distance(position, target, victim);
        if (distance >= 0 && distance <= next_budget) {
            return true;
        }
    }
    return false;
}

[[nodiscard]] int reach_value_native(
    const ReachProbe& probe,
    std::int64_t budget
) noexcept {
    if (!probe.distance.has_value()) {
        return 0;
    }
    if (*probe.distance == 0) {
        return 260;
    }
    if (*probe.distance <= budget) {
        return static_cast<int>(std::max<std::int64_t>(
            60,
            230 - (*probe.distance - 1) * 80
        ));
    }
    return 0;
}

}  // namespace

std::optional<FullEvaluation> full_evaluate(
    const BoardState& board,
    const std::vector<int>& ep_targets,
    std::int64_t series_number,
    std::uint64_t max_reach_positions,
    const FullWeights& weights
) {
    if (
        series_number < 1
        || series_number == std::numeric_limits<std::int64_t>::max()
    ) {
        return std::nullopt;
    }
    const Position position{
        board.pawns,
        board.knights,
        board.bishops,
        board.rooks,
        board.queens,
        board.kings,
        board.occupied,
        board.white_to_move,
        series_number,
    };

    const std::int64_t white_budget = series_number
        + (board.white_to_move == WHITE ? 0 : 1);
    const std::int64_t black_budget = series_number
        + (board.white_to_move == BLACK ? 0 : 1);
    ReachProbe white_reach;
    ReachProbe black_reach;
    if (is_check(position)) {
        const bool checker = !board.white_to_move;
        white_reach = checker == WHITE
            ? ReachProbe{0, 0, true}
            : ReachProbe{std::nullopt, 0, true};
        black_reach = checker == BLACK
            ? ReachProbe{0, 0, true}
            : ReachProbe{std::nullopt, 0, true};
    } else {
        std::uint64_t reach_remaining = max_reach_positions;
        white_reach = probe_series_reach_native(
            board,
            board.white_to_move == WHITE ? ep_targets : std::vector<int>{},
            WHITE,
            static_cast<int>(std::min<std::int64_t>(2, white_budget)),
            std::min<std::uint64_t>(128, reach_remaining)
        );
        reach_remaining -= std::min(reach_remaining, white_reach.nodes);
        black_reach = probe_series_reach_native(
            board,
            board.white_to_move == BLACK ? ep_targets : std::vector<int>{},
            BLACK,
            static_cast<int>(std::min<std::int64_t>(2, black_budget)),
            std::min<std::uint64_t>(128, reach_remaining)
        );
    }

    FullEvaluation result;
    result.white_reach = white_reach;
    result.black_reach = black_reach;
    const auto assign_scaled = [](
        std::int64_t raw,
        std::int64_t weight,
        std::int64_t& target
    ) noexcept {
        const auto scaled = bankers_scale(raw, weight);
        if (!scaled.has_value()) {
            return false;
        }
        target = *scaled;
        return true;
    };

    if (!assign_scaled(material(position), weights.material, result.material)) {
        return std::nullopt;
    }
    if (!assign_scaled(
            (king_flight_squares(position, WHITE)
             - king_flight_squares(position, BLACK)) * 28,
            weights.king_space,
            result.king_space
        )) {
        return std::nullopt;
    }
    if (is_check(position)) {
        result.series_reach = 0;
    } else if (white_reach.complete && black_reach.complete) {
        if (!assign_scaled(
                reach_value_native(white_reach, white_budget)
                    - reach_value_native(black_reach, black_budget),
                weights.series_reach,
                result.series_reach
            )) {
            return std::nullopt;
        }
    }
    const CaptureReach immediate_capture = immediate_capture_reach_native(
        board,
        ep_targets,
        position
    );
    const bool low_material = std::popcount(
        position.occupied[0] | position.occupied[1]
    ) <= 10;
    const std::uint64_t check_reach_positions = std::min<std::uint64_t>(
        max_reach_positions,
        white_reach.nodes + black_reach.nodes
    );
    const std::uint64_t capture_remaining = max_reach_positions
        - check_reach_positions;
    const std::uint64_t capture_limit =
        low_material && capture_remaining >= CAPTURE_REACH_POSITION_LIMIT
            ? CAPTURE_REACH_POSITION_LIMIT
            : 0;
    const CaptureReach two_move_capture = low_material
        ? two_move_capture_reach_native(
            board,
            ep_targets,
            series_number,
            capture_limit
        )
        : CaptureReach{};
    result.capture_reach_positions = two_move_capture.positions;
    result.capture_reach_complete = two_move_capture.complete;
    const auto white_promotion = promotion_score(
        position,
        WHITE
    );
    const auto black_promotion = promotion_score(
        position,
        BLACK
    );
    std::int64_t promotion_difference = 0;
    if (
        !white_promotion.has_value()
        || !black_promotion.has_value()
        || !checked_subtract(
            *white_promotion,
            *black_promotion,
            promotion_difference
        )
        || !assign_scaled(
            promotion_difference,
            weights.promotion_corridors,
            result.promotion_corridors
        )
    ) {
        return std::nullopt;
    }
    const int capturable_material = std::max(
        immediate_capture.value,
        two_move_capture.complete ? two_move_capture.value : 0
    );
    const std::int64_t vulnerability_raw = board.white_to_move == WHITE
        ? capturable_material
        : -capturable_material;
    if (!assign_scaled(
            vulnerability_raw,
            weights.immediate_vulnerability,
            result.immediate_vulnerability
        )) {
        return std::nullopt;
    }
    if (!assign_scaled(
            (
                useful_mobility_native(board, ep_targets, WHITE)
                - useful_mobility_native(board, ep_targets, BLACK)
            ) * 2,
            weights.useful_mobility,
            result.useful_mobility
        )) {
        return std::nullopt;
    }
    if (
        is_check(position)
        && !assign_scaled(
            board.white_to_move == WHITE ? -170 : 170,
            weights.boundary_check,
            result.boundary_check
        )
    ) {
        return std::nullopt;
    }
    const Bitboard capture_targets = immediate_capture.targets
        | two_move_capture.targets;
    // The two-move vulnerability term already prices ordinary capture
    // swings. Extend only when a capture route competes with a promotion
    // corridor, which cannot be combined safely in one static score. Exact
    // continuation stays in low-material promotion races so ordinary
    // middlegame leaves do not widen.
    result.tactical_unstable = low_material
        && promotable_pawn_is_reachable(
            position,
            board,
            capture_targets,
            series_number
        );

    const std::array<std::int64_t, 7> terms = {
        result.material,
        result.king_space,
        result.series_reach,
        result.promotion_corridors,
        result.immediate_vulnerability,
        result.useful_mobility,
        result.boundary_check,
    };
    for (const std::int64_t term : terms) {
        if (!checked_add(result.total, term, result.total)) {
            return std::nullopt;
        }
    }
    return result;
}

namespace {

[[nodiscard]] int teacher_attackers_count(
    const BoardState& board,
    bool color,
    int target
) noexcept {
    const Position position = evaluation_position(board);
    Bitboard pieces = board.occupied[color ? 1 : 0];
    int count = 0;
    while (pieces != 0) {
        const int source = static_cast<int>(std::countr_zero(pieces));
        pieces &= pieces - 1;
        count += (
            attacks_from(
                position,
                source,
                piece_type_at(position, source),
                color
            )
            & bit(target)
        ) != 0 ? 1 : 0;
    }
    return count;
}

[[nodiscard]] int teacher_minor_development(
    const BoardState& board,
    bool color
) noexcept {
    const Bitboard own = board.occupied[color ? 1 : 0];
    Bitboard minors = (board.knights | board.bishops) & own;
    const int home_rank = color == WHITE ? 0 : 7;
    int developed = 0;
    while (minors != 0) {
        const int source = static_cast<int>(std::countr_zero(minors));
        minors &= minors - 1;
        developed += (source >> 3) != home_rank ? 1 : 0;
    }
    return developed;
}

[[nodiscard]] std::array<int, 8> teacher_pawn_file_counts(
    const BoardState& board,
    bool color
) noexcept {
    std::array<int, 8> counts{};
    Bitboard pawns = board.pawns & board.occupied[color ? 1 : 0];
    while (pawns != 0) {
        const int source = static_cast<int>(std::countr_zero(pawns));
        pawns &= pawns - 1;
        ++counts[static_cast<std::size_t>(source & 7)];
    }
    return counts;
}

[[nodiscard]] int teacher_pawn_islands(
    const std::array<int, 8>& files
) noexcept {
    int islands = 0;
    bool prior = false;
    for (const int count : files) {
        const bool occupied = count != 0;
        islands += occupied && !prior ? 1 : 0;
        prior = occupied;
    }
    return islands;
}

[[nodiscard]] Bitboard teacher_passed_pawns(
    const BoardState& board,
    bool color
) noexcept {
    Bitboard candidates = board.pawns & board.occupied[color ? 1 : 0];
    const Bitboard enemy = board.pawns & board.occupied[(!color) ? 1 : 0];
    Bitboard passed = 0;
    while (candidates != 0) {
        const int source = static_cast<int>(std::countr_zero(candidates));
        candidates &= candidates - 1;
        const int source_file = source & 7;
        const int source_rank = source >> 3;
        bool blocked = false;
        Bitboard enemies = enemy;
        while (enemies != 0) {
            const int target = static_cast<int>(std::countr_zero(enemies));
            enemies &= enemies - 1;
            const int target_file = target & 7;
            const int target_rank = target >> 3;
            if (std::abs(target_file - source_file) > 1) {
                continue;
            }
            if (
                (color == WHITE && target_rank > source_rank)
                || (color == BLACK && target_rank < source_rank)
            ) {
                blocked = true;
                break;
            }
        }
        if (!blocked) {
            passed |= bit(source);
        }
    }
    return passed;
}

[[nodiscard]] int teacher_passed_advance(
    Bitboard passed,
    bool color
) noexcept {
    int value = 0;
    while (passed != 0) {
        const int source = static_cast<int>(std::countr_zero(passed));
        passed &= passed - 1;
        const int rank = source >> 3;
        value += color == WHITE ? rank - 1 : 6 - rank;
    }
    return value;
}

[[nodiscard]] int teacher_connected_passed(Bitboard passed) noexcept {
    std::array<bool, 8> files{};
    while (passed != 0) {
        const int source = static_cast<int>(std::countr_zero(passed));
        passed &= passed - 1;
        files[static_cast<std::size_t>(source & 7)] = true;
    }
    int connected = 0;
    for (std::size_t file = 0; file < files.size(); ++file) {
        if (!files[file]) {
            continue;
        }
        connected += (
            (file > 0 && files[file - 1])
            || (file + 1 < files.size() && files[file + 1])
        ) ? 1 : 0;
    }
    return connected;
}

[[nodiscard]] int teacher_rook_open_files(
    const BoardState& board,
    bool color
) noexcept {
    std::array<bool, 8> pawn_files{};
    Bitboard pawns = board.pawns;
    while (pawns != 0) {
        const int source = static_cast<int>(std::countr_zero(pawns));
        pawns &= pawns - 1;
        pawn_files[static_cast<std::size_t>(source & 7)] = true;
    }
    Bitboard rooks = board.rooks & board.occupied[color ? 1 : 0];
    int count = 0;
    while (rooks != 0) {
        const int source = static_cast<int>(std::countr_zero(rooks));
        rooks &= rooks - 1;
        count += !pawn_files[static_cast<std::size_t>(source & 7)] ? 1 : 0;
    }
    return count;
}

[[nodiscard]] int teacher_rook_seventh(
    const BoardState& board,
    bool color
) noexcept {
    Bitboard rooks = board.rooks & board.occupied[color ? 1 : 0];
    const int target_rank = color == WHITE ? 6 : 1;
    int count = 0;
    while (rooks != 0) {
        const int source = static_cast<int>(std::countr_zero(rooks));
        rooks &= rooks - 1;
        count += (source >> 3) == target_rank ? 1 : 0;
    }
    return count;
}

[[nodiscard]] int teacher_king_shelter(
    const BoardState& board,
    bool color
) noexcept {
    const Position position = evaluation_position(board);
    const int king = king_square(position, color);
    if (king < 0) {
        return 0;
    }
    const int king_file = king & 7;
    const int king_rank = king >> 3;
    const int direction = color == WHITE ? 1 : -1;
    const Bitboard own_pawns = board.pawns & board.occupied[color ? 1 : 0];
    int shield = 0;
    for (
        int file = std::max(0, king_file - 1);
        file <= std::min(7, king_file + 1);
        ++file
    ) {
        for (const auto [distance, value] : {
                 std::pair<int, int>{1, 2},
                 std::pair<int, int>{2, 1},
             }) {
            const int rank = king_rank + direction * distance;
            if (inside(file, rank) && (own_pawns & bit(square(file, rank))) != 0) {
                shield += value;
            }
        }
    }
    return shield;
}

[[nodiscard]] bool teacher_is_pinned(
    const BoardState& board,
    bool color,
    int target
) noexcept {
    const Position position = evaluation_position(board);
    const int king = king_square(position, color);
    if (king < 0 || king == target) {
        return false;
    }
    const int king_file = king & 7;
    const int king_rank = king >> 3;
    const int target_file = target & 7;
    const int target_rank = target >> 3;
    const int file_delta = target_file - king_file;
    const int rank_delta = target_rank - king_rank;
    const bool orthogonal = file_delta == 0 || rank_delta == 0;
    const bool diagonal = std::abs(file_delta) == std::abs(rank_delta);
    if (!orthogonal && !diagonal) {
        return false;
    }
    const int file_step = (file_delta > 0) - (file_delta < 0);
    const int rank_step = (rank_delta > 0) - (rank_delta < 0);
    const Bitboard occupancy = board.occupied[0] | board.occupied[1];
    int file = king_file + file_step;
    int rank = king_rank + rank_step;
    while (inside(file, rank) && square(file, rank) != target) {
        if ((occupancy & bit(square(file, rank))) != 0) {
            return false;
        }
        file += file_step;
        rank += rank_step;
    }
    if (!inside(file, rank)) {
        return false;
    }
    file += file_step;
    rank += rank_step;
    while (inside(file, rank)) {
        const int sniper = square(file, rank);
        if ((occupancy & bit(sniper)) == 0) {
            file += file_step;
            rank += rank_step;
            continue;
        }
        if ((board.occupied[(!color) ? 1 : 0] & bit(sniper)) == 0) {
            return false;
        }
        const int piece_type = piece_type_at(position, sniper);
        return piece_type == QUEEN
            || (orthogonal && piece_type == ROOK)
            || (diagonal && piece_type == BISHOP);
    }
    return false;
}

struct TeacherAttackMaterial {
    std::int64_t attacked = 0;
    std::int64_t hanging = 0;
    std::int64_t pinned = 0;
    std::int64_t queen_exposed = 0;
};

[[nodiscard]] TeacherAttackMaterial teacher_material_under_attack(
    const BoardState& board,
    bool color
) noexcept {
    const Position position = evaluation_position(board);
    const Bitboard own = board.occupied[color ? 1 : 0];
    TeacherAttackMaterial result;
    for (int piece_type = PAWN; piece_type < KING; ++piece_type) {
        Bitboard pieces = own;
        switch (piece_type) {
            case PAWN: pieces &= board.pawns; break;
            case KNIGHT: pieces &= board.knights; break;
            case BISHOP: pieces &= board.bishops; break;
            case ROOK: pieces &= board.rooks; break;
            case QUEEN: pieces &= board.queens; break;
            default: pieces = 0; break;
        }
        while (pieces != 0) {
            const int source = static_cast<int>(std::countr_zero(pieces));
            pieces &= pieces - 1;
            const int attackers = teacher_attackers_count(board, !color, source);
            const int defenders = teacher_attackers_count(board, color, source);
            const int value = PIECE_VALUES[static_cast<std::size_t>(piece_type)];
            if (attackers != 0) {
                result.attacked += value;
                if (defenders == 0) {
                    result.hanging += value;
                }
                if (piece_type == QUEEN) {
                    result.queen_exposed = 1;
                }
            }
            if (teacher_is_pinned(board, color, source)) {
                result.pinned += value;
            }
        }
    }
    return result;
}

[[nodiscard]] std::vector<int> teacher_ep_after_move(
    const ExpandedMove& expanded
) {
    if (
        expanded.is_pawn_move
        && std::abs(
            static_cast<int>(expanded.move.to_square)
            - static_cast<int>(expanded.move.from_square)
        ) == 16
    ) {
        return {
            (static_cast<int>(expanded.move.to_square)
             + static_cast<int>(expanded.move.from_square)) / 2,
        };
    }
    return {};
}

[[nodiscard]] bool teacher_delivered_mate(
    const ExpandedMove& expanded
) {
    return expanded.delivered_check
        && !has_legal_move(expanded.child, teacher_ep_after_move(expanded));
}

[[nodiscard]] std::array<std::int64_t, 9> teacher_direct_threats(
    const BoardState& boundary,
    const std::vector<int>& ep_targets,
    std::int64_t series_number,
    bool include_two_move,
    std::uint64_t& direct_work,
    std::uint64_t& two_move_work
) {
    const bool mover = boundary.white_to_move;
    const std::int64_t sign = mover == WHITE ? 1 : -1;
    std::array<std::int64_t, 9> values{};
    const auto direct = expand_legal_move_variants(boundary, ep_targets);
    direct_work = direct.size();
    for (const ExpandedMove& first : direct) {
        const int capture_value = expanded_capture_value(boundary, first);
        if (capture_value != 0) {
            ++values[0];
            values[1] += capture_value;
            values[2] = std::max<std::int64_t>(values[2], capture_value);
        }
        values[3] += first.delivered_check ? 1 : 0;
        values[4] += teacher_delivered_mate(first) ? 1 : 0;
        values[5] += first.move.promotion != 0 ? 1 : 0;
        if (!include_two_move || first.delivered_check || series_number < 2) {
            continue;
        }

        BoardState same_mover = first.child;
        same_mover.white_to_move = mover;
        const auto second = expand_legal_move_variants(same_mover, {});
        two_move_work += second.size();
        bool route_has_check = false;
        bool route_has_mate = false;
        int route_capture = 0;
        for (const ExpandedMove& reply : second) {
            route_capture = std::max(
                route_capture,
                expanded_capture_value(same_mover, reply)
            );
            route_has_check = route_has_check || reply.delivered_check;
            route_has_mate = route_has_mate || teacher_delivered_mate(reply);
        }
        values[6] = std::max<std::int64_t>(values[6], route_capture);
        values[7] += route_has_check ? 1 : 0;
        values[8] += route_has_mate ? 1 : 0;
    }
    for (std::int64_t& value : values) {
        value *= sign;
    }
    return values;
}

[[nodiscard]] int teacher_route_value(const ReachProbe& reach) noexcept {
    if (!reach.distance.has_value()) {
        return 0;
    }
    return 4 - static_cast<int>(std::min<std::int64_t>(
        4,
        std::max<std::int64_t>(0, *reach.distance)
    ));
}

[[nodiscard]] int teacher_ring_attacks(
    const BoardState& board,
    bool attacker,
    bool king_color
) noexcept {
    const Position position = evaluation_position(board);
    const int king = king_square(position, king_color);
    if (king < 0) {
        return 0;
    }
    Bitboard ring = KING_ATTACK_MASKS[static_cast<std::size_t>(king)] | bit(king);
    int total = 0;
    while (ring != 0) {
        const int target = static_cast<int>(std::countr_zero(ring));
        ring &= ring - 1;
        total += teacher_attackers_count(board, attacker, target);
    }
    return total;
}

[[nodiscard]] int teacher_promotable(
    const BoardState& board,
    bool color,
    std::int64_t series_number
) noexcept {
    const std::int64_t budget = series_number
        + (board.white_to_move == color ? 0 : 1);
    const int target_rank = color == WHITE ? 7 : 0;
    const int direction = color == WHITE ? 1 : -1;
    const Bitboard occupancy = board.occupied[0] | board.occupied[1];
    Bitboard pawns = board.pawns & board.occupied[color ? 1 : 0];
    int count = 0;
    while (pawns != 0) {
        const int source = static_cast<int>(std::countr_zero(pawns));
        pawns &= pawns - 1;
        const int file = source & 7;
        const int rank = source >> 3;
        const int distance = std::abs(target_rank - rank);
        bool blocked = false;
        for (
            int next_rank = rank + direction;
            next_rank != target_rank + direction;
            next_rank += direction
        ) {
            if ((occupancy & bit(square(file, next_rank))) != 0) {
                blocked = true;
                break;
            }
        }
        count += !blocked && distance <= budget ? 1 : 0;
    }
    return count;
}

[[nodiscard]] int teacher_king_edge_distance(
    const BoardState& board,
    bool color
) noexcept {
    const int king = king_square(evaluation_position(board), color);
    if (king < 0) {
        return 0;
    }
    const int file = king & 7;
    const int rank = king >> 3;
    return std::min({file, 7 - file, rank, 7 - rank});
}

[[nodiscard]] int teacher_control(
    const BoardState& board,
    bool color,
    Bitboard targets
) noexcept {
    int value = 0;
    while (targets != 0) {
        const int target = static_cast<int>(std::countr_zero(targets));
        targets &= targets - 1;
        value += teacher_attackers_count(board, color, target);
    }
    return value;
}

[[nodiscard]] bool teacher_model_feature_count(std::size_t count) noexcept {
    return count == 7
        || count == 14
        || count == 19
        || count == 38
        || count == 44
        || count == TEACHER_VALUE_FEATURE_COUNT;
}

}  // namespace

std::optional<TeacherValueFeaturesV3> teacher_value_features_v3(
    const BoardState& board,
    const std::vector<int>& ep_targets,
    std::int64_t series_number,
    std::uint64_t max_reach_positions,
    std::size_t feature_count
) {
    if (!teacher_model_feature_count(feature_count)) {
        return std::nullopt;
    }
    const FullWeights unit_weights{100, 100, 100, 100, 100, 100, 100};
    const auto base = full_evaluate(
        board,
        ep_targets,
        series_number,
        max_reach_positions,
        unit_weights
    );
    if (!base.has_value()) {
        return std::nullopt;
    }

    TeacherValueFeaturesV3 result;
    result.white_reach = base->white_reach;
    result.black_reach = base->black_reach;
    result.values[0] = base->material;
    result.values[1] = base->king_space;
    result.values[2] = base->series_reach;
    result.values[3] = base->promotion_corridors;
    result.values[4] = base->immediate_vulnerability;
    result.values[5] = base->useful_mobility;
    result.values[6] = base->boundary_check;
    if (feature_count == 7) {
        return result;
    }

    const std::int64_t centered_phase = std::min<std::int64_t>(
        series_number,
        10
    ) - 4;
    for (std::size_t index = 0; index < 7; ++index) {
        if (!checked_multiply(
                result.values[index],
                centered_phase,
                result.values[index + 7]
            )) {
            return std::nullopt;
        }
    }
    if (feature_count == 14) {
        return result;
    }

    result.values[14] = teacher_ring_attacks(board, WHITE, BLACK)
        - teacher_ring_attacks(board, BLACK, WHITE);
    result.values[15] = teacher_promotable(board, WHITE, series_number)
        - teacher_promotable(board, BLACK, series_number);
    result.values[16] = teacher_king_edge_distance(board, BLACK)
        - teacher_king_edge_distance(board, WHITE);
    result.values[17] = teacher_route_value(base->white_reach)
        - teacher_route_value(base->black_reach);
    result.values[18] = (
        base->white_reach.complete && base->black_reach.complete
    ) ? 1 : 0;
    if (feature_count == 19) {
        return result;
    }

    constexpr std::array<int, 4> CENTER = {
        square(3, 3), square(4, 3), square(3, 4), square(4, 4),
    };
    Bitboard center = 0;
    for (const int target : CENTER) {
        center |= bit(target);
    }
    Bitboard extended_center = 0;
    for (int file = 2; file < 6; ++file) {
        for (int rank = 2; rank < 6; ++rank) {
            extended_center |= bit(square(file, rank));
        }
    }
    result.values[19] = teacher_minor_development(board, WHITE)
        - teacher_minor_development(board, BLACK);
    const Position position = evaluation_position(board);
    for (const int target : CENTER) {
        const Bitboard target_bit = bit(target);
        if (((board.occupied[0] | board.occupied[1]) & target_bit) == 0) {
            continue;
        }
        const bool color = (board.occupied[1] & target_bit) != 0;
        const int multiplier = piece_type_at(position, target) == PAWN ? 2 : 1;
        result.values[20] += color == WHITE ? multiplier : -multiplier;
    }
    result.values[21] = teacher_control(board, WHITE, center)
        - teacher_control(board, BLACK, center);
    result.values[22] = teacher_control(board, WHITE, extended_center)
        - teacher_control(board, BLACK, extended_center);

    Bitboard white_pawns = board.pawns & board.occupied[1];
    Bitboard black_pawns = board.pawns & board.occupied[0];
    while (white_pawns != 0) {
        const int source = static_cast<int>(std::countr_zero(white_pawns));
        white_pawns &= white_pawns - 1;
        result.values[23] += (source >> 3) - 1;
    }
    while (black_pawns != 0) {
        const int source = static_cast<int>(std::countr_zero(black_pawns));
        black_pawns &= black_pawns - 1;
        result.values[23] -= 6 - (source >> 3);
    }

    const Bitboard white_passed = teacher_passed_pawns(board, WHITE);
    const Bitboard black_passed = teacher_passed_pawns(board, BLACK);
    result.values[24] = std::popcount(white_passed) - std::popcount(black_passed);
    result.values[25] = teacher_passed_advance(white_passed, WHITE)
        - teacher_passed_advance(black_passed, BLACK);
    result.values[26] = teacher_connected_passed(white_passed)
        - teacher_connected_passed(black_passed);

    const auto white_files = teacher_pawn_file_counts(board, WHITE);
    const auto black_files = teacher_pawn_file_counts(board, BLACK);
    for (std::size_t file = 0; file < white_files.size(); ++file) {
        const bool white_isolated = white_files[file] != 0
            && (file == 0 || white_files[file - 1] == 0)
            && (file + 1 == white_files.size() || white_files[file + 1] == 0);
        const bool black_isolated = black_files[file] != 0
            && (file == 0 || black_files[file - 1] == 0)
            && (file + 1 == black_files.size() || black_files[file + 1] == 0);
        result.values[27] -= white_isolated ? white_files[file] : 0;
        result.values[27] += black_isolated ? black_files[file] : 0;
        result.values[28] -= std::max(0, white_files[file] - 1);
        result.values[28] += std::max(0, black_files[file] - 1);
    }
    result.values[29] = -teacher_pawn_islands(white_files)
        + teacher_pawn_islands(black_files);
    result.values[30] = (
        std::popcount(board.bishops & board.occupied[1]) >= 2 ? 1 : 0
    ) - (
        std::popcount(board.bishops & board.occupied[0]) >= 2 ? 1 : 0
    );
    result.values[31] = teacher_rook_open_files(board, WHITE)
        - teacher_rook_open_files(board, BLACK);
    result.values[32] = teacher_rook_seventh(board, WHITE)
        - teacher_rook_seventh(board, BLACK);
    result.values[33] = teacher_king_shelter(board, WHITE)
        - teacher_king_shelter(board, BLACK);

    const TeacherAttackMaterial white_attack = teacher_material_under_attack(
        board,
        WHITE
    );
    const TeacherAttackMaterial black_attack = teacher_material_under_attack(
        board,
        BLACK
    );
    result.values[34] = black_attack.attacked - white_attack.attacked;
    result.values[35] = black_attack.hanging - white_attack.hanging;
    result.values[36] = black_attack.pinned - white_attack.pinned;
    result.values[37] = black_attack.queen_exposed - white_attack.queen_exposed;
    if (feature_count == 38) {
        return result;
    }

    const auto threats = teacher_direct_threats(
        board,
        ep_targets,
        series_number,
        feature_count == TEACHER_VALUE_FEATURE_COUNT,
        result.direct_move_variants,
        result.two_move_variants
    );
    std::copy(threats.begin(), threats.end(), result.values.begin() + 38);
    return result;
}

std::optional<std::int64_t> deep_teacher_score_v1(
    const TeacherValueFeaturesV3& features,
    const DeepTeacherLinearModelV1& model
) noexcept {
    if (
        model.fixed_point_scale != DEEP_TEACHER_FIXED_POINT_SCALE
        || !teacher_model_feature_count(model.feature_count)
    ) {
        return std::nullopt;
    }
    std::int64_t total = 0;
    for (std::size_t index = 0; index < model.feature_count; ++index) {
        std::int64_t term = 0;
        if (
            !checked_multiply(
                features.values[index],
                model.coefficients[index],
                term
            )
            || !checked_add(total, term, total)
        ) {
            return std::nullopt;
        }
    }
    return total;
}

bool root_candidate_is_proven_adverse_v1(
    const bool mover_white,
    const std::array<int, 2>& proof_bounds
) noexcept {
    const int opponent = mover_white ? -1 : 1;
    return proof_bounds[0] == opponent && proof_bounds[1] == opponent;
}

bool proof_aware_root_precedes_v1(
    const bool mover_white,
    const ProofAwareRootCandidateV1& left,
    const ProofAwareRootCandidateV1& right
) noexcept {
    const bool left_adverse = root_candidate_is_proven_adverse_v1(
        mover_white,
        left.proof_bounds
    );
    const bool right_adverse = root_candidate_is_proven_adverse_v1(
        mover_white,
        right.proof_bounds
    );
    if (left_adverse != right_adverse) {
        return !left_adverse;
    }
    if (left.score != right.score) {
        return mover_white ? left.score > right.score : left.score < right.score;
    }
    return left.machine_notation < right.machine_notation;
}

std::uint16_t legal_move_uci_key(const LegalMove& move) noexcept {
    std::uint16_t promotion = 0;
    switch (move.promotion) {
        case BISHOP:
            promotion = 1;
            break;
        case KNIGHT:
            promotion = 2;
            break;
        case QUEEN:
            promotion = 3;
            break;
        case ROOK:
            promotion = 4;
            break;
        default:
            break;
    }
    const std::uint16_t from_file = static_cast<std::uint16_t>(
        move.from_square & 7
    );
    const std::uint16_t from_rank = static_cast<std::uint16_t>(
        move.from_square >> 3
    );
    const std::uint16_t to_file = static_cast<std::uint16_t>(
        move.to_square & 7
    );
    const std::uint16_t to_rank = static_cast<std::uint16_t>(
        move.to_square >> 3
    );
    return static_cast<std::uint16_t>(
        (((from_file * 8 + from_rank) * 8 + to_file) * 8 + to_rank) * 5
        + promotion
    );
}

std::string legal_move_uci(const LegalMove& move) {
    std::string result;
    result.reserve(move.promotion == 0 ? 4 : 5);
    result.push_back(static_cast<char>('a' + (move.from_square & 7)));
    result.push_back(static_cast<char>('1' + (move.from_square >> 3)));
    result.push_back(static_cast<char>('a' + (move.to_square & 7)));
    result.push_back(static_cast<char>('1' + (move.to_square >> 3)));
    if (move.promotion != 0) {
        constexpr std::array<char, 7> SYMBOLS = {
            '\0', 'p', 'n', 'b', 'r', 'q', 'k',
        };
        result.push_back(SYMBOLS[move.promotion]);
    }
    return result;
}

std::vector<ExpandedMove> expand_legal_move_variants(
    const BoardState& position,
    const std::vector<int>& ep_targets
) {
    std::vector<ExpandedMove> legal;
    MoveList moves = pseudo_moves(position, ep_targets);
    const auto move_key = [](const Move& move) noexcept {
        return legal_move_uci_key(LegalMove{
            move.from,
            move.to,
            move.promotion,
            move.required_ep_square,
        });
    };
    std::sort(
        moves.begin(),
        moves.end(),
        [&move_key](const Move& left, const Move& right) {
            return move_key(left) < move_key(right);
        }
    );
    legal.reserve(moves.size());
    const bool mover = position.white_to_move;
    const Bitboard enemy = position.occupied[(!mover) ? 1 : 0];
    const Position evaluation = evaluation_position(position);
    std::optional<std::uint16_t> previous_legal_key;
    for (const Move& move : moves) {
        BoardState child = apply_move(position, move);
        const Bitboard own_king = child.kings
            & child.occupied[mover ? 1 : 0];
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
        const std::uint16_t key = move_key(move);
        if (previous_legal_key == key) {
            continue;
        }
        previous_legal_key = key;
        const int moving_piece = piece_type_at(evaluation, move.from);
        const bool en_passant = move.required_ep_square >= 0
            && moving_piece == PAWN
            && move.to == move.required_ep_square
            && (position.occupied[0] & bit(move.to)) == 0
            && (position.occupied[1] & bit(move.to)) == 0;
        const bool is_capture = en_passant || (enemy & bit(move.to)) != 0;
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
                static_cast<std::int8_t>(move.from),
                static_cast<std::int8_t>(move.to),
                static_cast<std::int8_t>(move.promotion),
                static_cast<std::int8_t>(move.required_ep_square),
            },
            child,
            moving_piece == PAWN,
            is_capture,
            delivered_check,
        });
    }
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

using NativeSeriesOutcome = CompleteSeriesOutcome;

enum class TacticalKind : std::uint8_t {
    Mate = 0,
    Check = 1,
    Promotion = 2,
    Capture = 3,
};

struct TacticalOpportunity {
    TacticalKind kind;
    std::string signature;

    bool operator==(const TacticalOpportunity&) const = default;

    bool operator<(const TacticalOpportunity& other) const noexcept {
        if (kind != other.kind) {
            return kind < other.kind;
        }
        return signature < other.signature;
    }
};

struct TacticalMoveSummary {
    std::int64_t checks = 0;
    std::int64_t immediate_mates = 0;
    std::int64_t captures = 0;
    std::int64_t promotions = 0;
    std::vector<TacticalOpportunity> opportunities;
};

[[nodiscard]] TacticalMoveSummary summarize_tactical_moves(
    const BoardState& board,
    bool collect_opportunities
) {
    TacticalMoveSummary summary;
    const bool mover = board.white_to_move;
    const Bitboard enemy = board.occupied[(!mover) ? 1 : 0];
    const Position evaluation = evaluation_position(board);
    for (const Move& move : pseudo_moves(board, {})) {
        BoardState child = apply_move(board, move);
        const Bitboard own_king = child.kings
            & child.occupied[mover ? 1 : 0];
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
        const bool is_pawn_move = moving_piece == PAWN;
        const bool en_passant = move.required_ep_square >= 0
            && is_pawn_move
            && move.to == move.required_ep_square
            && (board.occupied[0] & bit(move.to)) == 0
            && (board.occupied[1] & bit(move.to)) == 0;
        const bool is_capture = en_passant || (enemy & bit(move.to)) != 0;
        summary.captures += is_capture ? 1 : 0;
        summary.promotions += move.promotion != 0 ? 1 : 0;

        std::string signature;
        bool signature_ready = false;
        const auto add_opportunity = [&](TacticalKind kind) {
            if (!collect_opportunities) {
                return;
            }
            if (!signature_ready) {
                signature = move_uci(move);
                signature_ready = true;
            }
            summary.opportunities.push_back(TacticalOpportunity{
                kind,
                signature,
            });
        };

        const Bitboard opponent_king = child.kings
            & child.occupied[(!mover) ? 1 : 0];
        const bool delivered_check = opponent_king != 0
            && board_attacked_by(
                child,
                static_cast<int>(std::countr_zero(opponent_king)),
                mover
            );
        if (delivered_check) {
            ++summary.checks;
            std::vector<int> child_ep_targets;
            if (is_pawn_move && std::abs(move.to - move.from) == 16) {
                child_ep_targets.push_back((move.from + move.to) / 2);
            }
            const bool mate = !has_legal_move(child, child_ep_targets);
            summary.immediate_mates += mate ? 1 : 0;
            add_opportunity(mate ? TacticalKind::Mate : TacticalKind::Check);
        }
        if (move.promotion != 0) {
            add_opportunity(TacticalKind::Promotion);
        }
        if (is_capture) {
            add_opportunity(TacticalKind::Capture);
        }
    }
    return summary;
}

struct FrontierInspection {
    std::int64_t score = 0;
    std::vector<TacticalOpportunity> opportunities;
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
        return seed;
    }
};

using PackedUciMove = std::uint16_t;

[[nodiscard]] PackedUciMove pack_uci_move(
    const LegalMove& move
) noexcept {
    return legal_move_uci_key(move);
}

[[nodiscard]] std::string unpack_uci_move(PackedUciMove packed) {
    constexpr std::array<char, 5> PROMOTIONS = {'\0', 'b', 'n', 'q', 'r'};
    const std::uint16_t promotion = packed % 5;
    packed = static_cast<PackedUciMove>(packed / 5);
    const std::uint16_t to_rank = packed % 8;
    packed = static_cast<PackedUciMove>(packed / 8);
    const std::uint16_t to_file = packed % 8;
    packed = static_cast<PackedUciMove>(packed / 8);
    const std::uint16_t from_rank = packed % 8;
    const std::uint16_t from_file = packed / 8;

    std::string result;
    result.reserve(promotion == 0 ? 4 : 5);
    result.push_back(static_cast<char>('a' + from_file));
    result.push_back(static_cast<char>('1' + from_rank));
    result.push_back(static_cast<char>('a' + to_file));
    result.push_back(static_cast<char>('1' + to_rank));
    if (promotion != 0) {
        result.push_back(PROMOTIONS[promotion]);
    }
    return result;
}

class CompactMovePath {
public:
    static constexpr std::size_t INLINE_CAPACITY = 8;

    CompactMovePath() = default;

    CompactMovePath(const CompactMovePath& other) {
        copy_from(other);
    }

    CompactMovePath(CompactMovePath&& other) noexcept {
        move_from(other);
    }

    CompactMovePath& operator=(const CompactMovePath& other) {
        if (this != &other) {
            CompactMovePath copy(other);
            *this = std::move(copy);
        }
        return *this;
    }

    CompactMovePath& operator=(CompactMovePath&& other) noexcept {
        if (this != &other) {
            release_heap();
            size_ = 0;
            capacity_ = INLINE_CAPACITY;
            inline_moves_ = {};
            move_from(other);
        }
        return *this;
    }

    ~CompactMovePath() {
        release_heap();
    }

    [[nodiscard]] bool empty() const noexcept {
        return size_ == 0;
    }

    [[nodiscard]] std::size_t size() const noexcept {
        return size_;
    }

    [[nodiscard]] PackedUciMove operator[](std::size_t index) const noexcept {
        return data()[index];
    }

    [[nodiscard]] PackedUciMove back() const noexcept {
        return data()[size_ - 1];
    }

    void push_back(const LegalMove& move) {
        push_back(pack_uci_move(move));
    }

    [[nodiscard]] std::vector<std::string> to_uci_vector() const {
        std::vector<std::string> result;
        result.reserve(size_);
        for (std::size_t index = 0; index < size_; ++index) {
            result.push_back(unpack_uci_move(data()[index]));
        }
        return result;
    }

    friend bool operator==(
        const CompactMovePath& left,
        const CompactMovePath& right
    ) noexcept {
        return left.size_ == right.size_
            && std::equal(
                left.data(),
                left.data() + left.size_,
                right.data()
            );
    }

    friend bool operator<(
        const CompactMovePath& left,
        const CompactMovePath& right
    ) noexcept {
        return std::lexicographical_compare(
            left.data(),
            left.data() + left.size_,
            right.data(),
            right.data() + right.size_
        );
    }

private:
    void push_back(PackedUciMove move) {
        if (size_ == capacity_) {
            if (capacity_ > std::numeric_limits<std::uint32_t>::max() / 2) {
                throw std::length_error("progressive move path is too long");
            }
            const std::uint32_t next_capacity = capacity_ * 2;
            auto* replacement = new PackedUciMove[next_capacity];
            std::copy(data(), data() + size_, replacement);
            release_heap();
            heap_moves_ = replacement;
            capacity_ = next_capacity;
        }
        mutable_data()[size_] = move;
        ++size_;
    }

    [[nodiscard]] const PackedUciMove* data() const noexcept {
        return capacity_ <= INLINE_CAPACITY
            ? inline_moves_.data()
            : heap_moves_;
    }

    [[nodiscard]] PackedUciMove* mutable_data() noexcept {
        return capacity_ <= INLINE_CAPACITY
            ? inline_moves_.data()
            : heap_moves_;
    }

    void release_heap() noexcept {
        if (capacity_ > INLINE_CAPACITY) {
            delete[] heap_moves_;
            heap_moves_ = nullptr;
        }
    }

    void copy_from(const CompactMovePath& other) {
        size_ = other.size_;
        capacity_ = other.capacity_;
        if (other.capacity_ <= INLINE_CAPACITY) {
            inline_moves_ = other.inline_moves_;
            return;
        }
        auto* copy = new PackedUciMove[other.capacity_];
        std::copy(other.data(), other.data() + other.size_, copy);
        heap_moves_ = copy;
    }

    void move_from(CompactMovePath& other) noexcept {
        size_ = other.size_;
        capacity_ = other.capacity_;
        if (other.capacity_ <= INLINE_CAPACITY) {
            inline_moves_ = other.inline_moves_;
        } else {
            heap_moves_ = other.heap_moves_;
            other.heap_moves_ = nullptr;
            other.inline_moves_ = {};
        }
        other.size_ = 0;
        other.capacity_ = INLINE_CAPACITY;
    }

    std::array<PackedUciMove, INLINE_CAPACITY> inline_moves_{};
    PackedUciMove* heap_moves_ = nullptr;
    std::uint32_t size_ = 0;
    std::uint32_t capacity_ = INLINE_CAPACITY;
};

static_assert(sizeof(CompactMovePath) <= 32);

struct NativeFrontierState {
    BoardState board;
    CompactMovePath moves;
    Bitboard pending_ep_targets = 0;
    bool made_progress = false;
    std::uint64_t path_count = 1;
    std::int64_t halfmove_clock = 0;
    std::int64_t fullmove_number = 1;
    std::vector<TacticalOpportunity> tactical_provenance;
};

struct NativeCompletedSeries {
    BoardState board;
    CompactMovePath moves;
    std::vector<int> boundary_ep_targets;
    std::int64_t halfmove_clock;
    std::int64_t fullmove_number;
    std::int64_t series_number;
    std::int64_t quiet_series;
    NativeSeriesOutcome outcome = NativeSeriesOutcome::None;
    bool ended_by_check = false;
    std::uint64_t path_count = 1;
    std::vector<TacticalOpportunity> tactical_provenance;
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
        FrontierInspection,
        FrontierScoreIdentityHash
    > frontier_score_cache;
    std::uint64_t deadline_poll_counter = 0;

    bool deadline_reached(bool force = false) {
        if (!request.deadline.has_value()) {
            return false;
        }
        // A clock read per generated chess position is measurable at the
        // smallest series. Poll every 16 safe expansion boundaries instead;
        // force is used before and after whole-kernel phases.
        ++deadline_poll_counter;
        if (
            !force
            && (deadline_poll_counter & static_cast<std::uint64_t>(15)) != 0
        ) {
            return false;
        }
        if (std::chrono::steady_clock::now() < *request.deadline) {
            return false;
        }
        response.status = SeriesGenerationStatus::Deadline;
        response.message = "native complete-series deadline reached";
        return true;
    }

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

    bool add_path_count(std::uint64_t& target, std::uint64_t amount) {
        const std::uint64_t limit = request.path_count_saturation_limit;
        if (target <= limit && amount <= limit - target) {
            target += amount;
            return true;
        }
        if (
            request.path_count_overflow_mode
            != PathCountOverflowMode::Saturate
        ) {
            return unsupported("native series path counter overflow");
        }
        target = limit;
        if (
            response.stats.path_count_saturations
            != std::numeric_limits<std::uint64_t>::max()
        ) {
            ++response.stats.path_count_saturations;
        }
        return true;
    }

    bool charge_position() {
        if (deadline_reached()) {
            return false;
        }
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
        if (deadline_reached()) {
            return false;
        }
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

[[nodiscard]] std::optional<FrontierInspection> calculate_frontier_inspection(
    const CompleteSeriesRequest& request,
    const NativeFrontierState& state
) {
    if (!request.frontier_weights.has_value()) {
        return FrontierInspection{};
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

    auto tactical_summary = summarize_tactical_moves(
        board,
        request.tactical_protection
    );
    std::int64_t tactical = 0;
    std::int64_t term = 0;
    if (
        !checked_multiply(tactical_summary.immediate_mates, 5'000'000, term)
        || !checked_add(tactical, term, tactical)
        || !checked_multiply(tactical_summary.checks, 50'000, term)
        || !checked_add(tactical, term, tactical)
        || !checked_multiply(tactical_summary.promotions, 2'000, term)
        || !checked_add(tactical, term, tactical)
        || !checked_multiply(tactical_summary.captures, 100, term)
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
    std::sort(
        tactical_summary.opportunities.begin(),
        tactical_summary.opportunities.end()
    );
    tactical_summary.opportunities.erase(
        std::unique(
            tactical_summary.opportunities.begin(),
            tactical_summary.opportunities.end()
        ),
        tactical_summary.opportunities.end()
    );
    return FrontierInspection{
        combined,
        std::move(tactical_summary.opportunities),
    };
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
    };
    const auto cached = context.frontier_score_cache.find(key);
    if (cached != context.frontier_score_cache.end()) {
        score = cached->second.score;
        return true;
    }
    if (!context.charge_frontier_score()) {
        return false;
    }
    auto calculated = calculate_frontier_inspection(context.request, state);
    if (!calculated.has_value()) {
        return context.unsupported("native frontier score overflow");
    }
    score = calculated->score;
    context.frontier_score_cache.emplace(key, std::move(*calculated));
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
    constexpr std::size_t PARALLEL_SCORE_THRESHOLD = 64;
    const bool parallel =
        context.request.worker_threads > 1
        && context.request.frontier_weights.has_value()
        && frontier.size() >= PARALLEL_SCORE_THRESHOLD;
    if (!parallel) {
        for (auto& item : frontier) {
            if (context.deadline_reached()) {
                return false;
            }
            std::int64_t score = 0;
            if (!frontier_score(context, item, score)) {
                return false;
            }
            ranked.push_back(RankedState{std::move(item), score});
        }
    } else {
        struct PendingScore {
            FrontierScoreIdentity identity;
            const NativeFrontierState* state;
        };
        const std::size_t no_pending = std::numeric_limits<std::size_t>::max();
        std::vector<std::int64_t> scores(frontier.size(), 0);
        std::vector<std::size_t> score_sources(frontier.size(), no_pending);
        std::vector<PendingScore> pending;
        std::unordered_map<
            FrontierScoreIdentity,
            std::size_t,
            FrontierScoreIdentityHash
        > pending_indices;
        pending.reserve(frontier.size());
        pending_indices.reserve(frontier.size());

        // Preserve the serial cache/work-accounting order exactly. Only the
        // pure score calculation for unique misses executes concurrently.
        for (std::size_t index = 0; index < frontier.size(); ++index) {
            if (context.deadline_reached()) {
                return false;
            }
            const auto& item = frontier[index];
            const FrontierScoreIdentity identity{
                board_identity(item.board),
            };
            const auto cached = context.frontier_score_cache.find(identity);
            if (cached != context.frontier_score_cache.end()) {
                scores[index] = cached->second.score;
                continue;
            }
            const auto [found, inserted] = pending_indices.emplace(
                identity,
                pending.size()
            );
            score_sources[index] = found->second;
            if (!inserted) {
                continue;
            }
            if (!context.charge_frontier_score()) {
                return false;
            }
            pending.push_back(PendingScore{identity, &item});
        }

        std::vector<std::optional<FrontierInspection>> calculated(
            pending.size()
        );
        std::vector<std::int64_t> pending_scores(pending.size());
        std::atomic<bool> deadline_cancelled{false};
        BoundedNativePool::instance().run(
            pending.size(),
            context.request.worker_threads,
            [&](std::size_t index) {
                if (
                    deadline_cancelled.load(std::memory_order_relaxed)
                    || (
                        context.request.deadline.has_value()
                        && std::chrono::steady_clock::now()
                            >= *context.request.deadline
                    )
                ) {
                    deadline_cancelled.store(true, std::memory_order_relaxed);
                    return;
                }
                calculated[index] = calculate_frontier_inspection(
                    context.request,
                    *pending[index].state
                );
            }
        );
        if (deadline_cancelled.load(std::memory_order_relaxed)) {
            static_cast<void>(context.deadline_reached(true));
            return false;
        }
        if (context.deadline_reached(true)) {
            return false;
        }
        for (std::size_t index = 0; index < pending.size(); ++index) {
            if (!calculated[index].has_value()) {
                return context.unsupported("native frontier score overflow");
            }
            pending_scores[index] = calculated[index]->score;
            context.frontier_score_cache.emplace(
                pending[index].identity,
                std::move(*calculated[index])
            );
        }
        for (std::size_t index = 0; index < frontier.size(); ++index) {
            if (score_sources[index] != no_pending) {
                scores[index] = pending_scores[score_sources[index]];
            }
            ranked.push_back(RankedState{
                std::move(frontier[index]),
                scores[index],
            });
        }
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
    if (!context.add_path_count(stats.raw_series, completed.path_count)) {
        return false;
    }
    if (
        completed.ended_by_check
        && !context.add_path_count(stats.checking_series, completed.path_count)
    ) {
        return false;
    }
    if (
        completed.outcome == NativeSeriesOutcome::Checkmate
        && !context.add_path_count(stats.checkmates, completed.path_count)
    ) {
        return false;
    }
    if (
        completed.outcome == NativeSeriesOutcome::Stalemate
        && !context.add_path_count(stats.stalemates, completed.path_count)
    ) {
        return false;
    }
    if (
        context.request.stop_on_mover_mate
        && completed.outcome == NativeSeriesOutcome::Checkmate
        && completed.ended_by_check
    ) {
        context.response.stopped_on_mover_mate = true;
    }
    context.completed.push_back(std::move(completed));
    return true;
}

[[nodiscard]] std::string tactical_signature(
    std::size_t ply,
    const std::string& uci
);

[[nodiscard]] NativeCompletedSeries finish_series(
    const CompleteSeriesRequest& request,
    const NativeFrontierState& state,
    BoardState board,
    CompactMovePath moves,
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
    std::vector<TacticalOpportunity> tactical_provenance;
    if (request.tactical_protection) {
        tactical_provenance = state.tactical_provenance;
        if (delivered_check && !moves.empty()) {
            tactical_provenance.push_back(TacticalOpportunity{
                outcome == NativeSeriesOutcome::Checkmate
                    ? TacticalKind::Mate
                    : TacticalKind::Check,
                tactical_signature(
                    moves.size(),
                    unpack_uci_move(moves.back())
                ),
            });
            std::sort(tactical_provenance.begin(), tactical_provenance.end());
            tactical_provenance.erase(
                std::unique(
                    tactical_provenance.begin(),
                    tactical_provenance.end()
                ),
                tactical_provenance.end()
            );
        }
    }
    return NativeCompletedSeries{
        board,
        std::move(moves),
        ep_targets,
        state.halfmove_clock,
        state.fullmove_number,
        request.series_number + 1,
        quiet_series,
        outcome,
        delivered_check,
        state.path_count,
        std::move(tactical_provenance),
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
        state.halfmove_clock,
        state.fullmove_number,
        request.series_number,
        request.quiet_series,
        board_in_check(state.board)
            ? NativeSeriesOutcome::Checkmate
            : NativeSeriesOutcome::Stalemate,
        false,
        state.path_count,
        request.tactical_protection
            ? state.tactical_provenance
            : std::vector<TacticalOpportunity>{},
    };
}

void record_played_tactical_provenance(
    NativeFrontierState& state,
    const ExpandedMove& expanded
) {
    const std::string signature = tactical_signature(
        state.moves.size(),
        legal_move_uci(expanded.move)
    );
    if (expanded.move.promotion != 0) {
        state.tactical_provenance.push_back(TacticalOpportunity{
            TacticalKind::Promotion,
            signature,
        });
    }
    if (expanded.is_capture) {
        state.tactical_provenance.push_back(TacticalOpportunity{
            TacticalKind::Capture,
            signature,
        });
    }
    std::sort(
        state.tactical_provenance.begin(),
        state.tactical_provenance.end()
    );
    state.tactical_provenance.erase(
        std::unique(
            state.tactical_provenance.begin(),
            state.tactical_provenance.end()
        ),
        state.tactical_provenance.end()
    );
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

[[nodiscard]] std::string tactical_signature(
    std::size_t ply,
    const std::string& uci
) {
    return std::to_string(ply) + ":" + uci;
}

constexpr std::size_t TACTICAL_FRONTIER_RESERVE_MAX = 64;
// Keep synchronized with TACTICAL_FINAL_ORDINARY_QUOTA_DENOMINATOR in
// search.py. Delivered terminal mates are seeded before this quota.
constexpr std::size_t FINAL_ORDINARY_QUOTA_DENOMINATOR = 2;

bool bound_ordinary_frontier(
    NativeGenerationContext& context,
    std::vector<NativeFrontierState>& frontier,
    std::size_t cap
) {
    struct RankedIndex {
        std::size_t index;
        std::int64_t score;
    };
    std::vector<RankedIndex> ranked;
    ranked.reserve(frontier.size());
    for (std::size_t index = 0; index < frontier.size(); ++index) {
        if (context.deadline_reached()) {
            return false;
        }
        std::int64_t score = 0;
        if (!frontier_score(context, frontier[index], score)) {
            return false;
        }
        ranked.push_back(RankedIndex{index, score});
    }

    const bool mover = context.request.board.white_to_move;
    const auto ranked_before =
        [&frontier, mover](const RankedIndex& left, const RankedIndex& right) {
            if (left.score != right.score) {
                return mover == WHITE
                    ? left.score > right.score
                    : left.score < right.score;
            }
            return frontier[left.index].moves < frontier[right.index].moves;
        };
    struct Group {
        PackedUciMove move;
        std::vector<RankedIndex> states;
    };
    std::vector<Group> groups;
    std::unordered_map<PackedUciMove, std::size_t> group_indices;
    const std::size_t prefix_length = context.request.required_prefix.size();
    for (const auto& item : ranked) {
        if (context.deadline_reached()) {
            return false;
        }
        const auto& state = frontier[item.index];
        const std::size_t group_index = std::min(
            prefix_length,
            state.moves.size() - 1
        );
        const PackedUciMove group_move = state.moves[group_index];
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
    std::vector<RankedIndex> chosen;
    chosen.reserve(cap);
    for (auto& group : groups) {
        const std::size_t retained = std::min(quota, group.states.size());
        std::partial_sort(
            group.states.begin(),
            group.states.begin() + retained,
            group.states.end(),
            ranked_before
        );
        chosen.insert(
            chosen.end(),
            group.states.begin(),
            group.states.begin() + retained
        );
    }
    std::sort(chosen.begin(), chosen.end(), ranked_before);
    if (chosen.size() > cap) {
        chosen.resize(cap);
    }

    std::vector<bool> selected_indices(frontier.size(), false);
    for (const auto& item : chosen) {
        selected_indices[item.index] = true;
    }
    if (chosen.size() < cap) {
        std::vector<RankedIndex> remaining;
        remaining.reserve(ranked.size() - chosen.size());
        for (const auto& item : ranked) {
            if (!selected_indices[item.index]) {
                remaining.push_back(item);
            }
        }
        const std::size_t fill_count = std::min(
            cap - chosen.size(),
            remaining.size()
        );
        std::partial_sort(
            remaining.begin(),
            remaining.begin() + fill_count,
            remaining.end(),
            ranked_before
        );
        for (std::size_t index = 0; index < fill_count; ++index) {
            selected_indices[remaining[index].index] = true;
            chosen.push_back(remaining[index]);
        }
        std::sort(chosen.begin(), chosen.end(), ranked_before);
    }

    const std::uint64_t discarded_count = static_cast<std::uint64_t>(
        frontier.size() - chosen.size()
    );
    std::uint64_t discarded_paths = 0;
    for (std::size_t index = 0; index < frontier.size(); ++index) {
        if (context.deadline_reached()) {
            return false;
        }
        if (
            !selected_indices[index]
            && !context.add_path_count(
                discarded_paths,
                frontier[index].path_count
            )
        ) {
            return false;
        }
    }
    auto& stats = context.response.stats;
    if (
        !context.add(stats.frontier_prunes, 1)
        || !context.add(stats.frontier_states_pruned, discarded_count)
        || !context.add_path_count(stats.frontier_paths_pruned, discarded_paths)
    ) {
        return false;
    }
    std::vector<NativeFrontierState> selected;
    selected.reserve(chosen.size());
    for (const auto& item : chosen) {
        selected.push_back(std::move(frontier[item.index]));
    }
    frontier = std::move(selected);
    return true;
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
    const std::size_t cap = static_cast<std::size_t>(
        *context.request.max_frontier_states
    );
    if (frontier.size() <= cap) {
        return order_frontier(context, frontier);
    }
    if (
        !context.request.tactical_protection
        && context.request.worker_threads <= 1
    ) {
        return bound_ordinary_frontier(context, frontier, cap);
    }
    if (!order_frontier(context, frontier)) {
        return false;
    }
    struct Group {
        PackedUciMove move;
        std::vector<NativeFrontierState> states;
    };
    std::vector<Group> groups;
    std::unordered_map<PackedUciMove, std::size_t> group_indices;
    const std::size_t prefix_length = context.request.required_prefix.size();
    for (const auto& item : frontier) {
        if (context.deadline_reached()) {
            return false;
        }
        const std::size_t group_index = std::min(
            prefix_length,
            item.moves.size() - 1
        );
        const PackedUciMove group_move = item.moves[group_index];
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
    std::set<CompactMovePath> selected_moves;
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

    if (context.request.tactical_protection) {
        struct Representative {
            TacticalOpportunity opportunity;
            std::size_t rank;
            const NativeFrontierState* state;
        };
        std::map<TacticalOpportunity, Representative> representatives;
        for (std::size_t rank = 0; rank < frontier.size(); ++rank) {
            if (context.deadline_reached()) {
                return false;
            }
            const auto& item = frontier[rank];
            std::vector<TacticalOpportunity> opportunities =
                item.tactical_provenance;
            const FrontierScoreIdentity identity{
                board_identity(item.board),
            };
            const auto inspected = context.frontier_score_cache.find(identity);
            if (inspected == context.frontier_score_cache.end()) {
                return context.unsupported(
                    "native tactical frontier inspection cache miss"
                );
            }
            const std::size_t next_ply = item.moves.size() + 1;
            for (const auto& opportunity : inspected->second.opportunities) {
                opportunities.push_back(TacticalOpportunity{
                    opportunity.kind,
                    tactical_signature(next_ply, opportunity.signature),
                });
            }
            std::sort(opportunities.begin(), opportunities.end());
            opportunities.erase(
                std::unique(opportunities.begin(), opportunities.end()),
                opportunities.end()
            );
            for (const auto& opportunity : opportunities) {
                representatives.emplace(
                    opportunity,
                    Representative{opportunity, rank, &item}
                );
            }
        }

        std::vector<Representative> protected_states;
        protected_states.reserve(representatives.size());
        for (const auto& [opportunity, representative] : representatives) {
            static_cast<void>(opportunity);
            protected_states.push_back(representative);
        }
        std::sort(
            protected_states.begin(),
            protected_states.end(),
            [](const Representative& left, const Representative& right) {
                if (left.opportunity.kind != right.opportunity.kind) {
                    return left.opportunity.kind < right.opportunity.kind;
                }
                if (left.rank != right.rank) {
                    return left.rank < right.rank;
                }
                if (
                    left.opportunity.signature
                    != right.opportunity.signature
                ) {
                    return left.opportunity.signature
                        < right.opportunity.signature;
                }
                return left.state->moves < right.state->moves;
            }
        );

        std::set<CompactMovePath> reserve_candidates;
        for (const auto& representative : protected_states) {
            if (!selected_moves.contains(representative.state->moves)) {
                reserve_candidates.insert(representative.state->moves);
            }
        }
        const std::size_t reserve_limit = std::min(
            std::min(
                cap,
                TACTICAL_FRONTIER_RESERVE_MAX / 2
            ) * 2,
            TACTICAL_FRONTIER_RESERVE_MAX
        );
        std::size_t extras = 0;
        for (const auto& representative : protected_states) {
            if (selected_moves.contains(representative.state->moves)) {
                continue;
            }
            selected.push_back(*representative.state);
            selected_moves.insert(representative.state->moves);
            ++extras;
            if (extras == reserve_limit) {
                break;
            }
        }
        if (
            !context.add(
                stats.tactical_frontier_states_retained,
                static_cast<std::uint64_t>(extras)
            )
            || !context.add(
                stats.tactical_frontier_reserve_drops,
                static_cast<std::uint64_t>(
                    reserve_candidates.size() - extras
                )
            )
        ) {
            return false;
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
        if (context.deadline_reached()) {
            return false;
        }
        if (
            !selected_moves.contains(item.moves)
            && !context.add_path_count(discarded_paths, item.path_count)
        ) {
            return false;
        }
    }
    if (
        !context.add(stats.frontier_prunes, 1)
        || !context.add(stats.frontier_states_pruned, discarded_count)
        || !context.add_path_count(stats.frontier_paths_pruned, discarded_paths)
    ) {
        return false;
    }
    frontier = std::move(selected);
    return true;
}

struct FinalScoreCalculation {
    std::int64_t score = 0;
    const char* error = nullptr;
};

FinalScoreCalculation calculate_final_series_score_value(
    const CompleteSeriesRequest& request,
    const NativeCompletedSeries& series
) {
    const auto& selection = *request.final_series_score;
    if (series.outcome == NativeSeriesOutcome::Checkmate) {
        const bool mover = request.board.white_to_move;
        const bool winner = series.ended_by_check ? mover : !mover;
        std::int64_t score = 0;
        const bool valid = winner == WHITE
            ? checked_subtract(selection.mate_score, selection.ply_from_root, score)
            : checked_subtract(selection.ply_from_root, selection.mate_score, score);
        return valid
            ? FinalScoreCalculation{score, nullptr}
            : FinalScoreCalculation{
                0,
                "native final mate-distance score overflow",
            };
    }
    if (
        series.outcome == NativeSeriesOutcome::Stalemate
        || series.outcome == NativeSeriesOutcome::TenSeriesDraw
    ) {
        return FinalScoreCalculation{0, nullptr};
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
        return FinalScoreCalculation{
            0,
            "native final static score overflow",
        };
    }
    if (selection.neural_ordering_model == 0) {
        return FinalScoreCalculation{*evaluated, nullptr};
    }

    // Terminal results returned above remain authoritative. This student is
    // used only to order non-terminal candidates at the complete boundary
    // entering Series 3; it never replaces minimax leaf evaluation.
    if (series.series_number != 3 || board.white_to_move != WHITE) {
        return FinalScoreCalculation{
            0,
            "native neural final boundary escaped Series-3 scope",
        };
    }
    const ActiveNeuralFeatures active = extract_neural_features(
        board,
        series.series_number,
        series.quiet_series,
        series.series_number,
        target_bits(series.boundary_ep_targets),
        series.ended_by_check
    );
    const auto neural_score = fixed_point_predict(S3_NETWORK, active);
    if (!neural_score.has_value()) {
        return FinalScoreCalculation{
            0,
            "native final neural score overflow",
        };
    }
    const auto blended = blend_neural_score(
        *evaluated,
        *neural_score,
        selection.neural_blend_percent
    );
    return blended.has_value()
        ? FinalScoreCalculation{*blended, nullptr}
        : FinalScoreCalculation{
            0,
            "native final neural blend overflow",
        };
}

bool calculate_final_series_score(
    NativeGenerationContext& context,
    const NativeCompletedSeries& series,
    std::int64_t& score
) {
    const auto calculated = calculate_final_series_score_value(
        context.request,
        series
    );
    if (calculated.error != nullptr) {
        return context.unsupported(calculated.error);
    }
    score = calculated.score;
    return true;
}

[[nodiscard]] std::vector<TacticalOpportunity> complete_tactical_provenance(
    const NativeCompletedSeries& series
) {
    std::vector<TacticalOpportunity> result = series.tactical_provenance;
    std::sort(result.begin(), result.end());
    result.erase(std::unique(result.begin(), result.end()), result.end());
    return result;
}

bool merge_complete_series(NativeGenerationContext& context) {
    std::vector<NativeMergedSeries> merged;
    std::unordered_map<
        CompleteIdentity,
        std::size_t,
        CompleteIdentityHash
    > indices;
    for (auto& completed : context.completed) {
        if (context.deadline_reached()) {
            return false;
        }
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
        if (!context.add_path_count(incumbent.path_count, completed.path_count)) {
            return false;
        }
        const bool prefer_candidate =
            completed.moves.size() > incumbent.representative.moves.size()
            || (
                completed.moves.size() == incumbent.representative.moves.size()
                && completed.moves < incumbent.representative.moves
            );
        if (context.request.tactical_protection) {
            std::vector<TacticalOpportunity> combined_provenance =
                incumbent.representative.tactical_provenance;
            combined_provenance.insert(
                combined_provenance.end(),
                completed.tactical_provenance.begin(),
                completed.tactical_provenance.end()
            );
            std::sort(combined_provenance.begin(), combined_provenance.end());
            combined_provenance.erase(
                std::unique(
                    combined_provenance.begin(),
                    combined_provenance.end()
                ),
                combined_provenance.end()
            );
            if (prefer_candidate) {
                completed.tactical_provenance = std::move(combined_provenance);
            } else {
                incumbent.representative.tactical_provenance = std::move(
                    combined_provenance
                );
            }
        }
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
        constexpr std::size_t PARALLEL_FINAL_SCORE_THRESHOLD = 64;
        if (
            context.request.worker_threads <= 1
            || merged.size() < PARALLEL_FINAL_SCORE_THRESHOLD
        ) {
            for (auto& item : merged) {
                if (context.deadline_reached()) {
                    return false;
                }
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
        } else {
            std::vector<FinalScoreCalculation> calculated(merged.size());
            std::atomic<bool> deadline_cancelled{false};
            BoundedNativePool::instance().run(
                merged.size(),
                context.request.worker_threads,
                [&](std::size_t index) {
                    if (
                        deadline_cancelled.load(std::memory_order_relaxed)
                        || (
                            context.request.deadline.has_value()
                            && std::chrono::steady_clock::now()
                                >= *context.request.deadline
                        )
                    ) {
                        deadline_cancelled.store(true, std::memory_order_relaxed);
                        return;
                    }
                    calculated[index] = calculate_final_series_score_value(
                        context.request,
                        merged[index].representative
                    );
                }
            );
            if (deadline_cancelled.load(std::memory_order_relaxed)) {
                static_cast<void>(context.deadline_reached(true));
                return false;
            }
            if (context.deadline_reached(true)) {
                return false;
            }
            for (std::size_t index = 0; index < merged.size(); ++index) {
                if (calculated[index].error != nullptr) {
                    return context.unsupported(calculated[index].error);
                }
                ranked.push_back(RankedSeries{
                    std::move(merged[index]),
                    calculated[index].score,
                });
            }
        }
        const bool mover = context.request.board.white_to_move;
        const auto ranked_before =
            [mover](const RankedSeries& left, const RankedSeries& right) {
                if (left.score != right.score) {
                    return mover == WHITE
                        ? left.score > right.score
                        : left.score < right.score;
                }
                return left.series.representative.moves
                    < right.series.representative.moves;
            };
        const std::size_t final_cap = static_cast<std::size_t>(
            context.request.final_series_score->max_returned_series
        );
        if (
            !context.request.tactical_protection
            && ranked.size() > final_cap
        ) {
            // The ordinary browser root contract only consumes the best
            // `final_cap` series. Keep those entries in the exact same total
            // order without fully sorting every discarded series.
            std::partial_sort(
                ranked.begin(),
                ranked.begin() + final_cap,
                ranked.end(),
                ranked_before
            );
            ranked.resize(final_cap);
        } else {
            // Tactical reserve selection below addresses candidates by their
            // full rank, so that path still requires a complete ordering.
            std::sort(ranked.begin(), ranked.end(), ranked_before);
        }
        if (
            ranked.size() > final_cap
            && context.request.tactical_protection
        ) {
            struct FinalRepresentative {
                TacticalOpportunity opportunity;
                std::size_t rank;
            };
            std::map<TacticalOpportunity, FinalRepresentative> representatives;
            for (std::size_t rank = 0; rank < ranked.size(); ++rank) {
                for (const auto& opportunity : complete_tactical_provenance(
                        ranked[rank].series.representative
                    )) {
                    representatives.emplace(
                        opportunity,
                        FinalRepresentative{opportunity, rank}
                    );
                }
            }
            std::vector<FinalRepresentative> protected_series;
            protected_series.reserve(representatives.size());
            std::set<std::size_t> tactical_candidate_ranks;
            for (const auto& [opportunity, representative] : representatives) {
                static_cast<void>(opportunity);
                protected_series.push_back(representative);
                tactical_candidate_ranks.insert(representative.rank);
            }
            std::sort(
                protected_series.begin(),
                protected_series.end(),
                [&ranked](
                    const FinalRepresentative& left,
                    const FinalRepresentative& right
                ) {
                    if (left.opportunity.kind != right.opportunity.kind) {
                        return left.opportunity.kind < right.opportunity.kind;
                    }
                    if (left.rank != right.rank) {
                        return left.rank < right.rank;
                    }
                    if (
                        left.opportunity.signature
                        != right.opportunity.signature
                    ) {
                        return left.opportunity.signature
                            < right.opportunity.signature;
                    }
                    return ranked[left.rank].series.representative.moves
                        < ranked[right.rank].series.representative.moves;
                }
            );
            std::vector<bool> selected(ranked.size(), false);
            std::size_t selected_count = 0;
            // Delivered terminal mates remain authoritative even when a
            // deliberately inverted heuristic ranks them below quiet lines.
            for (
                std::size_t rank = 0;
                rank < ranked.size() && selected_count < final_cap;
                ++rank
            ) {
                const auto& candidate = ranked[rank].series.representative;
                if (
                    candidate.outcome != NativeSeriesOutcome::Checkmate
                    || !candidate.ended_by_check
                ) {
                    continue;
                }
                selected[rank] = true;
                ++selected_count;
            }
            const std::size_t ordinary_quota = std::min(
                final_cap,
                final_cap / FINAL_ORDINARY_QUOTA_DENOMINATOR
                    + final_cap % FINAL_ORDINARY_QUOTA_DENOMINATOR
            );
            for (
                std::size_t rank = 0;
                rank < ordinary_quota && selected_count < final_cap;
                ++rank
            ) {
                if (selected[rank]) {
                    continue;
                }
                selected[rank] = true;
                ++selected_count;
            }
            for (const auto& representative : protected_series) {
                if (selected_count == final_cap) {
                    break;
                }
                if (selected[representative.rank]) {
                    continue;
                }
                selected[representative.rank] = true;
                ++selected_count;
            }
            for (
                std::size_t rank = 0;
                rank < ranked.size() && selected_count < final_cap;
                ++rank
            ) {
                if (selected[rank]) {
                    continue;
                }
                selected[rank] = true;
                ++selected_count;
            }
            std::uint64_t tactical_retained = 0;
            std::uint64_t tactical_drops = 0;
            for (const std::size_t rank : tactical_candidate_ranks) {
                if (!selected[rank]) {
                    ++tactical_drops;
                } else if (rank >= final_cap) {
                    ++tactical_retained;
                }
            }
            if (
                !context.add(
                    stats.tactical_final_series_retained,
                    tactical_retained
                )
                || !context.add(
                    stats.tactical_final_reserve_drops,
                    tactical_drops
                )
            ) {
                return false;
            }
            std::vector<RankedSeries> retained;
            retained.reserve(final_cap);
            for (std::size_t rank = 0; rank < ranked.size(); ++rank) {
                if (selected[rank]) {
                    retained.push_back(std::move(ranked[rank]));
                }
            }
            ranked = std::move(retained);
        }
        if (ranked.size() > final_cap) {
            ranked.resize(final_cap);
        }
        context.response.series.reserve(ranked.size());
        for (auto& item : ranked) {
            auto& representative = item.series.representative;
            context.response.series.push_back(CompleteSeriesCandidate{
                CompleteSeriesPath{
                    representative.moves.to_uci_vector(),
                    item.series.path_count,
                },
                representative.board,
                representative.halfmove_clock,
                representative.fullmove_number,
                representative.series_number,
                representative.quiet_series,
                std::move(representative.boundary_ep_targets),
                representative.outcome,
                representative.ended_by_check,
            });
        }
        return true;
    }

    context.response.series.reserve(merged.size());
    for (auto& item : merged) {
        auto& representative = item.representative;
        context.response.series.push_back(CompleteSeriesCandidate{
            CompleteSeriesPath{
                representative.moves.to_uci_vector(),
                item.path_count,
            },
            representative.board,
            representative.halfmove_clock,
            representative.fullmove_number,
            representative.series_number,
            representative.quiet_series,
            std::move(representative.boundary_ep_targets),
            representative.outcome,
            representative.ended_by_check,
        });
    }
    std::sort(
        context.response.series.begin(),
        context.response.series.end(),
        [](const CompleteSeriesCandidate& left,
           const CompleteSeriesCandidate& right) {
            return left.path.moves < right.path.moves;
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
                return legal_move_uci(move.move)
                    == request.required_prefix[index];
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
        root.moves.push_back(selected->move);
        if (request.tactical_protection) {
            record_played_tactical_provenance(root, *selected);
        }
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

bool is_in_check(const BoardState& position) noexcept {
    return board_in_check(position);
}

bool has_insufficient_material(const BoardState& position) noexcept {
    return board_has_insufficient_material(position);
}

std::vector<int> canonical_ep_targets(
    const BoardState& position,
    Bitboard pending_ep_targets
) {
    return canonical_boundary_ep_targets(position, pending_ep_targets);
}

CompleteSeriesResponse generate_complete_series(
    const CompleteSeriesRequest& request
) {
    NativeGenerationContext context{request};
    if (
        request.series_number < 1
        || request.series_number == std::numeric_limits<std::int64_t>::max()
        || request.quiet_series < 0
        || request.quiet_series == std::numeric_limits<std::int64_t>::max()
        || request.worker_threads < 1
        || request.worker_threads > 64
        || request.halfmove_clock < 0
        || request.fullmove_number < 1
        || (
            request.path_count_overflow_mode != PathCountOverflowMode::Reject
            && request.path_count_overflow_mode
                != PathCountOverflowMode::Saturate
        )
        || request.path_count_saturation_limit == 0
        || (
            request.max_frontier_states.has_value()
            && *request.max_frontier_states == 0
        )
        || (request.max_positions.has_value() && *request.max_positions == 0)
        || (
            request.tactical_protection
            && !request.frontier_weights.has_value()
        )
        || (
            request.final_series_score.has_value()
            && (
                request.final_series_score->max_returned_series == 0
                || request.final_series_score->ply_from_root < 0
                || request.final_series_score->mate_score < 1
                || (
                    request.final_series_score->neural_ordering_model == 0
                    && request.final_series_score->neural_blend_percent != 0
                )
                || (
                    request.final_series_score->neural_ordering_model != 0
                    && (
                        request.final_series_score->neural_ordering_model
                            != S3_NEURAL_ORDERING_MODEL
                        || request.final_series_score->neural_blend_percent < 0
                        || request.final_series_score->neural_blend_percent > 100
                        || request.series_number != 2
                        || request.board.white_to_move != BLACK
                    )
                )
            )
        )
    ) {
        context.response.status = SeriesGenerationStatus::Unsupported;
        context.response.message = "native complete-series request is out of range";
        return context.response;
    }

    if (context.deadline_reached(true)) {
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
        const auto process_variants = [
            &context,
            &following,
            &indices,
            &request,
            mover
        ](
            const NativeFrontierState& item,
            const std::vector<ExpandedMove>& variants
        ) -> bool {
            if (variants.empty()) {
                if (!record_completed(context, stuck_series(request, item))) {
                    return false;
                }
                return true;
            }

            for (const auto& expanded : variants) {
                if (context.deadline_reached()) {
                    return false;
                }
                NativeFrontierState candidate{
                    expanded.child,
                    item.moves,
                    item.pending_ep_targets,
                    item.made_progress || expanded.is_pawn_move || expanded.is_capture,
                    item.path_count,
                    item.halfmove_clock,
                    item.fullmove_number,
                    request.tactical_protection
                        ? item.tactical_provenance
                        : std::vector<TacticalOpportunity>{},
                };
                if (!update_frontier_clocks(context, candidate, expanded, mover)) {
                    return false;
                }
                candidate.moves.push_back(expanded.move);
                if (request.tactical_protection) {
                    record_played_tactical_provenance(candidate, expanded);
                }
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
                        return false;
                    }
                    if (context.response.stopped_on_mover_mate) {
                        return true;
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
                if (!context.add_path_count(total_paths, candidate.path_count)) {
                    return false;
                }
                const bool prefer_candidate = candidate.moves < incumbent.moves;
                if (request.tactical_protection) {
                    std::vector<TacticalOpportunity> combined_provenance =
                        incumbent.tactical_provenance;
                    combined_provenance.insert(
                        combined_provenance.end(),
                        candidate.tactical_provenance.begin(),
                        candidate.tactical_provenance.end()
                    );
                    std::sort(
                        combined_provenance.begin(),
                        combined_provenance.end()
                    );
                    combined_provenance.erase(
                        std::unique(
                            combined_provenance.begin(),
                            combined_provenance.end()
                        ),
                        combined_provenance.end()
                    );
                    if (prefer_candidate) {
                        candidate.tactical_provenance = std::move(
                            combined_provenance
                        );
                    } else {
                        incumbent.tactical_provenance = std::move(
                            combined_provenance
                        );
                    }
                }
                if (prefer_candidate) {
                    candidate.path_count = total_paths;
                    incumbent = std::move(candidate);
                } else {
                    incumbent.path_count = total_paths;
                }
            }
            return true;
        };

        constexpr std::size_t PARALLEL_EXPANSION_THRESHOLD = 8;
        const bool enough_position_budget =
            !request.max_positions.has_value()
            || (
                context.response.stats.positions_visited
                    <= *request.max_positions
                && context.response.stats.frontier_score_positions
                    <= *request.max_positions
                        - context.response.stats.positions_visited
                && frontier.size()
                    <= *request.max_positions
                        - context.response.stats.positions_visited
                        - context.response.stats.frontier_score_positions
            );
        const bool parallel_expansion =
            request.worker_threads > 1
            // A bound-only mate exit must charge exactly through the proving
            // frontier item. Pre-expanding the whole frontier would make the
            // logical work result depend on execution thread count.
            && !request.stop_on_mover_mate
            && frontier.size() >= PARALLEL_EXPANSION_THRESHOLD
            && enough_position_budget;
        if (!parallel_expansion) {
            for (const auto& item : frontier) {
                if (!context.charge_position()) {
                    return std::move(context.response);
                }
                std::vector<ExpandedMove> variants = expand_legal_move_variants(
                    item.board,
                    item.moves.empty()
                        ? request.ep_targets
                        : std::vector<int>{}
                );
                if (!process_variants(item, variants)) {
                    return std::move(context.response);
                }
                if (context.response.stopped_on_mover_mate) {
                    break;
                }
            }
        } else {
            // Position work is charged in the same stable frontier order as
            // the serial kernel. Move generation itself is pure, so each
            // frontier item can then be expanded independently and consumed
            // in its original index order without changing merge winners,
            // path counts, or returned series ordering.
            for (std::size_t index = 0; index < frontier.size(); ++index) {
                if (!context.charge_position()) {
                    return std::move(context.response);
                }
            }
            std::vector<std::vector<ExpandedMove>> expanded(frontier.size());
            std::atomic<bool> deadline_cancelled{false};
            BoundedNativePool::instance().run(
                frontier.size(),
                request.worker_threads,
                [&](std::size_t index) {
                    if (
                        deadline_cancelled.load(std::memory_order_relaxed)
                        || (
                            request.deadline.has_value()
                            && std::chrono::steady_clock::now()
                                >= *request.deadline
                        )
                    ) {
                        deadline_cancelled.store(true, std::memory_order_relaxed);
                        return;
                    }
                    const auto& item = frontier[index];
                    expanded[index] = expand_legal_move_variants(
                        item.board,
                        item.moves.empty()
                            ? request.ep_targets
                            : std::vector<int>{}
                    );
                }
            );
            if (deadline_cancelled.load(std::memory_order_relaxed)) {
                static_cast<void>(context.deadline_reached(true));
                return std::move(context.response);
            }
            if (context.deadline_reached(true)) {
                return std::move(context.response);
            }
            for (std::size_t index = 0; index < frontier.size(); ++index) {
                if (!process_variants(frontier[index], expanded[index])) {
                    return std::move(context.response);
                }
                if (context.response.stopped_on_mover_mate) {
                    break;
                }
            }
        }
        if (context.response.stopped_on_mover_mate) {
            break;
        }
        if (!bound_frontier(context, following)) {
            return std::move(context.response);
        }
        frontier = std::move(following);
    }

    if (!merge_complete_series(context)) {
        return std::move(context.response);
    }
    if (context.deadline_reached(true)) {
        context.response.series.clear();
        return std::move(context.response);
    }
    return std::move(context.response);
}

}  // namespace spc::native

#ifndef SPC_NATIVE_CORE_ONLY
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

template <typename Value, std::size_t Size>
PyObject* py_integer_tuple(const std::array<Value, Size>& values) {
    PyObject* result = PyTuple_New(static_cast<Py_ssize_t>(values.size()));
    if (result == nullptr) {
        return nullptr;
    }
    for (std::size_t index = 0; index < values.size(); ++index) {
        PyObject* value = PyLong_FromLongLong(
            static_cast<long long>(values[index])
        );
        if (value == nullptr) {
            Py_DECREF(result);
            return nullptr;
        }
        PyTuple_SET_ITEM(result, static_cast<Py_ssize_t>(index), value);
    }
    return result;
}

bool set_owned_dict_item(
    PyObject* dictionary,
    const char* key,
    PyObject* value
) {
    if (value == nullptr) {
        return false;
    }
    const int status = PyDict_SetItemString(dictionary, key, value);
    Py_DECREF(value);
    return status == 0;
}

PyObject* py_neural_ordering_identity(PyObject*, PyObject*) {
    return Py_BuildValue(
        "(ssKLLs)",
        spc::native::S3_NEURAL_ARTIFACT_ID,
        spc::native::S3_NEURAL_ARTIFACT_SHA256,
        static_cast<unsigned long long>(spc::native::NEURAL_FEATURE_COUNT),
        static_cast<long long>(spc::native::S3_NEURAL_ORDERING_MODEL),
        static_cast<long long>(
            spc::native::S3_NEURAL_ORDERING_BLEND_PERCENT
        ),
        spc::native::S3_NEURAL_INFERENCE_SCOPE
    );
}

PyObject* py_neural_ordering_parameters(PyObject*, PyObject*) {
    PyObject* result = PyDict_New();
    if (result == nullptr) {
        return nullptr;
    }
    const bool built = set_owned_dict_item(
        result,
        "feature_count",
        PyLong_FromUnsignedLongLong(spc::native::S3_NETWORK.feature_count)
    ) && set_owned_dict_item(
        result,
        "hidden_size",
        PyLong_FromUnsignedLongLong(spc::native::S3_NETWORK.hidden_size)
    ) && set_owned_dict_item(
        result,
        "input_weights",
        py_integer_tuple(spc::native::S3_INPUT_WEIGHTS)
    ) && set_owned_dict_item(
        result,
        "hidden_bias",
        py_integer_tuple(spc::native::S3_HIDDEN_BIAS)
    ) && set_owned_dict_item(
        result,
        "output_weights",
        py_integer_tuple(spc::native::S3_OUTPUT_WEIGHTS)
    ) && set_owned_dict_item(
        result,
        "output_bias",
        PyLong_FromLongLong(spc::native::S3_NETWORK.output_bias)
    ) && set_owned_dict_item(
        result,
        "output_denominator",
        PyLong_FromLongLong(spc::native::S3_NETWORK.output_denominator)
    ) && set_owned_dict_item(
        result,
        "activation_clip",
        PyLong_FromLongLong(spc::native::S3_NETWORK.activation_clip)
    ) && set_owned_dict_item(
        result,
        "score_clip",
        PyLong_FromLongLong(spc::native::S3_NETWORK.score_clip)
    );
    if (!built) {
        Py_DECREF(result);
        return nullptr;
    }
    return result;
}

PyObject* py_neural_ordering_evaluate(PyObject*, PyObject* arguments) {
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
    long long series_number = 0;
    long long quiet_series = 0;
    long long moves_remaining = 0;
    int known_in_check = 0;
    PyObject* ep_targets_object = nullptr;
    if (!PyArg_ParseTuple(
            arguments,
            "KKKKKKKKKKpLLLpO:neural_ordering_evaluate",
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
            &series_number,
            &quiet_series,
            &moves_remaining,
            &known_in_check,
            &ep_targets_object
        )) {
        return nullptr;
    }
    if (
        series_number < 1
        || quiet_series < 0
        || moves_remaining < 0
        || moves_remaining > series_number
    ) {
        PyErr_SetString(
            PyExc_ValueError,
            "invalid progressive neural feature metadata"
        );
        return nullptr;
    }
    std::vector<int> ep_targets;
    if (!parse_square_sequence(
            ep_targets_object,
            ep_targets,
            "ep_targets must be an iterable of squares"
        )) {
        return nullptr;
    }
    const spc::native::BoardState board{
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
    if (!spc::native::valid_neural_board_bitboards(board)) {
        PyErr_SetString(
            PyExc_ValueError,
            "invalid neural board bitboards"
        );
        return nullptr;
    }
    const auto active = spc::native::extract_neural_features(
        board,
        series_number,
        quiet_series,
        moves_remaining,
        spc::native::target_bits(ep_targets),
        known_in_check != 0
    );
    const auto score = spc::native::fixed_point_predict(
        spc::native::S3_NETWORK,
        active
    );
    if (!score.has_value()) {
        PyErr_SetString(PyExc_OverflowError, "native neural inference overflow");
        return nullptr;
    }
    PyObject* features = PyTuple_New(static_cast<Py_ssize_t>(active.size));
    if (features == nullptr) {
        return nullptr;
    }
    for (std::size_t index = 0; index < active.size; ++index) {
        PyObject* value = PyLong_FromUnsignedLong(active.values[index]);
        if (value == nullptr) {
            Py_DECREF(features);
            return nullptr;
        }
        PyTuple_SET_ITEM(features, static_cast<Py_ssize_t>(index), value);
    }
    PyObject* result = PyTuple_New(2);
    PyObject* score_object = PyLong_FromLongLong(*score);
    if (result == nullptr || score_object == nullptr) {
        Py_XDECREF(result);
        Py_XDECREF(score_object);
        Py_DECREF(features);
        return nullptr;
    }
    PyTuple_SET_ITEM(result, 0, features);
    PyTuple_SET_ITEM(result, 1, score_object);
    return result;
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
    std::optional<spc::native::FastWeights>& weights,
    bool& tactical_protection
) {
    tactical_protection = false;
    if (object == Py_None) {
        weights.reset();
        return true;
    }
    PyObject* sequence = PySequence_Fast(
        object,
        "frontier_weights must be None or five/six signed integers"
    );
    if (sequence == nullptr) {
        return false;
    }
    const Py_ssize_t size = PySequence_Fast_GET_SIZE(sequence);
    if (size != 5 && size != 6) {
        Py_DECREF(sequence);
        PyErr_SetString(
            PyExc_ValueError,
            "frontier_weights must contain five weights and an optional tactical flag"
        );
        return false;
    }
    if (size == 6) {
        const long long parsed_flag = PyLong_AsLongLong(
            PySequence_Fast_GET_ITEM(sequence, 5)
        );
        if (parsed_flag == -1 && PyErr_Occurred()) {
            Py_DECREF(sequence);
            return false;
        }
        if (parsed_flag != 0 && parsed_flag != 1) {
            Py_DECREF(sequence);
            PyErr_SetString(
                PyExc_ValueError,
                "frontier tactical flag must be zero or one"
            );
            return false;
        }
        tactical_protection = parsed_flag != 0;
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
        "final_series_score must be None, eight integers, or ten integers"
    );
    if (sequence == nullptr) {
        return false;
    }
    const Py_ssize_t size = PySequence_Fast_GET_SIZE(sequence);
    if (size != 8 && size != 10) {
        Py_DECREF(sequence);
        PyErr_SetString(
            PyExc_ValueError,
            "final_series_score must contain exactly eight or ten integers"
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
    long long neural_model = 0;
    long long neural_blend_percent = 0;
    if (size == 10) {
        neural_model = PyLong_AsLongLong(
            PySequence_Fast_GET_ITEM(sequence, 8)
        );
        neural_blend_percent = PyLong_AsLongLong(
            PySequence_Fast_GET_ITEM(sequence, 9)
        );
        if (
            (neural_model == -1 && PyErr_Occurred())
            || (neural_blend_percent == -1 && PyErr_Occurred())
        ) {
            Py_DECREF(sequence);
            return false;
        }
        if (
            neural_model < 1
            || neural_model > std::numeric_limits<std::uint8_t>::max()
            || neural_blend_percent < 0
            || neural_blend_percent > 100
        ) {
            Py_DECREF(sequence);
            PyErr_SetString(
                PyExc_ValueError,
                "neural final-series ordering selection is out of range"
            );
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
        static_cast<std::uint8_t>(neural_model),
        neural_blend_percent,
    };
    return true;
}

PyObject* generation_stats_tuple(
    const spc::native::SeriesGenerationStats& stats
) {
    PyObject* result = PyTuple_New(18);
    if (result == nullptr) {
        return nullptr;
    }
    const std::array<std::uint64_t, 17> values = {
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
        stats.tactical_frontier_states_retained,
        stats.tactical_frontier_reserve_drops,
        stats.tactical_final_series_retained,
        stats.tactical_final_reserve_drops,
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
    PyTuple_SET_ITEM(result, 17, work_limit);
    return result;
}

PyObject* complete_series_tuple(
    const std::vector<spc::native::CompleteSeriesCandidate>& series
) {
    PyObject* result = PyTuple_New(static_cast<Py_ssize_t>(series.size()));
    if (result == nullptr) {
        return nullptr;
    }
    for (std::size_t index = 0; index < series.size(); ++index) {
        const auto& item = series[index].path;
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
    bool tactical_protection = false;
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
                frontier_weights,
                tactical_protection
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

    spc::native::CompleteSeriesRequest request{
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
    request.tactical_protection = tactical_protection;

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

PyObject* py_generate_full_game_batch(PyObject*, PyObject* arguments) {
    unsigned long long first_attempt = 0;
    unsigned long long attempt_count = 0;
    unsigned long long seed = 0;
    unsigned long long max_attempt_series = 0;
    unsigned long long max_frontier_states = 0;
    unsigned long long max_positions_per_series = 0;
    unsigned long long max_positions_per_game = 0;
    unsigned long long candidate_count = 0;
    long long material = 0;
    long long king_space = 0;
    long long promotion_corridors = 0;
    long long immediate_vulnerability = 0;
    long long boundary_check = 0;
    if (!PyArg_ParseTuple(
            arguments,
            "KKKKKKKKLLLLL:generate_full_game_batch",
            &first_attempt,
            &attempt_count,
            &seed,
            &max_attempt_series,
            &max_frontier_states,
            &max_positions_per_series,
            &max_positions_per_game,
            &candidate_count,
            &material,
            &king_space,
            &promotion_corridors,
            &immediate_vulnerability,
            &boundary_check
        )) {
        return nullptr;
    }
    if (
        attempt_count > std::numeric_limits<std::uint32_t>::max()
        || candidate_count == 0
        || candidate_count > std::numeric_limits<std::uint32_t>::max()
        || max_frontier_states == 0
        || max_positions_per_series == 0
        || (max_positions_per_game == 0 && max_attempt_series == 0)
        || (
            attempt_count != 0
            && first_attempt > std::numeric_limits<std::uint64_t>::max()
                - (attempt_count - 1)
        )
    ) {
        PyErr_SetString(
            PyExc_ValueError,
            "invalid native full-game batch configuration"
        );
        return nullptr;
    }

    const spc::native::FullGameBatchConfig config{
        first_attempt,
        attempt_count,
        seed,
        max_attempt_series,
        max_frontier_states,
        max_positions_per_series,
        max_positions_per_game,
        static_cast<std::uint32_t>(candidate_count),
        {
            material,
            king_space,
            promotion_corridors,
            immediate_vulnerability,
            boundary_check,
        },
    };
    std::vector<std::uint8_t> payload;
    std::exception_ptr failure;
    PyThreadState* saved_thread = PyEval_SaveThread();
    try {
        const auto records = spc::native::generate_full_games(config);
        payload = spc::native::encode_full_game_batch(config, records);
    } catch (...) {
        failure = std::current_exception();
    }
    PyEval_RestoreThread(saved_thread);

    try {
        if (failure != nullptr) {
            std::rethrow_exception(failure);
        }
        if (
            payload.size()
            > static_cast<std::size_t>(PY_SSIZE_T_MAX)
        ) {
            return PyErr_NoMemory();
        }
        return PyBytes_FromStringAndSize(
            reinterpret_cast<const char*>(payload.data()),
            static_cast<Py_ssize_t>(payload.size())
        );
    } catch (const std::bad_alloc&) {
        return PyErr_NoMemory();
    } catch (const std::length_error&) {
        return PyErr_NoMemory();
    } catch (const std::invalid_argument& error) {
        PyErr_SetString(PyExc_ValueError, error.what());
        return nullptr;
    } catch (...) {
        PyErr_SetString(PyExc_RuntimeError, "native full-game batch failed");
        return nullptr;
    }
}

PyObject* py_generate_full_game_batch_v2(PyObject*, PyObject* arguments) {
    const char* request_data = nullptr;
    Py_ssize_t request_size = 0;
    if (!PyArg_ParseTuple(
            arguments,
            "y#:generate_full_game_batch_v2",
            &request_data,
            &request_size
        )) {
        return nullptr;
    }

    std::vector<std::uint8_t> request;
    try {
        request.assign(
            reinterpret_cast<const std::uint8_t*>(request_data),
            reinterpret_cast<const std::uint8_t*>(request_data) + request_size
        );
    } catch (const std::bad_alloc&) {
        return PyErr_NoMemory();
    } catch (const std::length_error&) {
        return PyErr_NoMemory();
    }

    std::vector<std::uint8_t> payload;
    std::exception_ptr failure;
    PyThreadState* saved_thread = PyEval_SaveThread();
    try {
        payload = spc::native::generate_full_game_batch_v2(request);
    } catch (...) {
        failure = std::current_exception();
    }
    PyEval_RestoreThread(saved_thread);

    try {
        if (failure != nullptr) {
            std::rethrow_exception(failure);
        }
        if (payload.size() > static_cast<std::size_t>(PY_SSIZE_T_MAX)) {
            return PyErr_NoMemory();
        }
        return PyBytes_FromStringAndSize(
            reinterpret_cast<const char*>(payload.data()),
            static_cast<Py_ssize_t>(payload.size())
        );
    } catch (const std::bad_alloc&) {
        return PyErr_NoMemory();
    } catch (const std::length_error&) {
        return PyErr_NoMemory();
    } catch (const std::invalid_argument& error) {
        PyErr_SetString(PyExc_ValueError, error.what());
        return nullptr;
    } catch (...) {
        PyErr_SetString(PyExc_RuntimeError, "native full-game v2 batch failed");
        return nullptr;
    }
}

constexpr const char* COMPLETE_SERIES_BATCH_CAPSULE =
    "scottish_progressive.CompleteSeriesBatch.v1";

bool parse_complete_series_batch_request(
    PyObject* arguments,
    spc::native::CompleteSeriesRequest& request,
    bool timed = false,
    bool parallel = false
) {
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
    unsigned long long remaining_nanoseconds = 0;
    unsigned long long worker_threads = 1;
    const bool parsed = timed && parallel
        ? PyArg_ParseTuple(
            arguments,
            "KKKKKKKKKKpLLLLOOOOOOKK:prepare_complete_series_timed_parallel",
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
            &final_series_score_object,
            &remaining_nanoseconds,
            &worker_threads
        )
        : timed
        ? PyArg_ParseTuple(
            arguments,
            "KKKKKKKKKKpLLLLOOOOOOK:prepare_complete_series_timed",
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
            &final_series_score_object,
            &remaining_nanoseconds
        )
        : PyArg_ParseTuple(
            arguments,
            "KKKKKKKKKKpLLLLOOOOOO:prepare_complete_series",
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
        );
    if (!parsed) {
        return false;
    }
    if (
        worker_threads < 1
        || worker_threads > std::numeric_limits<std::uint32_t>::max()
    ) {
        PyErr_SetString(
            PyExc_ValueError,
            "worker_threads must fit uint32 and be positive"
        );
        return false;
    }

    std::vector<int> ep_targets;
    std::vector<std::string> required_prefix;
    std::optional<std::uint64_t> max_frontier_states;
    std::optional<std::uint64_t> max_positions;
    std::optional<spc::native::FastWeights> frontier_weights;
    std::optional<spc::native::FinalSeriesScore> final_series_score;
    bool tactical_protection = false;
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
            frontier_weights,
            tactical_protection
        )
        || !parse_optional_final_series_score(
            final_series_score_object,
            final_series_score
        )
    ) {
        return false;
    }

    request = spc::native::CompleteSeriesRequest{
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
    request.tactical_protection = tactical_protection;
    if (timed) {
        const auto now = std::chrono::steady_clock::now();
        const auto bounded_nanoseconds = std::min<unsigned long long>(
            remaining_nanoseconds,
            static_cast<unsigned long long>(
                std::numeric_limits<std::int64_t>::max()
            )
        );
        const auto requested = std::chrono::duration_cast<
            std::chrono::steady_clock::duration
        >(std::chrono::nanoseconds(
            static_cast<std::int64_t>(bounded_nanoseconds)
        ));
        const auto maximum = std::chrono::steady_clock::time_point::max() - now;
        request.deadline = now + std::min(requested, maximum);
    }
    request.worker_threads = static_cast<std::uint32_t>(worker_threads);
    return true;
}

void destroy_complete_series_batch(PyObject* capsule) noexcept {
    void* pointer = PyCapsule_GetPointer(
        capsule,
        COMPLETE_SERIES_BATCH_CAPSULE
    );
    if (pointer == nullptr) {
        PyErr_Clear();
        return;
    }
    delete static_cast<spc::native::CompleteSeriesResponse*>(pointer);
}

PyObject* complete_series_batch_result(
    std::unique_ptr<spc::native::CompleteSeriesResponse> response
) {
    PyObject* status = PyLong_FromLong(static_cast<long>(response->status));
    PyObject* message = PyUnicode_FromString(response->message.c_str());
    PyObject* stats = generation_stats_tuple(response->stats);
    PyObject* series = complete_series_tuple(response->series);
    if (
        status == nullptr
        || message == nullptr
        || stats == nullptr
        || series == nullptr
    ) {
        Py_XDECREF(status);
        Py_XDECREF(message);
        Py_XDECREF(stats);
        Py_XDECREF(series);
        return nullptr;
    }
    PyObject* capsule = PyCapsule_New(
        response.get(),
        COMPLETE_SERIES_BATCH_CAPSULE,
        destroy_complete_series_batch
    );
    if (capsule == nullptr) {
        Py_DECREF(status);
        Py_DECREF(message);
        Py_DECREF(stats);
        Py_DECREF(series);
        return nullptr;
    }
    response.release();
    PyObject* result = PyTuple_New(5);
    if (result == nullptr) {
        Py_DECREF(status);
        Py_DECREF(message);
        Py_DECREF(stats);
        Py_DECREF(series);
        Py_DECREF(capsule);
        return nullptr;
    }
    PyTuple_SET_ITEM(result, 0, status);
    PyTuple_SET_ITEM(result, 1, message);
    PyTuple_SET_ITEM(result, 2, stats);
    PyTuple_SET_ITEM(result, 3, series);
    PyTuple_SET_ITEM(result, 4, capsule);
    return result;
}

PyObject* py_prepare_complete_series(PyObject*, PyObject* arguments) {
    spc::native::CompleteSeriesRequest request{};
    try {
        if (!parse_complete_series_batch_request(arguments, request)) {
            return nullptr;
        }
    } catch (const std::bad_alloc&) {
        return PyErr_NoMemory();
    } catch (const std::length_error&) {
        return PyErr_NoMemory();
    } catch (...) {
        PyErr_SetString(
            PyExc_RuntimeError,
            "native complete-series batch argument parsing failed"
        );
        return nullptr;
    }

    auto response = std::make_unique<spc::native::CompleteSeriesResponse>();
    try {
        *response = spc::native::generate_complete_series(request);
    } catch (const std::bad_alloc&) {
        return PyErr_NoMemory();
    } catch (...) {
        PyErr_SetString(
            PyExc_RuntimeError,
            "native complete-series batch generation failed"
        );
        return nullptr;
    }

    return complete_series_batch_result(std::move(response));
}

PyObject* prepare_complete_series_timed_impl(
    PyObject* arguments,
    bool parallel
) {
    spc::native::CompleteSeriesRequest request{};
    try {
        if (!parse_complete_series_batch_request(
                arguments,
                request,
                true,
                parallel
            )) {
            return nullptr;
        }
    } catch (const std::bad_alloc&) {
        return PyErr_NoMemory();
    } catch (const std::length_error&) {
        return PyErr_NoMemory();
    } catch (...) {
        PyErr_SetString(
            PyExc_RuntimeError,
            "native timed complete-series argument parsing failed"
        );
        return nullptr;
    }

    auto response = std::make_unique<spc::native::CompleteSeriesResponse>();
    std::exception_ptr failure;
    PyThreadState* saved_thread = PyEval_SaveThread();
    try {
        *response = spc::native::generate_complete_series(request);
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
        PyErr_SetString(
            PyExc_RuntimeError,
            "native timed complete-series generation failed"
        );
        return nullptr;
    }

    return complete_series_batch_result(std::move(response));
}

PyObject* py_prepare_complete_series_timed(PyObject*, PyObject* arguments) {
    return prepare_complete_series_timed_impl(arguments, false);
}

PyObject* py_prepare_complete_series_timed_parallel(
    PyObject*,
    PyObject* arguments
) {
    return prepare_complete_series_timed_impl(arguments, true);
}

PyObject* square_vector_tuple(const std::vector<int>& squares) {
    PyObject* result = PyTuple_New(static_cast<Py_ssize_t>(squares.size()));
    if (result == nullptr) {
        return nullptr;
    }
    for (std::size_t index = 0; index < squares.size(); ++index) {
        PyObject* value = PyLong_FromLong(squares[index]);
        if (value == nullptr) {
            Py_DECREF(result);
            return nullptr;
        }
        PyTuple_SET_ITEM(result, static_cast<Py_ssize_t>(index), value);
    }
    return result;
}

PyObject* py_complete_series_candidate(PyObject*, PyObject* arguments) {
    PyObject* capsule = nullptr;
    Py_ssize_t index = 0;
    if (!PyArg_ParseTuple(
            arguments,
            "On:complete_series_candidate",
            &capsule,
            &index
        )) {
        return nullptr;
    }
    auto* response = static_cast<spc::native::CompleteSeriesResponse*>(
        PyCapsule_GetPointer(capsule, COMPLETE_SERIES_BATCH_CAPSULE)
    );
    if (response == nullptr) {
        return nullptr;
    }
    if (
        index < 0
        || static_cast<std::size_t>(index) >= response->series.size()
    ) {
        PyErr_SetString(PyExc_IndexError, "complete-series candidate index out of range");
        return nullptr;
    }
    const auto& candidate = response->series[static_cast<std::size_t>(index)];
    const auto& board = candidate.board;
    PyObject* result = PyTuple_New(18);
    if (result == nullptr) {
        return nullptr;
    }
    const std::array<spc::native::Bitboard, 10> masks = {
        board.pawns,
        board.knights,
        board.bishops,
        board.rooks,
        board.queens,
        board.kings,
        board.occupied[1],
        board.occupied[0],
        board.promoted,
        board.castling_rights,
    };
    for (std::size_t item = 0; item < masks.size(); ++item) {
        PyObject* value = PyLong_FromUnsignedLongLong(masks[item]);
        if (value == nullptr) {
            Py_DECREF(result);
            return nullptr;
        }
        PyTuple_SET_ITEM(result, static_cast<Py_ssize_t>(item), value);
    }
    PyObject* turn = PyBool_FromLong(board.white_to_move ? 1 : 0);
    PyObject* halfmove = PyLong_FromLongLong(candidate.halfmove_clock);
    PyObject* fullmove = PyLong_FromLongLong(candidate.fullmove_number);
    PyObject* series_number = PyLong_FromLongLong(candidate.series_number);
    PyObject* quiet_series = PyLong_FromLongLong(candidate.quiet_series);
    PyObject* ep_targets = square_vector_tuple(candidate.ep_targets);
    PyObject* outcome = PyLong_FromLong(static_cast<long>(candidate.outcome));
    PyObject* ended_by_check = PyBool_FromLong(candidate.ended_by_check ? 1 : 0);
    if (
        turn == nullptr
        || halfmove == nullptr
        || fullmove == nullptr
        || series_number == nullptr
        || quiet_series == nullptr
        || ep_targets == nullptr
        || outcome == nullptr
        || ended_by_check == nullptr
    ) {
        Py_XDECREF(turn);
        Py_XDECREF(halfmove);
        Py_XDECREF(fullmove);
        Py_XDECREF(series_number);
        Py_XDECREF(quiet_series);
        Py_XDECREF(ep_targets);
        Py_XDECREF(outcome);
        Py_XDECREF(ended_by_check);
        Py_DECREF(result);
        return nullptr;
    }
    PyTuple_SET_ITEM(result, 10, turn);
    PyTuple_SET_ITEM(result, 11, halfmove);
    PyTuple_SET_ITEM(result, 12, fullmove);
    PyTuple_SET_ITEM(result, 13, series_number);
    PyTuple_SET_ITEM(result, 14, quiet_series);
    PyTuple_SET_ITEM(result, 15, ep_targets);
    PyTuple_SET_ITEM(result, 16, outcome);
    PyTuple_SET_ITEM(result, 17, ended_by_check);
    return result;
}

PyObject* optional_distance_object(
    const std::optional<std::int64_t>& distance
) {
    if (!distance.has_value()) {
        Py_RETURN_NONE;
    }
    return PyLong_FromLongLong(*distance);
}

PyObject* py_teacher_value_features_v3_impl(
    PyObject* arguments,
    const bool include_receipt
) {
    constexpr Py_ssize_t MIN_ARGUMENT_COUNT = 14;
    constexpr Py_ssize_t MAX_ARGUMENT_COUNT = 15;
    const Py_ssize_t argument_count = PyTuple_GET_SIZE(arguments);
    if (
        argument_count < MIN_ARGUMENT_COUNT
        || argument_count > MAX_ARGUMENT_COUNT
    ) {
        PyErr_Format(
            PyExc_TypeError,
            "%s() takes 14 or 15 arguments (%zd given)",
            include_receipt
                ? "teacher_value_features_v3_with_receipt"
                : "teacher_value_features_v3",
            argument_count
        );
        return nullptr;
    }

    std::array<unsigned long long, 10> masks{};
    for (Py_ssize_t index = 0; index < 10; ++index) {
        masks[static_cast<std::size_t>(index)] = PyLong_AsUnsignedLongLong(
            PyTuple_GET_ITEM(arguments, index)
        );
        if (
            masks[static_cast<std::size_t>(index)]
                == static_cast<unsigned long long>(-1)
            && PyErr_Occurred()
        ) {
            return nullptr;
        }
    }
    const int white_to_move = PyObject_IsTrue(PyTuple_GET_ITEM(arguments, 10));
    if (white_to_move < 0) {
        return nullptr;
    }
    const long long series_number = PyLong_AsLongLong(
        PyTuple_GET_ITEM(arguments, 11)
    );
    if (series_number == -1 && PyErr_Occurred()) {
        return nullptr;
    }
    std::vector<int> ep_targets;
    try {
        if (!parse_square_sequence(
                PyTuple_GET_ITEM(arguments, 12),
                ep_targets,
                "ep_targets must be an iterable of squares"
            )) {
            return nullptr;
        }
    } catch (const std::bad_alloc&) {
        return PyErr_NoMemory();
    } catch (const std::length_error&) {
        return PyErr_NoMemory();
    }
    const unsigned long long max_reach_positions = PyLong_AsUnsignedLongLong(
        PyTuple_GET_ITEM(arguments, 13)
    );
    if (
        max_reach_positions == static_cast<unsigned long long>(-1)
        && PyErr_Occurred()
    ) {
        return nullptr;
    }
    std::size_t feature_count = spc::native::TEACHER_VALUE_FEATURE_COUNT;
    if (argument_count == MAX_ARGUMENT_COUNT) {
        const unsigned long long supplied = PyLong_AsUnsignedLongLong(
            PyTuple_GET_ITEM(arguments, 14)
        );
        if (supplied == static_cast<unsigned long long>(-1) && PyErr_Occurred()) {
            return nullptr;
        }
        if (
            !(
                supplied == 7
                || supplied == 14
                || supplied == 19
                || supplied == 38
                || supplied == 44
                || supplied == 47
            )
        ) {
            PyErr_SetString(
                PyExc_ValueError,
                "teacher feature count must be a frozen prefix group"
            );
            return nullptr;
        }
        feature_count = static_cast<std::size_t>(supplied);
    }

    const spc::native::BoardState board{
        masks[0],
        masks[1],
        masks[2],
        masks[3],
        masks[4],
        masks[5],
        {masks[7], masks[6]},
        masks[8],
        masks[9],
        white_to_move != 0,
    };
    std::optional<spc::native::TeacherValueFeaturesV3> evaluated;
    try {
        evaluated = spc::native::teacher_value_features_v3(
            board,
            ep_targets,
            series_number,
            max_reach_positions,
            feature_count
        );
    } catch (const std::bad_alloc&) {
        return PyErr_NoMemory();
    } catch (...) {
        PyErr_SetString(
            PyExc_RuntimeError,
            "native teacher-value feature extraction failed"
        );
        return nullptr;
    }
    if (!evaluated.has_value()) {
        PyErr_SetString(
            PyExc_OverflowError,
            "native teacher-value feature extraction exceeded signed 64-bit arithmetic"
        );
        return nullptr;
    }
    PyObject* result = PyTuple_New(static_cast<Py_ssize_t>(feature_count));
    if (result == nullptr) {
        return nullptr;
    }
    for (std::size_t index = 0; index < feature_count; ++index) {
        PyObject* value = PyLong_FromLongLong(evaluated->values[index]);
        if (value == nullptr) {
            Py_DECREF(result);
            return nullptr;
        }
        PyTuple_SET_ITEM(result, static_cast<Py_ssize_t>(index), value);
    }
    if (!include_receipt) {
        return result;
    }
    PyObject* receipt = Py_BuildValue(
        "{sKsKsKsKsOsO}",
        "white_reach_positions",
        static_cast<unsigned long long>(evaluated->white_reach.nodes),
        "black_reach_positions",
        static_cast<unsigned long long>(evaluated->black_reach.nodes),
        "direct_move_variants",
        static_cast<unsigned long long>(evaluated->direct_move_variants),
        "two_move_variants",
        static_cast<unsigned long long>(evaluated->two_move_variants),
        "white_reach_complete",
        evaluated->white_reach.complete ? Py_True : Py_False,
        "black_reach_complete",
        evaluated->black_reach.complete ? Py_True : Py_False
    );
    if (receipt == nullptr) {
        Py_DECREF(result);
        return nullptr;
    }
    PyObject* packaged = PyTuple_Pack(2, result, receipt);
    Py_DECREF(result);
    Py_DECREF(receipt);
    return packaged;
}

PyObject* py_teacher_value_features_v3(PyObject*, PyObject* arguments) {
    return py_teacher_value_features_v3_impl(arguments, false);
}

PyObject* py_teacher_value_features_v3_with_receipt(
    PyObject*,
    PyObject* arguments
) {
    return py_teacher_value_features_v3_impl(arguments, true);
}

PyObject* py_deep_teacher_score_v1(PyObject*, PyObject* arguments) {
    PyObject* feature_object = nullptr;
    PyObject* coefficient_object = nullptr;
    long long fixed_point_scale = 0;
    if (!PyArg_ParseTuple(
            arguments,
            "OOL:deep_teacher_score_v1",
            &feature_object,
            &coefficient_object,
            &fixed_point_scale
        )) {
        return nullptr;
    }
    PyObject* feature_sequence = PySequence_Fast(
        feature_object,
        "features must be an iterable of exact integers"
    );
    if (feature_sequence == nullptr) {
        return nullptr;
    }
    PyObject* coefficient_sequence = PySequence_Fast(
        coefficient_object,
        "coefficients must be an iterable of exact integers"
    );
    if (coefficient_sequence == nullptr) {
        Py_DECREF(feature_sequence);
        return nullptr;
    }
    const Py_ssize_t feature_count = PySequence_Fast_GET_SIZE(feature_sequence);
    const Py_ssize_t coefficient_count = PySequence_Fast_GET_SIZE(
        coefficient_sequence
    );
    if (
        feature_count
            != static_cast<Py_ssize_t>(spc::native::TEACHER_VALUE_FEATURE_COUNT)
        || !(
            coefficient_count == 7
            || coefficient_count == 14
            || coefficient_count == 19
            || coefficient_count == 38
            || coefficient_count == 44
            || coefficient_count == 47
        )
        || !PyLong_CheckExact(PyTuple_GET_ITEM(arguments, 2))
        || fixed_point_scale != spc::native::DEEP_TEACHER_FIXED_POINT_SCALE
    ) {
        Py_DECREF(feature_sequence);
        Py_DECREF(coefficient_sequence);
        PyErr_SetString(
            PyExc_ValueError,
            "deep teacher model must use 47 features, a frozen prefix group, and scale 1000000000"
        );
        return nullptr;
    }

    spc::native::TeacherValueFeaturesV3 features;
    spc::native::DeepTeacherLinearModelV1 model;
    model.feature_count = static_cast<std::size_t>(coefficient_count);
    model.fixed_point_scale = fixed_point_scale;
    for (Py_ssize_t index = 0; index < feature_count; ++index) {
        if (!PyLong_CheckExact(PySequence_Fast_GET_ITEM(feature_sequence, index))) {
            Py_DECREF(feature_sequence);
            Py_DECREF(coefficient_sequence);
            PyErr_SetString(PyExc_TypeError, "features must be exact integers");
            return nullptr;
        }
        const long long value = PyLong_AsLongLong(
            PySequence_Fast_GET_ITEM(feature_sequence, index)
        );
        if (value == -1 && PyErr_Occurred()) {
            Py_DECREF(feature_sequence);
            Py_DECREF(coefficient_sequence);
            return nullptr;
        }
        features.values[static_cast<std::size_t>(index)] = value;
    }
    for (Py_ssize_t index = 0; index < coefficient_count; ++index) {
        if (!PyLong_CheckExact(PySequence_Fast_GET_ITEM(coefficient_sequence, index))) {
            Py_DECREF(feature_sequence);
            Py_DECREF(coefficient_sequence);
            PyErr_SetString(PyExc_TypeError, "coefficients must be exact integers");
            return nullptr;
        }
        const long long value = PyLong_AsLongLong(
            PySequence_Fast_GET_ITEM(coefficient_sequence, index)
        );
        if (value == -1 && PyErr_Occurred()) {
            Py_DECREF(feature_sequence);
            Py_DECREF(coefficient_sequence);
            return nullptr;
        }
        model.coefficients[static_cast<std::size_t>(index)] = value;
    }
    Py_DECREF(feature_sequence);
    Py_DECREF(coefficient_sequence);

    const auto score = spc::native::deep_teacher_score_v1(features, model);
    if (!score.has_value()) {
        PyErr_SetString(
            PyExc_OverflowError,
            "deep teacher fixed-point dot product exceeded signed 64-bit arithmetic"
        );
        return nullptr;
    }
    return PyLong_FromLongLong(*score);
}

PyObject* py_proof_aware_root_precedes_v1(PyObject*, PyObject* arguments) {
    constexpr Py_ssize_t ARGUMENT_COUNT = 7;
    if (PyTuple_GET_SIZE(arguments) != ARGUMENT_COUNT) {
        PyErr_Format(
            PyExc_TypeError,
            "proof_aware_root_precedes_v1() takes exactly %zd arguments (%zd given)",
            ARGUMENT_COUNT,
            PyTuple_GET_SIZE(arguments)
        );
        return nullptr;
    }

    PyObject* mover_object = PyTuple_GET_ITEM(arguments, 0);
    PyObject* left_score_object = PyTuple_GET_ITEM(arguments, 1);
    PyObject* left_bounds_object = PyTuple_GET_ITEM(arguments, 2);
    PyObject* left_notation_object = PyTuple_GET_ITEM(arguments, 3);
    PyObject* right_score_object = PyTuple_GET_ITEM(arguments, 4);
    PyObject* right_bounds_object = PyTuple_GET_ITEM(arguments, 5);
    PyObject* right_notation_object = PyTuple_GET_ITEM(arguments, 6);
    if (!PyBool_Check(mover_object)) {
        PyErr_SetString(PyExc_TypeError, "mover_white must be an exact bool");
        return nullptr;
    }
    if (
        !PyLong_CheckExact(left_score_object)
        || !PyLong_CheckExact(right_score_object)
    ) {
        PyErr_SetString(PyExc_TypeError, "root scores must be exact integers");
        return nullptr;
    }
    if (
        !PyUnicode_CheckExact(left_notation_object)
        || !PyUnicode_CheckExact(right_notation_object)
    ) {
        PyErr_SetString(PyExc_TypeError, "machine notations must be exact strings");
        return nullptr;
    }

    const long long left_score = PyLong_AsLongLong(left_score_object);
    if (left_score == -1 && PyErr_Occurred()) return nullptr;
    const long long right_score = PyLong_AsLongLong(right_score_object);
    if (right_score == -1 && PyErr_Occurred()) return nullptr;

    const auto parse_bounds = [](
        PyObject* object,
        std::array<int, 2>& target
    ) -> bool {
        PyObject* sequence = PySequence_Fast(
            object,
            "proof bounds must be a two-item iterable"
        );
        if (sequence == nullptr) return false;
        if (PySequence_Fast_GET_SIZE(sequence) != 2) {
            Py_DECREF(sequence);
            PyErr_SetString(PyExc_ValueError, "proof bounds must contain two values");
            return false;
        }
        for (Py_ssize_t index = 0; index < 2; ++index) {
            PyObject* value = PySequence_Fast_GET_ITEM(sequence, index);
            if (!PyLong_CheckExact(value)) {
                Py_DECREF(sequence);
                PyErr_SetString(PyExc_TypeError, "proof bounds must be exact integers");
                return false;
            }
            const long parsed = PyLong_AsLong(value);
            if ((parsed == -1 && PyErr_Occurred()) || parsed < -1 || parsed > 1) {
                Py_DECREF(sequence);
                if (!PyErr_Occurred()) {
                    PyErr_SetString(PyExc_ValueError, "proof bounds must be in [-1, 1]");
                }
                return false;
            }
            target[static_cast<std::size_t>(index)] = static_cast<int>(parsed);
        }
        Py_DECREF(sequence);
        return true;
    };

    std::array<int, 2> left_bounds{};
    std::array<int, 2> right_bounds{};
    if (
        !parse_bounds(left_bounds_object, left_bounds)
        || !parse_bounds(right_bounds_object, right_bounds)
    ) {
        return nullptr;
    }

    Py_ssize_t left_length = 0;
    const char* left_notation = PyUnicode_AsUTF8AndSize(
        left_notation_object,
        &left_length
    );
    if (left_notation == nullptr) return nullptr;
    Py_ssize_t right_length = 0;
    const char* right_notation = PyUnicode_AsUTF8AndSize(
        right_notation_object,
        &right_length
    );
    if (right_notation == nullptr) return nullptr;

    const spc::native::ProofAwareRootCandidateV1 left{
        left_score,
        left_bounds,
        std::string_view(left_notation, static_cast<std::size_t>(left_length)),
    };
    const spc::native::ProofAwareRootCandidateV1 right{
        right_score,
        right_bounds,
        std::string_view(right_notation, static_cast<std::size_t>(right_length)),
    };
    if (spc::native::proof_aware_root_precedes_v1(
            mover_object == Py_True,
            left,
            right
        )) {
        Py_RETURN_TRUE;
    }
    Py_RETURN_FALSE;
}

PyObject* py_full_evaluate(PyObject*, PyObject* arguments) {
    constexpr Py_ssize_t ARGUMENT_COUNT = 21;
    if (PyTuple_GET_SIZE(arguments) != ARGUMENT_COUNT) {
        PyErr_Format(
            PyExc_TypeError,
            "full_evaluate() takes exactly %zd arguments (%zd given)",
            ARGUMENT_COUNT,
            PyTuple_GET_SIZE(arguments)
        );
        return nullptr;
    }

    std::array<unsigned long long, 10> masks{};
    for (Py_ssize_t index = 0; index < 10; ++index) {
        masks[static_cast<std::size_t>(index)] = PyLong_AsUnsignedLongLong(
            PyTuple_GET_ITEM(arguments, index)
        );
        if (
            masks[static_cast<std::size_t>(index)]
                == static_cast<unsigned long long>(-1)
            && PyErr_Occurred()
        ) {
            return nullptr;
        }
    }
    const int white_to_move = PyObject_IsTrue(PyTuple_GET_ITEM(arguments, 10));
    if (white_to_move < 0) {
        return nullptr;
    }
    const long long series_number = PyLong_AsLongLong(
        PyTuple_GET_ITEM(arguments, 11)
    );
    if (series_number == -1 && PyErr_Occurred()) {
        return nullptr;
    }
    std::vector<int> ep_targets;
    try {
        if (!parse_square_sequence(
                PyTuple_GET_ITEM(arguments, 12),
                ep_targets,
                "ep_targets must be an iterable of squares"
            )) {
            return nullptr;
        }
    } catch (const std::bad_alloc&) {
        return PyErr_NoMemory();
    } catch (const std::length_error&) {
        return PyErr_NoMemory();
    }
    const unsigned long long max_reach_positions = PyLong_AsUnsignedLongLong(
        PyTuple_GET_ITEM(arguments, 13)
    );
    if (
        max_reach_positions == static_cast<unsigned long long>(-1)
        && PyErr_Occurred()
    ) {
        return nullptr;
    }
    std::array<long long, 7> weight_values{};
    for (Py_ssize_t index = 0; index < 7; ++index) {
        weight_values[static_cast<std::size_t>(index)] = PyLong_AsLongLong(
            PyTuple_GET_ITEM(arguments, index + 14)
        );
        if (
            weight_values[static_cast<std::size_t>(index)] == -1
            && PyErr_Occurred()
        ) {
            return nullptr;
        }
    }

    const spc::native::BoardState board{
        masks[0],
        masks[1],
        masks[2],
        masks[3],
        masks[4],
        masks[5],
        {masks[7], masks[6]},
        masks[8],
        masks[9],
        white_to_move != 0,
    };
    const spc::native::FullWeights weights{
        weight_values[0],
        weight_values[1],
        weight_values[2],
        weight_values[3],
        weight_values[4],
        weight_values[5],
        weight_values[6],
    };

    std::optional<spc::native::FullEvaluation> evaluated;
    try {
        evaluated = spc::native::full_evaluate(
            board,
            ep_targets,
            series_number,
            max_reach_positions,
            weights
        );
    } catch (const std::bad_alloc&) {
        return PyErr_NoMemory();
    } catch (...) {
        PyErr_SetString(PyExc_RuntimeError, "native full evaluation failed");
        return nullptr;
    }
    if (!evaluated.has_value()) {
        PyErr_SetString(
            PyExc_OverflowError,
            "native full evaluation exceeded signed 64-bit arithmetic"
        );
        return nullptr;
    }

    PyObject* result = PyTuple_New(16);
    if (result == nullptr) {
        return nullptr;
    }
    const std::array<std::int64_t, 8> terms = {
        evaluated->total,
        evaluated->material,
        evaluated->king_space,
        evaluated->series_reach,
        evaluated->promotion_corridors,
        evaluated->immediate_vulnerability,
        evaluated->useful_mobility,
        evaluated->boundary_check,
    };
    for (std::size_t index = 0; index < terms.size(); ++index) {
        PyObject* value = PyLong_FromLongLong(terms[index]);
        if (value == nullptr) {
            Py_DECREF(result);
            return nullptr;
        }
        PyTuple_SET_ITEM(result, static_cast<Py_ssize_t>(index), value);
    }
    PyObject* white_distance = optional_distance_object(
        evaluated->white_reach.distance
    );
    PyObject* black_distance = optional_distance_object(
        evaluated->black_reach.distance
    );
    PyObject* reach_complete = PyBool_FromLong(
        evaluated->white_reach.complete && evaluated->black_reach.complete
    );
    PyObject* white_nodes = PyLong_FromUnsignedLongLong(
        evaluated->white_reach.nodes
    );
    PyObject* black_nodes = PyLong_FromUnsignedLongLong(
        evaluated->black_reach.nodes
    );
    PyObject* capture_positions = PyLong_FromUnsignedLongLong(
        evaluated->capture_reach_positions
    );
    PyObject* capture_complete = PyBool_FromLong(
        evaluated->capture_reach_complete
    );
    PyObject* tactical_unstable = PyBool_FromLong(
        evaluated->tactical_unstable
    );
    if (
        white_distance == nullptr
        || black_distance == nullptr
        || reach_complete == nullptr
        || white_nodes == nullptr
        || black_nodes == nullptr
        || capture_positions == nullptr
        || capture_complete == nullptr
        || tactical_unstable == nullptr
    ) {
        Py_XDECREF(white_distance);
        Py_XDECREF(black_distance);
        Py_XDECREF(reach_complete);
        Py_XDECREF(white_nodes);
        Py_XDECREF(black_nodes);
        Py_XDECREF(capture_positions);
        Py_XDECREF(capture_complete);
        Py_XDECREF(tactical_unstable);
        Py_DECREF(result);
        return nullptr;
    }
    PyTuple_SET_ITEM(result, 8, white_distance);
    PyTuple_SET_ITEM(result, 9, black_distance);
    PyTuple_SET_ITEM(result, 10, reach_complete);
    PyTuple_SET_ITEM(result, 11, white_nodes);
    PyTuple_SET_ITEM(result, 12, black_nodes);
    PyTuple_SET_ITEM(result, 13, capture_positions);
    PyTuple_SET_ITEM(result, 14, capture_complete);
    PyTuple_SET_ITEM(result, 15, tactical_unstable);
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
        const std::string uci = spc::native::legal_move_uci(move);
        PyObject* entry = Py_BuildValue(
            "(siiii)",
            uci.c_str(),
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
        const std::string uci = spc::native::legal_move_uci(item.move);
        PyObject* entry = Py_BuildValue(
            "(siiiiKKKKKKKKKKiii)",
            uci.c_str(),
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

constexpr const char* SUBTREE_SEARCH_CAPSULE =
    "scottish_progressive.SubtreeSearchSession.v1";

void destroy_subtree_search(PyObject* capsule) noexcept {
    void* pointer = PyCapsule_GetPointer(capsule, SUBTREE_SEARCH_CAPSULE);
    if (pointer == nullptr) {
        PyErr_Clear();
        return;
    }
    delete static_cast<spc::native::SubtreeSearchSession*>(pointer);
}

spc::native::SubtreeSearchSession* subtree_search_session(PyObject* capsule) {
    return static_cast<spc::native::SubtreeSearchSession*>(
        PyCapsule_GetPointer(capsule, SUBTREE_SEARCH_CAPSULE)
    );
}

bool parse_exact_signed_sequence(
    PyObject* object,
    long long* values,
    Py_ssize_t expected,
    const char* label
) {
    PyObject* sequence = PySequence_Fast(object, label);
    if (sequence == nullptr) {
        return false;
    }
    if (PySequence_Fast_GET_SIZE(sequence) != expected) {
        Py_DECREF(sequence);
        PyErr_SetString(PyExc_ValueError, label);
        return false;
    }
    for (Py_ssize_t index = 0; index < expected; ++index) {
        values[index] = PyLong_AsLongLong(
            PySequence_Fast_GET_ITEM(sequence, index)
        );
        if (values[index] == -1 && PyErr_Occurred()) {
            Py_DECREF(sequence);
            return false;
        }
    }
    Py_DECREF(sequence);
    return true;
}

bool parse_subtree_deep_teacher_model(
    PyObject* object,
    std::optional<spc::native::SubtreeDeepTeacherValueModel>& result
) {
    if (object == Py_None) {
        result.reset();
        return true;
    }
    PyObject* sequence = PySequence_Fast(
        object,
        "native deep-teacher model must contain eight fields"
    );
    if (sequence == nullptr) {
        return false;
    }
    if (PySequence_Fast_GET_SIZE(sequence) != 8) {
        Py_DECREF(sequence);
        PyErr_SetString(
            PyExc_ValueError,
            "native deep-teacher model must contain eight fields"
        );
        return false;
    }
    std::array<std::string, 5> identities;
    for (Py_ssize_t index = 0; index < 5; ++index) {
        Py_ssize_t length = 0;
        const char* value = PyUnicode_AsUTF8AndSize(
            PySequence_Fast_GET_ITEM(sequence, index),
            &length
        );
        if (value == nullptr) {
            Py_DECREF(sequence);
            return false;
        }
        identities[static_cast<std::size_t>(index)] = std::string(
            value,
            static_cast<std::size_t>(length)
        );
    }
    const std::size_t feature_count = PyLong_AsSize_t(
        PySequence_Fast_GET_ITEM(sequence, 5)
    );
    if (feature_count == static_cast<std::size_t>(-1) && PyErr_Occurred()) {
        Py_DECREF(sequence);
        return false;
    }
    if (feature_count > spc::native::TEACHER_VALUE_FEATURE_COUNT) {
        Py_DECREF(sequence);
        PyErr_SetString(
            PyExc_ValueError,
            "native deep-teacher feature count is invalid"
        );
        return false;
    }
    std::array<long long, spc::native::TEACHER_VALUE_FEATURE_COUNT>
        coefficients{};
    if (!parse_exact_signed_sequence(
            PySequence_Fast_GET_ITEM(sequence, 6),
            coefficients.data(),
            static_cast<Py_ssize_t>(feature_count),
            "native deep-teacher coefficient count is invalid"
        )) {
        Py_DECREF(sequence);
        return false;
    }
    const long long scale = PyLong_AsLongLong(
        PySequence_Fast_GET_ITEM(sequence, 7)
    );
    if (scale == -1 && PyErr_Occurred()) {
        Py_DECREF(sequence);
        return false;
    }
    Py_DECREF(sequence);
    spc::native::SubtreeDeepTeacherValueModel parsed;
    parsed.base_profile_id = std::move(identities[0]);
    parsed.variant_id = std::move(identities[1]);
    parsed.model_id = std::move(identities[2]);
    parsed.model_sha256 = std::move(identities[3]);
    parsed.native_source_identity = std::move(identities[4]);
    parsed.linear.feature_count = feature_count;
    parsed.linear.fixed_point_scale = scale;
    for (std::size_t index = 0; index < feature_count; ++index) {
        parsed.linear.coefficients[index] = coefficients[index];
    }
    result = std::move(parsed);
    return true;
}

bool parse_subtree_state(
    PyObject* object,
    spc::native::SubtreeState& state
) {
    PyObject* sequence = PySequence_Fast(
        object,
        "native subtree state must contain sixteen fields"
    );
    if (sequence == nullptr) {
        return false;
    }
    if (PySequence_Fast_GET_SIZE(sequence) != 16) {
        Py_DECREF(sequence);
        PyErr_SetString(
            PyExc_ValueError,
            "native subtree state must contain sixteen fields"
        );
        return false;
    }
    std::array<unsigned long long, 10> bitboards{};
    for (Py_ssize_t index = 0; index < 10; ++index) {
        bitboards[static_cast<std::size_t>(index)] = PyLong_AsUnsignedLongLong(
            PySequence_Fast_GET_ITEM(sequence, index)
        );
        if (
            bitboards[static_cast<std::size_t>(index)]
                == static_cast<unsigned long long>(-1)
            && PyErr_Occurred()
        ) {
            Py_DECREF(sequence);
            return false;
        }
    }
    const int white_to_move = PyObject_IsTrue(
        PySequence_Fast_GET_ITEM(sequence, 10)
    );
    if (white_to_move < 0) {
        Py_DECREF(sequence);
        return false;
    }
    std::array<long long, 4> integers{};
    for (Py_ssize_t index = 0; index < 4; ++index) {
        integers[static_cast<std::size_t>(index)] = PyLong_AsLongLong(
            PySequence_Fast_GET_ITEM(sequence, index + 11)
        );
        if (
            integers[static_cast<std::size_t>(index)] == -1
            && PyErr_Occurred()
        ) {
            Py_DECREF(sequence);
            return false;
        }
    }
    std::vector<int> ep_targets;
    const bool parsed_targets = parse_square_sequence(
        PySequence_Fast_GET_ITEM(sequence, 15),
        ep_targets,
        "native subtree ep_targets must be an iterable of squares"
    );
    Py_DECREF(sequence);
    if (!parsed_targets) {
        return false;
    }
    state = spc::native::SubtreeState{
        {
            bitboards[0],
            bitboards[1],
            bitboards[2],
            bitboards[3],
            bitboards[4],
            bitboards[5],
            {bitboards[7], bitboards[6]},
            bitboards[8],
            bitboards[9],
            white_to_move != 0,
        },
        integers[0],
        integers[1],
        integers[2],
        integers[3],
        std::move(ep_targets),
    };
    return true;
}

bool parse_remaining_deadline(
    PyObject* object,
    std::optional<std::chrono::steady_clock::time_point>& deadline
) {
    deadline.reset();
    if (object == Py_None) {
        return true;
    }
    const unsigned long long raw = PyLong_AsUnsignedLongLong(object);
    if (raw == static_cast<unsigned long long>(-1) && PyErr_Occurred()) {
        return false;
    }
    const auto now = std::chrono::steady_clock::now();
    const auto bounded = std::min<unsigned long long>(
        raw,
        static_cast<unsigned long long>(std::numeric_limits<std::int64_t>::max())
    );
    const auto requested = std::chrono::duration_cast<
        std::chrono::steady_clock::duration
    >(std::chrono::nanoseconds(static_cast<std::int64_t>(bounded)));
    deadline = now + std::min(
        requested,
        std::chrono::steady_clock::time_point::max() - now
    );
    return true;
}

bool parse_optional_u64_credit(
    PyObject* object,
    std::optional<std::uint64_t>& credit
) {
    credit.reset();
    if (object == Py_None) {
        return true;
    }
    const unsigned long long raw = PyLong_AsUnsignedLongLong(object);
    if (raw == static_cast<unsigned long long>(-1) && PyErr_Occurred()) {
        return false;
    }
    credit = static_cast<std::uint64_t>(raw);
    return true;
}

PyObject* subtree_state_tuple(const spc::native::SubtreeState& state) {
    PyObject* targets = PyTuple_New(
        static_cast<Py_ssize_t>(state.ep_targets.size())
    );
    if (targets == nullptr) {
        return nullptr;
    }
    for (std::size_t index = 0; index < state.ep_targets.size(); ++index) {
        PyObject* square = PyLong_FromLong(state.ep_targets[index]);
        if (square == nullptr) {
            Py_DECREF(targets);
            return nullptr;
        }
        PyTuple_SET_ITEM(targets, static_cast<Py_ssize_t>(index), square);
    }
    PyObject* result = PyTuple_New(16);
    if (result == nullptr) {
        Py_DECREF(targets);
        return nullptr;
    }
    const std::array<std::uint64_t, 10> bitboards = {
        state.board.pawns,
        state.board.knights,
        state.board.bishops,
        state.board.rooks,
        state.board.queens,
        state.board.kings,
        state.board.occupied[1],
        state.board.occupied[0],
        state.board.promoted,
        state.board.castling_rights,
    };
    for (std::size_t index = 0; index < bitboards.size(); ++index) {
        PyObject* value = PyLong_FromUnsignedLongLong(bitboards[index]);
        if (value == nullptr) {
            Py_DECREF(targets);
            Py_DECREF(result);
            return nullptr;
        }
        PyTuple_SET_ITEM(result, static_cast<Py_ssize_t>(index), value);
    }
    PyObject* turn = PyBool_FromLong(state.board.white_to_move ? 1 : 0);
    const std::array<std::int64_t, 4> integers = {
        state.halfmove_clock,
        state.fullmove_number,
        state.series_number,
        state.quiet_series,
    };
    if (turn == nullptr) {
        Py_DECREF(targets);
        Py_DECREF(result);
        return nullptr;
    }
    PyTuple_SET_ITEM(result, 10, turn);
    for (std::size_t index = 0; index < integers.size(); ++index) {
        PyObject* value = PyLong_FromLongLong(integers[index]);
        if (value == nullptr) {
            Py_DECREF(targets);
            Py_DECREF(result);
            return nullptr;
        }
        PyTuple_SET_ITEM(result, static_cast<Py_ssize_t>(index + 11), value);
    }
    PyTuple_SET_ITEM(result, 15, targets);
    return result;
}

PyObject* subtree_stats_tuple(const spc::native::SubtreeSearchStats& stats) {
    const std::array<std::uint64_t, 36> values = {
        stats.nodes,
        stats.leaf_evaluations,
        stats.generated_raw_series,
        stats.generated_unique_series,
        stats.intra_series_transpositions,
        stats.tt_hits,
        stats.alpha_beta_cutoffs,
        stats.pvs_zero_window_searches,
        stats.pvs_researches,
        stats.pvs_tt_writes_rolled_back,
        stats.branch_caps,
        stats.series_generation_positions,
        stats.frontier_score_positions,
        stats.static_evaluation_positions,
        stats.evaluation_reach_positions,
        stats.evaluation_capture_positions,
        stats.incomplete_reach_evaluations,
        stats.tactical_leaf_extensions,
        stats.overlay_evaluations,
        stats.overlay_reach_positions,
        stats.overlay_direct_move_variants,
        stats.overlay_two_move_variants,
        stats.generation_positions,
        stats.frontier_prunes,
        stats.frontier_states_pruned,
        stats.frontier_paths_pruned,
        stats.tactical_frontier_states_retained,
        stats.tactical_frontier_reserve_drops,
        stats.tactical_final_series_retained,
        stats.tactical_final_reserve_drops,
        stats.peak_frontier_states,
        stats.generation_work_limit_hits,
        stats.series_generation_cache_hits,
        stats.series_generation_cache_evictions,
        stats.series_generation_cache_peak,
        stats.series_generation_cache_entries_peak,
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

PyObject* string_vector_tuple(const std::vector<std::string>& values) {
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

PyObject* retained_root_candidate_tuple(
    const spc::native::RetainedRootCandidate& candidate
) {
    PyObject* identity = PyUnicode_FromString(
        candidate.candidate_identity.c_str()
    );
    PyObject* order = PyLong_FromUnsignedLongLong(candidate.order_index);
    PyObject* order_key = PyUnicode_FromString(candidate.order_key.c_str());
    PyObject* moves = string_vector_tuple(candidate.series.path.moves);
    PyObject* count = PyLong_FromUnsignedLongLong(
        candidate.series.path.transposition_count
    );
    PyObject* state = subtree_state_tuple(spc::native::SubtreeState{
        candidate.series.board,
        candidate.series.halfmove_clock,
        candidate.series.fullmove_number,
        candidate.series.series_number,
        candidate.series.quiet_series,
        candidate.series.ep_targets,
    });
    PyObject* outcome = PyLong_FromLong(
        static_cast<long>(candidate.series.outcome)
    );
    PyObject* ended = PyBool_FromLong(
        candidate.series.ended_by_check ? 1 : 0
    );
    PyObject* terminal = candidate.terminal_score.has_value()
        ? PyLong_FromLongLong(*candidate.terminal_score)
        : Py_NewRef(Py_None);
    PyObject* proof = Py_BuildValue(
        "(ii)",
        candidate.terminal_proof_bounds[0],
        candidate.terminal_proof_bounds[1]
    );
    const std::array<PyObject*, 10> objects = {
        identity,
        order,
        order_key,
        moves,
        count,
        state,
        outcome,
        ended,
        terminal,
        proof,
    };
    if (std::any_of(objects.begin(), objects.end(), [](PyObject* object) {
            return object == nullptr;
        })) {
        for (PyObject* object : objects) {
            Py_XDECREF(object);
        }
        return nullptr;
    }
    PyObject* result = PyTuple_New(10);
    if (result == nullptr) {
        for (PyObject* object : objects) {
            Py_DECREF(object);
        }
        return nullptr;
    }
    for (std::size_t index = 0; index < objects.size(); ++index) {
        PyTuple_SET_ITEM(result, static_cast<Py_ssize_t>(index), objects[index]);
    }
    return result;
}

PyObject* retained_root_candidates_tuple(
    const std::vector<spc::native::RetainedRootCandidate>& candidates
) {
    PyObject* result = PyTuple_New(static_cast<Py_ssize_t>(candidates.size()));
    if (result == nullptr) {
        return nullptr;
    }
    for (std::size_t index = 0; index < candidates.size(); ++index) {
        PyObject* candidate = retained_root_candidate_tuple(candidates[index]);
        if (candidate == nullptr) {
            Py_DECREF(result);
            return nullptr;
        }
        PyTuple_SET_ITEM(result, static_cast<Py_ssize_t>(index), candidate);
    }
    return result;
}

PyObject* subtree_work_tuple(const spc::native::SubtreeWorkReceipt& work) {
    PyObject* cumulative = subtree_stats_tuple(work.cumulative_stats);
    PyObject* call = subtree_stats_tuple(work.call_stats);
    PyObject* counters = Py_BuildValue(
        "(KKKKKKKKKKK)",
        work.external_work,
        work.native_work_before,
        work.native_work_after,
        work.call_native_work,
        work.total_accounted_work,
        work.tt_entries,
        work.tt_entries_peak,
        work.tt_capacity,
        work.eval_entries,
        work.eval_entries_peak,
        work.eval_capacity
    );
    PyObject* credit = work.call_work_credit.has_value()
        ? PyLong_FromUnsignedLongLong(*work.call_work_credit)
        : Py_NewRef(Py_None);
    if (
        cumulative == nullptr
        || call == nullptr
        || counters == nullptr
        || credit == nullptr
    ) {
        Py_XDECREF(cumulative);
        Py_XDECREF(call);
        Py_XDECREF(counters);
        Py_XDECREF(credit);
        return nullptr;
    }
    PyObject* result = PyTuple_New(4);
    if (result == nullptr) {
        Py_DECREF(cumulative);
        Py_DECREF(call);
        Py_DECREF(counters);
        Py_DECREF(credit);
        return nullptr;
    }
    PyTuple_SET_ITEM(result, 0, cumulative);
    PyTuple_SET_ITEM(result, 1, call);
    PyTuple_SET_ITEM(result, 2, counters);
    PyTuple_SET_ITEM(result, 3, credit);
    return result;
}

PyObject* retained_root_enumeration_tuple(
    const spc::native::RetainedRootEnumerationResult& response
) {
    PyObject* status = PyLong_FromLong(static_cast<long>(response.status));
    PyObject* message = PyUnicode_FromString(response.message.c_str());
    PyObject* identity = PyUnicode_FromString(
        response.enumeration_identity.c_str()
    );
    PyObject* mover = PyBool_FromLong(response.root_white_to_move ? 1 : 0);
    PyObject* width = PyLong_FromUnsignedLongLong(response.requested_width);
    PyObject* retained = PyLong_FromUnsignedLongLong(response.retained_count);
    PyObject* complete = PyBool_FromLong(response.width_complete ? 1 : 0);
    PyObject* preferred = string_vector_tuple(response.preferred_series);
    PyObject* candidates = retained_root_candidates_tuple(response.candidates);
    PyObject* work = subtree_work_tuple(response.work);
    PyObject* selective = PyBool_FromLong(response.selective ? 1 : 0);
    PyObject* evaluation_limit = PyBool_FromLong(
        response.evaluation_work_limit_reached ? 1 : 0
    );
    PyObject* terminal_scan = PyBool_FromLong(
        response.terminal_mate_scan ? 1 : 0
    );
    const std::array<PyObject*, 13> objects = {
        status,
        message,
        identity,
        mover,
        width,
        retained,
        complete,
        preferred,
        candidates,
        work,
        selective,
        evaluation_limit,
        terminal_scan,
    };
    if (std::any_of(objects.begin(), objects.end(), [](PyObject* object) {
            return object == nullptr;
        })) {
        for (PyObject* object : objects) {
            Py_XDECREF(object);
        }
        return nullptr;
    }
    PyObject* result = PyTuple_New(static_cast<Py_ssize_t>(objects.size()));
    if (result == nullptr) {
        for (PyObject* object : objects) {
            Py_DECREF(object);
        }
        return nullptr;
    }
    for (std::size_t index = 0; index < objects.size(); ++index) {
        PyTuple_SET_ITEM(result, static_cast<Py_ssize_t>(index), objects[index]);
    }
    return result;
}

bool parse_retained_root_candidate(
    PyObject* object,
    spc::native::RetainedRootCandidate& candidate
) {
    PyObject* sequence = PySequence_Fast(
        object,
        "retained root candidate must contain ten fields"
    );
    if (sequence == nullptr) {
        return false;
    }
    if (PySequence_Fast_GET_SIZE(sequence) != 10) {
        Py_DECREF(sequence);
        PyErr_SetString(
            PyExc_ValueError,
            "retained root candidate must contain ten fields"
        );
        return false;
    }
    const char* identity = PyUnicode_AsUTF8(PySequence_Fast_GET_ITEM(sequence, 0));
    const unsigned long long order = PyLong_AsUnsignedLongLong(
        PySequence_Fast_GET_ITEM(sequence, 1)
    );
    const char* order_key = PyUnicode_AsUTF8(
        PySequence_Fast_GET_ITEM(sequence, 2)
    );
    std::vector<std::string> moves;
    const bool parsed_moves = parse_string_sequence(
        PySequence_Fast_GET_ITEM(sequence, 3),
        moves,
        "retained root candidate moves must be strings"
    );
    const unsigned long long count = PyLong_AsUnsignedLongLong(
        PySequence_Fast_GET_ITEM(sequence, 4)
    );
    spc::native::SubtreeState state;
    const bool parsed_state = parse_subtree_state(
        PySequence_Fast_GET_ITEM(sequence, 5),
        state
    );
    const long outcome = PyLong_AsLong(PySequence_Fast_GET_ITEM(sequence, 6));
    const int ended = PyObject_IsTrue(PySequence_Fast_GET_ITEM(sequence, 7));
    PyObject* terminal_object = PySequence_Fast_GET_ITEM(sequence, 8);
    std::optional<std::int64_t> terminal;
    if (terminal_object != Py_None) {
        const long long score = PyLong_AsLongLong(terminal_object);
        if (score == -1 && PyErr_Occurred()) {
            Py_DECREF(sequence);
            return false;
        }
        terminal = score;
    }
    std::array<long long, 2> proof{};
    const bool parsed_proof = parse_exact_signed_sequence(
        PySequence_Fast_GET_ITEM(sequence, 9),
        proof.data(),
        2,
        "retained root candidate proof must contain two integers"
    );
    if (
        identity == nullptr
        || order_key == nullptr
        || !parsed_moves
        || !parsed_state
        || ended < 0
        || (order == static_cast<unsigned long long>(-1) && PyErr_Occurred())
        || (count == static_cast<unsigned long long>(-1) && PyErr_Occurred())
        || (outcome == -1 && PyErr_Occurred())
        || !parsed_proof
    ) {
        Py_DECREF(sequence);
        return false;
    }
    if (
        count == 0
        || outcome < 0
        || outcome > static_cast<long>(
            spc::native::CompleteSeriesOutcome::TenSeriesDraw
        )
        || proof[0] < -1
        || proof[0] > 1
        || proof[1] < -1
        || proof[1] > 1
    ) {
        Py_DECREF(sequence);
        PyErr_SetString(PyExc_ValueError, "retained root candidate is invalid");
        return false;
    }
    try {
        candidate = spc::native::RetainedRootCandidate{
            identity,
            static_cast<std::uint64_t>(order),
            order_key,
            {
                {std::move(moves), static_cast<std::uint64_t>(count)},
                state.board,
                state.halfmove_clock,
                state.fullmove_number,
                state.series_number,
                state.quiet_series,
                std::move(state.ep_targets),
                static_cast<spc::native::CompleteSeriesOutcome>(outcome),
                ended != 0,
            },
            terminal,
            {
                static_cast<int>(proof[0]),
                static_cast<int>(proof[1]),
            },
        };
    } catch (const std::bad_alloc&) {
        Py_DECREF(sequence);
        PyErr_NoMemory();
        return false;
    }
    Py_DECREF(sequence);
    return true;
}

bool parse_retained_root_candidates(
    PyObject* object,
    std::vector<spc::native::RetainedRootCandidate>& candidates
) {
    PyObject* sequence = PySequence_Fast(
        object,
        "retained root candidates must be an iterable"
    );
    if (sequence == nullptr) {
        return false;
    }
    const Py_ssize_t size = PySequence_Fast_GET_SIZE(sequence);
    try {
        candidates.clear();
        candidates.reserve(static_cast<std::size_t>(size));
        for (Py_ssize_t index = 0; index < size; ++index) {
            spc::native::RetainedRootCandidate candidate;
            if (!parse_retained_root_candidate(
                    PySequence_Fast_GET_ITEM(sequence, index),
                    candidate
                )) {
                Py_DECREF(sequence);
                return false;
            }
            candidates.push_back(std::move(candidate));
        }
    } catch (const std::bad_alloc&) {
        Py_DECREF(sequence);
        PyErr_NoMemory();
        return false;
    }
    Py_DECREF(sequence);
    return true;
}

PyObject* retained_root_candidate_result_tuple(
    const spc::native::RetainedRootCandidateResult& response
) {
    PyObject* status = PyLong_FromLong(static_cast<long>(response.status));
    PyObject* message = PyUnicode_FromString(response.message.c_str());
    PyObject* enumeration = PyUnicode_FromString(
        response.enumeration_identity.c_str()
    );
    PyObject* identity = PyUnicode_FromString(
        response.candidate_identity.c_str()
    );
    PyObject* order = PyLong_FromUnsignedLongLong(response.order_index);
    PyObject* bound = PyLong_FromLong(static_cast<long>(response.bound));
    PyObject* score = PyLong_FromLongLong(response.score);
    PyObject* terminal = PyBool_FromLong(response.terminal ? 1 : 0);
    spc::native::RetainedRootCandidate root_record{
        response.candidate_identity,
        response.order_index,
        "",
        response.root_series,
        response.terminal
            ? std::optional<std::int64_t>{response.score}
            : std::nullopt,
        response.terminal
            ? response.proof_bounds
            : std::array<int, 2>{-1, 1},
    };
    root_record.order_key = [&response]() {
        std::string value;
        for (std::size_t index = 0;
             index < response.root_series.path.moves.size();
             ++index) {
            if (index != 0) {
                value.push_back('/');
            }
            value += response.root_series.path.moves[index];
        }
        return value;
    }();
    PyObject* root = retained_root_candidate_tuple(root_record);
    PyObject* pv = complete_series_tuple(response.child_principal_variation);
    PyObject* proof = Py_BuildValue(
        "(ii)",
        response.proof_bounds[0],
        response.proof_bounds[1]
    );
    PyObject* work = subtree_work_tuple(response.work);
    PyObject* selective = PyBool_FromLong(response.selective ? 1 : 0);
    PyObject* evaluation_limit = PyBool_FromLong(
        response.evaluation_work_limit_reached ? 1 : 0
    );
    PyObject* rolled_back = PyLong_FromUnsignedLongLong(
        response.tt_writes_rolled_back
    );
    const std::array<PyObject*, 15> objects = {
        status,
        message,
        enumeration,
        identity,
        order,
        bound,
        score,
        terminal,
        root,
        pv,
        proof,
        work,
        selective,
        evaluation_limit,
        rolled_back,
    };
    if (std::any_of(objects.begin(), objects.end(), [](PyObject* object) {
            return object == nullptr;
        })) {
        for (PyObject* object : objects) {
            Py_XDECREF(object);
        }
        return nullptr;
    }
    PyObject* result = PyTuple_New(static_cast<Py_ssize_t>(objects.size()));
    if (result == nullptr) {
        for (PyObject* object : objects) {
            Py_DECREF(object);
        }
        return nullptr;
    }
    for (std::size_t index = 0; index < objects.size(); ++index) {
        PyTuple_SET_ITEM(result, static_cast<Py_ssize_t>(index), objects[index]);
    }
    return result;
}

PyObject* py_create_subtree_search(PyObject*, PyObject* arguments) {
    unsigned long long max_series = 0;
    PyObject* max_work_object = nullptr;
    long long requested_depth = 0;
    long long mate_score = 0;
    unsigned long long cache_capacity = 0;
    unsigned long long external_cache_weight = 0;
    unsigned long long worker_threads = 0;
    int root_tactical = 0;
    PyObject* fast_weights_object = nullptr;
    PyObject* full_weights_object = nullptr;
    unsigned long long root_tt_capacity = 0;
    unsigned long long root_eval_capacity = 0;
    PyObject* deep_teacher_model_object = nullptr;
    if (!PyArg_ParseTuple(
            arguments,
            "KOLLKKKpOOKKO:create_subtree_search",
            &max_series,
            &max_work_object,
            &requested_depth,
            &mate_score,
            &cache_capacity,
            &external_cache_weight,
            &worker_threads,
            &root_tactical,
            &fast_weights_object,
            &full_weights_object,
            &root_tt_capacity,
            &root_eval_capacity,
            &deep_teacher_model_object
        )) {
        return nullptr;
    }
    std::optional<std::uint64_t> max_work;
    if (
        !parse_optional_positive_u64(
            max_work_object,
            max_work,
            "native subtree max_work must be positive"
        )
    ) {
        return nullptr;
    }
    std::array<long long, 5> fast{};
    std::array<long long, 7> full{};
    std::optional<spc::native::SubtreeDeepTeacherValueModel>
        deep_teacher_model;
    if (
        !parse_exact_signed_sequence(
            fast_weights_object,
            fast.data(),
            static_cast<Py_ssize_t>(fast.size()),
            "native subtree fast_weights must contain five integers"
        )
        || !parse_exact_signed_sequence(
            full_weights_object,
            full.data(),
            static_cast<Py_ssize_t>(full.size()),
            "native subtree full_weights must contain seven integers"
        )
        || !parse_subtree_deep_teacher_model(
            deep_teacher_model_object,
            deep_teacher_model
        )
    ) {
        return nullptr;
    }
    if (
        worker_threads < 1
        || worker_threads > std::numeric_limits<std::uint32_t>::max()
    ) {
        PyErr_SetString(PyExc_ValueError, "native subtree worker_threads is invalid");
        return nullptr;
    }
    try {
        auto session = std::make_unique<spc::native::SubtreeSearchSession>(
            spc::native::SubtreeSearchConfig{
                max_series,
                max_work,
                requested_depth,
                mate_score,
                cache_capacity,
                external_cache_weight,
                static_cast<std::uint32_t>(worker_threads),
                root_tactical != 0,
                {fast[0], fast[1], fast[2], fast[3], fast[4]},
                {
                    full[0],
                    full[1],
                    full[2],
                    full[3],
                    full[4],
                    full[5],
                    full[6],
                },
                root_tt_capacity,
                root_eval_capacity,
                std::move(deep_teacher_model),
            }
        );
        PyObject* capsule = PyCapsule_New(
            session.get(),
            SUBTREE_SEARCH_CAPSULE,
            destroy_subtree_search
        );
        if (capsule == nullptr) {
            return nullptr;
        }
        session.release();
        return capsule;
    } catch (const std::bad_alloc&) {
        return PyErr_NoMemory();
    } catch (const std::exception& error) {
        PyErr_SetString(PyExc_ValueError, error.what());
        return nullptr;
    }
}

PyObject* py_subtree_search(PyObject*, PyObject* arguments) {
    PyObject* capsule = nullptr;
    PyObject* state_object = nullptr;
    long long depth = 0;
    long long alpha = 0;
    long long beta = 0;
    long long ply_from_root = 0;
    unsigned long long external_work = 0;
    PyObject* remaining_nanoseconds_object = nullptr;
    if (!PyArg_ParseTuple(
            arguments,
            "OOLLLLKO:subtree_search",
            &capsule,
            &state_object,
            &depth,
            &alpha,
            &beta,
            &ply_from_root,
            &external_work,
            &remaining_nanoseconds_object
        )) {
        return nullptr;
    }
    auto* session = subtree_search_session(capsule);
    if (session == nullptr) {
        return nullptr;
    }
    spc::native::SubtreeState state;
    try {
        if (!parse_subtree_state(state_object, state)) {
            return nullptr;
        }
    } catch (const std::bad_alloc&) {
        return PyErr_NoMemory();
    }
    std::optional<std::chrono::steady_clock::time_point> deadline;
    if (!parse_remaining_deadline(remaining_nanoseconds_object, deadline)) {
        return nullptr;
    }

    spc::native::SubtreeSearchResult response;
    std::exception_ptr failure;
    PyThreadState* saved_thread = PyEval_SaveThread();
    try {
        response = session->search(
            state,
            depth,
            alpha,
            beta,
            ply_from_root,
            external_work,
            deadline
        );
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
    } catch (const std::exception& error) {
        PyErr_SetString(PyExc_RuntimeError, error.what());
        return nullptr;
    }

    PyObject* status = PyLong_FromLong(static_cast<long>(response.status));
    PyObject* message = PyUnicode_FromString(response.message.c_str());
    PyObject* score = PyLong_FromLongLong(response.score);
    PyObject* pv = complete_series_tuple(response.principal_variation);
    PyObject* bounds = Py_BuildValue(
        "(ii)",
        response.proof_bounds[0],
        response.proof_bounds[1]
    );
    PyObject* stats = subtree_stats_tuple(response.stats);
    PyObject* selective = PyBool_FromLong(response.selective ? 1 : 0);
    PyObject* evaluation_limit = PyBool_FromLong(
        response.evaluation_work_limit_reached ? 1 : 0
    );
    if (
        status == nullptr
        || message == nullptr
        || score == nullptr
        || pv == nullptr
        || bounds == nullptr
        || stats == nullptr
        || selective == nullptr
        || evaluation_limit == nullptr
    ) {
        Py_XDECREF(status);
        Py_XDECREF(message);
        Py_XDECREF(score);
        Py_XDECREF(pv);
        Py_XDECREF(bounds);
        Py_XDECREF(stats);
        Py_XDECREF(selective);
        Py_XDECREF(evaluation_limit);
        return nullptr;
    }
    PyObject* result = PyTuple_New(8);
    if (result == nullptr) {
        Py_DECREF(status);
        Py_DECREF(message);
        Py_DECREF(score);
        Py_DECREF(pv);
        Py_DECREF(bounds);
        Py_DECREF(stats);
        Py_DECREF(selective);
        Py_DECREF(evaluation_limit);
        return nullptr;
    }
    PyTuple_SET_ITEM(result, 0, status);
    PyTuple_SET_ITEM(result, 1, message);
    PyTuple_SET_ITEM(result, 2, score);
    PyTuple_SET_ITEM(result, 3, pv);
    PyTuple_SET_ITEM(result, 4, bounds);
    PyTuple_SET_ITEM(result, 5, stats);
    PyTuple_SET_ITEM(result, 6, selective);
    PyTuple_SET_ITEM(result, 7, evaluation_limit);
    return result;
}

PyObject* py_subtree_enumerate_root(PyObject*, PyObject* arguments) {
    PyObject* capsule = nullptr;
    PyObject* state_object = nullptr;
    PyObject* preferred_object = nullptr;
    unsigned long long requested_width = 0;
    int terminal_mate_scan = 0;
    unsigned long long external_work = 0;
    PyObject* credit_object = nullptr;
    PyObject* remaining_object = nullptr;
    if (!PyArg_ParseTuple(
            arguments,
            "OOOKpKOO:subtree_enumerate_root",
            &capsule,
            &state_object,
            &preferred_object,
            &requested_width,
            &terminal_mate_scan,
            &external_work,
            &credit_object,
            &remaining_object
        )) {
        return nullptr;
    }
    auto* session = subtree_search_session(capsule);
    if (session == nullptr) {
        return nullptr;
    }
    spc::native::SubtreeState state;
    std::vector<std::string> preferred;
    std::optional<std::uint64_t> call_work_credit;
    std::optional<std::chrono::steady_clock::time_point> deadline;
    try {
        if (
            !parse_subtree_state(state_object, state)
            || !parse_string_sequence(
                preferred_object,
                preferred,
                "preferred root series must be an iterable of UCI strings"
            )
            || !parse_optional_u64_credit(credit_object, call_work_credit)
            || !parse_remaining_deadline(remaining_object, deadline)
        ) {
            return nullptr;
        }
    } catch (const std::bad_alloc&) {
        return PyErr_NoMemory();
    }
    spc::native::RetainedRootEnumerationResult response;
    std::exception_ptr failure;
    PyThreadState* saved_thread = PyEval_SaveThread();
    try {
        response = session->enumerate_retained_root(
            state,
            preferred,
            requested_width,
            terminal_mate_scan != 0,
            external_work,
            call_work_credit,
            deadline
        );
    } catch (...) {
        failure = std::current_exception();
    }
    PyEval_RestoreThread(saved_thread);
    try {
        if (failure != nullptr) {
            std::rethrow_exception(failure);
        }
        return retained_root_enumeration_tuple(response);
    } catch (const std::bad_alloc&) {
        return PyErr_NoMemory();
    } catch (const std::exception& error) {
        PyErr_SetString(PyExc_RuntimeError, error.what());
        return nullptr;
    }
}

PyObject* py_subtree_import_root(PyObject*, PyObject* arguments) {
    PyObject* capsule = nullptr;
    PyObject* state_object = nullptr;
    PyObject* identity_object = nullptr;
    int root_white = 0;
    unsigned long long requested_width = 0;
    int width_complete = 0;
    PyObject* preferred_object = nullptr;
    PyObject* candidates_object = nullptr;
    unsigned long long external_work = 0;
    PyObject* credit_object = nullptr;
    PyObject* remaining_object = nullptr;
    if (!PyArg_ParseTuple(
            arguments,
            "OOOpKpOOKOO:subtree_import_root",
            &capsule,
            &state_object,
            &identity_object,
            &root_white,
            &requested_width,
            &width_complete,
            &preferred_object,
            &candidates_object,
            &external_work,
            &credit_object,
            &remaining_object
        )) {
        return nullptr;
    }
    auto* session = subtree_search_session(capsule);
    if (session == nullptr) {
        return nullptr;
    }
    const char* identity = PyUnicode_AsUTF8(identity_object);
    if (identity == nullptr) {
        return nullptr;
    }
    spc::native::RetainedRootImportRequest request;
    request.enumeration_identity = identity;
    request.root_white_to_move = root_white != 0;
    request.requested_width = requested_width;
    request.width_complete = width_complete != 0;
    request.external_work = external_work;
    try {
        if (
            !parse_subtree_state(state_object, request.boundary)
            || !parse_string_sequence(
                preferred_object,
                request.preferred_series,
                "preferred root series must be an iterable of UCI strings"
            )
            || !parse_retained_root_candidates(
                candidates_object,
                request.candidates
            )
            || !parse_optional_u64_credit(
                credit_object,
                request.call_work_credit
            )
            || !parse_remaining_deadline(
                remaining_object,
                request.deadline
            )
        ) {
            return nullptr;
        }
    } catch (const std::bad_alloc&) {
        return PyErr_NoMemory();
    }
    spc::native::RetainedRootEnumerationResult response;
    std::exception_ptr failure;
    PyThreadState* saved_thread = PyEval_SaveThread();
    try {
        response = session->import_retained_root(request);
    } catch (...) {
        failure = std::current_exception();
    }
    PyEval_RestoreThread(saved_thread);
    try {
        if (failure != nullptr) {
            std::rethrow_exception(failure);
        }
        return retained_root_enumeration_tuple(response);
    } catch (const std::bad_alloc&) {
        return PyErr_NoMemory();
    } catch (const std::exception& error) {
        PyErr_SetString(PyExc_RuntimeError, error.what());
        return nullptr;
    }
}

PyObject* py_subtree_search_root_candidate(PyObject*, PyObject* arguments) {
    PyObject* capsule = nullptr;
    PyObject* enumeration_object = nullptr;
    PyObject* candidate_object = nullptr;
    long long child_depth = 0;
    long long alpha = 0;
    long long beta = 0;
    unsigned long long external_work = 0;
    PyObject* credit_object = nullptr;
    PyObject* remaining_object = nullptr;
    int rollback = 0;
    if (!PyArg_ParseTuple(
            arguments,
            "OOOLLLKOOp:subtree_search_root_candidate",
            &capsule,
            &enumeration_object,
            &candidate_object,
            &child_depth,
            &alpha,
            &beta,
            &external_work,
            &credit_object,
            &remaining_object,
            &rollback
        )) {
        return nullptr;
    }
    auto* session = subtree_search_session(capsule);
    if (session == nullptr) {
        return nullptr;
    }
    const char* enumeration = PyUnicode_AsUTF8(enumeration_object);
    const char* candidate = PyUnicode_AsUTF8(candidate_object);
    if (enumeration == nullptr || candidate == nullptr) {
        return nullptr;
    }
    spc::native::RetainedRootCandidateRequest request;
    request.enumeration_identity = enumeration;
    request.candidate_identity = candidate;
    request.child_depth = child_depth;
    request.alpha = alpha;
    request.beta = beta;
    request.external_work = external_work;
    request.tt_persistence = rollback
        ? spc::native::SubtreeTTPersistence::Rollback
        : spc::native::SubtreeTTPersistence::Commit;
    if (
        !parse_optional_u64_credit(
            credit_object,
            request.call_work_credit
        )
        || !parse_remaining_deadline(remaining_object, request.deadline)
    ) {
        return nullptr;
    }
    spc::native::RetainedRootCandidateResult response;
    std::exception_ptr failure;
    PyThreadState* saved_thread = PyEval_SaveThread();
    try {
        response = session->search_retained_root_candidate(request);
    } catch (...) {
        failure = std::current_exception();
    }
    PyEval_RestoreThread(saved_thread);
    try {
        if (failure != nullptr) {
            std::rethrow_exception(failure);
        }
        return retained_root_candidate_result_tuple(response);
    } catch (const std::bad_alloc&) {
        return PyErr_NoMemory();
    } catch (const std::exception& error) {
        PyErr_SetString(PyExc_RuntimeError, error.what());
        return nullptr;
    }
}

PyObject* py_subtree_begin_transaction(PyObject*, PyObject* arguments) {
    PyObject* capsule = nullptr;
    if (!PyArg_ParseTuple(
            arguments,
            "O:subtree_begin_transaction",
            &capsule
        )) {
        return nullptr;
    }
    auto* session = subtree_search_session(capsule);
    if (session == nullptr) {
        return nullptr;
    }
    try {
        session->begin_tt_transaction();
    } catch (const std::bad_alloc&) {
        return PyErr_NoMemory();
    }
    Py_RETURN_NONE;
}

PyObject* py_subtree_rollback_transaction(PyObject*, PyObject* arguments) {
    PyObject* capsule = nullptr;
    if (!PyArg_ParseTuple(
            arguments,
            "O:subtree_rollback_transaction",
            &capsule
        )) {
        return nullptr;
    }
    auto* session = subtree_search_session(capsule);
    if (session == nullptr) {
        return nullptr;
    }
    try {
        return PyLong_FromUnsignedLongLong(
            session->rollback_tt_transaction()
        );
    } catch (const std::exception& error) {
        PyErr_SetString(PyExc_RuntimeError, error.what());
        return nullptr;
    }
}

PyObject* py_subtree_external_cache_present(PyObject*, PyObject* arguments) {
    PyObject* capsule = nullptr;
    if (!PyArg_ParseTuple(
            arguments,
            "O:subtree_external_cache_present",
            &capsule
        )) {
        return nullptr;
    }
    auto* session = subtree_search_session(capsule);
    if (session == nullptr) {
        return nullptr;
    }
    return PyBool_FromLong(session->external_cache_present() ? 1 : 0);
}

PyObject* py_subtree_touch_external_cache(PyObject*, PyObject* arguments) {
    PyObject* capsule = nullptr;
    if (!PyArg_ParseTuple(
            arguments,
            "O:subtree_touch_external_cache",
            &capsule
        )) {
        return nullptr;
    }
    auto* session = subtree_search_session(capsule);
    if (session == nullptr) {
        return nullptr;
    }
    try {
        session->touch_external_cache();
    } catch (const std::exception& error) {
        PyErr_SetString(PyExc_RuntimeError, error.what());
        return nullptr;
    }
    Py_RETURN_NONE;
}

PyObject* py_subtree_insert_external_cache(PyObject*, PyObject* arguments) {
    PyObject* capsule = nullptr;
    unsigned long long weight = 0;
    if (!PyArg_ParseTuple(
            arguments,
            "OK:subtree_insert_external_cache",
            &capsule,
            &weight
        )) {
        return nullptr;
    }
    auto* session = subtree_search_session(capsule);
    if (session == nullptr) {
        return nullptr;
    }
    try {
        session->insert_external_cache(weight);
    } catch (const std::exception& error) {
        PyErr_SetString(PyExc_RuntimeError, error.what());
        return nullptr;
    }
    Py_RETURN_NONE;
}

PyMethodDef METHODS[] = {
    {
        "neural_ordering_identity",
        py_neural_ordering_identity,
        METH_NOARGS,
        PyDoc_STR("Return the frozen Series-3 ordering model identity and scope.")
    },
    {
        "neural_ordering_parameters",
        py_neural_ordering_parameters,
        METH_NOARGS,
        PyDoc_STR("Return every compiled frozen Series-3 inference parameter.")
    },
    {
        "neural_ordering_evaluate",
        py_neural_ordering_evaluate,
        METH_VARARGS,
        PyDoc_STR("Return canonical active features and the frozen fixed-point score.")
    },
    {
        "create_subtree_search",
        py_create_subtree_search,
        METH_VARARGS,
        PyDoc_STR("Create one exact descendant-search session.")
    },
    {
        "subtree_search",
        py_subtree_search,
        METH_VARARGS,
        PyDoc_STR("Search one series-boundary descendant transactionally.")
    },
    {
        "subtree_enumerate_root",
        py_subtree_enumerate_root,
        METH_VARARGS,
        PyDoc_STR("Enumerate one deterministic retained root manifest.")
    },
    {
        "subtree_import_root",
        py_subtree_import_root,
        METH_VARARGS,
        PyDoc_STR("Validate and import one retained root manifest.")
    },
    {
        "subtree_search_root_candidate",
        py_subtree_search_root_candidate,
        METH_VARARGS,
        PyDoc_STR("Search one retained root candidate with an explicit window.")
    },
    {
        "subtree_begin_transaction",
        py_subtree_begin_transaction,
        METH_VARARGS,
        PyDoc_STR("Begin a native descendant TT transaction.")
    },
    {
        "subtree_rollback_transaction",
        py_subtree_rollback_transaction,
        METH_VARARGS,
        PyDoc_STR("Rollback the current native descendant TT transaction.")
    },
    {
        "subtree_external_cache_present",
        py_subtree_external_cache_present,
        METH_VARARGS,
        PyDoc_STR("Whether the mirrored Python root cache entry is resident.")
    },
    {
        "subtree_touch_external_cache",
        py_subtree_touch_external_cache,
        METH_VARARGS,
        PyDoc_STR("Touch the mirrored Python root cache entry.")
    },
    {
        "subtree_insert_external_cache",
        py_subtree_insert_external_cache,
        METH_VARARGS,
        PyDoc_STR("Insert the mirrored Python root cache entry.")
    },
    {
        "generate_full_game_batch_v2",
        py_generate_full_game_batch_v2,
        METH_VARARGS,
        PyDoc_STR(
            "Generate a deterministic packed v2 batch with profile attribution."
        )
    },
    {
        "generate_full_game_batch",
        py_generate_full_game_batch,
        METH_VARARGS,
        PyDoc_STR("Generate a deterministic packed batch of complete S1 games.")
    },
    {
        "prepare_complete_series",
        py_prepare_complete_series,
        METH_VARARGS,
        PyDoc_STR("Prepare an opaque exact batch with lazily decoded final states.")
    },
    {
        "prepare_complete_series_timed",
        py_prepare_complete_series_timed,
        METH_VARARGS,
        PyDoc_STR(
            "Prepare an opaque exact batch with a cooperative steady-clock deadline."
        )
    },
    {
        "prepare_complete_series_timed_parallel",
        py_prepare_complete_series_timed_parallel,
        METH_VARARGS,
        PyDoc_STR(
            "Prepare an exact timed batch with bounded opt-in native workers."
        )
    },
    {
        "complete_series_candidate",
        py_complete_series_candidate,
        METH_VARARGS,
        PyDoc_STR("Decode one final state from an opaque complete-series batch.")
    },
    {
        "generate_complete_series",
        py_generate_complete_series,
        METH_VARARGS,
        PyDoc_STR("Bulk exact complete-series generation for supported frontiers.")
    },
    {
        "full_evaluate",
        py_full_evaluate,
        METH_VARARGS,
        PyDoc_STR("Exact compiled full leaf evaluation with bounded reach work.")
    },
    {
        "teacher_value_features_v3",
        py_teacher_value_features_v3,
        METH_VARARGS,
        PyDoc_STR("Exact compiled frozen teacher feature-prefix contract.")
    },
    {
        "teacher_value_features_v3_with_receipt",
        py_teacher_value_features_v3_with_receipt,
        METH_VARARGS,
        PyDoc_STR("Exact compiled teacher features plus deterministic work receipt.")
    },
    {
        "deep_teacher_score_v1",
        py_deep_teacher_score_v1,
        METH_VARARGS,
        PyDoc_STR("Exact frozen-prefix deep-teacher fixed-point dot product.")
    },
    {
        "proof_aware_root_precedes_v1",
        py_proof_aware_root_precedes_v1,
        METH_VARARGS,
        PyDoc_STR("Proof-aware mover score and canonical-notation comparator.")
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
#endif
