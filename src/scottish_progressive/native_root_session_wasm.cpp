#include "native_root_session_wasm.hpp"

#include "native_subtree.hpp"
#include "native_subtree_wasm_support.hpp"

#include <algorithm>
#include <array>
#include <charconv>
#include <chrono>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <exception>
#include <iomanip>
#include <limits>
#include <memory>
#include <optional>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#if defined(__EMSCRIPTEN__)
#include <emscripten/heap.h>
#endif

namespace {

constexpr std::uint32_t ROOT_ABI_VERSION = 2;
constexpr std::size_t MAX_REQUEST_BYTES = 16U * 1024U * 1024U;
constexpr std::size_t MAX_JSON_DEPTH = 24;
constexpr std::size_t MAX_JSON_NODES = 250'000;
constexpr std::uint64_t MAX_SAFE_JSON_INTEGER = 9'007'199'254'740'991ULL;
constexpr std::uint64_t MAX_ROOT_WIDTH = 512;
constexpr std::uint64_t MAX_CACHE_CAPACITY = 1'048'576;
constexpr std::uint64_t MAX_WASM_MEMORY_BYTES = 256ULL * 1024ULL * 1024ULL;
constexpr std::size_t MAX_ID_BYTES = 256;
constexpr std::size_t MAX_CANONICAL_ID_BYTES = 16U * 1024U * 1024U;
constexpr std::size_t MAX_UCI_MOVES = 256;

enum class JsonKind : std::uint8_t {
    Null,
    Boolean,
    Number,
    String,
    Array,
    Object,
};

struct JsonValue {
    JsonKind kind = JsonKind::Null;
    bool boolean = false;
    // Strings are decoded UTF-8. Numbers preserve their original JSON token so
    // routing values such as deadline_monotonic_ms can be echoed exactly.
    std::string text;
    std::vector<JsonValue> array;
    std::vector<std::pair<std::string, JsonValue>> object;

    [[nodiscard]] const JsonValue* find(std::string_view key) const noexcept {
        if (kind != JsonKind::Object) {
            return nullptr;
        }
        const auto found = std::find_if(
            object.begin(),
            object.end(),
            [key](const auto& item) { return item.first == key; }
        );
        return found == object.end() ? nullptr : &found->second;
    }
};

class RequestError final : public std::runtime_error {
public:
    RequestError(std::string code_value, std::string message_value)
        : std::runtime_error(std::move(message_value)),
          code(std::move(code_value)) {}

    std::string code;
};

class JsonParser {
public:
    explicit JsonParser(std::string_view input_value) : input(input_value) {}

    [[nodiscard]] JsonValue parse() {
        skip_space();
        JsonValue result = parse_value(0);
        skip_space();
        if (cursor != input.size()) {
            fail("JSON has trailing data");
        }
        return result;
    }

private:
    std::string_view input;
    std::size_t cursor = 0;
    std::size_t nodes = 0;

    [[noreturn]] void fail(const char* message) const {
        throw RequestError("invalid-json", message);
    }

    void skip_space() noexcept {
        while (
            cursor < input.size()
            && (
                input[cursor] == ' '
                || input[cursor] == '\t'
                || input[cursor] == '\r'
                || input[cursor] == '\n'
            )
        ) {
            ++cursor;
        }
    }

    [[nodiscard]] char take() {
        if (cursor >= input.size()) {
            fail("JSON ended unexpectedly");
        }
        return input[cursor++];
    }

    bool consume(char expected) noexcept {
        if (cursor < input.size() && input[cursor] == expected) {
            ++cursor;
            return true;
        }
        return false;
    }

    void literal(std::string_view expected) {
        if (input.substr(cursor, expected.size()) != expected) {
            fail("JSON literal is invalid");
        }
        cursor += expected.size();
    }

    [[nodiscard]] static int hex_digit(char character) noexcept {
        if (character >= '0' && character <= '9') {
            return character - '0';
        }
        if (character >= 'a' && character <= 'f') {
            return 10 + character - 'a';
        }
        if (character >= 'A' && character <= 'F') {
            return 10 + character - 'A';
        }
        return -1;
    }

    [[nodiscard]] std::uint32_t unicode_escape() {
        if (cursor + 4 > input.size()) {
            fail("JSON unicode escape is truncated");
        }
        std::uint32_t result = 0;
        for (int index = 0; index < 4; ++index) {
            const int digit = hex_digit(input[cursor++]);
            if (digit < 0) {
                fail("JSON unicode escape is invalid");
            }
            result = (result << 4U) | static_cast<std::uint32_t>(digit);
        }
        return result;
    }

    static void append_utf8(std::string& output, std::uint32_t codepoint) {
        if (codepoint <= 0x7fU) {
            output.push_back(static_cast<char>(codepoint));
        } else if (codepoint <= 0x7ffU) {
            output.push_back(static_cast<char>(0xc0U | (codepoint >> 6U)));
            output.push_back(static_cast<char>(0x80U | (codepoint & 0x3fU)));
        } else if (codepoint <= 0xffffU) {
            output.push_back(static_cast<char>(0xe0U | (codepoint >> 12U)));
            output.push_back(static_cast<char>(
                0x80U | ((codepoint >> 6U) & 0x3fU)
            ));
            output.push_back(static_cast<char>(0x80U | (codepoint & 0x3fU)));
        } else {
            output.push_back(static_cast<char>(0xf0U | (codepoint >> 18U)));
            output.push_back(static_cast<char>(
                0x80U | ((codepoint >> 12U) & 0x3fU)
            ));
            output.push_back(static_cast<char>(
                0x80U | ((codepoint >> 6U) & 0x3fU)
            ));
            output.push_back(static_cast<char>(0x80U | (codepoint & 0x3fU)));
        }
    }

    [[nodiscard]] std::string parse_string() {
        if (take() != '"') {
            fail("JSON string is missing its quote");
        }
        std::string result;
        while (cursor < input.size()) {
            const unsigned char character = static_cast<unsigned char>(take());
            if (character == '"') {
                return result;
            }
            if (character < 0x20U) {
                fail("JSON string contains a control character");
            }
            if (character != '\\') {
                result.push_back(static_cast<char>(character));
                continue;
            }
            const char escaped = take();
            switch (escaped) {
                case '"': result.push_back('"'); break;
                case '\\': result.push_back('\\'); break;
                case '/': result.push_back('/'); break;
                case 'b': result.push_back('\b'); break;
                case 'f': result.push_back('\f'); break;
                case 'n': result.push_back('\n'); break;
                case 'r': result.push_back('\r'); break;
                case 't': result.push_back('\t'); break;
                case 'u': {
                    std::uint32_t codepoint = unicode_escape();
                    if (codepoint >= 0xd800U && codepoint <= 0xdbffU) {
                        if (
                            cursor + 2 > input.size()
                            || input[cursor] != '\\'
                            || input[cursor + 1] != 'u'
                        ) {
                            fail("JSON high surrogate has no low surrogate");
                        }
                        cursor += 2;
                        const std::uint32_t low = unicode_escape();
                        if (low < 0xdc00U || low > 0xdfffU) {
                            fail("JSON low surrogate is invalid");
                        }
                        codepoint = 0x10000U
                            + ((codepoint - 0xd800U) << 10U)
                            + (low - 0xdc00U);
                    } else if (codepoint >= 0xdc00U && codepoint <= 0xdfffU) {
                        fail("JSON has an unpaired low surrogate");
                    }
                    append_utf8(result, codepoint);
                    break;
                }
                default: fail("JSON string escape is invalid");
            }
        }
        fail("JSON string is unterminated");
    }

    [[nodiscard]] JsonValue parse_number() {
        const std::size_t begin = cursor;
        consume('-');
        if (cursor >= input.size()) {
            fail("JSON number is truncated");
        }
        if (input[cursor] == '0') {
            ++cursor;
            if (cursor < input.size() && input[cursor] >= '0' && input[cursor] <= '9') {
                fail("JSON number has a leading zero");
            }
        } else {
            if (input[cursor] < '1' || input[cursor] > '9') {
                fail("JSON number is invalid");
            }
            while (
                cursor < input.size()
                && input[cursor] >= '0'
                && input[cursor] <= '9'
            ) {
                ++cursor;
            }
        }
        if (consume('.')) {
            if (
                cursor >= input.size()
                || input[cursor] < '0'
                || input[cursor] > '9'
            ) {
                fail("JSON fraction is invalid");
            }
            while (
                cursor < input.size()
                && input[cursor] >= '0'
                && input[cursor] <= '9'
            ) {
                ++cursor;
            }
        }
        if (
            cursor < input.size()
            && (input[cursor] == 'e' || input[cursor] == 'E')
        ) {
            ++cursor;
            if (
                cursor < input.size()
                && (input[cursor] == '+' || input[cursor] == '-')
            ) {
                ++cursor;
            }
            if (
                cursor >= input.size()
                || input[cursor] < '0'
                || input[cursor] > '9'
            ) {
                fail("JSON exponent is invalid");
            }
            while (
                cursor < input.size()
                && input[cursor] >= '0'
                && input[cursor] <= '9'
            ) {
                ++cursor;
            }
        }
        JsonValue result;
        result.kind = JsonKind::Number;
        result.text = std::string(input.substr(begin, cursor - begin));
        return result;
    }

    [[nodiscard]] JsonValue parse_value(std::size_t depth) {
        if (depth > MAX_JSON_DEPTH || ++nodes > MAX_JSON_NODES) {
            fail("JSON exceeds the compiled structural envelope");
        }
        skip_space();
        if (cursor >= input.size()) {
            fail("JSON value is missing");
        }
        const char next = input[cursor];
        if (next == 'n') {
            literal("null");
            return JsonValue{};
        }
        if (next == 't' || next == 'f') {
            JsonValue result;
            result.kind = JsonKind::Boolean;
            result.boolean = next == 't';
            literal(next == 't' ? "true" : "false");
            return result;
        }
        if (next == '"') {
            JsonValue result;
            result.kind = JsonKind::String;
            result.text = parse_string();
            return result;
        }
        if (next == '-' || (next >= '0' && next <= '9')) {
            return parse_number();
        }
        if (next == '[') {
            ++cursor;
            JsonValue result;
            result.kind = JsonKind::Array;
            skip_space();
            if (consume(']')) {
                return result;
            }
            while (true) {
                result.array.push_back(parse_value(depth + 1));
                skip_space();
                if (consume(']')) {
                    return result;
                }
                if (!consume(',')) {
                    fail("JSON array separator is missing");
                }
                skip_space();
            }
        }
        if (next == '{') {
            ++cursor;
            JsonValue result;
            result.kind = JsonKind::Object;
            skip_space();
            if (consume('}')) {
                return result;
            }
            while (true) {
                if (cursor >= input.size() || input[cursor] != '"') {
                    fail("JSON object key is not a string");
                }
                std::string key = parse_string();
                if (result.find(key) != nullptr) {
                    fail("JSON object contains a duplicate key");
                }
                skip_space();
                if (!consume(':')) {
                    fail("JSON object colon is missing");
                }
                result.object.emplace_back(
                    std::move(key),
                    parse_value(depth + 1)
                );
                skip_space();
                if (consume('}')) {
                    return result;
                }
                if (!consume(',')) {
                    fail("JSON object separator is missing");
                }
                skip_space();
            }
        }
        fail("JSON value token is invalid");
    }
};

void write_json_string(std::ostringstream& stream, std::string_view value) {
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

[[nodiscard]] JsonValue parse_json_request(
    const char* request,
    std::uint32_t length
) {
    if (request == nullptr || length == 0 || length > MAX_REQUEST_BYTES) {
        throw RequestError(
            "request-envelope-invalid",
            "root-session JSON request is empty or exceeds 16 MiB"
        );
    }
    const std::string_view bytes(request, length);
    if (bytes.find('\0') != std::string_view::npos) {
        throw RequestError(
            "request-envelope-invalid",
            "root-session JSON request contains an embedded NUL"
        );
    }
    for (std::size_t index = 0; index < bytes.size();) {
        const auto lead = static_cast<unsigned char>(bytes[index]);
        if (lead < 0x80U) {
            ++index;
            continue;
        }
        std::size_t continuation_count = 0;
        std::uint32_t codepoint = 0;
        std::uint32_t minimum = 0;
        if (lead >= 0xc2U && lead <= 0xdfU) {
            continuation_count = 1;
            codepoint = lead & 0x1fU;
            minimum = 0x80U;
        } else if (lead >= 0xe0U && lead <= 0xefU) {
            continuation_count = 2;
            codepoint = lead & 0x0fU;
            minimum = 0x800U;
        } else if (lead >= 0xf0U && lead <= 0xf4U) {
            continuation_count = 3;
            codepoint = lead & 0x07U;
            minimum = 0x10000U;
        } else {
            throw RequestError(
                "invalid-utf8",
                "root-session JSON request is not valid UTF-8"
            );
        }
        if (index + continuation_count >= bytes.size()) {
            throw RequestError(
                "invalid-utf8",
                "root-session JSON request has a truncated UTF-8 sequence"
            );
        }
        for (std::size_t offset = 1; offset <= continuation_count; ++offset) {
            const auto continuation = static_cast<unsigned char>(
                bytes[index + offset]
            );
            if ((continuation & 0xc0U) != 0x80U) {
                throw RequestError(
                    "invalid-utf8",
                    "root-session JSON request is not valid UTF-8"
                );
            }
            codepoint = (codepoint << 6U) | (continuation & 0x3fU);
        }
        if (
            codepoint < minimum
            || codepoint > 0x10ffffU
            || (codepoint >= 0xd800U && codepoint <= 0xdfffU)
        ) {
            throw RequestError(
                "invalid-utf8",
                "root-session JSON request has a non-canonical UTF-8 scalar"
            );
        }
        index += continuation_count + 1;
    }
    return JsonParser(bytes).parse();
}

void require_object(const JsonValue& value, const char* label) {
    if (value.kind != JsonKind::Object) {
        throw RequestError(
            "request-shape-invalid",
            std::string(label) + " must be a JSON object"
        );
    }
}

void require_keys(
    const JsonValue& value,
    std::initializer_list<std::string_view> required,
    std::initializer_list<std::string_view> optional = {}
) {
    require_object(value, "request");
    for (const std::string_view key : required) {
        if (value.find(key) == nullptr) {
            throw RequestError(
                "request-field-missing",
                std::string("root-session request is missing ")
                    + std::string(key)
            );
        }
    }
    for (const auto& [key, ignored] : value.object) {
        (void)ignored;
        const bool known = std::find(required.begin(), required.end(), key)
                != required.end()
            || std::find(optional.begin(), optional.end(), key)
                != optional.end();
        if (!known) {
            throw RequestError(
                "request-field-unknown",
                std::string("root-session request has unknown field ") + key
            );
        }
    }
}

[[nodiscard]] const JsonValue& field(
    const JsonValue& object,
    std::string_view name
) {
    const JsonValue* result = object.find(name);
    if (result == nullptr) {
        throw RequestError(
            "request-field-missing",
            std::string("root-session request is missing ") + std::string(name)
        );
    }
    return *result;
}

[[nodiscard]] std::string string_field(
    const JsonValue& object,
    std::string_view name,
    std::size_t maximum = MAX_ID_BYTES,
    bool allow_empty = false
) {
    const JsonValue& value = field(object, name);
    if (
        value.kind != JsonKind::String
        || (!allow_empty && value.text.empty())
        || value.text.size() > maximum
        || value.text.find('\0') != std::string::npos
    ) {
        throw RequestError(
            "request-field-invalid",
            std::string(name) + " is not a bounded string"
        );
    }
    return value.text;
}

[[nodiscard]] bool bool_field(
    const JsonValue& object,
    std::string_view name
) {
    const JsonValue& value = field(object, name);
    if (value.kind != JsonKind::Boolean) {
        throw RequestError(
            "request-field-invalid",
            std::string(name) + " is not a boolean"
        );
    }
    return value.boolean;
}

[[nodiscard]] std::uint64_t u64_field(
    const JsonValue& object,
    std::string_view name,
    std::uint64_t minimum = 0,
    std::uint64_t maximum = MAX_SAFE_JSON_INTEGER
) {
    const JsonValue& value = field(object, name);
    if (
        value.kind != JsonKind::Number
        || value.text.empty()
        || value.text.front() == '-'
        || value.text.find_first_of(".eE") != std::string::npos
    ) {
        throw RequestError(
            "request-field-invalid",
            std::string(name) + " is not an exact unsigned integer"
        );
    }
    std::uint64_t result = 0;
    const auto parsed = std::from_chars(
        value.text.data(),
        value.text.data() + value.text.size(),
        result
    );
    if (
        parsed.ec != std::errc{}
        || parsed.ptr != value.text.data() + value.text.size()
        || result < minimum
        || result > maximum
    ) {
        throw RequestError(
            "request-field-invalid",
            std::string(name) + " is outside its exact integer envelope"
        );
    }
    return result;
}

[[nodiscard]] std::int64_t i64_field(
    const JsonValue& object,
    std::string_view name,
    std::int64_t minimum,
    std::int64_t maximum
) {
    const JsonValue& value = field(object, name);
    if (
        value.kind != JsonKind::Number
        || value.text.empty()
        || value.text.find_first_of(".eE") != std::string::npos
    ) {
        throw RequestError(
            "request-field-invalid",
            std::string(name) + " is not an exact signed integer"
        );
    }
    std::int64_t result = 0;
    const auto parsed = std::from_chars(
        value.text.data(),
        value.text.data() + value.text.size(),
        result
    );
    if (
        parsed.ec != std::errc{}
        || parsed.ptr != value.text.data() + value.text.size()
        || result < minimum
        || result > maximum
    ) {
        throw RequestError(
            "request-field-invalid",
            std::string(name) + " is outside its exact integer envelope"
        );
    }
    return result;
}

[[nodiscard]] double finite_number_field(
    const JsonValue& object,
    std::string_view name,
    double minimum
) {
    const JsonValue& value = field(object, name);
    if (value.kind != JsonKind::Number) {
        throw RequestError(
            "request-field-invalid",
            std::string(name) + " is not a number"
        );
    }
    char* end = nullptr;
    const double result = std::strtod(value.text.c_str(), &end);
    if (
        end != value.text.c_str() + value.text.size()
        || !std::isfinite(result)
        || result < minimum
    ) {
        throw RequestError(
            "request-field-invalid",
            std::string(name) + " is outside its finite numeric envelope"
        );
    }
    return result;
}

[[nodiscard]] bool bounded_ascii(
    std::string_view value,
    std::size_t exact_length = 0
) noexcept {
    if (
        value.empty()
        || value.size() > MAX_ID_BYTES
        || (exact_length != 0 && value.size() != exact_length)
    ) {
        return false;
    }
    return std::all_of(value.begin(), value.end(), [](unsigned char item) {
        return item >= 0x21U && item <= 0x7eU;
    });
}

[[nodiscard]] bool lowercase_hex(
    std::string_view value,
    std::size_t length
) noexcept {
    return value.size() == length
        && std::all_of(value.begin(), value.end(), [](char item) {
            return (item >= '0' && item <= '9')
                || (item >= 'a' && item <= 'f');
        });
}

[[nodiscard]] bool valid_uci(std::string_view move) noexcept {
    if (move.size() != 4 && move.size() != 5) {
        return false;
    }
    if (
        move[0] < 'a' || move[0] > 'h'
        || move[1] < '1' || move[1] > '8'
        || move[2] < 'a' || move[2] > 'h'
        || move[3] < '1' || move[3] > '8'
    ) {
        return false;
    }
    return move.size() == 4
        || move[4] == 'q'
        || move[4] == 'r'
        || move[4] == 'b'
        || move[4] == 'n';
}

[[nodiscard]] std::vector<std::string> string_array(
    const JsonValue& value,
    std::size_t maximum,
    bool require_uci_moves
) {
    if (value.kind != JsonKind::Array || value.array.size() > maximum) {
        throw RequestError(
            "request-field-invalid",
            "root-session string array exceeds its envelope"
        );
    }
    std::vector<std::string> result;
    result.reserve(value.array.size());
    for (const JsonValue& item : value.array) {
        if (
            item.kind != JsonKind::String
            || item.text.size() > MAX_ID_BYTES
            || (require_uci_moves && !valid_uci(item.text))
        ) {
            throw RequestError(
                "request-field-invalid",
                "root-session string array contains an invalid item"
            );
        }
        result.push_back(item.text);
    }
    return result;
}

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

[[nodiscard]] std::string ep_csv(const JsonValue& value) {
    const std::vector<std::string> targets = string_array(value, 8, false);
    if (!std::is_sorted(targets.begin(), targets.end())) {
        throw RequestError(
            "boundary-invalid",
            "Progressive EP targets are not in canonical order"
        );
    }
    if (std::adjacent_find(targets.begin(), targets.end()) != targets.end()) {
        throw RequestError(
            "boundary-invalid",
            "Progressive EP targets are duplicated"
        );
    }
    std::string result;
    for (std::size_t index = 0; index < targets.size(); ++index) {
        if (
            targets[index].size() != 2
            || targets[index][0] < 'a'
            || targets[index][0] > 'h'
            || targets[index][1] < '1'
            || targets[index][1] > '8'
        ) {
            throw RequestError(
                "boundary-invalid",
                "Progressive EP target is not a board square"
            );
        }
        if (index != 0) {
            result.push_back(',');
        }
        result += targets[index];
    }
    return result.empty() ? std::string{"-"} : result;
}

[[nodiscard]] spc::native::SubtreeState parse_boundary_object(
    const JsonValue& boundary
) {
    require_keys(
        boundary,
        {"fen", "series", "quiet_series", "ep_targets", "promoted_hex", "chess960"},
        {
            "board_fen",
            "series_number",
            "side_to_move",
            "quiet_draw_pending",
            "progressive_ep",
        }
    );
    if (bool_field(boundary, "chess960")) {
        throw RequestError(
            "chess960-unsupported",
            "root-session WASM supports standard chess only"
        );
    }
    const std::string fen = string_field(boundary, "fen", 512);
    const std::int64_t series = i64_field(boundary, "series", 1, 256);
    const std::int64_t quiet = i64_field(
        boundary,
        "quiet_series",
        0,
        1'000'000
    );
    const std::string promoted = string_field(boundary, "promoted_hex", 18);
    if (
        fen.front() == ' '
        || fen.back() == ' '
        || !lowercase_hex(promoted, 16)
    ) {
        throw RequestError(
            "boundary-invalid",
            "root boundary FEN or promoted provenance is not canonical"
        );
    }
    const std::string progressive_ep = ep_csv(field(boundary, "ep_targets"));
    if (
        (boundary.find("board_fen") != nullptr
            && string_field(boundary, "board_fen", 512) != fen)
        || (boundary.find("series_number") != nullptr
            && i64_field(boundary, "series_number", 1, 256) != series)
        || (boundary.find("quiet_draw_pending") != nullptr
            && bool_field(boundary, "quiet_draw_pending") != (quiet >= 10))
        || (boundary.find("progressive_ep") != nullptr
            && ep_csv(field(boundary, "progressive_ep")) != progressive_ep)
    ) {
        throw RequestError(
            "boundary-alias-mismatch",
            "exact boundary aliases do not match their canonical fields"
        );
    }
    spc::native::SubtreeState state;
    std::string error;
    if (!spc::wasm::parse_exact_boundary(
            fen.c_str(),
            static_cast<std::int32_t>(series),
            static_cast<std::int32_t>(quiet),
            progressive_ep.c_str(),
            promoted.c_str(),
            state,
            error
        )) {
        throw RequestError("boundary-invalid", std::move(error));
    }
    std::string canonical_ep;
    for (std::size_t index = 0; index < state.ep_targets.size(); ++index) {
        if (index != 0) {
            canonical_ep.push_back(',');
        }
        canonical_ep.push_back(static_cast<char>('a' + (state.ep_targets[index] & 7)));
        canonical_ep.push_back(static_cast<char>('1' + (state.ep_targets[index] >> 3)));
    }
    if ((canonical_ep.empty() ? std::string{"-"} : canonical_ep) != progressive_ep) {
        throw RequestError(
            "boundary-invalid",
            "Progressive EP targets are not canonical for this exact board"
        );
    }
    if (boundary.find("side_to_move") != nullptr) {
        const std::string side = string_field(boundary, "side_to_move", 5);
        if (side != (state.board.white_to_move ? "white" : "black")) {
            throw RequestError(
                "boundary-alias-mismatch",
                "exact boundary side_to_move alias diverged"
            );
        }
    }
    return state;
}

struct SessionIdentity {
    std::string source_fingerprint;
    std::string kernel_sha256;
    std::string module_js_sha256;
    std::string certificate_id;
    std::string runtime_variant;
    std::uint64_t thread_count = 1;
    std::string engine_version;
    std::string ruleset_version;
    std::string profile_id;
};

[[nodiscard]] SessionIdentity parse_identity(const JsonValue& request) {
    SessionIdentity result;
    result.source_fingerprint = string_field(request, "source_fingerprint");
    result.kernel_sha256 = string_field(request, "kernel_sha256");
    result.module_js_sha256 = string_field(request, "module_js_sha256");
    result.certificate_id = string_field(request, "certificate_id");
    result.runtime_variant = string_field(request, "runtime_variant");
    result.thread_count = u64_field(request, "thread_count", 1, 1);
    result.engine_version = string_field(request, "engine_version");
    result.ruleset_version = string_field(request, "ruleset_version");
    result.profile_id = string_field(request, "profile_id");
    if (
        !lowercase_hex(result.source_fingerprint, 16)
        || !lowercase_hex(result.kernel_sha256, 64)
        || !lowercase_hex(result.module_js_sha256, 64)
        || !bounded_ascii(result.certificate_id)
        || result.runtime_variant != "single"
        || !bounded_ascii(result.engine_version)
        || !bounded_ascii(result.ruleset_version)
        || !bounded_ascii(result.profile_id)
    ) {
        throw RequestError(
            "artifact-identity-invalid",
            "root-session artifact identity is invalid"
        );
    }
    return result;
}

[[nodiscard]] bool same_identity(
    const SessionIdentity& left,
    const SessionIdentity& right
) noexcept {
    return left.source_fingerprint == right.source_fingerprint
        && left.kernel_sha256 == right.kernel_sha256
        && left.module_js_sha256 == right.module_js_sha256
        && left.certificate_id == right.certificate_id
        && left.runtime_variant == right.runtime_variant
        && left.thread_count == right.thread_count
        && left.engine_version == right.engine_version
        && left.ruleset_version == right.ruleset_version
        && left.profile_id == right.profile_id;
}

void write_identity(std::ostringstream& stream, const SessionIdentity& identity) {
    stream << "\"source_fingerprint\":";
    write_json_string(stream, identity.source_fingerprint);
    stream << ",\"kernel_sha256\":";
    write_json_string(stream, identity.kernel_sha256);
    stream << ",\"module_js_sha256\":";
    write_json_string(stream, identity.module_js_sha256);
    stream << ",\"certificate_id\":";
    write_json_string(stream, identity.certificate_id);
    stream << ",\"runtime_variant\":";
    write_json_string(stream, identity.runtime_variant);
    stream << ",\"thread_count\":" << identity.thread_count
           << ",\"engine_version\":";
    write_json_string(stream, identity.engine_version);
    stream << ",\"ruleset_version\":";
    write_json_string(stream, identity.ruleset_version);
    stream << ",\"profile_id\":";
    write_json_string(stream, identity.profile_id);
}

struct ParsedConfig {
    spc::native::SubtreeSearchConfig core;
    std::array<std::int64_t, 7> weights{};
};

[[nodiscard]] ParsedConfig parse_config(const JsonValue& value) {
    require_keys(
        value,
        {
            "max_depth",
            "width",
            "max_work",
            "mate_score",
            "series_cache_capacity",
            "external_cache_weight",
            "worker_threads",
            "root_tactical_protection",
            "root_contract_tt_capacity",
            "root_contract_eval_capacity",
            "weights",
        }
    );
    const JsonValue& weights = field(value, "weights");
    require_keys(
        weights,
        {
            "material",
            "king_space",
            "series_reach",
            "promotion_corridors",
            "immediate_vulnerability",
            "useful_mobility",
            "boundary_check",
        }
    );
    ParsedConfig result;
    result.weights = {
        i64_field(weights, "material", 25, 300),
        i64_field(weights, "king_space", 25, 300),
        i64_field(weights, "series_reach", 25, 300),
        i64_field(weights, "promotion_corridors", 25, 300),
        i64_field(weights, "immediate_vulnerability", 25, 300),
        i64_field(weights, "useful_mobility", 25, 300),
        i64_field(weights, "boundary_check", 25, 300),
    };
    const auto max_depth = i64_field(value, "max_depth", 1, 8);
    const auto width = u64_field(value, "width", 1, MAX_ROOT_WIDTH);
    const auto max_work = u64_field(value, "max_work", 1);
    const auto mate_score = i64_field(
        value,
        "mate_score",
        1,
        1'000'000'000
    );
    const auto series_cache = u64_field(
        value,
        "series_cache_capacity",
        1,
        MAX_CACHE_CAPACITY
    );
    const auto external_cache = u64_field(
        value,
        "external_cache_weight",
        0,
        series_cache
    );
    const auto worker_threads = u64_field(value, "worker_threads", 1, 1);
    const auto tt_capacity = u64_field(
        value,
        "root_contract_tt_capacity",
        1,
        MAX_CACHE_CAPACITY
    );
    const auto eval_capacity = u64_field(
        value,
        "root_contract_eval_capacity",
        1,
        MAX_CACHE_CAPACITY
    );
    result.core = spc::native::SubtreeSearchConfig{
        width,
        std::optional<std::uint64_t>{max_work},
        max_depth,
        mate_score,
        series_cache,
        external_cache,
        static_cast<std::uint32_t>(worker_threads),
        bool_field(value, "root_tactical_protection"),
        spc::native::FastWeights{
            result.weights[0],
            result.weights[1],
            result.weights[3],
            result.weights[4],
            result.weights[6],
        },
        spc::native::FullWeights{
            result.weights[0],
            result.weights[1],
            result.weights[2],
            result.weights[3],
            result.weights[4],
            result.weights[5],
            result.weights[6],
        },
        tt_capacity,
        eval_capacity,
    };
    return result;
}

[[nodiscard]] std::uint64_t wasm_heap_bytes() noexcept {
#if defined(__EMSCRIPTEN__)
    return static_cast<std::uint64_t>(emscripten_get_heap_size());
#else
    return 0;
#endif
}

[[nodiscard]] std::uint64_t wasm_heap_limit_bytes() noexcept {
#if defined(__EMSCRIPTEN__)
    return static_cast<std::uint64_t>(emscripten_get_heap_max());
#else
    return MAX_WASM_MEMORY_BYTES;
#endif
}

struct RootSession {
    std::uint32_t id = 0;
    SessionIdentity identity;
    spc::native::SubtreeState boundary;
    ParsedConfig parsed_config;
    bool canonical_root_tactical_protection = false;
    std::unique_ptr<spc::native::SubtreeSearchSession> core;
    std::uint64_t native_work_after = 0;
    std::uint64_t external_work = 0;
    bool has_external_work = false;
    std::optional<double> deadline_monotonic_ms;
    std::uint64_t memory_peak_bytes = 0;
    spc::native::SubtreeWorkReceipt last_work;
    std::vector<spc::native::RetainedRootCandidate> retained_candidates;
    std::string retained_enumeration_identity;

    void update_memory() noexcept {
        memory_peak_bytes = std::max(memory_peak_bytes, wasm_heap_bytes());
    }
};

thread_local std::unique_ptr<RootSession> active_session;
thread_local std::uint32_t next_session_id = 1;
thread_local std::string root_last_result;

[[nodiscard]] RootSession& session_for(std::uint32_t session_id) {
    if (
        !active_session
        || session_id == 0
        || active_session->id != session_id
    ) {
        throw RequestError(
            "session-not-found",
            "root-session handle is missing, stale, or already destroyed"
        );
    }
    return *active_session;
}

void required_schema(
    const JsonValue& request,
    std::string_view expected
) {
    const std::string schema = string_field(request, "schema");
    if (schema != expected) {
        throw RequestError(
            "request-schema-invalid",
            "root-session request schema is unsupported"
        );
    }
}

void validate_identity(const JsonValue& request, const RootSession& session) {
    if (!same_identity(parse_identity(request), session.identity)) {
        throw RequestError(
            "artifact-identity-mismatch",
            "root-session request does not match the pinned artifact identity"
        );
    }
}

struct RoutingEcho {
    std::string request_id;
    std::string iteration_id;
    std::uint64_t generation = 0;
    std::string deadline_token;
    double deadline_value = 0;
    std::uint64_t remaining_time_ms = 0;
    std::uint64_t external_work = 0;
    std::uint64_t native_work_before = 0;
    std::uint64_t call_work_credit = 0;
};

[[nodiscard]] RoutingEcho parse_routing(
    const JsonValue& request,
    RootSession& session
) {
    RoutingEcho result;
    result.request_id = string_field(request, "request_id");
    result.iteration_id = string_field(request, "iteration_id");
    result.generation = u64_field(request, "generation", 0);
    result.deadline_value = finite_number_field(
        request,
        "deadline_monotonic_ms",
        0
    );
    result.deadline_token = field(request, "deadline_monotonic_ms").text;
    result.remaining_time_ms = u64_field(
        request,
        "remaining_time_ms",
        0,
        2'147'483'647
    );
    result.external_work = u64_field(request, "external_work", 0);
    result.native_work_before = u64_field(request, "native_work_before", 0);
    result.call_work_credit = u64_field(request, "call_work_credit", 0);
    if (result.native_work_before != session.native_work_after) {
        throw RequestError(
            "native-work-mismatch",
            "root-session native_work_before is stale or belongs to another Worker"
        );
    }
    if (session.has_external_work && result.external_work < session.external_work) {
        throw RequestError(
            "external-work-regressed",
            "root-session external_work regressed"
        );
    }
    if (
        session.deadline_monotonic_ms.has_value()
        && result.deadline_value > *session.deadline_monotonic_ms
    ) {
        throw RequestError(
            "deadline-extension-rejected",
            "root-session deadline_monotonic_ms attempted to extend the session"
        );
    }
    return result;
}

[[nodiscard]] std::optional<std::chrono::steady_clock::time_point>
relative_deadline(const RoutingEcho& routing) {
    return std::chrono::steady_clock::now()
        + std::chrono::milliseconds(routing.remaining_time_ms);
}

void commit_routing(RootSession& session, const RoutingEcho& routing) {
    session.external_work = routing.external_work;
    session.has_external_work = true;
    session.deadline_monotonic_ms = session.deadline_monotonic_ms.has_value()
        ? std::min(*session.deadline_monotonic_ms, routing.deadline_value)
        : std::optional<double>{routing.deadline_value};
}

void write_routing(
    std::ostringstream& stream,
    const RoutingEcho& routing
) {
    stream << ",\"request_id\":";
    write_json_string(stream, routing.request_id);
    stream << ",\"iteration_id\":";
    write_json_string(stream, routing.iteration_id);
    stream << ",\"generation\":" << routing.generation
           << ",\"deadline_monotonic_ms\":" << routing.deadline_token
           << ",\"remaining_time_ms\":" << routing.remaining_time_ms;
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

[[nodiscard]] const char* bound_name(
    spc::native::SubtreeBoundKind bound
) noexcept {
    using spc::native::SubtreeBoundKind;
    switch (bound) {
        case SubtreeBoundKind::Unknown: return "unknown";
        case SubtreeBoundKind::Exact: return "exact";
        case SubtreeBoundKind::Upper: return "upper";
        case SubtreeBoundKind::Lower: return "lower";
    }
    return "unknown";
}

[[nodiscard]] const char* outcome_name(
    spc::native::CompleteSeriesOutcome outcome
) noexcept {
    using spc::native::CompleteSeriesOutcome;
    switch (outcome) {
        case CompleteSeriesOutcome::None: return nullptr;
        case CompleteSeriesOutcome::Checkmate: return "checkmate";
        case CompleteSeriesOutcome::Stalemate: return "stalemate";
        case CompleteSeriesOutcome::TenSeriesDraw: return "ten_series_draw";
    }
    return nullptr;
}

void write_string_array(
    std::ostringstream& stream,
    const std::vector<std::string>& values
) {
    stream << '[';
    bool first = true;
    for (const std::string& value : values) {
        if (!first) {
            stream << ',';
        }
        first = false;
        write_json_string(stream, value);
    }
    stream << ']';
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
           << ",\"frontier_paths_pruned\":"
           << stats.frontier_paths_pruned
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

void write_work(
    std::ostringstream& stream,
    const spc::native::SubtreeWorkReceipt& work,
    const RootSession& session
) {
    stream << "{\"call_work_credit\":";
    if (work.call_work_credit.has_value()) {
        stream << *work.call_work_credit;
    } else {
        stream << "null";
    }
    stream << ",\"external_work\":" << work.external_work
           << ",\"native_work_before\":" << work.native_work_before
           << ",\"native_work_after\":" << work.native_work_after
           << ",\"call_native_work\":" << work.call_native_work
           << ",\"total_accounted_work\":" << work.total_accounted_work
           << ",\"tt_entries\":" << work.tt_entries
           << ",\"tt_entries_peak\":" << work.tt_entries_peak
           << ",\"tt_capacity\":" << work.tt_capacity
           << ",\"eval_entries\":" << work.eval_entries
           << ",\"eval_entries_peak\":" << work.eval_entries_peak
           << ",\"eval_capacity\":" << work.eval_capacity
           << ",\"series_cache_capacity\":"
           << session.parsed_config.core.series_cache_capacity
           << ",\"series_cache_weight_peak\":"
           << work.cumulative_stats.series_generation_cache_peak
           << ",\"series_cache_entries_peak\":"
           << work.cumulative_stats.series_generation_cache_entries_peak
           << ",\"call_stats\":";
    write_stats(stream, work.call_stats);
    stream << ",\"cumulative_stats\":";
    write_stats(stream, work.cumulative_stats);
    stream << '}';
}

void write_memory(std::ostringstream& stream, RootSession& session) {
    session.update_memory();
    stream << ",\"memory_bytes\":" << wasm_heap_bytes()
           << ",\"memory_peak_bytes\":" << session.memory_peak_bytes
           << ",\"memory_limit_bytes\":" << wasm_heap_limit_bytes()
           << ",\"cache_capacities\":{\"series_cache_capacity\":"
           << session.parsed_config.core.series_cache_capacity
           << ",\"tt_capacity\":"
           << session.parsed_config.core.root_contract_tt_capacity
           << ",\"eval_capacity\":"
           << session.parsed_config.core.root_contract_eval_capacity
           << '}';
}

[[nodiscard]] spc::native::SubtreeState candidate_state(
    const spc::native::CompleteSeriesCandidate& candidate
) {
    return spc::native::SubtreeState{
        candidate.board,
        candidate.halfmove_clock,
        candidate.fullmove_number,
        candidate.series_number,
        candidate.quiet_series,
        candidate.ep_targets,
    };
}

void write_complete_series(
    std::ostringstream& stream,
    const spc::native::CompleteSeriesCandidate& candidate
) {
    stream << "{\"moves\":";
    write_string_array(stream, candidate.path.moves);
    stream << ",\"machine_notation\":";
    write_json_string(stream, machine_notation(candidate.path.moves));
    stream << ",\"transposition_count\":"
           << candidate.path.transposition_count
           << ",\"child_boundary\":"
           << spc::wasm::exact_boundary_json(candidate_state(candidate))
           << ",\"outcome\":";
    const char* outcome = outcome_name(candidate.outcome);
    if (outcome == nullptr) {
        stream << "null";
    } else {
        write_json_string(stream, outcome);
    }
    stream << ",\"ended_by_check\":"
           << (candidate.ended_by_check ? "true" : "false")
           << '}';
}

void write_candidate(
    std::ostringstream& stream,
    const spc::native::RetainedRootCandidate& candidate
) {
    stream << "{\"candidate_identity\":";
    write_json_string(stream, candidate.candidate_identity);
    stream << ",\"order_index\":" << candidate.order_index
           << ",\"order_key\":";
    write_json_string(stream, candidate.order_key);
    stream << ",\"terminal_score\":";
    if (candidate.terminal_score.has_value()) {
        stream << *candidate.terminal_score;
    } else {
        stream << "null";
    }
    stream << ",\"terminal_proof_bounds\":["
           << candidate.terminal_proof_bounds[0] << ','
           << candidate.terminal_proof_bounds[1]
           << "],\"root_series\":";
    write_complete_series(stream, candidate.series);
    stream << '}';
}

void write_manifest_fields(
    std::ostringstream& stream,
    const spc::native::RetainedRootEnumerationResult& result
) {
    stream << ",\"enumeration_identity\":";
    write_json_string(stream, result.enumeration_identity);
    stream << ",\"root_white_to_move\":"
           << (result.root_white_to_move ? "true" : "false")
           << ",\"requested_width\":" << result.requested_width
           << ",\"retained_count\":" << result.retained_count
           << ",\"width_complete\":"
           << (result.width_complete ? "true" : "false")
           << ",\"preferred_series\":";
    write_string_array(stream, result.preferred_series);
    stream << ",\"candidates\":[";
    bool first = true;
    for (const auto& candidate : result.candidates) {
        if (!first) {
            stream << ',';
        }
        first = false;
        write_candidate(stream, candidate);
    }
    stream << ']';
}

[[nodiscard]] std::array<int, 2> proof_bounds(const JsonValue& value) {
    if (value.kind != JsonKind::Array || value.array.size() != 2) {
        throw RequestError(
            "manifest-invalid",
            "root manifest proof bounds have the wrong shape"
        );
    }
    std::array<int, 2> result{};
    for (std::size_t index = 0; index < 2; ++index) {
        const JsonValue& item = value.array[index];
        if (
            item.kind != JsonKind::Number
            || item.text.find_first_of(".eE") != std::string::npos
        ) {
            throw RequestError(
                "manifest-invalid",
                "root manifest proof bound is not exact"
            );
        }
        std::int64_t parsed = 0;
        const auto converted = std::from_chars(
            item.text.data(),
            item.text.data() + item.text.size(),
            parsed
        );
        if (
            converted.ec != std::errc{}
            || converted.ptr != item.text.data() + item.text.size()
            || parsed < -1
            || parsed > 1
        ) {
            throw RequestError(
                "manifest-invalid",
                "root manifest proof bound is outside [-1,1]"
            );
        }
        result[index] = static_cast<int>(parsed);
    }
    return result;
}

[[nodiscard]] spc::native::CompleteSeriesOutcome parse_outcome(
    const JsonValue& value
) {
    using spc::native::CompleteSeriesOutcome;
    if (value.kind == JsonKind::Null) {
        return CompleteSeriesOutcome::None;
    }
    if (value.kind != JsonKind::String) {
        throw RequestError("manifest-invalid", "root series outcome is invalid");
    }
    if (value.text == "checkmate") return CompleteSeriesOutcome::Checkmate;
    if (value.text == "stalemate") return CompleteSeriesOutcome::Stalemate;
    if (value.text == "ten_series_draw") {
        return CompleteSeriesOutcome::TenSeriesDraw;
    }
    throw RequestError("manifest-invalid", "root series outcome is unknown");
}

[[nodiscard]] spc::native::CompleteSeriesCandidate parse_complete_series(
    const JsonValue& value
) {
    require_keys(
        value,
        {
            "moves",
            "machine_notation",
            "transposition_count",
            "child_boundary",
            "outcome",
            "ended_by_check",
        }
    );
    spc::native::CompleteSeriesCandidate result;
    result.path.moves = string_array(field(value, "moves"), MAX_UCI_MOVES, true);
    if (
        result.path.moves.empty()
        || string_field(value, "machine_notation", 1'535)
            != machine_notation(result.path.moves)
    ) {
        throw RequestError(
            "manifest-invalid",
            "root series machine notation does not match its UCI moves"
        );
    }
    result.path.transposition_count = u64_field(
        value,
        "transposition_count",
        1
    );
    const auto child = parse_boundary_object(field(value, "child_boundary"));
    result.board = child.board;
    result.halfmove_clock = child.halfmove_clock;
    result.fullmove_number = child.fullmove_number;
    result.series_number = child.series_number;
    result.quiet_series = child.quiet_series;
    result.ep_targets = child.ep_targets;
    result.outcome = parse_outcome(field(value, "outcome"));
    result.ended_by_check = bool_field(value, "ended_by_check");
    return result;
}

[[nodiscard]] spc::native::RetainedRootCandidate parse_candidate(
    const JsonValue& value,
    std::uint64_t expected_index
) {
    require_keys(
        value,
        {
            "candidate_identity",
            "order_index",
            "order_key",
            "terminal_score",
            "terminal_proof_bounds",
            "root_series",
        }
    );
    spc::native::RetainedRootCandidate result;
    result.candidate_identity = string_field(
        value,
        "candidate_identity",
        MAX_CANONICAL_ID_BYTES
    );
    result.order_index = u64_field(value, "order_index", 0, MAX_ROOT_WIDTH);
    result.order_key = string_field(value, "order_key", 1'535);
    result.series = parse_complete_series(field(value, "root_series"));
    result.terminal_proof_bounds = proof_bounds(
        field(value, "terminal_proof_bounds")
    );
    const JsonValue& terminal_score = field(value, "terminal_score");
    if (terminal_score.kind == JsonKind::Null) {
        result.terminal_score.reset();
    } else {
        // The core authoritatively recomputes this after replay.
        JsonValue holder;
        holder.kind = JsonKind::Object;
        holder.object.emplace_back("score", terminal_score);
        result.terminal_score = i64_field(
            holder,
            "score",
            -2'000'000'000,
            2'000'000'000
        );
    }
    if (
        result.order_index != expected_index
        || result.order_key != machine_notation(result.series.path.moves)
    ) {
        throw RequestError(
            "manifest-invalid",
            "root manifest candidate ordering or tie key diverged"
        );
    }
    return result;
}

struct ParsedManifest {
    std::string enumeration_identity;
    bool root_white_to_move = true;
    std::uint64_t requested_width = 0;
    bool width_complete = false;
    std::vector<std::string> preferred_series;
    std::vector<spc::native::RetainedRootCandidate> candidates;
};

[[nodiscard]] ParsedManifest parse_manifest(const JsonValue& value) {
    require_keys(
        value,
        {
            "enumeration_identity",
            "root_white_to_move",
            "requested_width",
            "retained_count",
            "width_complete",
            "preferred_series",
            "candidates",
        }
    );
    ParsedManifest result;
    result.enumeration_identity = string_field(
        value,
        "enumeration_identity",
        MAX_CANONICAL_ID_BYTES
    );
    result.root_white_to_move = bool_field(value, "root_white_to_move");
    result.requested_width = u64_field(
        value,
        "requested_width",
        1,
        MAX_ROOT_WIDTH
    );
    result.width_complete = bool_field(value, "width_complete");
    result.preferred_series = string_array(
        field(value, "preferred_series"),
        MAX_UCI_MOVES,
        true
    );
    const JsonValue& candidates = field(value, "candidates");
    if (
        candidates.kind != JsonKind::Array
        || candidates.array.empty()
        || candidates.array.size() > result.requested_width
        || u64_field(value, "retained_count", 1, MAX_ROOT_WIDTH)
            != candidates.array.size()
    ) {
        throw RequestError(
            "manifest-invalid",
            "root manifest retained count or candidate array is invalid"
        );
    }
    result.candidates.reserve(candidates.array.size());
    std::set<std::string> identities;
    for (std::size_t index = 0; index < candidates.array.size(); ++index) {
        auto parsed = parse_candidate(
            candidates.array[index],
            static_cast<std::uint64_t>(index)
        );
        if (!identities.insert(parsed.candidate_identity).second) {
            throw RequestError(
                "manifest-invalid",
                "root manifest duplicates a candidate identity"
            );
        }
        result.candidates.push_back(std::move(parsed));
    }
    return result;
}

[[nodiscard]] const spc::native::RetainedRootCandidate& retained_candidate(
    const RootSession& session,
    std::string_view identity
) {
    const auto found = std::find_if(
        session.retained_candidates.begin(),
        session.retained_candidates.end(),
        [identity](const auto& candidate) {
            return candidate.candidate_identity == identity;
        }
    );
    if (found == session.retained_candidates.end()) {
        throw RequestError(
            "candidate-identity-unknown",
            "root-session candidate identity is not retained"
        );
    }
    return *found;
}

void require_create_keys(const JsonValue& request) {
    require_keys(
        request,
        {
            "schema",
            "request_id",
            "iteration_id",
            "generation",
            "source_fingerprint",
            "kernel_sha256",
            "module_js_sha256",
            "certificate_id",
            "runtime_variant",
            "thread_count",
            "engine_version",
            "ruleset_version",
            "profile_id",
            "boundary",
            "config",
        }
    );
}

void require_enumerate_keys(const JsonValue& request) {
    require_keys(
        request,
        {
            "schema",
            "request_id",
            "iteration_id",
            "generation",
            "source_fingerprint",
            "kernel_sha256",
            "module_js_sha256",
            "certificate_id",
            "runtime_variant",
            "thread_count",
            "engine_version",
            "ruleset_version",
            "profile_id",
            "preferred_series",
            "external_work",
            "native_work_before",
            "call_work_credit",
            "deadline_monotonic_ms",
            "remaining_time_ms",
        }
    );
}

void require_import_keys(const JsonValue& request) {
    require_keys(
        request,
        {
            "schema",
            "request_id",
            "iteration_id",
            "generation",
            "source_fingerprint",
            "kernel_sha256",
            "module_js_sha256",
            "certificate_id",
            "runtime_variant",
            "thread_count",
            "engine_version",
            "ruleset_version",
            "profile_id",
            "manifest",
            "external_work",
            "native_work_before",
            "call_work_credit",
            "deadline_monotonic_ms",
            "remaining_time_ms",
        }
    );
}

void require_search_keys(const JsonValue& request) {
    require_keys(
        request,
        {
            "schema",
            "request_id",
            "iteration_id",
            "source_fingerprint",
            "kernel_sha256",
            "module_js_sha256",
            "certificate_id",
            "runtime_variant",
            "thread_count",
            "engine_version",
            "ruleset_version",
            "profile_id",
            "generation",
            "safety_revision",
            "incumbent_epoch",
            "task_id",
            "enumeration_identity",
            "candidate_identity",
            "order_index",
            "order_key",
            "purpose",
            "mate_score",
            "child_depth",
            "alpha",
            "beta",
            "tt_persistence",
            "external_work",
            "native_work_before",
            "call_work_credit",
            "deadline_monotonic_ms",
            "remaining_time_ms",
            "mover",
        }
    );
}

void write_config(std::ostringstream& stream, const ParsedConfig& parsed) {
    const auto& config = parsed.core;
    stream << "{\"max_depth\":" << config.requested_depth
           << ",\"width\":" << config.max_series_per_node
           << ",\"max_work\":" << *config.max_work
           << ",\"mate_score\":" << config.mate_score
           << ",\"series_cache_capacity\":"
           << config.series_cache_capacity
           << ",\"external_cache_weight\":"
           << config.external_cache_weight
           << ",\"worker_threads\":" << config.worker_threads
           << ",\"root_tactical_protection\":"
           << (config.root_tactical_protection ? "true" : "false")
           << ",\"root_contract_tt_capacity\":"
           << config.root_contract_tt_capacity
           << ",\"root_contract_eval_capacity\":"
           << config.root_contract_eval_capacity
           << ",\"weights\":{\"material\":" << parsed.weights[0]
           << ",\"king_space\":" << parsed.weights[1]
           << ",\"series_reach\":" << parsed.weights[2]
           << ",\"promotion_corridors\":" << parsed.weights[3]
           << ",\"immediate_vulnerability\":" << parsed.weights[4]
           << ",\"useful_mobility\":" << parsed.weights[5]
           << ",\"boundary_check\":" << parsed.weights[6]
           << "}}";
}

[[nodiscard]] std::string create_session_result(const JsonValue& request) {
    require_create_keys(request);
    required_schema(request, "spc-root-session-create-v1");
    const std::string request_id = string_field(request, "request_id");
    const std::string iteration_id = string_field(request, "iteration_id");
    const std::uint64_t generation = u64_field(request, "generation", 0);
    SessionIdentity identity = parse_identity(request);
    spc::native::SubtreeState boundary = parse_boundary_object(
        field(request, "boundary")
    );
    ParsedConfig parsed = parse_config(field(request, "config"));
    if (parsed.core.root_tactical_protection) {
        throw RequestError(
            "legacy-root-tactical-policy-unsupported",
            "root-session legacy tactical flag must be false; the exact boundary selects canonical descendant protection"
        );
    }
    if (boundary.quiet_series + parsed.core.requested_depth >= 10) {
        throw RequestError(
            "adjudication-horizon-unsupported",
            "root-session maximum depth reaches Python-owned quiet adjudication"
        );
    }
    if (active_session) {
        throw RequestError(
            "session-already-active",
            "this Worker already owns a live root session"
        );
    }
    if (next_session_id == 0) {
        throw RequestError(
            "session-id-exhausted",
            "this Worker exhausted its monotonic root-session identity space"
        );
    }
    auto next = std::make_unique<RootSession>();
    next->id = next_session_id++;
    next->identity = std::move(identity);
    next->boundary = std::move(boundary);
    next->parsed_config = std::move(parsed);
    next->canonical_root_tactical_protection =
        spc::native::root_tactical_protection_eligible(next->boundary);
    next->core = std::make_unique<spc::native::SubtreeSearchSession>(
        next->parsed_config.core
    );
    next->last_work.tt_capacity =
        next->parsed_config.core.root_contract_tt_capacity;
    next->last_work.eval_capacity =
        next->parsed_config.core.root_contract_eval_capacity;
    next->update_memory();
    active_session = std::move(next);
    RootSession& session = *active_session;

    std::ostringstream stream;
    stream << "{\"schema\":\"spc-root-session-create-result-v1\""
           << ",\"abi_version\":" << ROOT_ABI_VERSION
           << ",\"status\":\"ready\",\"session_id\":" << session.id
           << ",\"request_id\":";
    write_json_string(stream, request_id);
    stream << ",\"iteration_id\":";
    write_json_string(stream, iteration_id);
    stream << ",\"generation\":" << generation << ',';
    write_identity(stream, session.identity);
    stream << ",\"boundary\":"
           << spc::wasm::exact_boundary_json(session.boundary)
           << ",\"config\":";
    write_config(stream, session.parsed_config);
    stream << ",\"configured_max_depth\":"
           << session.parsed_config.core.requested_depth
           << ",\"canonical_root_tactical_policy\":\"canonical-boundary-policy-v1\""
           << ",\"canonical_root_tactical_protection\":"
           << (session.canonical_root_tactical_protection ? "true" : "false")
           << ",\"native_work_after\":0"
           << ",\"capabilities\":{\"enumerate\":true,\"import\":true"
           << ",\"search\":true,\"call_work_credit\":true"
           << ",\"hard_memory_limit\":true,\"tt_scout_rollback\":true"
           << ",\"persistent_depth_reuse\":true"
           << ",\"aspiration_windows\":true"
           << ",\"selected_owner_certification\":true"
           << ",\"canonical_root_tactical_policy\":true"
           << ",\"reply_mate_safety\":false}"
           << ",\"product_publishable\":false"
           << ",\"safety_certified\":false";
    write_memory(stream, session);
    stream << '}';
    return stream.str();
}

void accept_receipt(
    RootSession& session,
    const RoutingEcho& routing,
    const spc::native::SubtreeWorkReceipt& work
) {
    if (
        work.external_work != routing.external_work
        || work.native_work_before != routing.native_work_before
        || work.call_work_credit
            != std::optional<std::uint64_t>{routing.call_work_credit}
        || work.native_work_after < work.native_work_before
        || work.call_native_work
            != work.native_work_after - work.native_work_before
        || work.call_native_work > routing.call_work_credit
        || work.total_accounted_work
            != work.external_work + work.native_work_after
    ) {
        throw RequestError(
            "native-work-receipt-invalid",
            "native root core returned an inconsistent work receipt"
        );
    }
    session.native_work_after = work.native_work_after;
    session.last_work = work;
    commit_routing(session, routing);
}

[[nodiscard]] std::string enumeration_result_json(
    const char* schema,
    RootSession& session,
    const RoutingEcho& routing,
    const spc::native::RetainedRootEnumerationResult& result,
    bool imported
) {
    std::ostringstream stream;
    stream << "{\"schema\":";
    write_json_string(stream, schema);
    stream << ",\"abi_version\":" << ROOT_ABI_VERSION
           << ",\"session_id\":" << session.id
           << ",\"status\":";
    write_json_string(stream, status_name(result.status));
    stream << ",\"status_code\":" << static_cast<int>(result.status)
           << ",\"message\":";
    write_json_string(stream, result.message);
    write_routing(stream, routing);
    stream << ',';
    write_identity(stream, session.identity);
    stream << ",\"configured_max_depth\":"
           << session.parsed_config.core.requested_depth
           << ",\"imported\":" << (imported ? "true" : "false");
    write_manifest_fields(stream, result);
    stream << ",\"canonical_root_tactical_policy\":\"canonical-boundary-policy-v1\""
           << ",\"canonical_root_tactical_protection\":"
           << (result.canonical_root_tactical_protection ? "true" : "false")
           << ",\"selective\":" << (result.selective ? "true" : "false")
           << ",\"evaluation_work_limit_reached\":"
           << (result.evaluation_work_limit_reached ? "true" : "false")
           << ",\"work\":";
    write_work(stream, result.work, session);
    stream << ",\"product_publishable\":false"
           << ",\"safety_certified\":false";
    write_memory(stream, session);
    stream << '}';
    return stream.str();
}

[[nodiscard]] std::string enumerate_session_result(
    RootSession& session,
    const JsonValue& request
) {
    require_enumerate_keys(request);
    required_schema(request, "spc-root-session-enumerate-v1");
    validate_identity(request, session);
    const RoutingEcho routing = parse_routing(request, session);
    const std::vector<std::string> preferred = string_array(
        field(request, "preferred_series"),
        MAX_UCI_MOVES,
        true
    );
    const auto result = session.core->enumerate_retained_root(
        session.boundary,
        preferred,
        session.parsed_config.core.max_series_per_node,
        false,
        routing.external_work,
        std::optional<std::uint64_t>{routing.call_work_credit},
        relative_deadline(routing)
    );
    accept_receipt(session, routing, result.work);
    if (result.status == spc::native::SubtreeSearchStatus::Complete) {
        session.retained_enumeration_identity = result.enumeration_identity;
        session.retained_candidates = result.candidates;
    } else {
        session.retained_enumeration_identity.clear();
        session.retained_candidates.clear();
    }
    return enumeration_result_json(
        "spc-root-session-enumeration-result-v1",
        session,
        routing,
        result,
        false
    );
}

[[nodiscard]] std::string import_session_result(
    RootSession& session,
    const JsonValue& request
) {
    require_import_keys(request);
    required_schema(request, "spc-root-session-import-v1");
    validate_identity(request, session);
    const RoutingEcho routing = parse_routing(request, session);
    ParsedManifest manifest = parse_manifest(field(request, "manifest"));
    if (
        manifest.root_white_to_move != session.boundary.board.white_to_move
        || manifest.requested_width
            != session.parsed_config.core.max_series_per_node
    ) {
        throw RequestError(
            "manifest-config-mismatch",
            "root manifest boundary mover or width does not match this session"
        );
    }
    spc::native::RetainedRootImportRequest core_request;
    core_request.boundary = session.boundary;
    core_request.enumeration_identity = manifest.enumeration_identity;
    core_request.root_white_to_move = manifest.root_white_to_move;
    core_request.requested_width = manifest.requested_width;
    core_request.width_complete = manifest.width_complete;
    core_request.preferred_series = manifest.preferred_series;
    core_request.candidates = std::move(manifest.candidates);
    core_request.external_work = routing.external_work;
    core_request.call_work_credit = routing.call_work_credit;
    core_request.deadline = relative_deadline(routing);
    const auto result = session.core->import_retained_root(core_request);
    accept_receipt(session, routing, result.work);
    if (result.status == spc::native::SubtreeSearchStatus::Complete) {
        session.retained_enumeration_identity = result.enumeration_identity;
        session.retained_candidates = result.candidates;
    }
    return enumeration_result_json(
        "spc-root-session-import-result-v1",
        session,
        routing,
        result,
        true
    );
}

struct SearchEcho {
    RoutingEcho routing;
    std::uint64_t safety_revision = 0;
    std::uint64_t incumbent_epoch = 0;
    std::string task_id;
    std::string enumeration_identity;
    std::string candidate_identity;
    std::uint64_t order_index = 0;
    std::string order_key;
    std::string purpose;
    std::int64_t mate_score = 0;
    std::int64_t child_depth = 0;
    std::int64_t alpha = 0;
    std::int64_t beta = 0;
    std::string tt_persistence;
    std::string mover;
};

[[nodiscard]] SearchEcho parse_search(
    const JsonValue& request,
    RootSession& session
) {
    require_search_keys(request);
    required_schema(request, "spc-root-candidate-task-v1");
    validate_identity(request, session);
    SearchEcho result;
    result.routing = parse_routing(request, session);
    result.safety_revision = u64_field(request, "safety_revision", 0);
    result.incumbent_epoch = u64_field(request, "incumbent_epoch", 0);
    result.task_id = string_field(request, "task_id");
    result.enumeration_identity = string_field(
        request,
        "enumeration_identity",
        MAX_CANONICAL_ID_BYTES
    );
    result.candidate_identity = string_field(
        request,
        "candidate_identity",
        MAX_CANONICAL_ID_BYTES
    );
    result.order_index = u64_field(request, "order_index", 0, MAX_ROOT_WIDTH);
    result.order_key = string_field(request, "order_key", 1'535);
    result.purpose = string_field(request, "purpose");
    result.mate_score = i64_field(
        request,
        "mate_score",
        1,
        1'000'000'000
    );
    result.child_depth = i64_field(request, "child_depth", 0, 7);
    result.alpha = i64_field(
        request,
        "alpha",
        -2'000'000'000,
        2'000'000'000
    );
    result.beta = i64_field(
        request,
        "beta",
        -2'000'000'000,
        2'000'000'000
    );
    result.tt_persistence = string_field(request, "tt_persistence");
    result.mover = string_field(request, "mover");
    const bool purpose_ok = result.purpose == "full"
        || result.purpose == "scout"
        || result.purpose == "aspiration"
        || result.purpose == "threat-research"
        || result.purpose == "selected-certification";
    const bool aspiration = result.purpose == "aspiration";
    const bool rollback = result.tt_persistence == "rollback";
    const auto& retained = retained_candidate(session, result.candidate_identity);
    if (
        !purpose_ok
        || (result.purpose == "scout") != rollback
        || (
            result.tt_persistence != "commit"
            && result.tt_persistence != "rollback"
        )
        || result.enumeration_identity
            != session.retained_enumeration_identity
        || result.order_index != retained.order_index
        || result.order_key != retained.order_key
        || result.mate_score != session.parsed_config.core.mate_score
        || result.child_depth
            > session.parsed_config.core.requested_depth - 1
        || result.alpha >= result.beta
        || result.alpha < -2 * result.mate_score
        || result.beta > 2 * result.mate_score
        || (
            result.purpose == "scout"
            && result.beta != result.alpha + 1
        )
        || (
            aspiration
            && result.alpha == -2 * result.mate_score
            && result.beta == 2 * result.mate_score
        )
        || (
            result.purpose != "scout"
            && !aspiration
            && (
                result.alpha != -2 * result.mate_score
                || result.beta != 2 * result.mate_score
            )
        )
        || result.mover
            != (session.boundary.board.white_to_move ? "white" : "black")
    ) {
        throw RequestError(
            "candidate-task-invalid",
            "root candidate task is stale or inconsistent with this session"
        );
    }
    return result;
}

void write_search_echo(
    std::ostringstream& stream,
    const SearchEcho& search
) {
    write_routing(stream, search.routing);
    stream << ",\"safety_revision\":" << search.safety_revision
           << ",\"incumbent_epoch\":" << search.incumbent_epoch
           << ",\"task_id\":";
    write_json_string(stream, search.task_id);
    stream << ",\"enumeration_identity\":";
    write_json_string(stream, search.enumeration_identity);
    stream << ",\"candidate_identity\":";
    write_json_string(stream, search.candidate_identity);
    stream << ",\"order_index\":" << search.order_index
           << ",\"order_key\":";
    write_json_string(stream, search.order_key);
    stream << ",\"purpose\":";
    write_json_string(stream, search.purpose);
    stream << ",\"mate_score\":" << search.mate_score
           << ",\"child_depth\":" << search.child_depth
           << ",\"alpha\":" << search.alpha
           << ",\"beta\":" << search.beta
           << ",\"tt_persistence\":";
    write_json_string(stream, search.tt_persistence);
    stream << ",\"mover\":";
    write_json_string(stream, search.mover);
}

[[nodiscard]] std::string search_session_result(
    RootSession& session,
    const JsonValue& request
) {
    const SearchEcho search = parse_search(request, session);
    spc::native::RetainedRootCandidateRequest core_request;
    core_request.enumeration_identity = search.enumeration_identity;
    core_request.candidate_identity = search.candidate_identity;
    core_request.child_depth = search.child_depth;
    core_request.alpha = search.alpha;
    core_request.beta = search.beta;
    core_request.external_work = search.routing.external_work;
    core_request.call_work_credit = search.routing.call_work_credit;
    core_request.deadline = relative_deadline(search.routing);
    core_request.tt_persistence = search.tt_persistence == "rollback"
        ? spc::native::SubtreeTTPersistence::Rollback
        : spc::native::SubtreeTTPersistence::Commit;
    const auto result = session.core->search_retained_root_candidate(core_request);
    accept_receipt(session, search.routing, result.work);

    std::ostringstream stream;
    stream << "{\"schema\":\"spc-root-candidate-result-v1\""
           << ",\"abi_version\":" << ROOT_ABI_VERSION
           << ",\"session_id\":" << session.id
           << ",\"status\":";
    write_json_string(stream, status_name(result.status));
    stream << ",\"status_code\":" << static_cast<int>(result.status)
           << ",\"message\":";
    write_json_string(stream, result.message);
    write_search_echo(stream, search);
    stream << ',';
    write_identity(stream, session.identity);
    stream << ",\"configured_max_depth\":"
           << session.parsed_config.core.requested_depth
           << ",\"bound\":";
    write_json_string(stream, bound_name(result.bound));
    stream << ",\"score\":" << result.score
           << ",\"terminal\":" << (result.terminal ? "true" : "false")
           << ",\"proof_bounds\":[" << result.proof_bounds[0]
           << ',' << result.proof_bounds[1] << ']'
           << ",\"root_series\":";
    if (result.status == spc::native::SubtreeSearchStatus::Complete) {
        write_complete_series(stream, result.root_series);
    } else {
        stream << "null";
    }
    stream << ",\"child_pv\":[";
    bool first = true;
    for (const auto& child : result.child_principal_variation) {
        if (!first) {
            stream << ',';
        }
        first = false;
        write_complete_series(stream, child);
    }
    stream << "]"
           << ",\"selective\":" << (result.selective ? "true" : "false")
           << ",\"evaluation_work_limit_reached\":"
           << (result.evaluation_work_limit_reached ? "true" : "false")
           << ",\"tt_writes_rolled_back\":"
           << result.tt_writes_rolled_back
           << ",\"work\":";
    write_work(stream, result.work, session);
    stream << ",\"product_publishable\":false"
           << ",\"safety_certified\":false";
    write_memory(stream, session);
    stream << '}';
    return stream.str();
}

[[nodiscard]] std::string error_json(
    const char* schema,
    std::uint32_t session_id,
    std::string_view code,
    std::string_view message
) {
    std::ostringstream stream;
    stream << "{\"schema\":";
    write_json_string(stream, schema);
    stream << ",\"abi_version\":" << ROOT_ABI_VERSION
           << ",\"session_id\":" << session_id
           << ",\"status\":\"unsupported\",\"status_code\":4"
           << ",\"error_code\":";
    write_json_string(stream, code);
    stream << ",\"message\":";
    write_json_string(stream, message);
    stream << ",\"product_publishable\":false"
           << ",\"safety_certified\":false";
    if (active_session) {
        stream << ',';
        write_identity(stream, active_session->identity);
        stream << ",\"configured_max_depth\":"
               << active_session->parsed_config.core.requested_depth;
        write_memory(stream, *active_session);
    } else {
        stream << ",\"memory_bytes\":" << wasm_heap_bytes()
               << ",\"memory_peak_bytes\":" << wasm_heap_bytes()
               << ",\"memory_limit_bytes\":" << wasm_heap_limit_bytes();
    }
    stream << '}';
    return stream.str();
}

}  // namespace

extern "C" const char* spc_root_session_contract_json() {
    root_last_result = R"json({"schema":"spc-root-session-contract-v1","abi_version":2,"request_encoding":"caller-owned-utf8-pointer-length","response_ownership":"facade-owned","response_lifetime":"until-next-root-session-abi-call-on-this-worker","one_active_session_per_worker":true,"worker_threads":1,"pthreads_required":false,"product_publishable":false,"reply_mate_safety":false,"capabilities":{"enumerate":true,"import":true,"search":true,"call_work_credit":true,"hard_memory_limit":true,"tt_scout_rollback":true,"persistent_depth_reuse":true,"aspiration_windows":true,"selected_owner_certification":true,"canonical_root_tactical_policy":true},"request_schemas":{"create":"spc-root-session-create-v1","enumerate":"spc-root-session-enumerate-v1","import":"spc-root-session-import-v1","search":"spc-root-candidate-task-v1"},"result_schemas":{"create":"spc-root-session-create-result-v1","enumerate":"spc-root-session-enumeration-result-v1","import":"spc-root-session-import-result-v1","search":"spc-root-candidate-result-v1"},"hard_limits":{"maximum_request_utf8_bytes":16777216,"maximum_json_depth":24,"maximum_json_nodes":250000,"maximum_fen_utf8_bytes":512,"promoted_hex_exact_bytes":16,"maximum_ep_targets":8,"chess960":false,"minimum_depth":1,"maximum_depth":8,"minimum_width":1,"maximum_width":512,"minimum_max_work":1,"maximum_max_work":9007199254740991,"minimum_mate_score":1,"maximum_mate_score":1000000000,"minimum_aspiration_initial_delta":2048,"maximum_aspiration_attempts":4,"minimum_series_cache_capacity":1,"maximum_series_cache_capacity":1048576,"minimum_external_cache_weight":0,"external_cache_weight_lte_series_cache_capacity":true,"worker_threads":1,"root_tactical_protection_values":[false],"root_tactical_policy":"canonical-boundary-policy-v1","minimum_tt_capacity":1,"maximum_tt_capacity":1048576,"minimum_eval_capacity":1,"maximum_eval_capacity":1048576,"minimum_weight":25,"maximum_weight":300,"weight_fields":["material","king_space","series_reach","promotion_corridors","immediate_vulnerability","useful_mobility","boundary_check"],"maximum_series_number":256,"maximum_quiet_series":1000000,"maximum_wasm_memory_bytes":268435456},"manifest":{"preferred_series_required":true,"candidate_root_series_fields":["moves","machine_notation","transposition_count","child_boundary","outcome","ended_by_check"],"child_boundary_exact":true,"root_tactical_policy":"canonical-boundary-policy-v1"},"deadline":{"transport":"remaining_time_ms","coordinator_echo":"deadline_monotonic_ms","zero_means_immediate_timeout":true,"extension_rejected":true}})json";
    return root_last_result.c_str();
}

extern "C" const char* spc_root_session_create_json(
    const char* request_json,
    std::uint32_t request_length
) {
    try {
        root_last_result = create_session_result(
            parse_json_request(request_json, request_length)
        );
    } catch (const RequestError& error) {
        root_last_result = error_json(
            "spc-root-session-create-result-v1",
            active_session ? active_session->id : 0,
            error.code,
            error.what()
        );
    } catch (const std::exception& error) {
        root_last_result = error_json(
            "spc-root-session-create-result-v1",
            active_session ? active_session->id : 0,
            "session-create-failed",
            error.what()
        );
    } catch (...) {
        root_last_result = error_json(
            "spc-root-session-create-result-v1",
            active_session ? active_session->id : 0,
            "session-create-failed",
            "native root session creation failed"
        );
    }
    return root_last_result.c_str();
}

extern "C" const char* spc_root_session_enumerate_json(
    std::uint32_t session_id,
    const char* request_json,
    std::uint32_t request_length
) {
    try {
        RootSession& session = session_for(session_id);
        root_last_result = enumerate_session_result(
            session,
            parse_json_request(request_json, request_length)
        );
    } catch (const RequestError& error) {
        root_last_result = error_json(
            "spc-root-session-enumeration-result-v1",
            session_id,
            error.code,
            error.what()
        );
    } catch (const std::exception& error) {
        root_last_result = error_json(
            "spc-root-session-enumeration-result-v1",
            session_id,
            "session-enumeration-failed",
            error.what()
        );
    } catch (...) {
        root_last_result = error_json(
            "spc-root-session-enumeration-result-v1",
            session_id,
            "session-enumeration-failed",
            "native retained-root enumeration failed"
        );
    }
    return root_last_result.c_str();
}

extern "C" const char* spc_root_session_import_json(
    std::uint32_t session_id,
    const char* request_json,
    std::uint32_t request_length
) {
    try {
        RootSession& session = session_for(session_id);
        root_last_result = import_session_result(
            session,
            parse_json_request(request_json, request_length)
        );
    } catch (const RequestError& error) {
        root_last_result = error_json(
            "spc-root-session-import-result-v1",
            session_id,
            error.code,
            error.what()
        );
    } catch (const std::exception& error) {
        root_last_result = error_json(
            "spc-root-session-import-result-v1",
            session_id,
            "session-import-failed",
            error.what()
        );
    } catch (...) {
        root_last_result = error_json(
            "spc-root-session-import-result-v1",
            session_id,
            "session-import-failed",
            "native retained-root import failed"
        );
    }
    return root_last_result.c_str();
}

extern "C" const char* spc_root_session_search_json(
    std::uint32_t session_id,
    const char* request_json,
    std::uint32_t request_length
) {
    try {
        RootSession& session = session_for(session_id);
        root_last_result = search_session_result(
            session,
            parse_json_request(request_json, request_length)
        );
    } catch (const RequestError& error) {
        root_last_result = error_json(
            "spc-root-candidate-result-v1",
            session_id,
            error.code,
            error.what()
        );
    } catch (const std::exception& error) {
        root_last_result = error_json(
            "spc-root-candidate-result-v1",
            session_id,
            "session-search-failed",
            error.what()
        );
    } catch (...) {
        root_last_result = error_json(
            "spc-root-candidate-result-v1",
            session_id,
            "session-search-failed",
            "native retained-root candidate search failed"
        );
    }
    return root_last_result.c_str();
}

extern "C" std::int32_t spc_root_session_destroy(std::uint32_t session_id) {
    root_last_result.clear();
    if (
        !active_session
        || session_id == 0
        || active_session->id != session_id
    ) {
        return 0;
    }
    active_session.reset();
    return 1;
}

extern "C" std::uint32_t spc_root_session_abi_version() {
    return ROOT_ABI_VERSION;
}
