import type { TelemetryLap } from "./types";
export class RequestEpoch {
  private value = 0;
  begin() {
    return ++this.value;
  }
  current(key: number) {
    return key === this.value;
  }
}
export function nearestIndex(distance: number[], target: number): number {
  let lo = 0,
    hi = distance.length - 1;
  while (lo < hi) {
    const mid = Math.floor((lo + hi) / 2);
    if (distance[mid] < target) lo = mid + 1;
    else hi = mid;
  }
  return lo > 0 &&
    Math.abs(distance[lo - 1] - target) < Math.abs(distance[lo] - target)
    ? lo - 1
    : lo;
}
export function presentationIndices(
  values: (number | boolean | null)[],
  budget = 1200,
  categorical = false,
): number[] {
  if (values.length <= budget) return values.map((_, i) => i);
  const keep = new Set([0, values.length - 1]);
  if (categorical) {
    for (let i = 1; i < values.length; i++)
      if (values[i] !== values[i - 1]) {
        keep.add(i - 1);
        keep.add(i);
      }
  } else {
    const stride = Math.max(1, Math.ceil(values.length / (budget / 4)));
    for (let start = 0; start < values.length; start += stride) {
      let min = start,
        max = start;
      for (let i = start; i < Math.min(values.length, start + stride); i++) {
        if (values[i] === null || values[i - 1] === null) keep.add(i);
        if (
          values[i] !== null &&
          (values[min] === null || Number(values[i]) < Number(values[min]))
        )
          min = i;
        if (
          values[i] !== null &&
          (values[max] === null || Number(values[i]) > Number(values[max]))
        )
          max = i;
      }
      keep.add(start);
      keep.add(min);
      keep.add(max);
      keep.add(Math.min(values.length - 1, start + stride - 1));
    }
  }
  return [...keep].sort((a, b) => a - b);
}
export function fastestValidLap(
  laps: Pick<
    TelemetryLap,
    | "lap_number"
    | "lap_time_seconds"
    | "reliable"
    | "deleted"
    | "provider_generated"
    | "pit_in"
    | "pit_out"
    | "car_available"
    | "timing_available"
  >[],
): number | undefined {
  return laps
    .filter(
      (l) =>
        l.reliable &&
        !l.deleted &&
        !l.provider_generated &&
        !l.pit_in &&
        !l.pit_out &&
        l.car_available &&
        l.timing_available &&
        l.lap_time_seconds !== null &&
        Number.isFinite(l.lap_time_seconds) &&
        l.lap_time_seconds > 0,
    )
    .sort((a, b) => a.lap_time_seconds! - b.lap_time_seconds!)[0]?.lap_number;
}
export function numberLabel(
  value: number | boolean | null | undefined,
  digits = 1,
) {
  return value === null || value === undefined
    ? "—"
    : typeof value === "boolean"
      ? value
        ? "ACTIVE"
        : "INACTIVE"
      : value.toFixed(digits);
}
export function signed(value: number | null | undefined) {
  return value === null || value === undefined
    ? "—"
    : `${value >= 0 ? "+" : ""}${value.toFixed(3)} s`;
}
