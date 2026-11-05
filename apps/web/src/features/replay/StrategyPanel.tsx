"use client";

import { useEffect, useRef, useState } from "react";
import { AsyncState } from "@/components/ui/AsyncState";

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
      <p className="tool-context-note">
        A paired simulation: stay out, or stop for a new compound. Set the first
        driver-own lap on new tyres, then compare the alternatives at the
        current cursor.
      </p>
      <ol className="strategy-steps" aria-label="Strategy setup">
        <li data-ready={Boolean(driver && referenceLap !== null)}>
          <span>01</span>
          <div>
            <strong>Decision point</strong>
            <small>
              {driver
                ? `${driverLabel(driver)} · ${driver.laps_completed} completed own laps`
                : "Choose a driver and completed lap"}
            </small>
          </div>
        </li>
        <li data-ready={pitLap.trim() !== ""}>
          <span>02</span>
          <div>
            <strong>Alternative stop</strong>
            <small>
              {pitLap.trim()
                ? `Fit ${compound} · own lap ${pitLap}`
                : "Set the first lap on new tyres below"}
            </small>
          </div>
        </li>
        <li data-ready={visibleState.kind === "ready"}>
          <span>03</span>
          <div>
            <strong>Compare the uncertainty</strong>
            <small>500 paired paths · pit-loss sensitivity</small>
          </div>
        </li>
      </ol>
      {!driver || referenceLap === null ? (
        <AsyncState
          kind="unavailable"
          title="Select a completed decision point"
          detail="Choose a driver and seek to a completed lap before comparing strategies."
          href="/app/replays?mode=strategy"
          linkLabel="Choose a detailed race"
        />
      ) : null}
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
          <div className="strategy-stop-control">
            <label>
              First lap on new tyres (driver&apos;s own lap)
              <input
                aria-label="First lap on new tyres"
                inputMode="numeric"
                value={pitLap}
                placeholder={
                  driver ? `lap ${driver.laps_completed + 2}+` : "lap"
                }
                onChange={(event) => setPitLap(event.target.value)}
              />
            </label>
            {driver && (
              <button
                type="button"
                className="strategy-preset"
                onClick={() => setPitLap(String(driver.laps_completed + 2))}
              >
                Use lap {driver.laps_completed + 2}
              </button>
            )}
          </div>
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
            <AsyncState
              kind="error"
              title="Comparison could not be completed"
              detail={visibleState.message}
              onRetry={run}
              retryLabel="Run comparison again"
            />
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
  const preferred = data.strategies.find(
    (item) =>
      item.strategy.strategy_id === data.ranking?.recommended_strategy_id,
  )?.strategy.label;
  return (
    <div className="strategy-results">
      <div className="strategy-verdict">
        <span>SIMULATED · {data.ranking.status}</span>
        <strong>
          {data.ranking.recommended_strategy_id
            ? `Preferred under current assumptions: ${preferred ?? data.ranking.recommended_strategy_id}`
            : "No robust recommendation"}
        </strong>
        <p>{data.ranking.explanation}</p>
        <small>
          Lead wins{" "}
          {percentage(data.ranking.probability_leading_beats_runner_up)} of
          paired paths · pit sensitivity{" "}
          {data.ranking.pit_loss_sensitive
            ? "fragile — the preference changes with plausible pit loss"
            : "stable within the tested pit-loss range"}
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
                <dt>P10–P90 time spread</dt>
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
      <details className="model-details">
        <summary>Scenario and model details</summary>
        <p>
          Model {data.model_version ?? "unavailable"} · simulation{" "}
          {data.simulation_version} · seed {data.seed ?? 2216}
        </p>
        <ul>
          {data.assumptions.map((assumption, index) => (
            <li key={`${index}-${assumption}`}>
              {assumption.replaceAll("_", " ")}
            </li>
          ))}
        </ul>
        <div className="strategy-time-details">
          {data.strategies.map(({ strategy, outcome }) => (
            <p key={strategy.strategy_id}>
              <strong>{strategy.label}</strong> · simulated race-clock P10 /
              median / P90: {(outcome.race_time_ms.p10 / 1000).toFixed(1)} /{" "}
              {(outcome.race_time_ms.median / 1000).toFixed(1)} /{" "}
              {(outcome.race_time_ms.p90 / 1000).toFixed(1)} seconds.
            </p>
          ))}
        </div>
      </details>
    </div>
  );
}
