#include "native_subtree_wasm.hpp"

#include "native_subtree.hpp"

#include <algorithm>
#include <array>
#include <bit>
#include <charconv>
#include <chrono>
#include <cctype>
#include <cstdint>
#include <exception>
#include <iomanip>
#include <limits>
#include <optional>
#include <sstream>
#include <string>
#include <string_view>
#include <vector>

namespace {

constexpr std::uint32_t ABI_VERSION = 1;
constexpr std::int64_t MATE_SCORE = 1'000'000;
constexpr std::uint64_t CACHE_CAPACITY = 16'384;

[[nodiscard]] spc::native::SubtreeState initial_state() {
    using spc::native::Bitboard;
    using spc::native::BoardState;
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
    return spc::native::SubtreeState{
        BoardState{
            WHITE_PAWNS | BLACK_PAWNS,
            WHITE_KNIGHTS | BLACK_KNIGHTS,
            WHITE_BISHOPS | BLACK_BISHOPS,
            WHITE_ROOKS | BLACK_ROOKS,
            WHITE_QUEEN | BLACK_QUEEN,
            WHITE_KING | BLACK_KING,
            {BLACK_OCCUPIED, WHITE_OCCUPIED},
            0,
            CASTLING_RIGHTS,
            true,
        },
        0,
        1,
        1,
        0,
        {},
    };
}

[[nodiscard]] bool parse_i64(
    std::string_view text,
    std::int64_t& value
) noexcept {
    if (text.empty()) {
        return false;
    }
    const auto result = std::from_chars(
        text.data(),
        text.data() + text.size(),
        value
    );
    return result.ec == std::errc{} && result.ptr == text.data() + text.size();
}

[[nodiscard]] bool parse_promoted(
    const char* supplied,
    spc::native::Bitboard& promoted
) noexcept {
    std::string_view text = supplied == nullptr
        ? std::string_view{}
        : std::string_view{supplied};
    if (text.empty() || text == "-") {
        promoted = 0;
        return true;
    }
    if (text.starts_with("0x") || text.starts_with("0X")) {
        text.remove_prefix(2);
    }
    if (text.empty() || text.size() > 16) {
        return false;
    }
    const auto result = std::from_chars(
        text.data(),
        text.data() + text.size(),
        promoted,
        16
    );
    return result.ec == std::errc{} && result.ptr == text.data() + text.size();
}

[[nodiscard]] bool parse_square(std::string_view name, int& square) noexcept {
    if (
        name.size() != 2
        || name[0] < 'a'
        || name[0] > 'h'
        || name[1] < '1'
        || name[1] > '8'
    ) {
        return false;
    }
    square = static_cast<int>(name[0] - 'a')
        + 8 * static_cast<int>(name[1] - '1');
    return true;
}

[[nodiscard]] bool parse_ep_targets(
    std::string_view text,
    std::vector<int>& targets
) {
    targets.clear();
    if (text.empty() || text == "-") {
        return true;
    }
    std::size_t begin = 0;
    while (begin <= text.size()) {
        const std::size_t comma = text.find(',', begin);
        const std::size_t end = comma == std::string_view::npos
            ? text.size()
            : comma;
        int square = -1;
        if (!parse_square(text.substr(begin, end - begin), square)) {
            return false;
        }
        targets.push_back(square);
        if (comma == std::string_view::npos) {
            break;
        }
        begin = comma + 1;
    }
    std::sort(targets.begin(), targets.end());
    targets.erase(std::unique(targets.begin(), targets.end()), targets.end());
    return true;
}

[[nodiscard]] bool has_piece(
    const spc::native::BoardState& board,
    spc::native::Bitboard pieces,
    bool white,
    int square
) noexcept {
    const auto bit = spc::native::Bitboard{1} << square;
    return (pieces & bit) != 0
        && (board.occupied[white ? 1 : 0] & bit) != 0;
}

[[nodiscard]] bool parse_boundary(
    const char* fen_text,
    std::int32_t series_number,
    std::int32_t quiet_series,
    const char* progressive_ep,
    const char* promoted_hex,
    spc::native::SubtreeState& state,
    std::string& error
) {
    if (fen_text == nullptr || *fen_text == '\0') {
        error = "boundary FEN is required";
        return false;
    }
    if (series_number < 1 || series_number > 256 || quiet_series < 0) {
        error = "Progressive series or quiet-series value is out of range";
        return false;
    }

    std::istringstream fields{fen_text};
    std::array<std::string, 6> fen;
    for (auto& field : fen) {
        if (!(fields >> field)) {
            error = "boundary FEN must contain six fields";
            return false;
        }
    }
    std::string extra;
    if (fields >> extra) {
        error = "boundary FEN contains trailing fields";
        return false;
    }

    spc::native::BoardState board{};
    int rank = 7;
    int file = 0;
    int separators = 0;
    for (const char symbol : fen[0]) {
        if (symbol == '/') {
            if (file != 8 || rank == 0) {
                error = "boundary FEN board rows are malformed";
                return false;
            }
            --rank;
            file = 0;
            ++separators;
            continue;
        }
        if (symbol >= '1' && symbol <= '8') {
            file += symbol - '0';
            if (file > 8) {
                error = "boundary FEN board row is too wide";
                return false;
            }
            continue;
        }
        if (file >= 8 || rank < 0) {
            error = "boundary FEN piece placement is malformed";
            return false;
        }
        const bool white = std::isupper(static_cast<unsigned char>(symbol)) != 0;
        const char piece = static_cast<char>(
            std::tolower(static_cast<unsigned char>(symbol))
        );
        const auto bit = spc::native::Bitboard{1} << (rank * 8 + file);
        spc::native::Bitboard* piece_mask = nullptr;
        switch (piece) {
            case 'p': piece_mask = &board.pawns; break;
            case 'n': piece_mask = &board.knights; break;
            case 'b': piece_mask = &board.bishops; break;
            case 'r': piece_mask = &board.rooks; break;
            case 'q': piece_mask = &board.queens; break;
            case 'k': piece_mask = &board.kings; break;
            default:
                error = "boundary FEN contains an unknown piece";
                return false;
        }
        *piece_mask |= bit;
        board.occupied[white ? 1 : 0] |= bit;
        ++file;
    }
    if (rank != 0 || file != 8 || separators != 7) {
        error = "boundary FEN does not describe eight complete ranks";
        return false;
    }
    if (fen[1] == "w") {
        board.white_to_move = true;
    } else if (fen[1] == "b") {
        board.white_to_move = false;
    } else {
        error = "boundary FEN turn must be w or b";
        return false;
    }
    if (board.white_to_move != (series_number % 2 == 1)) {
        error = "boundary FEN turn does not match Progressive series parity";
        return false;
    }
    const auto white_kings = board.kings & board.occupied[1];
    const auto black_kings = board.kings & board.occupied[0];
    if (
        white_kings == 0
        || (white_kings & (white_kings - 1)) != 0
        || black_kings == 0
        || (black_kings & (black_kings - 1)) != 0
    ) {
        error = "boundary must contain exactly one king per side";
        return false;
    }

    if (fen[2] != "-") {
        std::array<bool, 4> seen{};
        for (const char right : fen[2]) {
            int index = -1;
            int rook_square = -1;
            int king_square = -1;
            bool white = false;
            switch (right) {
                case 'K': index = 0; rook_square = 7; king_square = 4; white = true; break;
                case 'Q': index = 1; rook_square = 0; king_square = 4; white = true; break;
                case 'k': index = 2; rook_square = 63; king_square = 60; break;
                case 'q': index = 3; rook_square = 56; king_square = 60; break;
                default:
                    error = "only standard KQkq castling rights are supported";
                    return false;
            }
            if (seen[static_cast<std::size_t>(index)]) {
                error = "boundary FEN repeats a castling right";
                return false;
            }
            seen[static_cast<std::size_t>(index)] = true;
            if (
                !has_piece(board, board.kings, white, king_square)
                || !has_piece(board, board.rooks, white, rook_square)
            ) {
                error = "boundary FEN castling right has no matching king and rook";
                return false;
            }
            board.castling_rights |= spc::native::Bitboard{1} << rook_square;
        }
    }
    if (!parse_promoted(promoted_hex, board.promoted)) {
        error = "promoted_hex is not a 64-bit hexadecimal mask";
        return false;
    }
    const auto occupied = board.occupied[0] | board.occupied[1];
    if (
        (board.promoted & ~occupied) != 0
        || (board.promoted & (board.pawns | board.kings)) != 0
    ) {
        error = "promoted mask must name occupied non-pawn, non-king squares";
        return false;
    }

    std::int64_t halfmove = 0;
    std::int64_t fullmove = 0;
    if (
        !parse_i64(fen[4], halfmove)
        || !parse_i64(fen[5], fullmove)
        || halfmove < 0
        || fullmove < 1
    ) {
        error = "boundary FEN clocks are out of range";
        return false;
    }
    const std::string_view explicit_ep = progressive_ep == nullptr
        ? std::string_view{}
        : std::string_view{progressive_ep};
    const std::string_view ep_text = explicit_ep.empty()
        ? std::string_view{fen[3]}
        : explicit_ep;
    std::vector<int> supplied_targets;
    if (!parse_ep_targets(ep_text, supplied_targets)) {
        error = "progressive EP targets must be comma-separated board squares";
        return false;
    }
    std::vector<int> canonical_targets;
    const int expected_rank = board.white_to_move ? 5 : 2;
    const int pawn_offset = board.white_to_move ? -8 : 8;
    for (const int target : supplied_targets) {
        const auto target_bit = spc::native::Bitboard{1} << target;
        const int pawn_square = target + pawn_offset;
        if (
            target / 8 != expected_rank
            || pawn_square < 0
            || pawn_square >= 64
            || (occupied & target_bit) != 0
            || !has_piece(board, board.pawns, !board.white_to_move, pawn_square)
        ) {
            error = "progressive EP target has invalid rank, occupancy, or pawn";
            return false;
        }
        const auto legal = spc::native::legal_move_variants(board, {target});
        if (std::any_of(
                legal.begin(),
                legal.end(),
                [target](const spc::native::LegalMove& move) {
                    return move.required_ep_square == target;
                }
            )) {
            canonical_targets.push_back(target);
        }
    }

    state = spc::native::SubtreeState{
        board,
        halfmove,
        fullmove,
        series_number,
        quiet_series,
        std::move(canonical_targets),
    };
    return true;
}

void write_json_string(std::ostringstream& stream, const std::string& value) {
    stream << '"';
    for (const unsigned char character : value) {
        switch (character) {
            case '"': stream << "\\\""; break;
            case '\\': stream << "\\\\"; break;
            case '\b': stream << "\\b"; break;
            case '\f': stream << "\\f"; break;
            case '\n': stream << "\\n"; break;
            case '\r': stream << "\\r"; break;
            case '\t': stream << "\\t"; break;
            default:
                if (character < 0x20U) {
                    constexpr char HEX[] = "0123456789abcdef";
                    stream << "\\u00"
                           << HEX[(character >> 4U) & 0x0fU]
                           << HEX[character & 0x0fU];
                } else {
                    stream << static_cast<char>(character);
                }
        }
    }
    stream << '"';
}

constexpr int PREFIX_PAWN = 1;
constexpr int PREFIX_KNIGHT = 2;
constexpr int PREFIX_BISHOP = 3;
constexpr int PREFIX_ROOK = 4;
constexpr int PREFIX_QUEEN = 5;
constexpr int PREFIX_KING = 6;

[[nodiscard]] constexpr spc::native::Bitboard prefix_bit(
    int square
) noexcept {
    return spc::native::Bitboard{1} << square;
}

[[nodiscard]] std::string square_name(int square) {
    std::string result(2, ' ');
    result[0] = static_cast<char>('a' + (square & 7));
    result[1] = static_cast<char>('1' + (square >> 3));
    return result;
}

[[nodiscard]] int prefix_piece_type_at(
    const spc::native::BoardState& board,
    int square
) noexcept {
    const auto mask = prefix_bit(square);
    if ((board.pawns & mask) != 0) {
        return PREFIX_PAWN;
    }
    if ((board.knights & mask) != 0) {
        return PREFIX_KNIGHT;
    }
    if ((board.bishops & mask) != 0) {
        return PREFIX_BISHOP;
    }
    if ((board.rooks & mask) != 0) {
        return PREFIX_ROOK;
    }
    if ((board.queens & mask) != 0) {
        return PREFIX_QUEEN;
    }
    if ((board.kings & mask) != 0) {
        return PREFIX_KING;
    }
    return 0;
}

[[nodiscard]] char fen_piece_at(
    const spc::native::BoardState& board,
    int square
) noexcept {
    constexpr std::array<char, 7> SYMBOLS = {
        '\0', 'p', 'n', 'b', 'r', 'q', 'k',
    };
    const int piece = prefix_piece_type_at(board, square);
    if (piece == 0) {
        return '\0';
    }
    char symbol = SYMBOLS[static_cast<std::size_t>(piece)];
    if ((board.occupied[1] & prefix_bit(square)) != 0) {
        symbol = static_cast<char>(
            std::toupper(static_cast<unsigned char>(symbol))
        );
    }
    return symbol;
}

[[nodiscard]] std::string board_placement(
    const spc::native::BoardState& board
) {
    std::string result;
    result.reserve(71);
    for (int rank = 7; rank >= 0; --rank) {
        int empty = 0;
        for (int file = 0; file < 8; ++file) {
            const char piece = fen_piece_at(board, rank * 8 + file);
            if (piece == '\0') {
                ++empty;
                continue;
            }
            if (empty != 0) {
                result.push_back(static_cast<char>('0' + empty));
                empty = 0;
            }
            result.push_back(piece);
        }
        if (empty != 0) {
            result.push_back(static_cast<char>('0' + empty));
        }
        if (rank != 0) {
            result.push_back('/');
        }
    }
    return result;
}

[[nodiscard]] std::string castling_text(
    const spc::native::BoardState& board
) {
    std::string result;
    if ((board.castling_rights & prefix_bit(7)) != 0) {
        result.push_back('K');
    }
    if ((board.castling_rights & prefix_bit(0)) != 0) {
        result.push_back('Q');
    }
    if ((board.castling_rights & prefix_bit(63)) != 0) {
        result.push_back('k');
    }
    if ((board.castling_rights & prefix_bit(56)) != 0) {
        result.push_back('q');
    }
    return result.empty() ? std::string{"-"} : result;
}

[[nodiscard]] std::string board_fen(
    const spc::native::BoardState& board,
    std::int64_t halfmove_clock,
    std::int64_t fullmove_number,
    std::optional<int> ep_square = std::nullopt
) {
    std::ostringstream stream;
    stream << board_placement(board)
           << (board.white_to_move ? " w " : " b ")
           << castling_text(board) << ' ';
    if (ep_square.has_value()) {
        stream << square_name(*ep_square);
    } else {
        stream << '-';
    }
    stream << ' ' << halfmove_clock << ' ' << fullmove_number;
    return stream.str();
}

[[nodiscard]] std::string promoted_hex(
    const spc::native::BoardState& board
) {
    std::ostringstream stream;
    stream << "0x" << std::hex << board.promoted;
    return stream.str();
}

[[nodiscard]] spc::native::Bitboard ep_target_bits(
    const std::vector<int>& targets
) noexcept {
    spc::native::Bitboard result = 0;
    for (const int target : targets) {
        if (target >= 0 && target < 64) {
            result |= prefix_bit(target);
        }
    }
    return result;
}

void update_prefix_ep_targets(
    spc::native::Bitboard& pending,
    const spc::native::ExpandedMove& expanded,
    bool mover
) noexcept {
    if (!expanded.is_pawn_move) {
        return;
    }
    const int prior_target = expanded.move.from_square + (mover ? -8 : 8);
    if (prior_target >= 0 && prior_target < 64) {
        pending &= ~prefix_bit(prior_target);
    }
    if (
        std::abs(expanded.move.to_square - expanded.move.from_square) == 16
    ) {
        pending |= prefix_bit(
            (expanded.move.from_square + expanded.move.to_square) / 2
        );
    }
}

[[nodiscard]] std::optional<int> orthodox_ep_after(
    const spc::native::ExpandedMove& expanded
) noexcept {
    if (
        expanded.is_pawn_move
        && std::abs(
            expanded.move.to_square - expanded.move.from_square
        ) == 16
    ) {
        return (
            expanded.move.from_square + expanded.move.to_square
        ) / 2;
    }
    return std::nullopt;
}

[[nodiscard]] char san_piece_symbol(int piece) noexcept {
    constexpr std::array<char, 7> SYMBOLS = {
        '\0', '\0', 'N', 'B', 'R', 'Q', 'K',
    };
    return piece >= 0 && piece < static_cast<int>(SYMBOLS.size())
        ? SYMBOLS[static_cast<std::size_t>(piece)]
        : '\0';
}

[[nodiscard]] std::string san_for_move(
    const spc::native::BoardState& board,
    const std::vector<spc::native::ExpandedMove>& variants,
    const spc::native::ExpandedMove& expanded,
    spc::native::Bitboard pending_ep_before,
    bool mover
) {
    const int piece = prefix_piece_type_at(
        board,
        expanded.move.from_square
    );
    const bool castling = piece == PREFIX_KING
        && std::abs(
            expanded.move.to_square - expanded.move.from_square
        ) == 2;
    std::string result;
    if (castling) {
        result = expanded.move.to_square > expanded.move.from_square
            ? "O-O"
            : "O-O-O";
    } else {
        if (piece != PREFIX_PAWN) {
            result.push_back(san_piece_symbol(piece));
            bool other = false;
            bool same_file = false;
            bool same_rank = false;
            for (const auto& candidate : variants) {
                if (
                    candidate.move.uci == expanded.move.uci
                    || candidate.move.to_square != expanded.move.to_square
                    || prefix_piece_type_at(
                        board,
                        candidate.move.from_square
                    ) != piece
                ) {
                    continue;
                }
                other = true;
                same_file = same_file
                    || (
                        (candidate.move.from_square & 7)
                        == (expanded.move.from_square & 7)
                    );
                same_rank = same_rank
                    || (
                        (candidate.move.from_square >> 3)
                        == (expanded.move.from_square >> 3)
                    );
            }
            if (other) {
                if (!same_file) {
                    result.push_back(static_cast<char>(
                        'a' + (expanded.move.from_square & 7)
                    ));
                } else if (!same_rank) {
                    result.push_back(static_cast<char>(
                        '1' + (expanded.move.from_square >> 3)
                    ));
                } else {
                    result += square_name(expanded.move.from_square);
                }
            }
        } else if (expanded.is_capture) {
            result.push_back(static_cast<char>(
                'a' + (expanded.move.from_square & 7)
            ));
        }
        if (expanded.is_capture) {
            result.push_back('x');
        }
        result += square_name(expanded.move.to_square);
        if (expanded.move.promotion != 0) {
            result.push_back('=');
            result.push_back(san_piece_symbol(expanded.move.promotion));
        }
    }
    if (expanded.delivered_check) {
        update_prefix_ep_targets(pending_ep_before, expanded, mover);
        const auto child_ep = spc::native::canonical_ep_targets(
            expanded.child,
            pending_ep_before
        );
        result.push_back(
            spc::native::has_legal_move(expanded.child, child_ep)
                ? '+'
                : '#'
        );
    }
    return result;
}

[[nodiscard]] bool split_prefix(
    const char* supplied,
    std::vector<std::string>& moves,
    std::string& error
) {
    moves.clear();
    if (supplied == nullptr || *supplied == '\0') {
        return true;
    }
    const std::string_view text{supplied};
    std::size_t begin = 0;
    while (begin <= text.size()) {
        const std::size_t slash = text.find('/', begin);
        const std::size_t end = slash == std::string_view::npos
            ? text.size()
            : slash;
        std::size_t left = begin;
        std::size_t right = end;
        while (
            left < right
            && std::isspace(static_cast<unsigned char>(text[left])) != 0
        ) {
            ++left;
        }
        while (
            right > left
            && std::isspace(static_cast<unsigned char>(text[right - 1])) != 0
        ) {
            --right;
        }
        if (left == right) {
            error = "prefix contains an empty UCI move";
            return false;
        }
        std::string move{text.substr(left, right - left)};
        std::transform(
            move.begin(),
            move.end(),
            move.begin(),
            [](unsigned char character) {
                return static_cast<char>(std::tolower(character));
            }
        );
        if (move.size() != 4 && move.size() != 5) {
            error = "prefix contains malformed UCI text";
            return false;
        }
        moves.push_back(std::move(move));
        if (slash == std::string_view::npos) {
            break;
        }
        begin = slash + 1;
    }
    return true;
}

struct PrefixFrame {
    std::size_t index = 0;
    std::string uci;
    std::string san;
    std::string fen;
    bool gives_check = false;
};

enum class PrefixOutcome : std::uint8_t {
    None = 0,
    Checkmate = 1,
    Stalemate = 2,
    TenSeriesDraw = 3,
};

[[nodiscard]] const char* prefix_outcome_name(
    PrefixOutcome outcome
) noexcept {
    switch (outcome) {
        case PrefixOutcome::Checkmate: return "checkmate";
        case PrefixOutcome::Stalemate: return "stalemate";
        case PrefixOutcome::TenSeriesDraw: return "ten-series-draw";
        case PrefixOutcome::None: return nullptr;
    }
    return nullptr;
}

[[nodiscard]] PrefixOutcome completed_outcome(
    const spc::native::BoardState& board,
    const std::vector<int>& ep_targets,
    std::int64_t quiet_series,
    bool delivered_check
) {
    const bool legal = spc::native::has_legal_move(board, ep_targets);
    if (delivered_check && !legal) {
        return PrefixOutcome::Checkmate;
    }
    if (!delivered_check && !legal) {
        return PrefixOutcome::Stalemate;
    }
    if (
        quiet_series >= 10
        && spc::native::has_insufficient_material(board)
    ) {
        return PrefixOutcome::TenSeriesDraw;
    }
    return PrefixOutcome::None;
}

void write_string_array(
    std::ostringstream& stream,
    const std::vector<std::string>& values
) {
    stream << '[';
    bool first = true;
    for (const auto& value : values) {
        if (!first) {
            stream << ',';
        }
        first = false;
        write_json_string(stream, value);
    }
    stream << ']';
}

void write_ep_array(
    std::ostringstream& stream,
    const std::vector<int>& targets
) {
    stream << '[';
    bool first = true;
    for (const int target : targets) {
        if (!first) {
            stream << ',';
        }
        first = false;
        write_json_string(stream, square_name(target));
    }
    stream << ']';
}

void write_boundary_payload(
    std::ostringstream& stream,
    const spc::native::SubtreeState& state
) {
    const std::optional<int> fen_ep = state.ep_targets.size() == 1
        ? std::optional<int>{state.ep_targets.front()}
        : std::nullopt;
    const std::string fen = board_fen(
        state.board,
        state.halfmove_clock,
        state.fullmove_number,
        fen_ep
    );
    stream << "{\"fen\":";
    write_json_string(stream, fen);
    stream << ",\"board_fen\":";
    write_json_string(stream, fen);
    stream << ",\"series\":" << state.series_number
           << ",\"series_number\":" << state.series_number
           << ",\"side_to_move\":\""
           << (state.board.white_to_move ? "white" : "black") << '"'
           << ",\"quiet_series\":" << state.quiet_series
           << ",\"quiet_draw_pending\":"
           << (state.quiet_series >= 10 ? "true" : "false")
           << ",\"ep_targets\":";
    write_ep_array(stream, state.ep_targets);
    stream << ",\"progressive_ep\":";
    write_ep_array(stream, state.ep_targets);
    stream << ",\"promoted_hex\":";
    write_json_string(stream, promoted_hex(state.board));
    stream << '}';
}

void write_legal_move_payload(
    std::ostringstream& stream,
    const spc::native::BoardState& board,
    const std::vector<spc::native::ExpandedMove>& variants,
    spc::native::Bitboard pending_ep,
    bool mover
) {
    stream << '[';
    bool first = true;
    for (const auto& expanded : variants) {
        if (!first) {
            stream << ',';
        }
        first = false;
        stream << "{\"uci\":";
        write_json_string(stream, expanded.move.uci);
        stream << ",\"san\":";
        write_json_string(
            stream,
            san_for_move(board, variants, expanded, pending_ep, mover)
        );
        stream << ",\"from\":";
        write_json_string(stream, square_name(expanded.move.from_square));
        stream << ",\"to\":";
        write_json_string(stream, square_name(expanded.move.to_square));
        stream << ",\"promotion\":";
        if (expanded.move.promotion == 0) {
            stream << "null";
        } else {
            std::string promotion(1, static_cast<char>(std::tolower(
                static_cast<unsigned char>(
                    san_piece_symbol(expanded.move.promotion)
                )
            )));
            write_json_string(stream, promotion);
        }
        stream << ",\"capture\":"
               << (expanded.is_capture ? "true" : "false")
               << ",\"gives_check\":"
               << (expanded.delivered_check ? "true" : "false")
               << '}';
    }
    stream << ']';
}

[[nodiscard]] std::string prefix_error_json(
    const std::string& code,
    const std::string& message
) {
    std::ostringstream stream;
    stream << "{\"schema\":\"spc-boundary-prefix-v1\""
           << ",\"abi_version\":" << ABI_VERSION
           << ",\"ok\":false,\"status\":\"invalid_prefix\""
           << ",\"error_code\":";
    write_json_string(stream, code);
    stream << ",\"message\":";
    write_json_string(stream, message);
    stream << '}';
    return stream.str();
}

[[nodiscard]] std::string run_prefix_json(
    const spc::native::SubtreeState& boundary,
    const char* prefix_uci
) {
    std::vector<std::string> requested;
    std::string error;
    if (!split_prefix(prefix_uci, requested, error)) {
        return prefix_error_json("invalid-move", error);
    }
    if (
        requested.size()
        > static_cast<std::uint64_t>(boundary.series_number)
    ) {
        return prefix_error_json(
            "series-overflow",
            "prefix exceeds the Progressive series budget"
        );
    }

    const bool mover = boundary.board.white_to_move;
    spc::native::BoardState board = boundary.board;
    std::int64_t halfmove_clock = boundary.halfmove_clock;
    std::int64_t fullmove_number = boundary.fullmove_number;
    spc::native::Bitboard pending_ep = 0;
    bool made_progress = false;
    bool complete = false;
    bool ended_by_check = false;
    PrefixOutcome outcome = PrefixOutcome::None;
    std::vector<int> final_ep;
    std::int64_t final_series = boundary.series_number;
    std::int64_t final_quiet = boundary.quiet_series;
    std::vector<std::string> played;
    std::vector<std::string> sans;
    std::vector<PrefixFrame> frames;

    for (std::size_t index = 0; index < requested.size(); ++index) {
        if (complete) {
            return prefix_error_json(
                "series-complete",
                "prefix continues after the Progressive series ended"
            );
        }
        const std::vector<int> ep_targets = index == 0
            ? boundary.ep_targets
            : std::vector<int>{};
        const auto variants = spc::native::expand_legal_move_variants(
            board,
            ep_targets
        );
        const auto selected = std::find_if(
            variants.begin(),
            variants.end(),
            [&](const spc::native::ExpandedMove& expanded) {
                return expanded.move.uci == requested[index];
            }
        );
        if (selected == variants.end()) {
            return prefix_error_json(
                "illegal-move",
                "prefix contains a move that is illegal at its series index"
            );
        }

        const std::string san = san_for_move(
            board,
            variants,
            *selected,
            pending_ep,
            mover
        );
        if (selected->is_pawn_move || selected->is_capture) {
            halfmove_clock = 0;
        } else if (
            halfmove_clock == std::numeric_limits<std::int64_t>::max()
        ) {
            return prefix_error_json(
                "clock-overflow",
                "halfmove clock overflowed while replaying prefix"
            );
        } else {
            ++halfmove_clock;
        }
        if (!mover) {
            if (fullmove_number == std::numeric_limits<std::int64_t>::max()) {
                return prefix_error_json(
                    "clock-overflow",
                    "fullmove clock overflowed while replaying prefix"
                );
            }
            ++fullmove_number;
        }
        update_prefix_ep_targets(pending_ep, *selected, mover);
        made_progress = made_progress
            || selected->is_pawn_move
            || selected->is_capture;
        board = selected->child;
        played.push_back(selected->move.uci);
        sans.push_back(san);
        frames.push_back(PrefixFrame{
            played.size(),
            selected->move.uci,
            san,
            board_fen(
                board,
                halfmove_clock,
                fullmove_number,
                orthodox_ep_after(*selected)
            ),
            selected->delivered_check,
        });

        if (
            selected->delivered_check
            || played.size()
                == static_cast<std::uint64_t>(boundary.series_number)
        ) {
            if (index + 1 != requested.size()) {
                return prefix_error_json(
                    "series-complete",
                    "prefix continues after check or series-budget completion"
                );
            }
            complete = true;
            ended_by_check = selected->delivered_check;
            final_ep = spc::native::canonical_ep_targets(board, pending_ep);
            final_series = boundary.series_number + 1;
            final_quiet = made_progress ? 0 : boundary.quiet_series + 1;
            outcome = completed_outcome(
                board,
                final_ep,
                final_quiet,
                ended_by_check
            );
            break;
        }

        board.white_to_move = mover;
        if (!spc::native::has_legal_move(board, {})) {
            if (index + 1 != requested.size()) {
                return prefix_error_json(
                    "series-complete",
                    "prefix continues after Progressive stalemate"
                );
            }
            complete = true;
            outcome = spc::native::is_in_check(board)
                ? PrefixOutcome::Checkmate
                : PrefixOutcome::Stalemate;
            break;
        }
    }

    std::vector<spc::native::ExpandedMove> legal_next;
    if (!complete) {
        const std::vector<int> ep_targets = played.empty()
            ? boundary.ep_targets
            : std::vector<int>{};
        legal_next = spc::native::expand_legal_move_variants(
            board,
            ep_targets
        );
        if (legal_next.empty()) {
            complete = true;
            outcome = spc::native::is_in_check(board)
                ? PrefixOutcome::Checkmate
                : PrefixOutcome::Stalemate;
        }
    }

    const std::vector<int> display_ep = complete
        ? final_ep
        : (played.empty() ? boundary.ep_targets : std::vector<int>{});
    const std::optional<int> display_fen_ep = display_ep.size() == 1
        ? std::optional<int>{display_ep.front()}
        : std::nullopt;
    const std::string display_fen = board_fen(
        board,
        halfmove_clock,
        fullmove_number,
        display_fen_ep
    );
    const std::int64_t remaining = std::max<std::int64_t>(
        0,
        boundary.series_number - static_cast<std::int64_t>(played.size())
    );
    const std::int64_t unused_moves = complete ? remaining : 0;
    const char* outcome_name = prefix_outcome_name(outcome);

    std::ostringstream stream;
    stream << "{\"schema\":\"spc-boundary-prefix-v1\""
           << ",\"abi_version\":" << ABI_VERSION
           << ",\"ok\":true,\"status\":\"complete\""
           << ",\"boundary_state\":";
    write_boundary_payload(stream, boundary);
    stream << ",\"fen\":";
    write_json_string(stream, display_fen);
    stream << ",\"board_fen\":";
    write_json_string(stream, display_fen);
    stream << ",\"series\":" << boundary.series_number
           << ",\"series_number\":" << boundary.series_number
           << ",\"side_to_move\":\""
           << (board.white_to_move ? "white" : "black") << '"'
           << ",\"active_series_side\":\""
           << (mover ? "white" : "black") << '"'
           << ",\"budget\":" << boundary.series_number
           << ",\"prefix\":";
    write_string_array(stream, played);
    stream << ",\"current_prefix\":";
    write_string_array(stream, played);
    stream << ",\"san\":";
    write_string_array(stream, sans);
    stream << ",\"notation\":";
    std::string notation;
    for (std::size_t index = 0; index < sans.size(); ++index) {
        if (index != 0) {
            notation += " / ";
        }
        notation += sans[index];
    }
    write_json_string(stream, notation);
    stream << ",\"frames\":[";
    bool first_frame = true;
    for (const auto& frame : frames) {
        if (!first_frame) {
            stream << ',';
        }
        first_frame = false;
        stream << "{\"index\":" << frame.index << ",\"uci\":";
        write_json_string(stream, frame.uci);
        stream << ",\"san\":";
        write_json_string(stream, frame.san);
        stream << ",\"board_fen\":";
        write_json_string(stream, frame.fen);
        stream << ",\"gives_check\":"
               << (frame.gives_check ? "true" : "false") << '}';
    }
    stream << "]"
           << ",\"remaining\":" << remaining
           << ",\"moves_remaining\":" << remaining
           << ",\"complete\":" << (complete ? "true" : "false")
           << ",\"completion_reason\":";
    if (!complete) {
        stream << "null";
    } else if (ended_by_check) {
        write_json_string(
            stream,
            outcome == PrefixOutcome::Checkmate ? "checkmate" : "check"
        );
    } else if (outcome_name != nullptr) {
        write_json_string(stream, outcome_name);
    } else {
        write_json_string(stream, "budget");
    }
    stream << ",\"check\":" << (ended_by_check ? "true" : "false")
           << ",\"ended_by_check\":"
           << (ended_by_check ? "true" : "false")
           << ",\"in_check\":"
           << (spc::native::is_in_check(board) ? "true" : "false")
           << ",\"outcome\":";
    if (outcome_name == nullptr) {
        stream << "null";
    } else {
        write_json_string(stream, outcome_name);
    }
    stream << ",\"unused_moves\":" << unused_moves
           << ",\"legal_next\":";
    write_legal_move_payload(stream, board, legal_next, pending_ep, mover);
    stream << ",\"legal_moves\":";
    write_legal_move_payload(stream, board, legal_next, pending_ep, mover);
    stream << ",\"next_state\":";
    if (!complete) {
        stream << "null";
    } else {
        write_boundary_payload(
            stream,
            spc::native::SubtreeState{
                board,
                halfmove_clock,
                fullmove_number,
                final_series,
                final_quiet,
                final_ep,
            }
        );
    }
    stream << '}';
    return stream.str();
}

[[nodiscard]] const char* status_name(
    spc::native::SubtreeSearchStatus status
) noexcept {
    using spc::native::SubtreeSearchStatus;
    switch (status) {
        case SubtreeSearchStatus::Complete: return "complete";
        case SubtreeSearchStatus::WorkLimit: return "work_limit";
        case SubtreeSearchStatus::Deadline: return "deadline";
        case SubtreeSearchStatus::AdjudicationPending:
            return "adjudication_pending";
        case SubtreeSearchStatus::Unsupported: return "unsupported";
    }
    return "unsupported";
}

void write_stats(
    std::ostringstream& stream,
    const spc::native::SubtreeSearchStats& stats
) {
    stream << "{\"nodes\":" << stats.nodes
           << ",\"leaf_evaluations\":" << stats.leaf_evaluations
           << ",\"generated_raw_series\":" << stats.generated_raw_series
           << ",\"generated_unique_series\":" << stats.generated_unique_series
           << ",\"intra_series_transpositions\":"
           << stats.intra_series_transpositions
           << ",\"tt_hits\":" << stats.tt_hits
           << ",\"alpha_beta_cutoffs\":" << stats.alpha_beta_cutoffs
           << ",\"pvs_zero_window_searches\":"
           << stats.pvs_zero_window_searches
           << ",\"pvs_researches\":" << stats.pvs_researches
           << ",\"pvs_tt_writes_rolled_back\":"
           << stats.pvs_tt_writes_rolled_back
           << ",\"branch_caps\":" << stats.branch_caps
           << ",\"series_generation_positions\":"
           << stats.series_generation_positions
           << ",\"frontier_score_positions\":"
           << stats.frontier_score_positions
           << ",\"static_evaluation_positions\":"
           << stats.static_evaluation_positions
           << ",\"evaluation_reach_positions\":"
           << stats.evaluation_reach_positions
           << ",\"incomplete_reach_evaluations\":"
           << stats.incomplete_reach_evaluations
           << ",\"generation_positions\":" << stats.generation_positions
           << ",\"frontier_prunes\":" << stats.frontier_prunes
           << ",\"frontier_states_pruned\":"
           << stats.frontier_states_pruned
           << ",\"frontier_paths_pruned\":" << stats.frontier_paths_pruned
           << ",\"tactical_frontier_states_retained\":"
           << stats.tactical_frontier_states_retained
           << ",\"tactical_frontier_reserve_drops\":"
           << stats.tactical_frontier_reserve_drops
           << ",\"tactical_final_series_retained\":"
           << stats.tactical_final_series_retained
           << ",\"tactical_final_reserve_drops\":"
           << stats.tactical_final_reserve_drops
           << ",\"peak_frontier_states\":" << stats.peak_frontier_states
           << ",\"generation_work_limit_hits\":"
           << stats.generation_work_limit_hits
           << ",\"series_generation_cache_hits\":"
           << stats.series_generation_cache_hits
           << ",\"series_generation_cache_evictions\":"
           << stats.series_generation_cache_evictions
           << ",\"series_generation_cache_peak\":"
           << stats.series_generation_cache_peak
           << ",\"series_generation_cache_entries_peak\":"
           << stats.series_generation_cache_entries_peak
           << '}';
}

[[nodiscard]] std::string error_json(
    const std::string& schema,
    const std::string& boundary,
    const std::string& message
) {
    std::ostringstream stream;
    stream << "{\"schema\":";
    write_json_string(stream, schema);
    stream << ",\"abi_version\":" << ABI_VERSION
           << ",\"mode\":\"kernel\",\"boundary\":";
    write_json_string(stream, boundary);
    stream << ",\"status\":\"unsupported\""
           << ",\"status_code\":4,\"message\":";
    write_json_string(stream, message);
    stream << ",\"safety_certified\":false"
           << ",\"safety_status\":\"unknown\""
           << ",\"completed_depth\":0,\"pv\":[],\"alternatives\":[]}";
    return stream.str();
}

[[nodiscard]] std::string run_kernel_json(
    const spc::native::SubtreeState& state,
    const std::string& schema,
    const std::string& boundary,
    std::int32_t depth_series,
    std::uint32_t max_series_per_node,
    std::uint32_t max_work,
    std::uint32_t time_limit_ms
) {
    if (
        depth_series < 1
        || depth_series > 64
        || max_series_per_node == 0
        || max_series_per_node > CACHE_CAPACITY
    ) {
        return error_json(schema, boundary, "kernel request is out of range");
    }
    const std::optional<std::uint64_t> work_limit = max_work == 0
        ? std::nullopt
        : std::optional<std::uint64_t>{max_work};
    std::optional<std::chrono::steady_clock::time_point> deadline;
    if (time_limit_ms != 0) {
        deadline = std::chrono::steady_clock::now()
            + std::chrono::milliseconds(time_limit_ms);
    }
    const spc::native::FastWeights fast_weights{100, 100, 100, 100, 100};
    const spc::native::FullWeights full_weights{
        100, 100, 100, 100, 100, 100, 100,
    };
    spc::native::SubtreeSearchSession session(
        spc::native::SubtreeSearchConfig{
            max_series_per_node,
            work_limit,
            depth_series,
            MATE_SCORE,
            CACHE_CAPACITY,
            0,
            1,
            false,
            fast_weights,
            full_weights,
        }
    );
    const auto result = session.search(
        state,
        depth_series,
        -2 * MATE_SCORE,
        2 * MATE_SCORE,
        0,
        0,
        deadline
    );

    std::ostringstream stream;
    stream << "{\"schema\":";
    write_json_string(stream, schema);
    stream << ",\"abi_version\":" << ABI_VERSION
           << ",\"mode\":\"kernel\",\"boundary\":";
    write_json_string(stream, boundary);
    stream << ",\"status\":";
    write_json_string(stream, status_name(result.status));
    stream << ",\"status_code\":" << static_cast<int>(result.status)
           << ",\"message\":";
    write_json_string(stream, result.message);
    // A complete minimax/PVS kernel result is not a public root-safety
    // certificate. The worker must fail closed and use the server until exact
    // reply-mate Found/Exhausted screening and retry are integrated.
    stream << ",\"safety_certified\":false"
           << ",\"safety_status\":\"not_screened\""
           << ",\"requested_depth\":" << depth_series
           << ",\"completed_depth\":"
           << (result.status == spc::native::SubtreeSearchStatus::Complete
                   ? depth_series
                   : 0)
           << ",\"series_number\":" << state.series_number
           << ",\"quiet_series\":" << state.quiet_series
           << ",\"width\":" << max_series_per_node
           << ",\"max_work\":" << max_work
           << ",\"score\":" << result.score
           << ",\"proof_bounds\":[" << result.proof_bounds[0]
           << ',' << result.proof_bounds[1] << ']'
           << ",\"exact_width\":"
           << (result.selective ? "false" : "true")
           << ",\"selective\":"
           << (result.selective ? "true" : "false")
           << ",\"evaluation_work_limit_reached\":"
           << (result.evaluation_work_limit_reached ? "true" : "false")
           << ",\"pv\":[";
    bool first_series = true;
    for (const auto& candidate : result.principal_variation) {
        if (!first_series) {
            stream << ',';
        }
        first_series = false;
        stream << '[';
        bool first_move = true;
        for (const auto& move : candidate.path.moves) {
            if (!first_move) {
                stream << ',';
            }
            first_move = false;
            write_json_string(stream, move);
        }
        stream << ']';
    }
    stream << "],\"alternatives\":[],\"stats\":";
    write_stats(stream, result.stats);
    stream << '}';
    return stream.str();
}

thread_local std::string last_result;

}  // namespace

extern "C" const char* spc_start_kernel_search_json(
    std::int32_t depth_series,
    std::uint32_t max_series_per_node,
    std::uint32_t max_work,
    std::uint32_t time_limit_ms
) {
    try {
        last_result = run_kernel_json(
            initial_state(),
            "spc-start-kernel-v1",
            "starting",
            depth_series,
            max_series_per_node,
            max_work,
            time_limit_ms
        );
    } catch (const std::exception& error) {
        last_result = error_json(
            "spc-start-kernel-v1", "starting", error.what()
        );
    } catch (...) {
        last_result = error_json(
            "spc-start-kernel-v1", "starting", "starting-position kernel failed"
        );
    }
    return last_result.c_str();
}

extern "C" const char* spc_boundary_kernel_search_json(
    const char* fen,
    std::int32_t series_number,
    std::int32_t quiet_series,
    const char* progressive_ep,
    const char* promoted_hex,
    std::int32_t depth_series,
    std::uint32_t max_series_per_node,
    std::uint32_t max_work,
    std::uint32_t time_limit_ms
) {
    try {
        spc::native::SubtreeState state;
        std::string error;
        if (!parse_boundary(
                fen,
                series_number,
                quiet_series,
                progressive_ep,
                promoted_hex,
                state,
                error
            )) {
            last_result = error_json(
                "spc-boundary-kernel-v1", "supplied", error
            );
            return last_result.c_str();
        }
        last_result = run_kernel_json(
            state,
            "spc-boundary-kernel-v1",
            "supplied",
            depth_series,
            max_series_per_node,
            max_work,
            time_limit_ms
        );
    } catch (const std::exception& error) {
        last_result = error_json(
            "spc-boundary-kernel-v1", "supplied", error.what()
        );
    } catch (...) {
        last_result = error_json(
            "spc-boundary-kernel-v1", "supplied", "boundary kernel failed"
        );
    }
    return last_result.c_str();
}

extern "C" const char* spc_boundary_prefix_json(
    const char* fen,
    std::int32_t series_number,
    std::int32_t quiet_series,
    const char* progressive_ep,
    const char* promoted_hex_value,
    const char* prefix_uci
) {
    try {
        spc::native::SubtreeState state;
        std::string error;
        if (!parse_boundary(
                fen,
                series_number,
                quiet_series,
                progressive_ep,
                promoted_hex_value,
                state,
                error
            )) {
            last_result = prefix_error_json("invalid-boundary", error);
            return last_result.c_str();
        }
        last_result = run_prefix_json(state, prefix_uci);
    } catch (const std::exception& error) {
        last_result = prefix_error_json("prefix-failed", error.what());
    } catch (...) {
        last_result = prefix_error_json(
            "prefix-failed",
            "boundary prefix replay failed"
        );
    }
    return last_result.c_str();
}

extern "C" std::uint32_t spc_start_kernel_abi_version() {
    return ABI_VERSION;
}
