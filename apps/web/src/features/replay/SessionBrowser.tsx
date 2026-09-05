"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { ApiClientError, getSessions } from "@/lib/api/client";
import type { SessionSummary } from "@/lib/api/types";

type BrowserState =
  | { kind: "loading" }
  | { kind: "ready"; sessions: SessionSummary[] }
  | { kind: "error"; message: string };

export function SessionBrowser() {
  const [state, setState] = useState<BrowserState>({ kind: "loading" });
  const [requestKey, setRequestKey] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    void getSessions({ signal: controller.signal })
      .then((response) => setState({ kind: "ready", sessions: response.items }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setState({
          kind: "error",
          message:
            error instanceof ApiClientError
              ? error.message
              : "Historical sessions could not be loaded.",
        });
      });
    return () => controller.abort();
  }, [requestKey]);

  if (state.kind === "loading") {
    return (
      <div className="session-state" role="status">
        <span className="session-state__pulse" aria-hidden="true" />
        Reading canonical session registry…
      </div>
    );
  }
  if (state.kind === "error") {
    return (
      <div className="session-state session-state--error" role="alert">
        <strong>API connection unavailable</strong>
        <span>{state.message}</span>
        <code>pnpm dev:api</code>
        <button
          type="button"
          onClick={() => {
            setState({ kind: "loading" });
            setRequestKey((key) => key + 1);
          }}
        >
          Retry sessions
        </button>
      </div>
    );
  }
  if (state.sessions.length === 0) {
    return (
      <div className="session-state">
        <strong>No canonical sessions found</strong>
        <span>Ingest a historical race, then return to this registry.</span>
      </div>
    );
  }

  return (
    <div className="session-register" aria-label="Historical sessions">
      <div className="session-register__head" aria-hidden="true">
        <span>Event</span>
        <span>Season</span>
        <span>Type</span>
        <span>Source</span>
        <span />
      </div>
      {state.sessions.map((session, index) => (
        <Link
          className="session-row"
          href={`/app/replay/${encodeURIComponent(session.session_id)}`}
          key={session.session_id}
        >
          <span className="session-row__event">
            <small>{String(index + 1).padStart(2, "0")}</small>
            <strong>{session.event_name}</strong>
          </span>
          <span>{session.season}</span>
          <span>{session.session_type}</span>
          <span className="session-row__source">
            <i aria-hidden="true" />
            {session.provider}
          </span>
          <span className="session-row__open">
            Open replay <b aria-hidden="true">↗</b>
          </span>
        </Link>
      ))}
    </div>
  );
}
