export const TELEMETRY_VIEWS = [
  "overview",
  "performance",
  "inputs",
  "track",
  "corners",
  "details",
] as const;
export type TelemetryView = (typeof TELEMETRY_VIEWS)[number];
export const QUALITY_FLAGS = [
  "allow_partial",
  "allow_pit_laps",
  "allow_deleted",
  "allow_provider_generated",
  "allow_unreliable",
] as const;
export type TelemetrySelection = PreparationSettings & {
  contextReference?: { kind: "race" | "replay" | "invalid"; id: string };
  validateContextLaps?: boolean;
  session?: string;
  revision?: string;
  driverA?: string;
  driverB?: string;
  lapA?: number;
  lapB?: number;
  view: TelemetryView;
  zoom?: [number, number];
};
export type ChannelValue = number | boolean | null;
export type Channels = Record<string, ChannelValue[]>;
export type TelemetrySession = {
  session_id: string;
  revision: string;
  season: number | null;
  session_type: string | null;
  event: string | null;
  circuit: string | null;
  drivers: string[];
  available_tables: string[];
  channel_availability: Record<string, string>;
  replay_session_id?: string | null;
  race_analytics_session_id?: string | null;
  replay_driver_numbers?: string[];
  replay_laps_by_driver?: Record<string, number[]>;
  replay_unavailable_reason?: string | null;
  race_analytics_unavailable_reason?: string | null;
};
export type SessionPage = {
  items: TelemetrySession[];
  total: number;
  offset: number;
  limit: number;
  source: "production-canonical";
  root_configured: boolean;
};
export type TelemetryLap = {
  driver: string;
  lap_number: number;
  lap_time_seconds: number | null;
  sector_1_seconds: number | null;
  sector_2_seconds: number | null;
  sector_3_seconds: number | null;
  compound: string | null;
  stint: number | null;
  tyre_life: number | null;
  track_status: string | null;
  reliable: boolean;
  deleted: boolean;
  provider_generated: boolean;
  pit_in: boolean;
  pit_out: boolean;
  timing_available: boolean;
  car_available: boolean;
  position_available: boolean;
  weather_available: boolean;
  qualifying_phase: string | null;
};
export type LapPage = {
  session_id: string;
  revision: string;
  driver: string;
  items: TelemetryLap[];
  total: number;
  offset: number;
  limit: number;
};
export type NativeSamples = {
  provenance: "PROVIDER_NATIVE";
  session_time_ns: number[];
  channels: Channels;
  channel_valid_masks: Record<string, boolean[]>;
  source_sensors: (string | null)[];
};
export type Trace = {
  session_id: string;
  revision: string;
  driver: string;
  lap_number: number;
  provenance: "DERIVED";
  distance_m: number[];
  elapsed_seconds: number[];
  channels: Channels;
  channel_valid_masks: Record<string, boolean[]>;
  source_samples: NativeSamples;
  channel_availability: Record<string, string>;
  context: TelemetryLap;
  flags: string[];
  start_covered: boolean;
  finish_covered: boolean;
  distance_derived: boolean;
  clock_source: string;
  brake_label: "BRAKE_ACTIVE";
  drs_interpretation: "UNKNOWN_PROVIDER_CODE";
  position_provenance: string;
};
export type AlignedTrace = {
  driver: string;
  lap_number: number;
  elapsed_seconds: (number | null)[];
  channels: Channels;
  valid_mask: boolean[];
  channel_valid_masks: Record<string, boolean[]>;
};
export type PreparationSettings = {
  allow_partial?: boolean;
  allow_pit_laps?: boolean;
  allow_deleted?: boolean;
  allow_provider_generated?: boolean;
  allow_unreliable?: boolean;
};
export type Comparison = {
  session_id: string;
  revision: string;
  provenance: "DERIVED";
  algorithm_version: string;
  corner_version: string;
  settings: Record<string, unknown>;
  distance_m: number[];
  a: AlignedTrace;
  b: AlignedTrace;
  delta_seconds: (number | null)[];
  valid_mask: boolean[];
  end_delta_seconds: number | null;
  observed_lap_delta_seconds: number | null;
  finish_line_agreement_applicable: boolean;
  finish_delta_error_seconds: number | null;
  delta_convention: string;
  flags: string[];
  availability_a: Record<string, string>;
  availability_b: Record<string, string>;
};
export type CornerMarker = {
  number: number;
  letter: string;
  distance_m: number;
  x: number | null;
  y: number | null;
  angle: number | null;
  source: string;
};
export type AnalysisWindow = {
  label: string;
  start_m: number;
  end_m: number;
  kind: "PROVIDER_CORNER" | "DISTANCE_WINDOW";
  marker: CornerMarker | null;
};
export type WindowMetrics = {
  entry_speed_kmh: number | null;
  minimum_speed_kmh: number | null;
  exit_speed_kmh: number | null;
  gear_at_minimum: number | null;
  time_seconds: number | null;
  brake_onset_m: number | null;
  brake_release_m: number | null;
  throttle_pickup_m: number | null;
  full_throttle_m: number | null;
  flags: string[];
};
export type CornerComparison = {
  window: AnalysisWindow;
  a: WindowMetrics;
  b: WindowMetrics;
  delta_seconds: number | null;
  algorithm_version: string;
  provenance: "DERIVED";
  persistence_seconds: number;
  brake_label: string;
};
export type Corners = {
  session_id: string;
  revision: string;
  algorithm_version: string;
  items: CornerComparison[];
  markers_available: boolean;
  claim: string;
};
export type Microsector = {
  index: number;
  start_m: number;
  end_m: number;
  time_a_seconds: number | null;
  time_b_seconds: number | null;
  delta_seconds: number | null;
  label: string;
};
export type Microsectors = {
  session_id: string;
  revision: string;
  sectors: Microsector[];
  total_delta_seconds: number | null;
  sum_error_seconds: number | null;
  flags: string[];
  accuracy: string;
};
export type TelemetryBundle = {
  comparison: Comparison;
  traceA: Trace;
  traceB: Trace;
  corners: Corners | null;
  microsectors: Microsectors | null;
  supplementalErrors: string[];
};
