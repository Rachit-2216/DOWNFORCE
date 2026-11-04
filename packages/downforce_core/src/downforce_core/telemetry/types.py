"""Immutable inputs and explicitly derived outputs; no provider observations are rewritten."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

type ChannelValue = float | int | bool | None
type Channel = tuple[ChannelValue, ...]
type Channels = Mapping[str, Channel]
ALIGNMENT_VERSION = "physical-common-distance-v1"
CORNER_VERSION = "provider-markers-or-distance-windows-v1"
MAX_GRID_POINTS = 20_000
DISCRETE_CHANNELS = frozenset({"gear", "brake", "drs_raw"})


class TraceRejected(ValueError):
    """A trace cannot safely support the requested derived operation."""


@dataclass(frozen=True)
class RawTelemetry:
    session_time_ns: tuple[int, ...]
    channels: Channels
    distance_m: tuple[float, ...] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "session_time_ns", tuple(self.session_time_ns))
        object.__setattr__(
            self,
            "channels",
            MappingProxyType({key: tuple(values) for key, values in self.channels.items()}),
        )
        if self.distance_m is not None:
            object.__setattr__(self, "distance_m", tuple(self.distance_m))
        count = len(self.session_time_ns)
        if any(len(values) != count for values in self.channels.values()):
            raise TraceRejected("channel_length_mismatch")
        if self.distance_m is not None and len(self.distance_m) != count:
            raise TraceRejected("distance_length_mismatch")
        if any(not isinstance(t, int) or isinstance(t, bool) for t in self.session_time_ns):
            raise TraceRejected("clock_requires_integer_nanoseconds")


@dataclass(frozen=True)
class LapBounds:
    start_ns: int
    end_ns: int

    def __post_init__(self) -> None:
        if self.end_ns <= self.start_ns:
            raise TraceRejected("invalid_lap_bounds")


@dataclass(frozen=True)
class LapMetadata:
    driver: str
    lap_number: int
    revision: str
    lap_time_seconds: float | None = None
    pit_in: bool = False
    pit_out: bool = False
    deleted: bool = False
    provider_generated: bool = False
    reliable: bool = True


@dataclass(frozen=True)
class ClockModel:
    """Session duration, or Date with a caller-verified explicit UTC origin."""

    kind: Literal["session_time", "date"]
    origin_utc_ns: int | None = None

    def __post_init__(self) -> None:
        if self.kind == "date" and self.origin_utc_ns is None:
            raise TraceRejected("date_requires_explicit_utc_origin")
        if self.kind == "session_time" and self.origin_utc_ns is not None:
            raise TraceRejected("session_time_does_not_accept_utc_origin")


@dataclass(frozen=True)
class PrepareSettings:
    allow_partial: bool = False
    allow_pit_laps: bool = False
    allow_deleted: bool = False
    allow_provider_generated: bool = False
    allow_unreliable: bool = False
    max_edge_gap_seconds: float = 2.0


@dataclass(frozen=True)
class AlignmentSettings:
    spacing_m: float = 1.0
    grid: Literal["fixed_spacing", "reference_union"] = "fixed_spacing"
    max_points: int = MAX_GRID_POINTS
    max_gap_seconds: float = 2.0
    max_gap_m: float = 100.0


@dataclass(frozen=True)
class LapTrace:
    metadata: LapMetadata
    bounds: LapBounds
    source: RawTelemetry
    elapsed_seconds: tuple[float, ...]
    distance_m: tuple[float, ...]
    channels: Channels
    flags: tuple[str, ...] = ()
    start_covered: bool = False
    finish_covered: bool = False
    distance_derived: bool = False
    provenance: Literal["DERIVED"] = "DERIVED"


@dataclass(frozen=True)
class AlignedTrace:
    elapsed_seconds: tuple[float | None, ...]
    channels: Channels
    valid_mask: tuple[bool, ...]


@dataclass(frozen=True)
class Comparison:
    trace_a: LapTrace
    trace_b: LapTrace
    distance_m: tuple[float, ...]
    a: AlignedTrace
    b: AlignedTrace
    delta_seconds: tuple[float | None, ...]
    valid_mask: tuple[bool, ...]
    end_delta_seconds: float | None
    observed_lap_delta_seconds: float | None
    finish_line_agreement_applicable: bool
    finish_delta_error_seconds: float | None
    flags: tuple[str, ...]
    settings: AlignmentSettings
    algorithm_version: str = ALIGNMENT_VERSION
    provenance: Literal["DERIVED"] = "DERIVED"
    delta_convention: str = "t_A(d) - t_B(d); negative A ahead, positive B ahead"


@dataclass(frozen=True)
class PositionSamples:
    """Independent position clock; coordinates remain in the provider's units."""

    session_time_ns: tuple[int, ...]
    x: tuple[float | None, ...]
    y: tuple[float | None, ...]
    z: tuple[float | None, ...] | None = None


@dataclass(frozen=True)
class CornerMarker:
    number: int
    letter: str
    distance_m: float
    x: float | None
    y: float | None
    angle: float | None
    source: str

    @property
    def label(self) -> str:
        return f"{self.number}{self.letter}"


@dataclass(frozen=True)
class AnalysisWindow:
    label: str
    start_m: float
    end_m: float
    kind: Literal["PROVIDER_CORNER", "DISTANCE_WINDOW"]
    marker: CornerMarker | None = None


@dataclass(frozen=True)
class WindowMetrics:
    entry_speed_kmh: float | None
    minimum_speed_kmh: float | None
    exit_speed_kmh: float | None
    gear_at_minimum: int | None
    time_seconds: float | None
    brake_onset_m: float | None
    brake_release_m: float | None
    throttle_pickup_m: float | None
    full_throttle_m: float | None
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class WindowComparison:
    window: AnalysisWindow
    a: WindowMetrics
    b: WindowMetrics
    delta_seconds: float | None
    algorithm_version: str = CORNER_VERSION
    provenance: Literal["DERIVED"] = "DERIVED"
    persistence_seconds: float = 0.2
    throttle_pickup_threshold_percent: float = 20
    full_throttle_threshold_percent: float = 98
    brake_label: str = "BRAKE_ACTIVE"


@dataclass(frozen=True)
class Microsector:
    index: int
    start_m: float
    end_m: float
    time_a_seconds: float | None
    time_b_seconds: float | None
    delta_seconds: float | None
    label: str = "DERIVED_DISTANCE_BIN_NOT_FIA_SECTOR"


@dataclass(frozen=True)
class MicrosectorSummary:
    sectors: tuple[Microsector, ...]
    total_delta_seconds: float | None
    sum_error_seconds: float | None
    largest_gain_a_index: int | None
    largest_gain_b_index: int | None
    fastest_a_index: int | None
    fastest_b_index: int | None
    flags: tuple[str, ...] = field(default_factory=tuple)
    slowest_a_index: int | None = None
    slowest_b_index: int | None = None
    accuracy: str = "LINEAR_NATIVE_TIME_DISTANCE_WITH_GAP_MASKS"
