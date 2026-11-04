"use client";
import { numberLabel, signed } from "./telemetry-utils";
import type { Corners } from "./types";
export function TelemetryCorners({
  corners,
  cursor,
  onFocus,
}: {
  corners: Corners | null;
  cursor: number;
  onFocus: (zoom: [number, number]) => void;
}) {
  if (!corners)
    return (
      <section className="tl-panel">
        <h2>Window analysis unavailable</h2>
        <p>The selected pair cannot currently support this derived analysis.</p>
      </section>
    );
  const selected = corners.items.find(
    (w) => cursor >= w.window.start_m && cursor <= w.window.end_m,
  );
  const metricRows = [
    { key: "entry_speed_kmh", label: "Entry speed", unit: "km/h" },
    { key: "minimum_speed_kmh", label: "Minimum speed", unit: "km/h" },
    { key: "exit_speed_kmh", label: "Exit speed", unit: "km/h" },
    { key: "gear_at_minimum", label: "Gear at minimum", unit: "" },
    { key: "brake_onset_m", label: "Brake onset", unit: "m" },
    { key: "brake_release_m", label: "Brake release", unit: "m" },
    { key: "throttle_pickup_m", label: "Throttle pickup", unit: "m" },
    { key: "full_throttle_m", label: "Full throttle", unit: "m" },
  ] as const;
  return (
    <section className="tl-panel">
      <header className="tl-section-head">
        <div>
          <p className="tl-eyebrow">
            DERIVED ·{" "}
            {corners.markers_available
              ? "PROVIDER CORNER WINDOWS"
              : "DISTANCE WINDOWS"}
          </p>
          <h2>
            {corners.markers_available
              ? "Corner-by-corner"
              : "Distance-window analysis"}
          </h2>
        </div>
        <p className="tl-caption">Select a row to focus all views.</p>
      </header>
      <div className="tl-table-wrap">
        <table>
          <thead>
            <tr>
              <th>Window</th>
              <th>Distance m</th>
              <th>Minimum A / B km/h</th>
              <th>Time A / B s</th>
              <th>Δ A − B</th>
            </tr>
          </thead>
          <tbody>
            {corners.items.map((w) => (
              <tr
                key={w.window.label}
                className={w === selected ? "selected" : ""}
              >
                <th>
                  <button
                    onClick={() => onFocus([w.window.start_m, w.window.end_m])}
                  >
                    {w.window.marker
                      ? `${w.window.marker.number}${w.window.marker.letter}`
                      : w.window.label}
                  </button>
                </th>
                <td>
                  {Math.round(w.window.start_m)}–{Math.round(w.window.end_m)}
                </td>
                <td>
                  {numberLabel(w.a.minimum_speed_kmh)} /{" "}
                  {numberLabel(w.b.minimum_speed_kmh)}
                </td>
                <td>
                  {numberLabel(w.a.time_seconds, 3)} /{" "}
                  {numberLabel(w.b.time_seconds, 3)}
                </td>
                <td
                  className={
                    w.delta_seconds !== null && w.delta_seconds < 0
                      ? "tl-a"
                      : "tl-b"
                  }
                >
                  {signed(w.delta_seconds)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {selected && (
        <div className="tl-window-detail">
          <h3>{selected.window.label} · shared cursor window</h3>
          <dl>
            {metricRows.map((row) => (
              <div key={row.key}>
                <dt>{row.label}</dt>
                <dd>
                  <span className="tl-a">
                    {numberLabel(selected.a[row.key])}
                  </span>{" "}
                  /{" "}
                  <span className="tl-b">
                    {numberLabel(selected.b[row.key])}
                  </span>{" "}
                  {row.unit}
                </dd>
              </div>
            ))}
          </dl>
          <p className="tl-caption">
            Brake is binary activity, never pressure. Input onset/pickup uses
            persistence {selected.persistence_seconds.toFixed(2)} s.{" "}
            {[...selected.a.flags, ...selected.b.flags].join(" · ")}
          </p>
        </div>
      )}
      <p className="tl-caption">
        Marker windows are not physical-apex detection. Gains follow A − B: a
        negative delta means A is faster.
      </p>
    </section>
  );
}
