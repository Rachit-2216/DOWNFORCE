"use client";

import { useEffect, useRef, useState } from "react";

import { ApiClientError, compareStrategies } from "@/lib/api/client";
import type {
  DriverState,
  StrategyCandidate,
  StrategyComparisonResponse,
} from "@/lib/api/types";

import { driverLabel } from "./replay-utils";

type RunState =
  | { kind: "idle" }
  | { kind: "loading"; key: string }
  | { kind: "error"; key: string; message: string }
  | { kind: "ready"; key: string; data: StrategyComparisonResponse };

const percentage = (value: number) => `${Math.round(value * 100)}%`;

export function StrategyPanel({
  sessionId,
  driver,
  cursorMs,
  referenceLap,
}: {
  sessionId: string;
  driver: DriverState | null;
  cursorMs: number;
  referenceLap: number | null;
}) {
  const [totalLaps, setTotalLaps] = useState("");
  const [pitLap, setPitLap] = useState("");
  const [compound, setCompound] = useState<"soft" | "medium" | "hard">(
    "medium",
  );
  const [state, setState] = useState<RunState>({ kind: "idle" });
  const abortRef = useRef<AbortController | null>(null);
  const contextKey = driver
    ? [
        sessionId,
        driver.driver_id,
        Math.round(cursorMs),
        referenceLap ?? "none",
        totalLaps.trim(),
        pitLap.trim(),
        compound,
      ].join(":")
    : "none";

  useEffect(() => {
    abortRef.current?.abort();
    return () => abortRef.current?.abort();
  }, [contextKey]);

  const visibleState: RunState =
    state.kind !== "idle" && state.key !== contextKey
      ? { kind: "idle" }
      : state;

  const run = () => {
    if (!driver || referenceLap === null) return;
    const distanceText = totalLaps.trim();
    const distance = distanceText === "" ? undefined : Number(distanceText);
    const stopLap = Number(pitLap);
    if (
      distance !== undefined &&
      (!Number.isInteger(distance) || distance <= referenceLap)
    ) {
      setState({
        kind: "error",
        key: contextKey,
        message: "The race-lap override must extend beyond the current lap.",
      });
      return;
    }
    const firstActionableLap = driver.laps_completed + 2;
    if (
      !Number.isInteger(stopLap) ||
      stopLap < firstActionableLap ||
      stopLap > 200 ||
      (distance !== undefined && stopLap > distance)
    ) {
      setState({
        kind: "error",
        key: contextKey,
        message: `The first new-tyre lap must be driver-own lap ${firstActionableLap} or later and within the scheduled distance.`,
      });
      return;
    }
    const strategies: StrategyCandidate[] = [
      { strategy_id: "stay-out", label: "Stay out", actions: [] },
      {
        strategy_id: `pit-l${stopLap}-${compound}`,
        label: `Fit ${compound} · own lap ${stopLap}`,
        actions: [{ type: "pit", lap: stopLap, compound }],
      },
    ];
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setState({ kind: "loading", key: contextKey });
    void compareStrategies(
      sessionId,
      {
        cursor_time_ms: Math.round(cursorMs),
        driver_id: driver.driver_id,
        strategies,
        scenario: {
          ...(distance === undefined ? {} : { scheduled_total_laps: distance }),
          pit_loss_mode: "sampled",
          require_two_compounds: false,
        },
        simulation_count: 500,
        seed: 2216,
      },
      { signal: controller.signal },
    )
      .then((data) => {
        if (!controller.signal.aborted)
          setState({ kind: "ready", key: contextKey, data });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setState({
          kind: "error",
          key: contextKey,
          message:
            error instanceof ApiClientError
              ? error.message
              : "Strategy comparison could not be completed.",
        });
      });
  };

  return (
    <section
      className="ops-panel strategy-panel"
      aria-labelledby="strategy-title"
    >
      <header className="ops-panel__head">
        <div>
          <span className="panel-index">05</span>
          <h2 id="strategy-title">Strategy engineering</h2>
        </div>
        <span>
          SIMULATED · seeded · {driver ? driverLabel(driver) : "no driver"}
        </span>
      </header>
      <div className="strategy-console">
        <div className="strategy-controls">
          <label>
            Race laps override (optional)
            <input
              aria-label="Race laps override"
              inputMode="numeric"
              value={totalLaps}
              placeholder="e.g. 70"
              onChange={(event) => setTotalLaps(event.target.value)}
            />
            <small>
              Blank uses canonical published distance; an override is a labelled
              scenario assumption.
            </small>
          </label>
          <label>
            First lap on new tyres (driver&apos;s own lap)
            <input
              aria-label="First lap on new tyres"
              inputMode="numeric"
              value={pitLap}
              placeholder={driver ? `lap ${driver.laps_completed + 2}+` : "lap"}
              onChange={(event) => setPitLap(event.target.value)}
            />
          </label>
          <label>
            Fit compound
            <select
              aria-label="Fit compound"
              value={compound}
              onChange={(event) =>
                setCompound(event.target.value as "soft" | "medium" | "hard")
              }
            >
              <option value="soft">Soft</option>
              <option value="medium">Medium</option>
              <option value="hard">Hard</option>
            </select>
          </label>
          <button
            type="button"
            onClick={run}
            disabled={
              !driver ||
              referenceLap === null ||
              visibleState.kind === "loading"
            }
          >
            {visibleState.kind === "loading"
              ? "Simulating 500 paths…"
              : "Compare strategy"}
          </button>
        </div>
        <div className="strategy-output" aria-live="polite">
          {visibleState.kind === "idle" && (
            <div className="strategy-primer">
              <strong>Stay out vs one stop</strong>
              <p>
                Field position, pace and pit uncertainty are simulated together.
                Results are estimates, never observed facts.
              </p>
            </div>
          )}
          {visibleState.kind === "loading" && (
            <div className="strategy-primer" role="status">
              <strong>Running paired uncertainty paths</strong>
              <p>
                The same random conditions are being applied to both candidates.
              </p>
            </div>
          )}
          {visibleState.kind === "error" && (
            <div role="alert">{visibleState.message}</div>
          )}
          {visibleState.kind === "ready" &&
            visibleState.data.status === "unavailable" && (
              <div className="strategy-unavailable" role="status">
                <strong>Simulation unavailable</strong>
                <span>
                  {(
                    visibleState.data.availability_reason ??
                    "Unsupported cursor"
                  ).replaceAll("_", " ")}
                </span>
              </div>
            )}
          {visibleState.kind === "ready" &&
            visibleState.data.status === "available" && (
              <StrategyResults data={visibleState.data} />
            )}
        </div>
      </div>
    </section>
  );
}

function StrategyResults({ data }: { data: StrategyComparisonResponse }) {
  if (!data.ranking || !data.strategies) return null;
  return (
    <div className="strategy-results">
      <div className="strategy-verdict">
        <span>SIMULATED · {data.ranking.status}</span>
        <strong>
          {data.ranking.recommended_strategy_id
            ? `Preferred under current assumptions: ${data.ranking.recommended_strategy_id}`
            : "No robust recommendation"}
        </strong>
        <p>{data.ranking.explanation}</p>
        <small>
          Lead wins{" "}
          {percentage(data.ranking.probability_leading_beats_runner_up)} of
          paired paths · pit sensitivity{" "}
          {data.ranking.pit_loss_sensitive ? "failed" : "passed"}
          {data.ranking.long_horizon_limited
            ? " · local tyre horizon exceeded"
            : ""}
          {data.ranking.input_data_limited ? " · background field limited" : ""}
        </small>
      </div>
      <div className="strategy-cards">
        {data.strategies.map(({ strategy, outcome }) => (
          <article key={strategy.strategy_id}>
            <span>{strategy.label}</span>
            <strong>P{outcome.median_position}</strong>
            <dl>
              <div>
                <dt>Expected</dt>
                <dd>P{outcome.expected_position.toFixed(2)}</dd>
              </div>
              <div>
                <dt>Top 3</dt>
                <dd>{percentage(outcome.probability_top_3)}</dd>
              </div>
              <div>
                <dt>Time P10–P90</dt>
                <dd>
                  {(
                    (outcome.race_time_ms.p90 - outcome.race_time_ms.p10) /
                    1_000
                  ).toFixed(1)}
                  s
                </dd>
              </div>
            </dl>
          </article>
        ))}
      </div>
      <p className="strategy-disclaimer">
        Simulated estimate · {data.simulation_count ?? 500} paths · seed 2216 ·
        dry green-flag continuation · background drivers make no unannounced
        future stops · no future incidents or traffic model · pit loss stressed
        at its marginal 90% bounds
      </p>
    </div>
  );
}
