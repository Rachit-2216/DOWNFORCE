"use client";

import { useEffect, useState } from "react";

import { ApiClientError, getHealth } from "@/lib/api/client";

type HealthState =
  | { kind: "checking"; message: string }
  | { kind: "online"; message: string }
  | { kind: "offline"; message: string };

export function ApiHealthIndicator() {
  const [state, setState] = useState<HealthState>({
    kind: "checking",
    message: "Checking local API connection…",
  });

  useEffect(() => {
    if (process.env.NODE_ENV !== "development") {
      return;
    }

    const controller = new AbortController();

    void getHealth({ signal: controller.signal })
      .then((health) => {
        setState({
          kind: "online",
          message: `${health.service} ${health.version} is reachable.`,
        });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) {
          return;
        }

        const message =
          error instanceof ApiClientError
            ? error.message
            : "The API health check failed unexpectedly.";
        setState({ kind: "offline", message });
      });

    return () => controller.abort();
  }, []);

  if (process.env.NODE_ENV !== "development") {
    return null;
  }

  return (
    <p className={`health health--${state.kind}`} role="status">
      <span aria-hidden="true" />
      {state.message}
    </p>
  );
}
