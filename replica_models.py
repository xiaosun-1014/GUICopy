"""Shared value models for replica capture and replay."""

from __future__ import annotations

import dataclasses
import types
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from typing import Any, Union, get_args, get_origin, get_type_hints


@dataclass(frozen=True)
class StateDiffProfile:
    """Thresholds and sampling cadence for CSS-scale PNG state comparisons."""

    pixel_channel_threshold: int = 12
    regional_changed_ratio: float = 0.02
    regional_mean_abs_diff: float = 3.5
    global_changed_ratio: float = 0.08
    stability_interval_ms: int = 200
    stability_rounds: int = 2

    def to_dict(self) -> dict[str, int | float]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, int | float]) -> "StateDiffProfile":
        return cls(**value)


@dataclass(frozen=True)
class DiffMetrics:
    """Auditable output from a visual comparison."""

    changed_pixel_ratio: float
    mean_abs_diff: float
    changed_pixel_count: int
    compared_pixel_count: int
    masked_pixel_count: int

    def to_dict(self) -> dict[str, int | float]:
        return asdict(self)


@dataclass
class Rect:
    x: float
    y: float
    width: float
    height: float
    coordinate_space: str


@dataclass
class Point:
    x: float
    y: float
    coordinate_space: str


@dataclass
class FrameHop:
    selector: str
    frame_id: str | None
    frame_name: str | None


@dataclass
class LocatorRecipe:
    source_expression: str
    page_var: str
    frame_chain: list[FrameHop]
    locator_kind: str
    locator_args: dict[str, object]
    ordinal_op: str | None
    ordinal_value: int | None


@dataclass
class SelectorClosure:
    action_id: str
    root_outer_html: str
    required_ancestor_count: int
    required_sibling_count: int
    accessible_name_sources: list[str]


@dataclass
class BootstrapPlan:
    source_start_line: int
    source_end_line: int
    skipped_in_offline_replay: bool
    entry_page_bindings: dict[str, str]


@dataclass
class PopupExpectation:
    context_line: int
    source_page_var: str
    info_var: str
    result_page_var: str
    body_action_ids: list[str]


@dataclass
class CaptureTimingProfile:
    locator_wait_ms: int = 5000
    scroll_into_view_ms: int = 3000
    visual_stability_ms: int = 3000
    dom_retry_count: int = 3
    dom_retry_interval_ms: int = 150
    action_budget_ms: int = 12000
    marker_budget_ms: int = 60000
    capture_timeout_s: int = 900
    flow_budget_ms: int = 900000
    virtual_scroll_max_steps: int = 40
    virtual_scroll_budget_ms: int = 10000


@dataclass
class DomNodeSnapshot:
    tag_name: str
    text: str
    attributes: dict[str, str]
    rect: Rect
    outer_html: str
    computed_style: dict[str, str]


@dataclass
class ActionTarget:
    action_id: str
    marker_id: str
    action_type: str
    action_source_kind: str
    action_args: dict[str, object]
    locator: LocatorRecipe | None
    dom: DomNodeSnapshot | None
    selector_closure: SelectorClosure | None
    point: Point | None
    key: str | None
    replay_policy: str
    skip_reason: str | None
    document_id: str
    transition_id: str | None


@dataclass
class RegionMember:
    member_id: str
    semantic_type: str
    dom: DomNodeSnapshot


@dataclass
class SeriesCollectionEvidence:
    collection_mode: str
    virtualized: bool
    visible_count: int
    collected_count: int
    harvest_steps: int
    reached_end: bool
    warning: str | None


@dataclass
class InteractionRegion:
    region_id: str
    region_type: str
    document_id: str
    root: DomNodeSnapshot
    members: list[RegionMember]
    series_collection: SeriesCollectionEvidence | None


@dataclass
class ReplicaDocument:
    document_id: str
    page_id: str
    page_var: str
    page_kind: str
    parent_document_id: str | None
    frame_selector: str | None
    frame_id: str | None
    frame_name: str | None
    viewport: dict[str, int]
    device_scale_factor: float
    screenshot_scale: str
    scroll_x: float
    scroll_y: float
    screenshot_asset_relpath: str
    screenshot_sha256: str
    screenshot_size_bytes: int
    targets: list[ActionTarget] = field(default_factory=list)
    regions: list[InteractionRegion] = field(default_factory=list)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ReplicaDocument":
        """Rehydrate nested target and region models from a snapshot JSON document."""
        return _decode_dataclass(value, cls)


@dataclass
class ReplicaPage:
    page_id: str
    page_var: str
    page_kind: str
    opener_page_id: str | None
    window_name: str | None
    entry_document_id: str
    is_active: bool
    is_closed: bool


@dataclass
class ReplicaTransition:
    transition_id: str
    action_id: str
    from_state_id: str
    to_state_id: str | None
    source_page_var: str
    target_page_var: str
    mode: str


@dataclass
class StateEvidence:
    topology_changed: bool
    url_changed: bool
    popup_changed: bool
    region_dom_changed: bool
    regional_changed_pixel_ratio: float
    regional_mean_abs_diff: float
    global_changed_pixel_ratio: float
    dynamic_mask_count: int
    decision_reason: str


@dataclass
class ReplicaState:
    state_id: str
    ordinal: int
    source_url: str
    active_page_var: str
    pages: list[ReplicaPage]
    documents: list[ReplicaDocument]
    transitions: list[ReplicaTransition]
    evidence: StateEvidence


def _decode(value: Any, annotation: Any) -> Any:
    if value is None:
        return None
    origin = get_origin(annotation)
    if origin is list:
        return [_decode(item, get_args(annotation)[0]) for item in value]
    if origin is dict:
        return value
    if origin in (Union, types.UnionType):
        for candidate in get_args(annotation):
            if candidate is not type(None):
                return _decode(value, candidate)
    if isinstance(annotation, type) and is_dataclass(annotation):
        return _decode_dataclass(value, annotation)
    return value


def _decode_dataclass(value: dict[str, Any], model: type[Any]) -> Any:
    hints = get_type_hints(model)
    decoded: dict[str, Any] = {}
    for item in fields(model):
        if item.name in value:
            decoded[item.name] = _decode(value[item.name], hints[item.name])
        elif item.default is not dataclasses.MISSING:
            decoded[item.name] = item.default
    return model(**decoded)


@dataclass
class ReplicaFlow:
    schema_version: int
    flow_id: str
    source_script_relpath: str
    source_script_sha256: str
    created_at: str
    viewport: dict[str, int]
    bootstrap: BootstrapPlan
    popup_expectations: list[PopupExpectation]
    timing_profile: CaptureTimingProfile
    entry_state_id: str
    states: list[ReplicaState]
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ReplicaFlow":
        if value.get("schema_version") != 1:
            raise ValueError("unsupported replica manifest schema version")
        flow = _decode_dataclass(value, cls)
        if flow.source_script_relpath.startswith(("/", "\\")) or ":" in flow.source_script_relpath:
            raise ValueError("source_script_relpath must be a relative path")
        return flow
