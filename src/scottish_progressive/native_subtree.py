from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

import chess

from . import evaluation
from .model import Outcome, ProgressiveState, SeriesResult
from .profiles import EngineProfile
from .rules import play_series


_REQUIRED_SYMBOLS = (
    "create_subtree_search",
    "subtree_search",
    "subtree_begin_transaction",
    "subtree_rollback_transaction",
    "subtree_external_cache_present",
    "subtree_touch_external_cache",
    "subtree_insert_external_cache",
    "subtree_enumerate_root",
    "subtree_import_root",
    "subtree_search_root_candidate",
)

NATIVE_MAX_HORIZON_PROOFS = 16
NATIVE_MAX_HORIZON_PROOF_PATH = 8

SUBTREE_STAT_FIELDS = (
    "nodes",
    "leaf_evaluations",
    "generated_raw_series",
    "generated_unique_series",
    "intra_series_transpositions",
    "tt_hits",
    "alpha_beta_cutoffs",
    "pvs_zero_window_searches",
    "pvs_researches",
    "pvs_tt_writes_rolled_back",
    "branch_caps",
    "series_generation_positions",
    "frontier_score_positions",
    "static_evaluation_positions",
    "evaluation_reach_positions",
    "evaluation_capture_positions",
    "incomplete_reach_evaluations",
    "tactical_leaf_extensions",
    "overlay_evaluations",
    "overlay_reach_positions",
    "overlay_direct_move_variants",
    "overlay_two_move_variants",
    "generation_positions",
    "frontier_prunes",
    "frontier_states_pruned",
    "frontier_paths_pruned",
    "tactical_frontier_states_retained",
    "tactical_frontier_reserve_drops",
    "tactical_final_series_retained",
    "tactical_final_reserve_drops",
    "peak_frontier_states",
    "generation_work_limit_hits",
    "series_generation_cache_hits",
    "series_generation_cache_evictions",
    "series_generation_cache_peak",
    "series_generation_cache_entries_peak",
)

_DEEP_TEACHER_FIXED_POINT_SCALE = 1_000_000_000
_DEEP_TEACHER_FEATURE_COUNTS = frozenset({7, 14, 19, 38, 44, 47})


def _bounded_ascii_identity(value: object) -> bool:
    return (
        type(value) is str
        and 1 <= len(value) <= 256
        and all("!" <= item <= "~" for item in value)
    )


def _lowercase_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(item in "0123456789abcdef" for item in value)
    )


@dataclass(frozen=True, slots=True)
class NativeDeepTeacherValueModel:
    """Validated, inactive-by-default model transport for native search."""

    base_profile_id: str
    variant_id: str
    model_id: str
    model_sha256: str
    native_source_identity: str
    coefficients: tuple[int, ...]
    fixed_point_scale: int = _DEEP_TEACHER_FIXED_POINT_SCALE

    def __post_init__(self) -> None:
        if not all(
            _bounded_ascii_identity(value)
            for value in (
                self.base_profile_id,
                self.variant_id,
                self.model_id,
            )
        ):
            raise ValueError("native deep-teacher identity is invalid")
        if not _lowercase_sha256(self.model_sha256):
            raise ValueError("native deep-teacher model SHA-256 is invalid")
        if not _lowercase_sha256(self.native_source_identity):
            raise ValueError("native deep-teacher source identity is invalid")
        if (
            type(self.coefficients) is not tuple
            or len(self.coefficients) not in _DEEP_TEACHER_FEATURE_COUNTS
            or any(type(value) is not int for value in self.coefficients)
        ):
            raise ValueError("native deep-teacher coefficients are invalid")
        if (
            type(self.fixed_point_scale) is not int
            or self.fixed_point_scale != _DEEP_TEACHER_FIXED_POINT_SCALE
            or any(
                abs(value) > self.fixed_point_scale
                for value in self.coefficients
            )
            or max(abs(value) for value in self.coefficients)
                != self.fixed_point_scale
        ):
            raise ValueError("native deep-teacher normalization is invalid")

    @property
    def feature_count(self) -> int:
        return len(self.coefficients)

    @classmethod
    def from_overlay_payload(
        cls,
        payload: object,
    ) -> NativeDeepTeacherValueModel:
        # Local import avoids a module cycle: the overlay's public search
        # protocol already imports this native transport through search.py.
        from .deep_teacher_overlay import DeepTeacherOverlayPayload

        if type(payload) is not DeepTeacherOverlayPayload:
            raise TypeError("deep-teacher overlay payload has the wrong type")
        payload.validate()
        return cls(
            base_profile_id=payload.base_profile_id,
            variant_id=payload.variant_id,
            model_id=payload.model_id,
            model_sha256=payload.model_sha256,
            native_source_identity=payload.native_source_identity,
            coefficients=payload.coefficients,
            fixed_point_scale=payload.fixed_point_scale,
        )

    def transport(self) -> tuple[object, ...]:
        return (
            self.base_profile_id,
            self.variant_id,
            self.model_id,
            self.model_sha256,
            self.native_source_identity,
            self.feature_count,
            self.coefficients,
            self.fixed_point_scale,
        )


@dataclass(frozen=True, slots=True)
class NativeSubtreeResult:
    status: int
    message: str
    score: int
    principal_variation: tuple[SeriesResult, ...]
    proof_bounds: tuple[int, int]
    stats: tuple[int, ...]
    selective: bool
    evaluation_work_limit_reached: bool


class NativeSubtreeBound(IntEnum):
    UNKNOWN = 0
    EXACT = 1
    UPPER = 2
    LOWER = 3


@dataclass(frozen=True, slots=True)
class NativeSubtreeWorkReceipt:
    cumulative_stats: tuple[int, ...]
    call_stats: tuple[int, ...]
    external_work: int
    native_work_before: int
    native_work_after: int
    call_native_work: int
    total_accounted_work: int
    call_work_credit: int | None
    tt_entries: int
    tt_entries_peak: int
    tt_capacity: int
    eval_entries: int
    eval_entries_peak: int
    eval_capacity: int


@dataclass(frozen=True, slots=True)
class NativeRetainedRootCandidate:
    candidate_identity: str
    order_index: int
    order_key: str
    series: SeriesResult
    terminal_score: int | None
    terminal_proof_bounds: tuple[int, int]
    transport: tuple[object, ...]


@dataclass(frozen=True, slots=True)
class NativeHorizonProof:
    """Exact selected-PV path plus its opponent's mating reply series."""

    rooted_path: tuple[SeriesResult, ...]
    mate_reply: SeriesResult

    def __post_init__(self) -> None:
        if (
            type(self.rooted_path) is not tuple
            or not self.rooted_path
            or len(self.rooted_path) > NATIVE_MAX_HORIZON_PROOF_PATH
            or any(type(item) is not SeriesResult for item in self.rooted_path)
            or type(self.mate_reply) is not SeriesResult
        ):
            raise TypeError("native horizon proof has an invalid series payload")

    def transport(self) -> tuple[object, ...]:
        return (
            tuple(_horizon_series_transport(item) for item in self.rooted_path),
            _horizon_series_transport(self.mate_reply, transposition_count=1),
        )


@dataclass(frozen=True, slots=True)
class NativeRootEnumerationResult:
    status: int
    message: str
    enumeration_identity: str
    root_white_to_move: bool
    requested_width: int
    retained_count: int
    width_complete: bool
    preferred_series: tuple[str, ...]
    candidates: tuple[NativeRetainedRootCandidate, ...]
    work: NativeSubtreeWorkReceipt
    selective: bool
    evaluation_work_limit_reached: bool
    terminal_mate_scan: bool


@dataclass(frozen=True, slots=True)
class NativeRootCandidateResult:
    status: int
    message: str
    enumeration_identity: str
    candidate_identity: str
    order_index: int
    bound: NativeSubtreeBound
    score: int
    terminal: bool
    root_series: SeriesResult | None
    child_principal_variation: tuple[SeriesResult, ...]
    proof_bounds: tuple[int, int]
    work: NativeSubtreeWorkReceipt
    selective: bool
    evaluation_work_limit_reached: bool
    tt_writes_rolled_back: int
    horizon_proof_set_identity: str
    horizon_proofs_validated: int
    horizon_proof_hits: int
    horizon_proof_hit_mask: int

    @property
    def principal_variation(self) -> tuple[SeriesResult, ...]:
        return (
            ()
            if self.root_series is None
            else (self.root_series,) + self.child_principal_variation
        )


def native_subtree_available() -> bool:
    native = evaluation._native_eval  # noqa: SLF001
    return native is not None and all(
        hasattr(native, symbol) for symbol in _REQUIRED_SYMBOLS
    )


def native_subtree_eligible(
    state: ProgressiveState,
    *,
    requested_depth: int,
    max_series_per_node: int | None,
    max_work: int | None,
    profile: EngineProfile,
    has_overlay: bool,
) -> bool:
    """Conservative all-or-nothing gate for the experimental descendant core."""

    if (
        not native_subtree_available()
        or has_overlay
        or type(profile) is not EngineProfile
        or state.board.chess960
        or max_series_per_node is None
        or type(max_series_per_node) is not int
        or not 1 <= max_series_per_node <= (1 << 63) - 1
        or type(requested_depth) is not int
        or not 1 <= requested_depth <= 8
        # The C++ slice intentionally does not implement the proof search used
        # by the ten-quiet-series exception. Keep the whole native horizon below
        # that boundary so unsupported can never be reached after work begins.
        # The selective tactical leaf may consume one additional complete
        # series beyond the nominal depth, so include that bounded token.
        or state.quiet_series + requested_depth + 1 >= 10
        or state.series_number + requested_depth + 1 >= (1 << 63) - 1
    ):
        return False
    if max_work is not None and (
        type(max_work) is not int or not 1 <= max_work <= (1 << 64) - 1
    ):
        return False
    return evaluation._native_full_evaluation_is_safe(  # noqa: SLF001
        state,
        profile.weights,
        max_work,
    )


def _state_tuple(state: ProgressiveState) -> tuple[object, ...]:
    board = state.board
    return (
        board.pawns,
        board.knights,
        board.bishops,
        board.rooks,
        board.queens,
        board.kings,
        board.occupied_co[chess.WHITE],
        board.occupied_co[chess.BLACK],
        board.promoted,
        board.castling_rights,
        board.turn,
        board.halfmove_clock,
        board.fullmove_number,
        state.series_number,
        state.quiet_series,
        state.ep_targets,
    )


_OUTCOME_BY_CODE = {
    0: None,
    1: Outcome.CHECKMATE,
    2: Outcome.STALEMATE,
    3: Outcome.TEN_SERIES_DRAW,
}
_OUTCOME_CODE = {value: key for key, value in _OUTCOME_BY_CODE.items()}


def _horizon_series_transport(
    series: SeriesResult,
    *,
    transposition_count: int | None = None,
) -> tuple[object, ...]:
    return (
        series.moves,
        series.transposition_count
        if transposition_count is None
        else transposition_count,
        _state_tuple(series.final_state),
        _OUTCOME_CODE[series.outcome],
        series.ended_by_check,
    )


def _work_receipt(raw: object) -> NativeSubtreeWorkReceipt:
    values = tuple(raw)
    if len(values) != 4:
        raise RuntimeError("native subtree work receipt shape mismatch")
    cumulative = tuple(int(value) for value in values[0])
    call = tuple(int(value) for value in values[1])
    counters = tuple(int(value) for value in values[2])
    if (
        len(cumulative) != len(SUBTREE_STAT_FIELDS)
        or len(call) != len(SUBTREE_STAT_FIELDS)
        or len(counters) != 11
        or any(value < 0 for value in cumulative + call + counters)
    ):
        raise RuntimeError("native subtree work receipt is invalid")
    credit = None if values[3] is None else int(values[3])
    generation_index = SUBTREE_STAT_FIELDS.index("generation_positions")
    if (
        counters[2] < counters[1]
        or counters[3] != counters[2] - counters[1]
        or call[generation_index] != counters[3]
        or cumulative[generation_index] != counters[2]
        or counters[4] != min((1 << 64) - 1, counters[0] + counters[2])
        or counters[5] > counters[6]
        or counters[6] > counters[7]
        or counters[7] < 1
        or counters[8] > counters[9]
        or counters[9] > counters[10]
        or counters[10] < 1
        or (credit is not None and (credit < 0 or counters[3] > credit))
    ):
        raise RuntimeError("native subtree work receipt invariants are invalid")
    return NativeSubtreeWorkReceipt(
        cumulative_stats=cumulative,
        call_stats=call,
        external_work=counters[0],
        native_work_before=counters[1],
        native_work_after=counters[2],
        call_native_work=counters[3],
        total_accounted_work=counters[4],
        call_work_credit=credit,
        tt_entries=counters[5],
        tt_entries_peak=counters[6],
        tt_capacity=counters[7],
        eval_entries=counters[8],
        eval_entries_peak=counters[9],
        eval_capacity=counters[10],
    )


def _retained_candidate(
    root: ProgressiveState,
    raw: object,
) -> NativeRetainedRootCandidate:
    values = tuple(raw)
    if len(values) != 10:
        raise RuntimeError("native retained-root candidate shape mismatch")
    moves = tuple(str(move) for move in values[3])
    if any(type(move) is not str for move in tuple(values[3])):
        raise RuntimeError("native retained-root candidate move is not a string")
    count = int(values[4])
    if count < 1:
        raise RuntimeError("native retained-root path count is invalid")
    replayed = play_series(root, moves).with_transposition_count(count)
    state_values = tuple(values[5])
    if state_values != _state_tuple(replayed.final_state):
        raise RuntimeError("native retained-root final state failed replay")
    outcome_code = int(values[6])
    if outcome_code not in _OUTCOME_BY_CODE:
        raise RuntimeError("native retained-root outcome is invalid")
    if (
        replayed.outcome != _OUTCOME_BY_CODE[outcome_code]
        or replayed.ended_by_check != bool(values[7])
    ):
        raise RuntimeError("native retained-root outcome failed replay")
    proof = tuple(int(value) for value in values[9])
    if len(proof) != 2 or any(value not in {-1, 0, 1} for value in proof):
        raise RuntimeError("native retained-root proof is invalid")
    candidate = NativeRetainedRootCandidate(
        candidate_identity=str(values[0]),
        order_index=int(values[1]),
        order_key=str(values[2]),
        series=replayed,
        terminal_score=(None if values[8] is None else int(values[8])),
        terminal_proof_bounds=(proof[0], proof[1]),
        transport=values,
    )
    if (
        candidate.order_index < 0
        or candidate.order_key != replayed.machine_notation
        or not candidate.candidate_identity
    ):
        raise RuntimeError("native retained-root identity/order is invalid")
    return candidate


def _enumeration_result(
    root: ProgressiveState,
    raw: object,
) -> NativeRootEnumerationResult:
    values = tuple(raw)
    if len(values) != 13:
        raise RuntimeError("native retained-root enumeration shape mismatch")
    status = int(values[0])
    candidates = (
        tuple(_retained_candidate(root, item) for item in values[8])
        if status == 0
        else ()
    )
    result = NativeRootEnumerationResult(
        status=status,
        message=str(values[1]),
        enumeration_identity=str(values[2]),
        root_white_to_move=bool(values[3]),
        requested_width=int(values[4]),
        retained_count=int(values[5]),
        width_complete=bool(values[6]),
        preferred_series=tuple(str(move) for move in values[7]),
        candidates=candidates,
        work=_work_receipt(values[9]),
        selective=bool(values[10]),
        evaluation_work_limit_reached=bool(values[11]),
        terminal_mate_scan=bool(values[12]),
    )
    if status == 0 and (
        not result.enumeration_identity
        or result.root_white_to_move != root.board.turn
        or result.retained_count != len(candidates)
        or tuple(candidate.order_index for candidate in candidates)
            != tuple(range(len(candidates)))
    ):
        raise RuntimeError("native retained-root enumeration is invalid")
    if status != 0 and (result.enumeration_identity or result.retained_count):
        raise RuntimeError("failed native root enumeration returned a manifest")
    return result


class NativeSubtreeSession:
    __slots__ = ("_capsule", "_descendant_width", "_native", "_root_states")

    def __init__(
        self,
        *,
        max_series_per_node: int,
        max_work: int | None,
        requested_depth: int,
        mate_score: int,
        cache_capacity: int,
        external_cache_weight: int,
        native_threads: int,
        root_tactical_protection: bool,
        profile: EngineProfile,
        root_contract_tt_capacity: int = 262_144,
        root_contract_eval_capacity: int = 262_144,
        deep_teacher_value_model: NativeDeepTeacherValueModel | None = None,
    ) -> None:
        native = evaluation._native_eval  # noqa: SLF001
        if native is None or not native_subtree_available():
            raise RuntimeError("source-matched native subtree API is unavailable")
        if deep_teacher_value_model is not None:
            if type(deep_teacher_value_model) is not NativeDeepTeacherValueModel:
                raise TypeError("native deep-teacher model has the wrong type")
            if deep_teacher_value_model.base_profile_id != profile.profile_id:
                raise ValueError(
                    "native deep-teacher model is bound to a different profile"
                )
            if (
                deep_teacher_value_model.native_source_identity
                != evaluation._native_source_identity()  # noqa: SLF001
            ):
                raise ValueError(
                    "native deep-teacher model is bound to different sources"
                )
        weights = profile.weights
        self._native = native
        self._descendant_width = max_series_per_node
        self._root_states: dict[str, ProgressiveState] = {}
        self._capsule = native.create_subtree_search(
            max_series_per_node,
            max_work,
            requested_depth,
            mate_score,
            cache_capacity,
            external_cache_weight,
            native_threads,
            root_tactical_protection,
            (
                weights.material,
                weights.king_space,
                weights.promotion_corridors,
                weights.immediate_vulnerability,
                weights.boundary_check,
            ),
            (
                weights.material,
                weights.king_space,
                weights.series_reach,
                weights.promotion_corridors,
                weights.immediate_vulnerability,
                weights.useful_mobility,
                weights.boundary_check,
            ),
            root_contract_tt_capacity,
            root_contract_eval_capacity,
            (
                None
                if deep_teacher_value_model is None
                else deep_teacher_value_model.transport()
            ),
        )

    def search(
        self,
        state: ProgressiveState,
        *,
        depth: int,
        alpha: int,
        beta: int,
        ply_from_root: int,
        external_work: int,
        remaining_nanoseconds: int | None,
    ) -> NativeSubtreeResult:
        raw = tuple(
            self._native.subtree_search(
                self._capsule,
                _state_tuple(state),
                depth,
                alpha,
                beta,
                ply_from_root,
                external_work,
                remaining_nanoseconds,
            )
        )
        if len(raw) != 8:
            raise RuntimeError("native subtree result shape mismatch")
        status = int(raw[0])
        pv: list[SeriesResult] = []
        cursor = state
        if status == 0:
            for raw_moves, raw_count in raw[3]:
                moves = tuple(raw_moves)
                if any(type(move) is not str for move in moves):
                    raise RuntimeError("native subtree PV contains a non-string move")
                count = int(raw_count)
                if count < 1:
                    raise RuntimeError("native subtree PV count is invalid")
                result = play_series(cursor, moves).with_transposition_count(count)
                pv.append(result)
                cursor = result.final_state
        stats = tuple(int(value) for value in raw[5])
        if len(stats) != len(SUBTREE_STAT_FIELDS):
            raise RuntimeError("native subtree stats shape mismatch")
        proof_bounds = tuple(int(value) for value in raw[4])
        if len(proof_bounds) != 2:
            raise RuntimeError("native subtree proof shape mismatch")
        return NativeSubtreeResult(
            status=status,
            message=str(raw[1]),
            score=int(raw[2]),
            principal_variation=tuple(pv),
            proof_bounds=(proof_bounds[0], proof_bounds[1]),
            stats=stats,
            selective=bool(raw[6]),
            evaluation_work_limit_reached=bool(raw[7]),
        )

    def enumerate_root(
        self,
        state: ProgressiveState,
        *,
        preferred_series: str | None,
        external_work: int,
        remaining_nanoseconds: int | None,
        call_work_credit: int | None = None,
        requested_width: int | None = None,
        terminal_mate_scan: bool = False,
    ) -> NativeRootEnumerationResult:
        preferred = (
            ()
            if preferred_series is None
            else tuple(preferred_series.split("/"))
        )
        raw = self._native.subtree_enumerate_root(
            self._capsule,
            _state_tuple(state),
            preferred,
            self._descendant_width if requested_width is None else requested_width,
            terminal_mate_scan,
            external_work,
            call_work_credit,
            remaining_nanoseconds,
        )
        result = _enumeration_result(state, raw)
        if result.status == 0 and not result.terminal_mate_scan:
            self._root_states = {result.enumeration_identity: state}
        else:
            self._root_states.clear()
        return result

    def import_root(
        self,
        state: ProgressiveState,
        manifest: NativeRootEnumerationResult,
        *,
        external_work: int,
        remaining_nanoseconds: int | None,
        call_work_credit: int | None = None,
    ) -> NativeRootEnumerationResult:
        raw = self._native.subtree_import_root(
            self._capsule,
            _state_tuple(state),
            manifest.enumeration_identity,
            manifest.root_white_to_move,
            manifest.requested_width,
            manifest.width_complete,
            manifest.preferred_series,
            tuple(candidate.transport for candidate in manifest.candidates),
            external_work,
            call_work_credit,
            remaining_nanoseconds,
        )
        result = _enumeration_result(state, raw)
        if result.status == 0:
            self._root_states = {result.enumeration_identity: state}
        # Native peer import is transactional: an invalid/interrupted
        # replacement leaves the previously verified manifest searchable.
        # Preserve the matching Python replay boundary on the same rule.
        return result

    def search_root_candidate(
        self,
        *,
        enumeration_identity: str,
        candidate_identity: str,
        child_depth: int,
        alpha: int,
        beta: int,
        external_work: int,
        remaining_nanoseconds: int | None,
        rollback_tt: bool,
        call_work_credit: int | None = None,
        horizon_proofs: tuple[NativeHorizonProof, ...] = (),
    ) -> NativeRootCandidateResult:
        if (
            type(horizon_proofs) is not tuple
            or len(horizon_proofs) > NATIVE_MAX_HORIZON_PROOFS
            or any(type(item) is not NativeHorizonProof for item in horizon_proofs)
        ):
            raise TypeError("native horizon proofs must be an exact tuple")
        raw = tuple(
            self._native.subtree_search_root_candidate(
                self._capsule,
                enumeration_identity,
                candidate_identity,
                child_depth,
                alpha,
                beta,
                external_work,
                call_work_credit,
                remaining_nanoseconds,
                rollback_tt,
                tuple(item.transport() for item in horizon_proofs),
            )
        )
        if len(raw) != 19:
            raise RuntimeError("native retained-root search shape mismatch")
        status = int(raw[0])
        root_state = self._root_states.get(enumeration_identity)
        if status == 0 and root_state is None:
            raise RuntimeError("native retained-root search has no boundary")
        root_series = (
            _retained_candidate(root_state, raw[8]).series
            if status == 0 and root_state is not None
            else None
        )
        child_pv: list[SeriesResult] = []
        cursor = root_series.final_state if root_series is not None else None
        if status == 0:
            if cursor is None:  # pragma: no cover - guarded above
                raise RuntimeError("native retained-root search lost its root")
            for raw_moves, raw_count in raw[9]:
                moves = tuple(raw_moves)
                if any(type(move) is not str for move in moves):
                    raise RuntimeError("native retained-root PV move is invalid")
                count = int(raw_count)
                if count < 1:
                    raise RuntimeError("native retained-root PV count is invalid")
                replayed = play_series(cursor, moves).with_transposition_count(
                    count
                )
                child_pv.append(replayed)
                cursor = replayed.final_state
        proof = tuple(int(value) for value in raw[10])
        if len(proof) != 2:
            raise RuntimeError("native retained-root proof shape mismatch")
        try:
            bound = NativeSubtreeBound(int(raw[5]))
        except ValueError as error:
            raise RuntimeError("native retained-root bound is invalid") from error
        result = NativeRootCandidateResult(
            status=status,
            message=str(raw[1]),
            enumeration_identity=str(raw[2]),
            candidate_identity=str(raw[3]),
            order_index=int(raw[4]),
            bound=bound,
            score=int(raw[6]),
            terminal=bool(raw[7]),
            root_series=root_series,
            child_principal_variation=tuple(child_pv),
            proof_bounds=(proof[0], proof[1]),
            work=_work_receipt(raw[11]),
            selective=bool(raw[12]),
            evaluation_work_limit_reached=bool(raw[13]),
            tt_writes_rolled_back=int(raw[14]),
            horizon_proof_set_identity=str(raw[15]),
            horizon_proofs_validated=int(raw[16]),
            horizon_proof_hits=int(raw[17]),
            horizon_proof_hit_mask=int(raw[18]),
        )
        if status == 0 and (
            result.bound is NativeSubtreeBound.UNKNOWN
            or result.enumeration_identity != enumeration_identity
            or result.candidate_identity != candidate_identity
            or root_series is None
        ):
            raise RuntimeError("native retained-root completed result is invalid")
        if status != 0 and result.bound is not NativeSubtreeBound.UNKNOWN:
            raise RuntimeError("failed native retained-root search returned a bound")
        if (
            result.horizon_proofs_validated < 0
            or result.horizon_proof_hits < 0
            or result.horizon_proof_hit_mask < 0
            or result.horizon_proof_hit_mask.bit_count()
                != result.horizon_proof_hits
            or bool(result.horizon_proof_hit_mask)
                != bool(result.horizon_proof_hits)
            or (
                result.horizon_proof_hit_mask
                    >= (1 << result.horizon_proofs_validated)
                if result.horizon_proofs_validated
                else result.horizon_proof_hit_mask != 0
            )
            or (
                status == 0
                and (
                    result.horizon_proofs_validated != len(horizon_proofs)
                    or bool(result.horizon_proof_set_identity)
                        != bool(horizon_proofs)
                )
            )
            or (
                status != 0
                and (
                    result.horizon_proof_set_identity
                    or result.horizon_proofs_validated
                    or result.horizon_proof_hits
                    or result.horizon_proof_hit_mask
                )
            )
        ):
            raise RuntimeError("native retained-root horizon proof receipt is invalid")
        return result

    def begin_transaction(self) -> None:
        self._native.subtree_begin_transaction(self._capsule)

    def rollback_transaction(self) -> int:
        writes = int(self._native.subtree_rollback_transaction(self._capsule))
        if writes < 0:
            raise RuntimeError("native subtree rollback count is invalid")
        return writes

    def external_cache_present(self) -> bool:
        return bool(self._native.subtree_external_cache_present(self._capsule))

    def touch_external_cache(self) -> None:
        self._native.subtree_touch_external_cache(self._capsule)

    def insert_external_cache(self, weight: int) -> None:
        if weight < 1:
            raise ValueError("external cache weight must be positive")
        self._native.subtree_insert_external_cache(self._capsule, weight)
