"use client";
import type { Load } from "./useTelemetryData";
import type {
  TelemetryLap,
  TelemetrySelection,
  TelemetrySession,
} from "./types";
import { numberLabel } from "./telemetry-utils";
export function TelemetrySelectors({
  selection: s,
  sessions,
  laps,
  update,
}: {
  selection: TelemetrySelection;
  sessions: TelemetrySession[];
  laps: Load<{ a: TelemetryLap[]; b: TelemetryLap[] }>;
  update: (s: TelemetrySelection) => void;
}) {
  const current = sessions.find((x) => x.session_id === s.session);
  function choose(session: TelemetrySession | undefined) {
    if (session)
      update({
        session: session.session_id,
        revision: session.revision,
        driverA: session.drivers[0],
        driverB: session.drivers[1] ?? session.drivers[0],
        view: s.view,
      });
  }
  const seasons = [
    ...new Set(sessions.map((x) => x.season).filter((x) => x !== null)),
  ].sort((a, b) => b - a);
  const seasonSessions = sessions.filter((x) => x.season === current?.season);
  const events = [
    ...new Set(seasonSessions.map((x) => x.event).filter((x) => x !== null)),
  ];
  const eventSessions = seasonSessions.filter(
    (x) => x.event === current?.event,
  );
  const types = [
    ...new Set(
      eventSessions.map((x) => x.session_type).filter((x) => x !== null),
    ),
  ];
  return (
    <section
      className="tl-selectors"
      aria-label="Telemetry comparison selection"
    >
      <div className="tl-session-controls">
        <label>
          Season
          <select
            value={current?.season ?? ""}
            onChange={(e) =>
              choose(sessions.find((x) => x.season === Number(e.target.value)))
            }
          >
            <option value="" disabled>
              Select season
            </option>
            {seasons.map((y) => (
              <option key={y} value={y}>
                {y}
              </option>
            ))}
          </select>
        </label>
        <label>
          Event
          <select
            value={current?.event ?? ""}
            onChange={(e) =>
              choose(seasonSessions.find((x) => x.event === e.target.value))
            }
          >
            <option value="" disabled>
              Select event
            </option>
            {events.map((e) => (
              <option key={e!} value={e!}>
                {e}
              </option>
            ))}
          </select>
        </label>
        <label>
          Session type
          <select
            value={current?.session_type ?? ""}
            onChange={(e) =>
              choose(
                eventSessions.find((x) => x.session_type === e.target.value),
              )
            }
          >
            <option value="" disabled>
              Select type
            </option>
            {types.map((t) => (
              <option key={t!} value={t!}>
                {t}
              </option>
            ))}
          </select>
        </label>
        <label>
          Canonical session
          <select
            value={s.session ?? ""}
            onChange={(e) =>
              choose(sessions.find((x) => x.session_id === e.target.value))
            }
          >
            <option value="" disabled>
              Select session
            </option>
            {sessions.map((x) => (
              <option key={x.session_id} value={x.session_id}>
                {x.season} · {x.event ?? x.session_id} ·{" "}
                {x.session_type ?? "Session"}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="tl-pair-controls">
        {(["A", "B"] as const).map((side) => (
          <fieldset key={side} className={`tl-side-${side.toLowerCase()}`}>
            <legend>
              <span>{side}</span> {side === "A" ? "REFERENCE" : "COMPARISON"}
            </legend>
            <label>
              Driver {side}
              <select
                value={s[`driver${side}`] ?? ""}
                onChange={(e) =>
                  update({
                    ...s,
                    [`driver${side}`]: e.target.value,
                    [`lap${side}`]: undefined,
                    zoom: undefined,
                  })
                }
              >
                <option value="" disabled>
                  Select driver
                </option>
                {current?.drivers.map((d) => (
                  <option key={d} value={d}>
                    Driver {d}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Lap {side}
              <select
                value={s[`lap${side}`] ?? ""}
                disabled={laps.kind !== "ready"}
                onChange={(e) =>
                  update({
                    ...s,
                    [`lap${side}`]: Number(e.target.value),
                    zoom: undefined,
                  })
                }
              >
                <option value="" disabled>
                  {laps.kind === "loading"
                    ? "Loading laps…"
                    : "Select observed lap"}
                </option>
                {laps.kind === "ready" &&
                  laps.data[side === "A" ? "a" : "b"].map((l) => (
                    <option key={l.lap_number} value={l.lap_number}>
                      L{l.lap_number} · {numberLabel(l.lap_time_seconds, 3)}s ·{" "}
                      {l.compound ?? "Unknown tyre"}
                      {l.deleted ? " · DELETED" : ""}
                      {l.provider_generated ? " · GENERATED" : ""}
                      {!l.reliable ? " · UNRELIABLE" : ""}
                      {l.pit_in || l.pit_out ? " · PIT" : ""}
                      {!l.car_available ? " · NO CAR DATA" : ""}
                    </option>
                  ))}
              </select>
            </label>
          </fieldset>
        ))}
      </div>
      <p className="tl-caption">
        Defaults choose fastest reliable, non-deleted, observed, non-pit laps.
        Compare drivers or two laps by the same driver.
      </p>
    </section>
  );
}
