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

export function OnlineTools({
  layout = "standalone",
}: {
  layout?: "standalone" | "landing";
}) {
  const headingId = useId();
  const [state, setState] = useState<RaceState>({ kind: "loading" });
  const [attempt, setAttempt] = useState(0);
  const [preview, setPreview] = useState<ToolMode>("replay");
  const [selectedSession, setSelectedSession] = useState("");

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
  const previewRace =
    sessions.find((session) => session.session_id === selectedSession) ??
    featured[0];
  const previewCopy = {
    replay: {
      title: "Find the moment that changed the race.",
      boundary: "Observed history",
      body: "Select a driver, scrub the timeline and follow timing, stints and race conditions together.",
      steps: [
        "Choose your driver",
        "Seek a lap or play the race",
        "Inspect timing and race events",
      ],
    },
    intelligence: {
      title: "Read beyond the last lap.",
      boundary: "Predicted, not observed",
      body: "Inspect next-lap pace, tyre residual and effective pit loss, with uncertainty visible alongside every estimate.",
      steps: [
        "Pause at a decision point",
        "Inspect the available clean history",
        "Read the estimate and its interval",
      ],
    },
    strategy: {
      title: "Change the decision. Test the consequence.",
      boundary: "Simulated, not observed",
      body: "Compare staying out against a tyre change over 500 paired paths. See when pit-loss uncertainty makes the answer fragile.",
      steps: [
        "Choose a pit lap and compound",
        "Run the paired simulation",
        "Inspect sensitivity, not just a winner",
      ],
    },
  }[preview];
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
    <section
      className={`online-tools online-tools--${layout}`}
      aria-labelledby={headingId}
    >
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
      <div className="tool-preview">
        <div
          className="tool-preview__tabs"
          role="tablist"
          aria-label="Explore the race tools"
        >
          {modes.map((mode) => (
            <button
              key={mode}
              type="button"
              role="tab"
              id={`${headingId}-${mode}`}
              aria-controls={`${headingId}-preview`}
              aria-selected={preview === mode}
              tabIndex={preview === mode ? 0 : -1}
              onKeyDown={(event) => {
                const index = modes.indexOf(mode);
                const next =
                  event.key === "ArrowRight"
                    ? (index + 1) % modes.length
                    : event.key === "ArrowLeft"
                      ? (index + modes.length - 1) % modes.length
                      : event.key === "Home"
                        ? 0
                        : event.key === "End"
                          ? modes.length - 1
                          : null;
                if (next === null) return;
                event.preventDefault();
                setPreview(modes[next]);
                event.currentTarget.parentElement
                  ?.querySelectorAll<HTMLButtonElement>('[role="tab"]')
                  [next]?.focus();
              }}
              onClick={() => setPreview(mode)}
            >
              {mode.charAt(0).toUpperCase() + mode.slice(1)}
            </button>
          ))}
        </div>
        <div
          className={`tool-preview__panel tool-preview__panel--${preview}`}
          role="tabpanel"
          id={`${headingId}-preview`}
          aria-labelledby={`${headingId}-${preview}`}
          tabIndex={0}
        >
          <div className="tool-preview__story" key={preview}>
            <span className="tool-preview__boundary">
              <i aria-hidden="true" />
              {previewCopy.boundary}
            </span>
            <h3>{previewCopy.title}</h3>
            <p>{previewCopy.body}</p>
            <label>
              Preview race
              <select
                aria-label="Preview race"
                value={previewRace?.session_id ?? ""}
                disabled={!sessions.length}
                onChange={(event) => setSelectedSession(event.target.value)}
              >
                {sessions.length ? (
                  sessions.map((session) => (
                    <option key={session.session_id} value={session.session_id}>
                      {session.season} · {session.event_name}
                    </option>
                  ))
                ) : (
                  <option value="">Choose a race from the library</option>
                )}
              </select>
            </label>
            <Link
              className="tool-preview__open"
              href={
                previewRace
                  ? raceToolHref(previewRace, preview)
                  : replayLibraryHref(preview)
              }
              prefetch={false}
            >
              Open {preview} <span aria-hidden="true">↗</span>
            </Link>
          </div>
          <ol
            className="tool-preview__journey"
            aria-label={`${preview} workflow`}
          >
            {previewCopy.steps.map((step, index) => (
              <li key={step}>
                <span aria-hidden="true">0{index + 1}</span>
                <strong>{step}</strong>
              </li>
            ))}
          </ol>
        </div>
      </div>
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
