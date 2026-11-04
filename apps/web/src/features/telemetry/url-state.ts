import {
  TELEMETRY_VIEWS,
  QUALITY_FLAGS,
  type TelemetrySelection,
  type TelemetrySession,
  type TelemetryView,
} from "./types";

export function parseTelemetryUrl(search: string): TelemetrySelection {
  const q = new URLSearchParams(search);
  const result: TelemetrySelection = {
    view: TELEMETRY_VIEWS.includes(q.get("view") as TelemetryView)
      ? (q.get("view") as TelemetryView)
      : "overview",
  };
  const session = q.get("session");
  const refs = ["race_session_id", "replay_session_id"].flatMap((key) =>
    q.getAll(key).map((id) => ({ key, id })),
  );
  if (refs.length || q.has("context_reference_invalid")) {
    const ref = refs[0];
    result.contextReference =
      refs.length === 1 &&
      !q.has("session") &&
      !q.has("context_reference_invalid") &&
      ref.id.length <= 240 &&
      /^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(ref.id)
        ? {
            kind: ref.key === "race_session_id" ? "race" : "replay",
            id: ref.id,
          }
        : { kind: "invalid", id: "" };
  }
  if (
    session &&
    /^[a-z0-9]+(?:[_-][a-z0-9]+)*$/.test(session) &&
    session.length <= 120
  )
    result.session = session;
  const revision = q.get("revision");
  if (revision && /^[0-9a-f]{64}$/.test(revision)) result.revision = revision;
  for (const side of ["A", "B"] as const) {
    const driver =
      q.get(`driver_${side.toLowerCase()}`) ??
      (side === "A" ? q.get("driver") : null);
    if (driver && /^[1-9][0-9]{0,2}$/.test(driver))
      result[`driver${side}`] = driver;
    const lap =
      q.get(`lap_${side.toLowerCase()}`) ??
      (side === "A" ? q.get("lap") : null);
    if (lap && /^\d+$/.test(lap) && Number(lap) >= 1 && Number(lap) <= 1000)
      result[`lap${side}`] = Number(lap);
  }
  const zoom = q.get("zoom")?.split(",").map(Number);
  for (const flag of QUALITY_FLAGS) {
    const values = q.getAll(flag);
    if (values.length)
      result[flag] = values.length === 1 && values[0] === "true";
  }
  if (
    zoom?.length === 2 &&
    zoom.every(Number.isFinite) &&
    zoom[0] >= 0 &&
    zoom[1] > zoom[0] &&
    zoom[1] <= 100000
  )
    result.zoom = [zoom[0], zoom[1]];
  return result;
}
export function telemetryQuery(state: TelemetrySelection): string {
  const q = new URLSearchParams();
  if (state.contextReference) {
    const ref = state.contextReference;
    q.set(
      ref.kind === "invalid"
        ? "context_reference_invalid"
        : `${ref.kind === "race" ? "race" : "replay"}_session_id`,
      ref.kind === "invalid" ? "1" : ref.id,
    );
  }
  for (const [key, value] of Object.entries({
    session: state.session,
    revision: state.revision,
    driver_a: state.driverA,
    lap_a: state.lapA,
    driver_b: state.driverB,
    lap_b: state.lapB,
    view: state.view,
    zoom: state.zoom?.map((v) => Math.round(v)).join(","),
  }))
    if (value !== undefined) q.set(key, String(value));
  for (const flag of QUALITY_FLAGS)
    if (state[flag] !== undefined) q.set(flag, String(state[flag] === true));
  return `?${q}`;
}
export function telemetryLink(session: string, driver?: string, lap?: number) {
  return `/app/telemetry${telemetryQuery({ session, driverA: driver, lapA: lap, view: "overview" })}`;
}
export function telemetryContextLink(
  kind: "race" | "replay",
  id: string,
  driver?: string,
  lap?: number,
) {
  return `/app/telemetry${telemetryQuery({ contextReference: { kind, id }, driverA: driver, lapA: lap, view: "overview" })}`;
}
export function resolveTelemetryContext(
  selection: TelemetrySelection,
  sessions: TelemetrySession[],
): TelemetrySelection | null {
  const ref = selection.contextReference;
  if (!ref) return selection;
  if (ref.kind === "invalid") return null;
  const matches = sessions.filter((session) =>
    ref.kind === "race"
      ? session.session_type?.toLowerCase() === "race" &&
        session.race_analytics_session_id === ref.id
      : session.replay_session_id === ref.id,
  );
  if (matches.length !== 1) return null;
  const current = matches[0];
  const driverA =
    selection.driverA && current.drivers.includes(selection.driverA)
      ? selection.driverA
      : current.drivers[0];
  const driverB =
    selection.driverB && current.drivers.includes(selection.driverB)
      ? selection.driverB
      : (current.drivers[1] ?? current.drivers[0]);
  return {
    ...selection,
    contextReference: undefined,
    validateContextLaps: true,
    session: current.session_id,
    revision: current.revision,
    zoom: undefined,
    driverA,
    driverB,
    lapA: driverA === selection.driverA ? selection.lapA : undefined,
    lapB: driverB === selection.driverB ? selection.lapB : undefined,
  };
}
