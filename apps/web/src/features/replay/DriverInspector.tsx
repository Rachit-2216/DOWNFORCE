import { memo } from "react";

import type { DriverState, Lap } from "@/lib/api/types";

import { driverLabel, formatLapTime } from "./replay-utils";

type Props = { driver: DriverState | null; laps: Lap[] };

function value(value: string | number | null): string {
  return value === null ? "—" : String(value);
}

export const DriverInspector = memo(function DriverInspector({
  driver,
  laps,
}: Props) {
  if (!driver)
    return (
      <section className="ops-panel driver-panel">
        <div className="panel-empty">
          Select a driver to inspect their canonical race history.
        </div>
      </section>
    );
  const driverLaps = laps
    .filter(
      (lap) =>
        lap.driver_id === driver.driver_id &&
        lap.lap_number <= driver.laps_completed,
    )
    .slice(-7)
    .reverse();
  return (
    <section className="ops-panel driver-panel" aria-labelledby="driver-title">
      <header className="ops-panel__head">
        <div>
          <span className="panel-index">03</span>
          <h2 id="driver-title">Driver detail</h2>
        </div>
        <span>{driver.status}</span>
      </header>
      <div className="driver-identity">
        <span className="driver-number">{driver.racing_number ?? "—"}</span>
        <div>
          <strong>{driver.full_name ?? driverLabel(driver)}</strong>
          <span>{driver.team_name ?? "Team unavailable"}</span>
        </div>
        <b>P{driver.position ?? "—"}</b>
      </div>
      <dl className="driver-metrics">
        <div>
          <dt>Gap</dt>
          <dd title="Not supplied by canonical RaceState">—</dd>
        </div>
        <div>
          <dt>Interval</dt>
          <dd title="Not supplied by canonical RaceState">—</dd>
        </div>
        <div>
          <dt>Last lap</dt>
          <dd>{formatLapTime(driver.last_lap_time_ms)}</dd>
        </div>
        <div>
          <dt>Stops</dt>
          <dd>{driver.pit_stop_count}</dd>
        </div>
        <div>
          <dt>Laps completed</dt>
          <dd>{driver.laps_completed}</dd>
        </div>
        <div>
          <dt>Last pit lap</dt>
          <dd>{driver.last_pit_lap ?? "—"}</dd>
        </div>
      </dl>
      <div className="stint-strip">
        <span className={`tyre tyre--${driver.compound}`}>
          {driver.compound.slice(0, 1).toUpperCase()}
        </span>
        <div>
          <small>Current stint</small>
          <strong>
            {driver.compound} / stint {value(driver.current_stint)}
          </strong>
        </div>
        <div>
          <small>Tyre age</small>
          <strong>
            {driver.tyre_age_laps === null
              ? "—"
              : `${Math.floor(driver.tyre_age_laps)} laps`}
          </strong>
        </div>
        {driver.in_pit && <em>In pit</em>}
      </div>
      <div className="lap-history">
        <div className="lap-history__head">
          <h3>Lap history</h3>
          <span>Latest first</span>
        </div>
        {driverLaps.length ? (
          <table>
            <thead>
              <tr>
                <th>Lap</th>
                <th>Time</th>
                <th>S1</th>
                <th>S2</th>
                <th>S3</th>
              </tr>
            </thead>
            <tbody>
              {driverLaps.map((lap) => (
                <tr key={lap.lap_number}>
                  <td>{lap.lap_number}</td>
                  <td>{formatLapTime(lap.lap_time_ms)}</td>
                  <td>{formatLapTime(lap.sector_1_time_ms)}</td>
                  <td>{formatLapTime(lap.sector_2_time_ms)}</td>
                  <td>{formatLapTime(lap.sector_3_time_ms)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div className="panel-empty panel-empty--compact">
            No completed laps at this cursor.
          </div>
        )}
      </div>
    </section>
  );
});
