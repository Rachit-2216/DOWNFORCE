import { memo } from "react";

import type { DriverState } from "@/lib/api/types";

import { driverLabel, sortDrivers } from "./replay-utils";

type Props = {
  drivers: DriverState[];
  selectedDriverId: string | null;
  onSelect: (driverId: string) => void;
};

function compoundCode(compound: string): string {
  const codes: Record<string, string> = {
    soft: "S",
    medium: "M",
    hard: "H",
    intermediate: "I",
    wet: "W",
    unknown: "?",
  };
  return codes[compound] ?? "?";
}

const TERMINAL_STATUSES = new Set(["retired", "finished", "dns", "dsq"]);

function statusLabel(status: string): string {
  return status.replaceAll("-", " ").toUpperCase();
}

export const TimingTower = memo(function TimingTower({
  drivers,
  selectedDriverId,
  onSelect,
}: Props) {
  const ordered = sortDrivers(drivers);
  return (
    <section className="ops-panel timing-panel" aria-labelledby="timing-title">
      <header className="ops-panel__head">
        <div>
          <span className="panel-index">01</span>
          <h2 id="timing-title">Timing</h2>
        </div>
        <span>{drivers.length} cars</span>
      </header>
      <div className="timing-head" aria-hidden="true">
        <span>Pos / Driver</span>
        <span title="Gap is not supplied by canonical RaceState">Gap</span>
        <span title="Interval is not supplied by canonical RaceState">Int</span>
        <span>Tyre</span>
      </div>
      <ol className="timing-list">
        {ordered.map((driver) => {
          const selected = driver.driver_id === selectedDriverId;
          const terminal = TERMINAL_STATUSES.has(driver.status);
          const visibleStatus = driver.in_pit
            ? "PIT"
            : driver.status === "active"
              ? null
              : statusLabel(driver.status);
          return (
            <li key={driver.driver_id}>
              <button
                type="button"
                className={`timing-row${selected ? " timing-row--selected" : ""}${terminal ? " timing-row--terminal" : ""}`}
                aria-label={`${driver.position === null ? "Position unavailable" : `Position ${driver.position}`}, ${driverLabel(driver)}, ${driver.team_name ?? "team unavailable"}, ${driver.compound} tyre, ${driver.tyre_age_laps === null ? "tyre age unavailable" : `${Math.floor(driver.tyre_age_laps)} laps old`}, status ${driver.status}, gap unavailable, interval unavailable`}
                aria-pressed={selected}
                onClick={() => onSelect(driver.driver_id)}
              >
                <span className="timing-row__identity">
                  <b>{driver.position ?? "—"}</b>
                  <i aria-hidden="true" />
                  <span>
                    <strong>{driverLabel(driver)}</strong>
                    <small>{driver.team_name ?? "Team unavailable"}</small>
                  </span>
                </span>
                <span aria-label="Gap unavailable">—</span>
                <span aria-label="Interval unavailable">—</span>
                <span
                  className={`tyre tyre--${driver.compound}`}
                  title={`${driver.compound}, ${driver.tyre_age_laps ?? "unknown"} laps old`}
                >
                  {compoundCode(driver.compound)}
                  <small>
                    {driver.tyre_age_laps === null
                      ? "—"
                      : Math.floor(driver.tyre_age_laps)}
                  </small>
                </span>
                {visibleStatus && (
                  <em
                    className={terminal ? "timing-status--terminal" : undefined}
                  >
                    {visibleStatus}
                  </em>
                )}
              </button>
            </li>
          );
        })}
      </ol>
      {drivers.length === 0 && (
        <div className="panel-empty">
          No driver state exists at this cursor.
        </div>
      )}
    </section>
  );
});
