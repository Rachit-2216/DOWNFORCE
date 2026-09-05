export type JsonScalar = null | boolean | number | string;
export type JsonValue = JsonScalar | JsonValue[] | { [key: string]: JsonValue };

export type Page = {
  offset: number;
  limit: number;
  total: number;
};

export type SessionSummary = {
  session_id: string;
  dataset_id: string;
  season: number;
  event_name: string;
  session_type: string;
  provider: string;
  created_at_utc: string;
};

export type SessionListResponse = Page & { items: SessionSummary[] };

export type RaceDataCapabilities = {
  results: boolean;
  grid: boolean;
  lap_times: boolean;
  lap_positions: boolean;
  pit_stops: boolean;
  stints: boolean;
  compounds: boolean;
  weather: boolean;
  race_control: boolean;
  track_positions: boolean;
  telemetry: boolean;
  speed: boolean;
  throttle: boolean;
  brake: boolean;
  gear: boolean;
  rpm: boolean;
  drs: boolean;
  ml_intelligence: boolean;
  strategy_simulation: boolean;
  counterfactual_support: boolean;
};

export type ArchiveQuality = {
  status: "verified" | "good" | "partial" | "degraded" | "unusable";
  reasons: string[];
  metrics: Record<string, number | string | boolean | null>;
  validated_at_utc: string;
};

export type ArchiveProvenance = {
  provider: string;
  provider_version: string;
  source: string;
  source_url: string;
  retrieved_at_utc: string;
  raw_sha256: string;
};

export type ArchiveSession = {
  session_id: string;
  session_type: string;
  status: "completed" | "upcoming" | "cancelled" | "unavailable";
  sync_status: string;
  capability_tier:
    | "archive"
    | "lap_data"
    | "lap_and_pit"
    | "detailed_timing"
    | "telemetry"
    | "full_downforce";
  capabilities: RaceDataCapabilities;
  quality: ArchiveQuality;
  provenance: ArchiveProvenance[];
  row_counts: Record<string, number>;
  data_revision: string | null;
  legacy_session_id: string | null;
};

export type CatalogEvent = {
  event_id: string;
  season: number;
  round_number: number;
  name: string;
  official_name: string;
  event_date: string;
  circuit_name: string;
  locality: string | null;
  country: string | null;
  country_code: string | null;
  status: "completed" | "upcoming" | "cancelled" | "unavailable";
  sessions: ArchiveSession[];
  drivers: string[];
  teams: string[];
};

export type CatalogEventListResponse = Page & { items: CatalogEvent[] };

export type CatalogSeason = {
  year: number;
  event_count: number;
  completed_event_count: number;
  latest_event_date: string | null;
};

export type CatalogSeasonListResponse = {
  items: CatalogSeason[];
  total: number;
  event_count: number;
  completed_event_count: number;
  archive_start_year: number;
  latest_completed_event_id: string | null;
  latest_completed_event_date: string | null;
};

export type ArchiveResult = {
  session_id: string;
  driver_id: string;
  driver_code: string | null;
  driver_name: string;
  team_id: string | null;
  team_name: string | null;
  car_number: number | null;
  grid_position: number | null;
  finish_position: number | null;
  points: number | null;
  laps_completed: number | null;
  status: string | null;
  classified: boolean | null;
  total_time_ms: number | null;
};

export type ArchiveLap = {
  session_id: string;
  driver_id: string;
  lap_number: number;
  position: number | null;
  lap_time_ms: number | null;
  average_speed_kph: number | null;
  is_fastest_lap: boolean | null;
};

export type ArchivePitStop = {
  session_id: string;
  driver_id: string;
  stop_number: number;
  lap_number: number | null;
  duration_ms: number | null;
  local_time: string | null;
};

export type ArchivePage<T> = Page & { items: T[] };

export type TableSummary = {
  availability: string;
  materialized: boolean;
  row_count: number;
  min_session_time_ms: number | null;
  max_session_time_ms: number | null;
};

export type SessionMetadata = {
  circuit_name?: string;
  country_code?: string;
  data_quality?: string;
  event_name?: string;
  round_number?: number;
  scheduled_start_utc?: string;
  season?: number;
  session_name?: string;
  session_origin_utc?: string;
  session_start_utc?: string;
  session_type?: string;
  [key: string]: JsonValue | undefined;
};

export type SessionResponse = {
  session_id: string;
  dataset_id: string;
  snapshot_id: string;
  session: SessionMetadata;
  provider: { [key: string]: JsonValue };
  capabilities: { [key: string]: JsonValue };
  completeness: { [key: string]: JsonValue };
  tables: Record<string, TableSummary>;
  warnings: string[];
  canonical_schema_version: string;
  normalization_version: string;
  timeline_version: string | null;
  replay_version: string | null;
};

export type Driver = {
  driver_id: string;
  racing_number: number | null;
  abbreviation: string | null;
  full_name: string | null;
  team_name: string | null;
  country_code: string | null;
};

export type DriverListResponse = { items: Driver[]; total: number };

export type Lap = {
  driver_id: string;
  lap_number: number;
  lap_start_time_ms: number | null;
  lap_end_time_ms: number | null;
  lap_time_ms: number | null;
  sector_1_time_ms: number | null;
  sector_2_time_ms: number | null;
  sector_3_time_ms: number | null;
  stint_number: number | null;
  compound: string;
  raw_compound: string | null;
  tyre_life_laps: number | null;
  is_personal_best: boolean | null;
  is_accurate: boolean | null;
  is_generated: boolean | null;
  is_deleted: boolean | null;
  deleted_reason: string | null;
  raw_track_status: string | null;
};

export type LapListResponse = Page & { items: Lap[] };

export type TrackPosition = {
  driver_id: string;
  session_time_ms: number;
  x_m: number;
  y_m: number;
  z_m: number | null;
  raw_status: string | null;
};

export type TrackPositionListResponse = Page & { items: TrackPosition[] };

export type RaceEventType =
  | "session-marker"
  | "track-status-changed"
  | "weather-observed"
  | "race-control-event"
  | "driver-stint-changed"
  | "driver-pit-entered"
  | "driver-position-changed"
  | "driver-lap-completed"
  | "driver-pit-exited"
  | "driver-status-changed";

export type TimelineEvent = {
  event_id: string;
  session_time_ms: number;
  priority: number;
  sequence: number;
  event_type: string;
  driver_id: string | null;
  source: string;
  source_key: string | null;
  payload: { [key: string]: JsonValue };
};

export type TimelineResponse = Page & { items: TimelineEvent[] };

export type WeatherState = {
  observed_at_ms: number;
  air_temperature_c: number | null;
  track_temperature_c: number | null;
  humidity_percent: number | null;
  pressure_hpa: number | null;
  rainfall: boolean | null;
  wind_speed_mps: number | null;
  wind_direction_deg: number | null;
};

export type DriverState = {
  driver_id: string;
  racing_number: number | null;
  abbreviation: string | null;
  full_name: string | null;
  team_name: string | null;
  status: string;
  position: number | null;
  laps_completed: number;
  current_stint: number | null;
  compound: string;
  tyre_age_laps: number | null;
  last_lap_time_ms: number | null;
  in_pit: boolean;
  pit_stop_count: number;
  last_pit_lap: number | null;
};

export type RaceControlState = {
  observed_at_ms: number;
  message: string;
  category: string | null;
  scope: string | null;
  driver_id: string | null;
};

export type RaceState = {
  replay_version: string;
  session_id: string;
  session_time_ms: number;
  reference_lap: number | null;
  track_status: string;
  weather: WeatherState | null;
  drivers: DriverState[];
  recent_race_control: RaceControlState[];
  completeness: Record<string, string>;
  data_quality: string;
};

export type IntelligenceInterval = {
  lower_ms: number;
  upper_ms: number;
};

export type IntelligenceResponse = {
  availability: "available" | "unavailable";
  reason: string | null;
  model_version: string | null;
  dataset_digest: string | null;
  assumptions: string[];
  as_of: { time_ms: number; lap: number | null };
  pace: {
    label: string;
    predicted_lap_time_ms: number;
    observed_latest_lap_time_ms: number;
    interval_80: IntelligenceInterval;
    interval_90: IntelligenceInterval;
  } | null;
  tyre_degradation: {
    label: string;
    compound: string;
    current_tyre_age_laps: number;
    predicted_residual_ms: number;
    interval_80_half_width_ms: number;
    interval_90_half_width_ms: number;
    curve: {
      laps_ahead: number;
      tyre_age_laps: number;
      predicted_pace_delta_ms: number;
    }[];
  } | null;
  pit_loss: {
    label: string;
    circuit: string;
    estimated_effective_loss_ms: number;
    interval_80: IntelligenceInterval;
    interval_90: IntelligenceInterval;
    stationary_duration_ms: null;
    stationary_duration_reason: string;
  } | null;
};

export type StrategyAction = {
  type: "pit";
  lap: number;
  compound: "soft" | "medium" | "hard";
};

export type StrategyCandidate = {
  strategy_id: string;
  label: string;
  actions: StrategyAction[];
};

export type StrategyOutcome = {
  expected_position: number;
  median_position: number;
  position_probabilities: Record<string, number>;
  probability_top_3: number;
  race_time_ms: { p10: number; median: number; p90: number };
  local_tyre_horizon_exceeded_probability: number;
};

export type StrategyComparisonResponse = {
  status: "available" | "unavailable";
  availability_reason: string | null;
  session_id: string;
  driver_id: string;
  cursor: { time_ms: number; lap: number | null };
  simulation_count?: number;
  seed?: number;
  model_version: string | null;
  dataset_digest: string | null;
  simulation_version: string;
  assumptions: string[];
  common_random_numbers?: boolean;
  strategies?: { strategy: StrategyCandidate; outcome: StrategyOutcome }[];
  ranking?: {
    status: "PREFERRED UNDER CURRENT ASSUMPTIONS" | "NO CLEAR PREFERENCE";
    recommended_strategy_id: string | null;
    leading_strategy_id: string;
    probability_leading_beats_runner_up: number;
    recommendation_threshold: number;
    pit_loss_sensitive: boolean;
    long_horizon_limited: boolean;
    input_data_limited: boolean;
    background_strategy_assumption: "no_unannounced_future_stops";
    guard_reasons: string[];
    explanation: string;
  };
  pit_loss_sensitivity?: Record<string, string>;
  outcome: StrategyOutcome | null;
};

export type AnalyticsCoverage = {
  sample_count: number;
  race_count: number;
  eligible_race_count: number;
  missing_count: number;
  verified_count: number;
  good_count: number;
  quality_exclusions: number;
  analytics_version: string;
  archive_source_revision: string;
  ratio: number | null;
};

export type AnalyticsSummary = {
  starts: number;
  finishes: number;
  wins: number;
  podiums: number;
  points: number;
  dnf: number;
  dns: number;
  dsq: number;
  average_grid: number | null;
  average_grid_samples: number;
  average_finish: number | null;
  average_finish_samples: number;
  positions_gained: number;
  positions_gained_samples: number;
  pit_stops: number | null;
  pit_coverage_races: number;
  [key: string]: JsonValue;
};

export type AnalyticsListItem = AnalyticsSummary & {
  entity_id: string;
  entity_name: string;
  start_season: number;
  end_season: number;
  [key: string]: JsonValue;
};

export type AnalyticsPageResponse = Page & {
  items: AnalyticsListItem[];
  coverage: AnalyticsCoverage;
  analytics_version: string;
  archive_source_revision: string;
};

export type AnalyticsPointsProgression = {
  entity_id: string;
  entity_name: string;
  points: { round_number: number; value: number }[];
};

export type SeasonAnalyticsResponse = {
  season: number;
  summary: Record<string, JsonValue>;
  competitiveness: Record<string, JsonValue>;
  drivers: Record<string, JsonValue>[];
  constructors: Record<string, JsonValue>[];
  races: Record<string, JsonValue>[];
  driver_points_progression: AnalyticsPointsProgression[];
  constructor_points_progression: AnalyticsPointsProgression[];
  coverage: Record<string, AnalyticsCoverage>;
  analytics_version: string;
  archive_source_revision: string;
};

export type EntityAnalyticsResponse = {
  entity: Record<string, JsonValue>;
  summary: Record<string, JsonValue>;
  seasons?: Record<string, JsonValue>[] | null;
  races: Page & { items: Record<string, JsonValue>[] };
  drivers?: Record<string, JsonValue>[] | null;
  constructors?: Record<string, JsonValue>[] | null;
  circuits?: Record<string, JsonValue>[] | null;
  finish_distribution?: Record<string, number> | null;
  pit_trend?: Record<string, JsonValue>[] | null;
  coverage: Record<string, AnalyticsCoverage>;
  analytics_version: string;
  archive_source_revision: string;
};

export type RaceAnalyticsResponse = {
  event: Record<string, JsonValue>;
  summary: Record<string, JsonValue>;
  drivers: Record<string, JsonValue>[];
  biggest_movers: Record<string, JsonValue>[];
  position_progression: Record<string, JsonValue>[];
  coverage: Record<string, AnalyticsCoverage>;
  analytics_version: string;
  archive_source_revision: string;
};

export type ComparisonAnalyticsResponse = {
  entity_type: "driver" | "constructor";
  mode: "common_races" | "all_selected_races";
  filters: Record<string, JsonValue>;
  entity_a: Record<string, JsonValue>;
  entity_b: Record<string, JsonValue>;
  common_race_count: number;
  head_to_head: Record<string, JsonValue>;
  coverage: AnalyticsCoverage;
  analytics_version: string;
  archive_source_revision: string;
};

export type RankingAnalyticsResponse = Page & {
  entity_type: "driver" | "constructor";
  metric: string;
  minimum_starts: number;
  items: Record<string, JsonValue>[];
  coverage: AnalyticsCoverage;
  analytics_version: string;
  archive_source_revision: string;
};
