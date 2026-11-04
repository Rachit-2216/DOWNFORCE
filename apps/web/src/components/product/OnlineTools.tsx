"use client";

import Link from "next/link";
import { useEffect, useId, useState } from "react";

import { getSessions } from "@/lib/api/client";
import type { SessionSummary } from "@/lib/api/types";
import {
  detailedRaceHref,
  replayLibraryHref,
  type ToolMode,
} from "@/lib/product-navigation";

// Exact published corpus identities, not event-name guesses. These three races
// have driver #1 and completed lap 5; unfamiliar sessions keep the default chooser.
const featuredSessionIds = [
  "session-2024-event-formula-1-hungarian-grand-prix-2024-sha256-cdc7f18370f678e135dd423e4794e6c76b2a318c08c5b7cf80f158e32ed61f10-type-race",
  "session-2023-event-formula-1-aramco-british-grand-prix-2023-sha256-4d61aa57796468000b205d16df24e22da4b6377c9aeebffe73b2c3dc49597a56-type-race",
  "session-2023-event-formula-1-pirelli-gran-premio-ditalia-2023-sha256-0974cff5fa7bbb9f8b54e7c88546e03a4ea7d294f4311782406fc351c08172d3-type-race",
];
const modes = ["replay", "intelligence", "strategy"] as const;

type RaceState =
  | { kind: "loading" }
  | { kind: "error" }
  | { kind: "ready"; sessions: SessionSummary[]; total: number };

function raceToolHref(session: SessionSummary, mode: ToolMode) {
  const href = detailedRaceHref(session.session_id, mode);
  return mode !== "replay" && featuredSessionIds.includes(session.session_id)
    ? `${href}&driver=1&lap=5`
    : href;
}

export function OnlineTools() {
  const headingId = useId();
  const [state, setState] = useState<RaceState>({ kind: "loading" });
  const [attempt, setAttempt] = useState(0);

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
  }, [attempt]);

  const sessions = state.kind === "ready" ? state.sessions : [];
  const preferred = featuredSessionIds.flatMap((id) => {
    const session = sessions.find((item) => item.session_id === id);
    return session ? [session] : [];
  });
  const featured = [
    ...preferred,
    ...sessions.filter(
      (session) => !featuredSessionIds.includes(session.session_id),
    ),
  ].slice(0, 3);
  const entryHref = (mode: ToolMode) =>
    featured[0] ? raceToolHref(featured[0], mode) : replayLibraryHref(mode);
  const tools = [
    { name: "Race archive", detail: "The historical record", href: "/app" },
    {
      name: "Analytics",
      detail: "Patterns across seasons",
      href: "/app/analytics",
    },
    {
      name: "Replay",
      detail: "The race, as it unfolded",
      href: replayLibraryHref(),
    },
    {
      name: "Intelligence",
      detail: "Estimates, not certainties",
      href: entryHref("intelligence"),
    },
    {
      name: "Strategy",
      detail: "Test another decision",
      href: entryHref("strategy"),
    },
  ];

  return (
    <section className="online-tools" aria-labelledby={headingId}>
      <header className="online-tools__heading">
        <div>
          <p className="online-tools__eyebrow">YOUR RACE WORKSPACE / ONLINE</p>
          <h2 id={headingId}>Choose a race. Make your next move.</h2>
        </div>
        <p>
          Start with published evidence. Open the tools in a real race context.
        </p>
      </header>
      <nav className="online-tools__nav" aria-label="Online tools">
        {tools.map((tool, index) => (
          <Link href={tool.href} key={tool.name} prefetch={false}>
            <span className="online-tools__nav-index" aria-hidden="true">
              0{index + 1}
            </span>
            <strong>
              {tool.name} <span aria-hidden="true">↗</span>
            </strong>
            <small>{tool.detail}</small>
          </Link>
        ))}
      </nav>
      <div className="online-tools__race-heading">
        <p className="online-tools__eyebrow">DETAILED RACE CONTEXTS</p>
        <span>
          {state.kind === "ready"
            ? `${state.total} published / ${featured.length} featured`
            : "Published session registry"}
        </span>
      </div>
      {state.kind === "loading" ? (
        <div className="online-tools__state" role="status">
          <span className="online-tools__signal" aria-hidden="true" />
          <strong>Loading published races…</strong>
          <p>Checking the available detailed sessions.</p>
        </div>
      ) : state.kind === "error" ? (
        <div
          className="online-tools__state online-tools__state--error"
          role="alert"
        >
          <strong>We could not load the race choices.</strong>
          <p>
            The race service is unavailable right now. You can retry or explore
            the archive.
          </p>
          <button
            type="button"
            onClick={() => {
              setState({ kind: "loading" });
              setAttempt((value) => value + 1);
            }}
          >
            Retry races <span aria-hidden="true">↻</span>
          </button>
        </div>
      ) : featured.length === 0 ? (
        <div className="online-tools__state" role="status">
          <strong>No detailed races are published right now.</strong>
          <p>
            The broader race archive and analytics are still available above.
          </p>
        </div>
      ) : (
        <div className="online-tools__races">
          {featured.map((session, index) => (
            <article className="online-tools__race" key={session.session_id}>
              <div className="online-tools__race-top">
                <span>{session.season}</span>
                <span className="online-tools__eyebrow">
                  {session.session_type} / 0{index + 1}
                </span>
              </div>
              <h3>{session.event_name}</h3>
              <p>
                One race. Observed history, model estimates and simulated
                alternatives.
              </p>
              <div className="online-tools__race-links">
                {modes.map((mode) => (
                  <Link
                    href={raceToolHref(session, mode)}
                    key={mode}
                    prefetch={false}
                  >
                    {mode} <span aria-hidden="true">↗</span>
                  </Link>
                ))}
              </div>
            </article>
          ))}
        </div>
      )}
      <footer className="online-tools__footer">
        <p>Featured choices are a starting point, not the whole library.</p>
        <Link href={replayLibraryHref()}>
          Full replay library <span aria-hidden="true">→</span>
        </Link>
      </footer>
    </section>
  );
}
