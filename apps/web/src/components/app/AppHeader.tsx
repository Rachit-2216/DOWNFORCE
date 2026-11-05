"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { isVisitorEdition } from "@/lib/data-profile";
import {
  detailedRaceHref,
  replayLibraryHref,
  type ToolMode,
} from "@/lib/product-navigation";
import { useToolMode } from "@/lib/use-tool-mode";

export type AppSection =
  | "archive"
  | "analytics"
  | "replay"
  | "intelligence"
  | "strategy"
  | "telemetry";

export function AppHeader({
  context,
  activeSection,
  raceContext,
  onToolChange,
}: {
  context: string;
  activeSection?: AppSection;
  raceContext?: { sessionId: string; driverId?: string; timeMs: number };
  onToolChange?: (mode: ToolMode) => void;
}) {
  const pathname = usePathname() ?? "";
  const mode = useToolMode();
  const current =
    activeSection ??
    (pathname.startsWith("/app/analytics")
      ? "analytics"
      : pathname.startsWith("/app/telemetry")
        ? "telemetry"
        : pathname.startsWith("/app/replay")
          ? mode
          : "archive");
  const areas = [
    { id: "archive", name: "Archive", href: "/app" },
    { id: "analytics", name: "Analytics", href: "/app/analytics" },
    {
      id: "replay",
      name: "Replays",
      href: raceContext
        ? detailedRaceHref(raceContext.sessionId, "replay", raceContext)
        : replayLibraryHref(),
    },
    {
      id: "intelligence",
      name: "Intelligence",
      href: raceContext
        ? detailedRaceHref(raceContext.sessionId, "intelligence", raceContext)
        : replayLibraryHref("intelligence"),
    },
    {
      id: "strategy",
      name: "Strategy",
      href: raceContext
        ? detailedRaceHref(raceContext.sessionId, "strategy", raceContext)
        : replayLibraryHref("strategy"),
    },
    ...(!isVisitorEdition()
      ? [{ id: "telemetry", name: "Telemetry", href: "/app/telemetry" }]
      : []),
  ];
  return (
    <>
      <a className="skip-link" href="#main-content">
        Skip to main content
      </a>
      <header className="app-header workspace-header">
        <Link className="wordmark" href="/" aria-label="DOWNFORCE">
          <span>DOWN</span>FORCE
          <i aria-hidden="true" />
        </Link>
        <nav aria-label="Product navigation">
          {areas.map((area, index) => (
            <Link
              href={area.href}
              key={area.id}
              aria-current={area.id === current ? "page" : undefined}
              onClick={(event) => {
                if (
                  raceContext &&
                  onToolChange &&
                  ["replay", "intelligence", "strategy"].includes(area.id) &&
                  !event.metaKey &&
                  !event.ctrlKey &&
                  !event.shiftKey &&
                  !event.altKey &&
                  event.button === 0
                ) {
                  event.preventDefault();
                  onToolChange(area.id as ToolMode);
                }
              }}
            >
              <span className="workspace-nav-index" aria-hidden="true">
                0{index + 1}
              </span>
              <span>{area.name}</span>
            </Link>
          ))}
        </nav>
        <span>{context}</span>
        <div className="workspace-header__note">
          <i aria-hidden="true" />
          Historical evidence. New perspectives.
          <small>Independent F1 intelligence</small>
        </div>
      </header>
    </>
  );
}
