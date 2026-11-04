"use client";

import { useSyncExternalStore } from "react";
import type { ToolMode } from "./product-navigation";

function subscribe(onChange: () => void) {
  window.addEventListener("popstate", onChange);
  return () => window.removeEventListener("popstate", onChange);
}

function currentMode(): ToolMode {
  const mode = new URLSearchParams(window.location.search).get("mode");
  return mode === "intelligence" || mode === "strategy" ? mode : "replay";
}

/** URL intent without requiring a client-search-params Suspense boundary. */
export function useToolMode(): ToolMode {
  return useSyncExternalStore(subscribe, currentMode, () => "replay");
}
