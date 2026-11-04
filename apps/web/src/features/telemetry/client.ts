import {
  ApiClientError,
  resolveApiBaseUrl,
  type ApiRequestOptions,
} from "@/lib/api/client";
import type {
  Comparison,
  Corners,
  LapPage,
  Microsectors,
  PreparationSettings,
  SessionPage,
  TelemetrySelection,
  Trace,
} from "./types";

type RecordValue = Record<string, unknown>;
const object = (v: unknown): v is RecordValue =>
  typeof v === "object" && v !== null && !Array.isArray(v);
const finite = (v: unknown): v is number =>
  typeof v === "number" && Number.isFinite(v);
const text = (v: unknown): v is string =>
  typeof v === "string" && v.length <= 10000;
const texts = (v: unknown): boolean =>
  Array.isArray(v) && v.length <= 1000 && v.every(text);
const nullableNumber = (v: unknown) => v === null || finite(v);
const nullableText = (v: unknown) => v === null || text(v);
const optionalContextId = (v: unknown, pattern: RegExp) =>
  v === undefined ||
  v === null ||
  (typeof v === "string" && v.length <= 240 && pattern.test(v));
const driverNumber = (v: unknown): v is string =>
  typeof v === "string" && /^(0|[1-9][0-9]{0,2})$/.test(v);
function contextMetadata(v: RecordValue): boolean {
  return (
    optionalContextId(
      v.replay_session_id,
      /^session-\d{4}-(?:round-\d{2}|event-[a-z0-9]+(?:-[a-z0-9]+)*)-type-[a-z0-9]+(?:-[a-z0-9]+)*$/,
    ) &&
    optionalContextId(
      v.race_analytics_session_id,
      /^archive-\d{4}-round-\d{2}-race$/,
    ) &&
    ["replay_unavailable_reason", "race_analytics_unavailable_reason"].every(
      (key) => v[key] === undefined || nullableText(v[key]),
    ) &&
    (v.replay_driver_numbers === undefined ||
      (Array.isArray(v.replay_driver_numbers) &&
        v.replay_driver_numbers.length <= 100 &&
        v.replay_driver_numbers.every(driverNumber))) &&
    (v.replay_laps_by_driver === undefined ||
      (object(v.replay_laps_by_driver) &&
        Object.keys(v.replay_laps_by_driver).length <= 100 &&
        Object.entries(v.replay_laps_by_driver).every(
          ([driver, laps]) =>
            driverNumber(driver) &&
            Array.isArray(laps) &&
            laps.length <= 1000 &&
            laps.every(
              (lap) => Number.isInteger(lap) && lap >= 1 && lap <= 1000,
            ),
        )))
  );
}
const revision = (v: unknown) =>
  typeof v === "string" && /^[0-9a-f]{64}$/.test(v);
const stringMap = (v: unknown) =>
  object(v) && Object.keys(v).length <= 100 && Object.values(v).every(text);
const bools = (v: unknown, n: number) =>
  Array.isArray(v) && v.length === n && v.every((x) => typeof x === "boolean");
const nums = (v: unknown, n?: number): v is number[] =>
  Array.isArray(v) &&
  v.length <= 20000 &&
  (n === undefined || v.length === n) &&
  v.every(finite);
const nullableNums = (v: unknown, n: number) =>
  Array.isArray(v) && v.length === n && v.every(nullableNumber);
const masks = (v: unknown, n: number, channelMap: unknown) =>
  object(v) &&
  object(channelMap) &&
  Object.keys(v).length <= 20 &&
  Object.keys(v).length === Object.keys(channelMap).length &&
  Object.keys(channelMap).every((key) => Object.hasOwn(v, key)) &&
  Object.values(v).every((x) => bools(x, n));
const channels = (v: unknown, n: number) =>
  object(v) &&
  Object.keys(v).length <= 20 &&
  Object.values(v).every(
    (x) =>
      Array.isArray(x) &&
      x.length === n &&
      x.every(
        (item) => item === null || typeof item === "boolean" || finite(item),
      ),
  );
const identity = (v: RecordValue) => text(v.session_id) && revision(v.revision);
function page(v: unknown): v is RecordValue & { items: unknown[] } {
  return (
    object(v) &&
    Array.isArray(v.items) &&
    v.items.length <= 100 &&
    finite(v.total) &&
    Number.isInteger(v.total) &&
    v.total >= 0 &&
    finite(v.offset) &&
    v.offset >= 0 &&
    finite(v.limit) &&
    v.limit >= 1 &&
    v.limit <= 100
  );
}
function lap(v: unknown): boolean {
  return (
    object(v) &&
    text(v.driver) &&
    finite(v.lap_number) &&
    Number.isInteger(v.lap_number) &&
    v.lap_number >= 1 &&
    v.lap_number <= 1000 &&
    [
      "lap_time_seconds",
      "sector_1_seconds",
      "sector_2_seconds",
      "sector_3_seconds",
      "stint",
      "tyre_life",
    ].every((k) => nullableNumber(v[k])) &&
    ["compound", "track_status", "qualifying_phase"].every((k) =>
      nullableText(v[k]),
    ) &&
    [
      "reliable",
      "deleted",
      "provider_generated",
      "pit_in",
      "pit_out",
      "timing_available",
      "car_available",
      "position_available",
      "weather_available",
    ].every((k) => typeof v[k] === "boolean")
  );
}
function isSessionPage(v: unknown): v is SessionPage {
  return (
    page(v) &&
    v.source === "production-canonical" &&
    typeof v.root_configured === "boolean" &&
    v.items.every(
      (s) =>
        object(s) &&
        identity(s) &&
        contextMetadata(s) &&
        nullableNumber(s.season) &&
        ["session_type", "event", "circuit"].every((k) => nullableText(s[k])) &&
        texts(s.drivers) &&
        texts(s.available_tables) &&
        stringMap(s.channel_availability),
    )
  );
}
function isLapPage(v: unknown): v is LapPage {
  return page(v) && identity(v) && text(v.driver) && v.items.every(lap);
}
function isTrace(v: unknown): v is Trace {
  if (
    !object(v) ||
    !identity(v) ||
    !nums(v.distance_m) ||
    v.distance_m.length < 2
  )
    return false;
  const n = v.distance_m.length,
    raw = v.source_samples;
  return (
    text(v.driver) &&
    finite(v.lap_number) &&
    v.provenance === "DERIVED" &&
    nums(v.elapsed_seconds, n) &&
    channels(v.channels, n) &&
    masks(v.channel_valid_masks, n, v.channels) &&
    object(raw) &&
    raw.provenance === "PROVIDER_NATIVE" &&
    nums(raw.session_time_ns) &&
    channels(raw.channels, raw.session_time_ns.length) &&
    masks(raw.channel_valid_masks, raw.session_time_ns.length, raw.channels) &&
    Array.isArray(raw.source_sensors) &&
    raw.source_sensors.length === raw.session_time_ns.length &&
    raw.source_sensors.every(nullableText) &&
    stringMap(v.channel_availability) &&
    lap(v.context) &&
    texts(v.flags) &&
    typeof v.start_covered === "boolean" &&
    typeof v.finish_covered === "boolean" &&
    typeof v.distance_derived === "boolean" &&
    text(v.clock_source) &&
    v.brake_label === "BRAKE_ACTIVE" &&
    v.drs_interpretation === "UNKNOWN_PROVIDER_CODE" &&
    text(v.position_provenance)
  );
}
function aligned(v: unknown, n: number) {
  return (
    object(v) &&
    text(v.driver) &&
    finite(v.lap_number) &&
    nullableNums(v.elapsed_seconds, n) &&
    channels(v.channels, n) &&
    bools(v.valid_mask, n) &&
    masks(v.channel_valid_masks, n, v.channels)
  );
}
function isComparison(v: unknown): v is Comparison {
  if (
    !object(v) ||
    !identity(v) ||
    !nums(v.distance_m) ||
    v.distance_m.length < 2
  )
    return false;
  const n = v.distance_m.length;
  return (
    v.provenance === "DERIVED" &&
    text(v.algorithm_version) &&
    text(v.corner_version) &&
    object(v.settings) &&
    aligned(v.a, n) &&
    aligned(v.b, n) &&
    nullableNums(v.delta_seconds, n) &&
    bools(v.valid_mask, n) &&
    nullableNumber(v.end_delta_seconds) &&
    nullableNumber(v.observed_lap_delta_seconds) &&
    typeof v.finish_line_agreement_applicable === "boolean" &&
    nullableNumber(v.finish_delta_error_seconds) &&
    text(v.delta_convention) &&
    texts(v.flags) &&
    stringMap(v.availability_a) &&
    stringMap(v.availability_b) &&
    v.distance_m.every((x, i, a) => i === 0 || x > a[i - 1])
  );
}
function metrics(v: unknown) {
  return (
    object(v) &&
    [
      "entry_speed_kmh",
      "minimum_speed_kmh",
      "exit_speed_kmh",
      "gear_at_minimum",
      "time_seconds",
      "brake_onset_m",
      "brake_release_m",
      "throttle_pickup_m",
      "full_throttle_m",
    ].every((k) => nullableNumber(v[k])) &&
    texts(v.flags)
  );
}
function isCorners(v: unknown): v is Corners {
  return (
    object(v) &&
    identity(v) &&
    text(v.algorithm_version) &&
    typeof v.markers_available === "boolean" &&
    text(v.claim) &&
    Array.isArray(v.items) &&
    v.items.length <= 1000 &&
    v.items.every(
      (item) =>
        object(item) &&
        object(item.window) &&
        text(item.window.label) &&
        finite(item.window.start_m) &&
        finite(item.window.end_m) &&
        ["PROVIDER_CORNER", "DISTANCE_WINDOW"].includes(
          String(item.window.kind),
        ) &&
        (item.window.marker === null ||
          (object(item.window.marker) &&
            finite(item.window.marker.number) &&
            text(item.window.marker.letter) &&
            finite(item.window.marker.distance_m) &&
            nullableNumber(item.window.marker.x) &&
            nullableNumber(item.window.marker.y))) &&
        metrics(item.a) &&
        metrics(item.b) &&
        nullableNumber(item.delta_seconds) &&
        item.provenance === "DERIVED" &&
        text(item.algorithm_version) &&
        finite(item.persistence_seconds) &&
        text(item.brake_label),
    )
  );
}
function isMicrosectors(v: unknown): v is Microsectors {
  return (
    object(v) &&
    identity(v) &&
    Array.isArray(v.sectors) &&
    v.sectors.length <= 1000 &&
    v.sectors.every(
      (s) =>
        object(s) &&
        finite(s.index) &&
        finite(s.start_m) &&
        finite(s.end_m) &&
        nullableNumber(s.time_a_seconds) &&
        nullableNumber(s.time_b_seconds) &&
        nullableNumber(s.delta_seconds) &&
        text(s.label),
    ) &&
    nullableNumber(v.total_delta_seconds) &&
    nullableNumber(v.sum_error_seconds) &&
    texts(v.flags) &&
    text(v.accuracy)
  );
}
async function request<T>(
  path: string,
  guard: (v: unknown) => v is T,
  options: ApiRequestOptions,
): Promise<T> {
  let response: Response;
  try {
    response = await (options.fetcher ?? fetch)(
      `${options.baseUrl ?? resolveApiBaseUrl()}${path}`,
      { headers: { Accept: "application/json" }, signal: options.signal },
    );
  } catch (e) {
    if (
      options.signal?.aborted ||
      (e instanceof DOMException && e.name === "AbortError")
    )
      throw e;
    throw new ApiClientError(
      "Unable to reach the telemetry API. Check the backend and API origin.",
      undefined,
      { cause: e },
    );
  }
  if (Number(response.headers.get("Content-Length")) > 32000000)
    throw new ApiClientError(
      "Telemetry payload exceeds the bounded response limit.",
    );
  const raw = await response.text();
  if (raw.length > 32000000)
    throw new ApiClientError(
      "Telemetry payload exceeds the bounded response limit.",
    );
  let payload: unknown;
  try {
    payload = JSON.parse(raw);
  } catch {
    throw new ApiClientError(
      `Telemetry API returned invalid JSON (HTTP ${response.status}).`,
      response.status,
    );
  }
  if (!response.ok) {
    const detail =
      object(payload) && object(payload.error) && text(payload.error.message)
        ? payload.error.message
        : object(payload) && text(payload.detail)
          ? payload.detail
          : `HTTP ${response.status}`;
    throw new ApiClientError(detail, response.status);
  }
  if (!guard(payload))
    throw new ApiClientError(
      "Telemetry API returned an invalid or unbounded response.",
      response.status,
    );
  return payload;
}
function query(values: Record<string, string | number | boolean | undefined>) {
  const q = new URLSearchParams();
  Object.entries(values).forEach(([k, v]) => {
    if (v !== undefined) q.set(k, String(v));
  });
  return `?${q}`;
}
const path = (session: string) =>
  `/api/v1/telemetry/sessions/${encodeURIComponent(session)}`;
export function getTelemetrySessions(
  filters: {
    season?: number;
    event?: string;
    session_type?: string;
    offset?: number;
  } = {},
  options: ApiRequestOptions = {},
) {
  return request(
    `/api/v1/telemetry/sessions${query({ ...filters, limit: 100 })}`,
    isSessionPage,
    options,
  );
}
export function getTelemetryLaps(
  session: string,
  driver: string,
  revision: string,
  offset = 0,
  options: ApiRequestOptions = {},
) {
  return request(
    `${path(session)}/laps${query({ driver, revision, offset, limit: 100 })}`,
    isLapPage,
    options,
  );
}
function pairQuery(s: TelemetrySelection, settings: PreparationSettings) {
  if (
    !s.session ||
    !s.revision ||
    !s.driverA ||
    !s.driverB ||
    !s.lapA ||
    !s.lapB
  )
    throw new ApiClientError(
      "Select a session revision and two observed laps.",
    );
  return query({
    driver_a: s.driverA,
    lap_a: s.lapA,
    driver_b: s.driverB,
    lap_b: s.lapB,
    revision: s.revision,
    max_points: 20000,
    spacing_m: 2,
    grid: "fixed_spacing",
    max_gap_seconds: 2,
    max_gap_m: 100,
    ...settings,
  });
}
export function getTelemetryComparison(
  s: TelemetrySelection,
  settings: PreparationSettings = {},
  options: ApiRequestOptions = {},
) {
  return request(
    `${path(s.session ?? "")}/compare${pairQuery(s, settings)}`,
    isComparison,
    options,
  );
}
export function getTelemetryTrace(
  s: TelemetrySelection,
  side: "A" | "B",
  settings: PreparationSettings = {},
  options: ApiRequestOptions = {},
) {
  return request(
    `${path(s.session ?? "")}/trace${query({ driver: s[`driver${side}`], lap: s[`lap${side}`], revision: s.revision, max_points: 20000, ...settings })}`,
    isTrace,
    options,
  );
}
export function getTelemetryCorners(
  s: TelemetrySelection,
  settings: PreparationSettings = {},
  options: ApiRequestOptions = {},
) {
  return request(
    `${path(s.session ?? "")}/corners${pairQuery(s, settings)}`,
    isCorners,
    options,
  );
}
export function getTelemetryMicrosectors(
  s: TelemetrySelection,
  settings: PreparationSettings = {},
  options: ApiRequestOptions = {},
) {
  return request(
    `${path(s.session ?? "")}/microsectors${pairQuery(s, settings)}&count=20`,
    isMicrosectors,
    options,
  );
}
