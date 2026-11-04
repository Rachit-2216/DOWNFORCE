"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { getSessions } from "@/lib/api/client";
import type { SessionSummary } from "@/lib/api/types";
import {
  detailedRaceHref,
  replayLibraryHref,
  type ToolMode,
} from "@/lib/product-navigation";
import { useToolMode } from "@/lib/use-tool-mode";

type BrowserState =
  | { kind: "loading" }
  | { kind: "ready"; sessions: SessionSummary[]; total: number }
  | { kind: "error" };

const modes: { id: ToolMode; label: string; description: string }[] = [
  {
    id: "replay",
    label: "Replays",
    description: "Follow observed race history, lap by lap.",
  },
  {
    id: "intelligence",
    label: "Intelligence",
    description: "Explore model estimates with their uncertainty.",
  },
  {
    id: "strategy",
    label: "Strategy",
    description: "Compare simulated alternatives under explicit assumptions.",
  },
];

export function SessionBrowser() {
  const [state, setState] = useState<BrowserState>({ kind: "loading" });
  const [requestKey, setRequestKey] = useState(0);
  const [query, setQuery] = useState("");
  const [season, setSeason] = useState("");
  const mode = useToolMode();

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    void getSessions({ signal: controller.signal })
      .then((response) => {
        if (!active || controller.signal.aborted) return;
        setState({
          kind: "ready",
          sessions: response.items,
          total: response.total,
        });
      })
      .catch(() => {
        if (active && !controller.signal.aborted) setState({ kind: "error" });
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, [requestKey]);

  const sessions = state.kind === "ready" ? state.sessions : [];
  const seasons = [...new Set(sessions.map((session) => session.season))].sort(
    (a, b) => b - a,
  );
  const filtered = sessions.filter(
    (session) =>
      (!season || String(session.season) === season) &&
      `${session.event_name} ${session.season} ${session.provider}`
        .toLowerCase()
        .includes(query.trim().toLowerCase()),
  );
  const chosen = modes.find((item) => item.id === mode)!;
  const chooseMode = (nextMode: ToolMode) => {
    if (nextMode === mode) return;
    window.history.pushState(null, "", replayLibraryHref(nextMode));
    window.dispatchEvent(new PopStateEvent("popstate"));
  };

  return (
    <div className="race-library">
      <div
        className="race-library__mode"
        role="group"
        aria-label="Open races in"
      >
        {modes.map((item) => (
          <button
            type="button"
            aria-pressed={mode === item.id}
            onClick={() => chooseMode(item.id)}
            key={item.id}
          >
            {item.label}
            <span aria-hidden="true">↗</span>
          </button>
        ))}
      </div>
      <p className="race-library__intent" key={mode}>
        {chosen.description} Choose a race below to begin.
      </p>
      {state.kind === "loading" ? (
        <div className="session-state" role="status">
          <span className="session-state__pulse" aria-hidden="true" />
          Loading the detailed race library…
        </div>
      ) : state.kind === "error" ? (
        <div className="session-state session-state--error" role="alert">
          <strong>The race library is unavailable right now.</strong>
          <span>
            You can try again or continue exploring the historical archive.
          </span>
          <div className="race-library__recovery">
            <button
              type="button"
              onClick={() => {
                setState({ kind: "loading" });
                setRequestKey((key) => key + 1);
              }}
            >
              Retry races
            </button>
            <Link href="/app">Explore archive</Link>
          </div>
        </div>
      ) : sessions.length === 0 ? (
        <div className="session-state">
          <strong>No detailed races are published right now.</strong>
          <span>The broader historical archive is still available.</span>
          <Link href="/app">Explore archive</Link>
        </div>
      ) : (
        <>
          <div className="race-library__filters">
            <label>
              <span>Search detailed races</span>
              <input
                type="search"
                placeholder="Race, season or source"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
              />
            </label>
            <label>
              <span>Season</span>
              <select
                value={season}
                onChange={(event) => setSeason(event.target.value)}
              >
                <option value="">All seasons</option>
                {seasons.map((year) => (
                  <option key={year} value={year}>
                    {year}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <div className="race-library__results">
            <p role="status" aria-live="polite" aria-atomic="true">
              {filtered.length} of {state.total} published races
              {sessions.length < state.total
                ? ` · ${sessions.length} loaded`
                : ""}
            </p>
            {(query || season) && (
              <button
                type="button"
                onClick={() => {
                  setQuery("");
                  setSeason("");
                }}
              >
                Clear filters
              </button>
            )}
          </div>
          {filtered.length === 0 ? (
            <div className="session-state">
              <strong>No races match your filters.</strong>
              <span>Try another event name or show all seasons.</span>
            </div>
          ) : (
            <div
              className="session-register"
              aria-label="Detailed races"
              key={`${mode}-${query}-${season}`}
            >
              <div className="session-register__head" aria-hidden="true">
                <span>Event</span>
                <span>Season</span>
                <span>Type</span>
                <span>Source</span>
                <span>Workspace</span>
              </div>
              {filtered.map((session, index) => (
                <Link
                  className="session-row"
                  href={detailedRaceHref(session.session_id, mode)}
                  key={session.session_id}
                  prefetch={false}
                >
                  <span className="session-row__event">
                    <small>{String(index + 1).padStart(2, "0")}</small>
                    <strong>{session.event_name}</strong>
                  </span>
                  <span className="session-row__season">{session.season}</span>
                  <span className="session-row__type">
                    {session.session_type}
                  </span>
                  <span className="session-row__source">
                    <i aria-hidden="true" />
                    {session.provider}
                  </span>
                  <span className="session-row__open">
                    Open {mode} <b aria-hidden="true">↗</b>
                  </span>
                </Link>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
