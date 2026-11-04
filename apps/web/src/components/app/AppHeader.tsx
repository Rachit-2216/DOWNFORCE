"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { isVisitorEdition } from "@/lib/data-profile";
import { replayLibraryHref } from "@/lib/product-navigation";
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
}: {
  context: string;
  activeSection?: AppSection;
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
    { id: "replay", name: "Replays", href: replayLibraryHref() },
    {
      id: "intelligence",
      name: "Intelligence",
      href: replayLibraryHref("intelligence"),
    },
    { id: "strategy", name: "Strategy", href: replayLibraryHref("strategy") },
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
          {areas.map((area) => (
            <Link
              href={area.href}
              key={area.id}
              aria-current={area.id === current ? "page" : undefined}
            >
              {area.name}
            </Link>
          ))}
        </nav>
        <span>{context}</span>
      </header>
    </>
  );
}
