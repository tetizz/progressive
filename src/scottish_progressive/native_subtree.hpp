#pragma once

#include "native_eval.hpp"

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <optional>
#include <string>
#include <vector>

namespace spc::native {

inline constexpr std::size_t RETAINED_ROOT_MAX_HORIZON_PROOFS = 16;
inline constexpr std::size_t RETAINED_ROOT_MAX_HORIZON_PROOF_PATH = 8;

enum class SubtreeSearchStatus : std::uint8_t {
    Complete = 0,
    WorkLimit = 1,
    Deadline = 2,
    AdjudicationPending = 3,
    Unsupported = 4,
};

// A completed zero-window call proves only one side of its caller's window.
// Unknown is reserved for non-complete calls and must never be reduced as a
// candidate score by a root coordinator.
enum class SubtreeBoundKind : std::uint8_t {
    Unknown = 0,
    Exact = 1,
    Upper = 2,
    Lower = 3,
};

enum class SubtreeTTPersistence : std::uint8_t {
    Commit = 0,
    Rollback = 1,
};

struct SubtreeState {
    BoardState board;
    std::int64_t halfmove_clock = 0;
    std::int64_t fullmove_number = 1;
    std::int64_t series_number = 1;
    std::int64_t quiet_series = 0;
    std::vector<int> ep_targets;
};

// Optional frozen value model shared by the CPython descendant core and the
// browser root-session core. Identity fields do not affect arithmetic, but are
// included in every retained-root identity so two coefficient payloads can
// never alias a cache/manifest contract.
struct SubtreeDeepTeacherValueModel {
    std::string base_profile_id;
    std::string variant_id;
    std::string model_id;
    std::string model_sha256;
    std::string native_source_identity;
    DeepTeacherLinearModelV1 linear;
};

struct SubtreeSearchConfig {
    std::uint64_t max_series_per_node = 1;
    std::optional<std::uint64_t> max_work;
    std::int64_t requested_depth = 1;
    std::int64_t mate_score = 1'000'000;
    std::uint64_t series_cache_capacity = 16'384;
    std::uint64_t external_cache_weight = 0;
    std::uint32_t worker_threads = 1;
    bool root_tactical_protection = false;
    FastWeights fast_weights{};
    FullWeights full_weights{};
    // Root-coordinator sessions fail closed before these maps exceed their
    // deterministic bounds. Both ceilings are part of the manifest identity
    // and every work receipt. The legacy Python descendant path does not
    // enable this contract mode, preserving its established stats/PV parity.
    std::uint64_t root_contract_tt_capacity = 262'144;
    std::uint64_t root_contract_eval_capacity = 262'144;
    // Absent by default. No production profile activates this field until a
    // separately certified model is packaged and selected by its manifest.
    std::optional<SubtreeDeepTeacherValueModel> deep_teacher_value_model;
};

struct SubtreeSearchStats {
    std::uint64_t nodes = 0;
    std::uint64_t leaf_evaluations = 0;
    std::uint64_t generated_raw_series = 0;
    std::uint64_t generated_unique_series = 0;
    std::uint64_t intra_series_transpositions = 0;
    std::uint64_t tt_hits = 0;
    std::uint64_t alpha_beta_cutoffs = 0;
    std::uint64_t pvs_zero_window_searches = 0;
    std::uint64_t pvs_researches = 0;
    std::uint64_t pvs_tt_writes_rolled_back = 0;
    std::uint64_t branch_caps = 0;
    std::uint64_t series_generation_positions = 0;
    std::uint64_t frontier_score_positions = 0;
    std::uint64_t static_evaluation_positions = 0;
    std::uint64_t evaluation_reach_positions = 0;
    std::uint64_t evaluation_capture_positions = 0;
    std::uint64_t incomplete_reach_evaluations = 0;
    std::uint64_t tactical_leaf_extensions = 0;
    std::uint64_t overlay_evaluations = 0;
    std::uint64_t overlay_reach_positions = 0;
    std::uint64_t overlay_direct_move_variants = 0;
    std::uint64_t overlay_two_move_variants = 0;
    std::uint64_t generation_positions = 0;
    std::uint64_t frontier_prunes = 0;
    std::uint64_t frontier_states_pruned = 0;
    std::uint64_t frontier_paths_pruned = 0;
    std::uint64_t tactical_frontier_states_retained = 0;
    std::uint64_t tactical_frontier_reserve_drops = 0;
    std::uint64_t tactical_final_series_retained = 0;
    std::uint64_t tactical_final_reserve_drops = 0;
    std::uint64_t peak_frontier_states = 0;
    std::uint64_t generation_work_limit_hits = 0;
    std::uint64_t series_generation_cache_hits = 0;
    std::uint64_t series_generation_cache_evictions = 0;
    std::uint64_t series_generation_cache_peak = 0;
    std::uint64_t series_generation_cache_entries_peak = 0;
};

struct SubtreeWorkReceipt {
    // Additive fields in call_stats are deltas from the start of this call.
    // Peak fields are the cumulative session gauges observed after the call;
    // subtracting a peak would not describe a meaningful per-call quantity.
    SubtreeSearchStats cumulative_stats;
    SubtreeSearchStats call_stats;
    std::uint64_t external_work = 0;
    std::uint64_t native_work_before = 0;
    std::uint64_t native_work_after = 0;
    std::uint64_t call_native_work = 0;
    std::uint64_t total_accounted_work = 0;
    // This call's native delta never exceeds the optional credit. Zero is a
    // valid credit, and a call may complete exactly when delta == credit.
    std::optional<std::uint64_t> call_work_credit;
    std::uint64_t tt_entries = 0;
    std::uint64_t tt_entries_peak = 0;
    std::uint64_t tt_capacity = 0;
    std::uint64_t eval_entries = 0;
    std::uint64_t eval_entries_peak = 0;
    std::uint64_t eval_capacity = 0;
};

struct SubtreeSearchResult {
    SubtreeSearchStatus status = SubtreeSearchStatus::Complete;
    std::string message;
    std::int64_t score = 0;
    std::vector<CompleteSeriesCandidate> principal_variation;
    std::array<int, 2> proof_bounds{-1, 1};
    SubtreeSearchStats stats;
    bool selective = false;
    bool evaluation_work_limit_reached = false;
};

struct RetainedRootCandidate {
    // candidate_identity is collision-free canonical text over the complete
    // path, final BoardState (including promoted/castling), clocks,
    // Progressive state, outcome, check flag, and path multiplicity.
    std::string candidate_identity;
    std::uint64_t order_index = 0;
    // Python's canonical root tie key: slash-separated UCI moves.
    std::string order_key;
    CompleteSeriesCandidate series;
    std::optional<std::int64_t> terminal_score;
    std::array<int, 2> terminal_proof_bounds{-1, 1};
};

struct RetainedRootEnumerationResult {
    SubtreeSearchStatus status = SubtreeSearchStatus::Complete;
    std::string message;
    // Exact text identity for the boundary, search/memory configuration, and
    // preferred-series input used for this retained list.
    std::string enumeration_identity;
    bool root_white_to_move = true;
    std::uint64_t requested_width = 0;
    std::uint64_t retained_count = 0;
    bool width_complete = false;
    // Evidence-only terminal scans may widen the root generation frontier
    // without changing max_series_per_node for any descendant. Their
    // candidates are delivered current-series checkmates only and are never
    // installed as a searchable retained root.
    bool terminal_mate_scan = false;
    // Exact generator telemetry retained for in-process proof consumers. These
    // counts are deliberately separate from the filtered mate-only manifest.
    std::uint64_t generation_checking_series = 0;
    // Canonical boundary policy propagated to descendant generation. Root
    // ply one is always tactically protected independently of this flag.
    bool canonical_root_tactical_protection = false;
    std::vector<std::string> preferred_series;
    std::vector<RetainedRootCandidate> candidates;
    SubtreeWorkReceipt work;
    bool selective = false;
    bool evaluation_work_limit_reached = false;
};

struct RetainedRootImportRequest {
    SubtreeState boundary;
    std::string enumeration_identity;
    bool root_white_to_move = true;
    std::uint64_t requested_width = 0;
    bool width_complete = false;
    std::vector<std::string> preferred_series;
    std::vector<RetainedRootCandidate> candidates;
    // Coordinator root-generation work is external to this worker. Candidate
    // replay performed during import is native worker work and is charged once
    // per imported candidate.
    std::uint64_t external_work = 0;
    std::optional<std::uint64_t> call_work_credit = std::nullopt;
    std::optional<std::chrono::steady_clock::time_point> deadline = std::nullopt;
};

struct RetainedRootHorizonProof {
    // The path is rooted at the retained boundary and includes the retained
    // root series. Its final boundary must be nonterminal with the adverse
    // side to move. mate_reply is the exact next complete series and must end
    // in checkmate.
    std::vector<CompleteSeriesCandidate> rooted_path;
    CompleteSeriesCandidate mate_reply;
};

struct RetainedRootCandidateRequest {
    std::string enumeration_identity;
    std::string candidate_identity;
    // Depth below the already-complete root series. A depth-one root
    // iteration therefore supplies child_depth == 0.
    std::int64_t child_depth = 0;
    std::int64_t alpha = 0;
    std::int64_t beta = 0;
    std::uint64_t external_work = 0;
    std::optional<std::uint64_t> call_work_credit = std::nullopt;
    std::optional<std::chrono::steady_clock::time_point> deadline = std::nullopt;
    // Rollback makes a scout's TT writes transactional. Generation/evaluation
    // memoization and all work/stat counters remain cumulative and receipted.
    SubtreeTTPersistence tt_persistence = SubtreeTTPersistence::Commit;
    // Empty preserves the ordinary retained-root search namespace exactly.
    // Non-empty payloads are authoritatively replayed before a same-depth,
    // full-window re-search and may substitute only their exact depth-zero
    // selected-PV boundaries.
    std::vector<RetainedRootHorizonProof> horizon_proofs;
};

struct RetainedRootCandidateResult {
    SubtreeSearchStatus status = SubtreeSearchStatus::Complete;
    std::string message;
    std::string enumeration_identity;
    std::string candidate_identity;
    std::uint64_t order_index = 0;
    SubtreeBoundKind bound = SubtreeBoundKind::Unknown;
    std::int64_t score = 0;
    bool terminal = false;
    CompleteSeriesCandidate root_series;
    std::vector<CompleteSeriesCandidate> child_principal_variation;
    std::array<int, 2> proof_bounds{-1, 1};
    SubtreeWorkReceipt work;
    bool selective = false;
    bool evaluation_work_limit_reached = false;
    std::uint64_t tt_writes_rolled_back = 0;
    std::string horizon_proof_set_identity;
    std::uint64_t horizon_proofs_validated = 0;
    std::uint64_t horizon_proof_hits = 0;
    // Bits align to the request's horizon_proofs order even though native
    // canonicalizes the proof-set identity internally. A warm exact TT reuse
    // may return zero because no depth-zero proof was revisited.
    std::uint16_t horizon_proof_hit_mask = 0;
};

// Returns a collision-free, versioned identity over every field in
// SubtreeState. It deliberately preserves clocks and promoted provenance.
[[nodiscard]] std::string subtree_state_identity(const SubtreeState& state);

// Mirrors Python's canonical root policy: late Progressive roots and roots
// with a concrete promotion-mate corridor protect every descendant frontier.
[[nodiscard]] bool root_tactical_protection_eligible(
    const SubtreeState& state
) noexcept;

class SubtreeSearchSession {
public:
    explicit SubtreeSearchSession(SubtreeSearchConfig config);
    ~SubtreeSearchSession();

    SubtreeSearchSession(const SubtreeSearchSession&) = delete;
    SubtreeSearchSession& operator=(const SubtreeSearchSession&) = delete;

    [[nodiscard]] SubtreeSearchResult search(
        const SubtreeState& state,
        std::int64_t depth,
        std::int64_t alpha,
        std::int64_t beta,
        std::int64_t ply_from_root,
        std::uint64_t external_work,
        std::optional<std::chrono::steady_clock::time_point> deadline
    );

    // This is a kernel/root-coordinator contract, not a public move result.
    // It does no root reply-mate safety screening and exposes no publishable
    // or safety-certified flag. Callers must retain Python/server safety hooks.
    // external_work and deadlines are monotonic for the lifetime of a session.
    [[nodiscard]] RetainedRootEnumerationResult enumerate_retained_root(
        const SubtreeState& state,
        const std::vector<std::string>& preferred_series,
        std::uint64_t requested_width,
        bool terminal_mate_scan,
        std::uint64_t external_work,
        std::optional<std::uint64_t> call_work_credit,
        std::optional<std::chrono::steady_clock::time_point> deadline
    );

    // Imports a coordinator-generated retained list without regenerating the
    // broad frontier. Every candidate identity is recomputed and each series
    // is authoritatively replayed through a bounded required-prefix call.
    [[nodiscard]] RetainedRootEnumerationResult import_retained_root(
        const RetainedRootImportRequest& request
    );

    [[nodiscard]] RetainedRootCandidateResult search_retained_root_candidate(
        const RetainedRootCandidateRequest& request
    );

    void begin_tt_transaction();
    [[nodiscard]] std::uint64_t rollback_tt_transaction();
    [[nodiscard]] bool external_cache_present() const;
    void touch_external_cache();
    void insert_external_cache(std::uint64_t weight);

private:
    class Impl;
    std::unique_ptr<Impl> impl_;
};

}  // namespace spc::native
