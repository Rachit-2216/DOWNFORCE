import type { Metadata } from "next";
import dynamic from "next/dynamic";
import Link from "next/link";
import { Suspense } from "react";
import { AppHeader } from "@/components/app/AppHeader";
import { isVisitorEdition, RUN_LOCALLY_URL } from "@/lib/data-profile";
import "@/features/telemetry/telemetry.css";
const TelemetryWorkspace = dynamic(() =>
  import("@/features/telemetry/TelemetryWorkspace").then(
    (module) => module.TelemetryWorkspace,
  ),
);
export const metadata: Metadata = {
  title: "Telemetry Lab | DOWNFORCE",
  description:
    "Compare canonical lap telemetry with one synchronized distance cursor.",
};
export default function TelemetryPage() {
  if (isVisitorEdition()) {
    return (
      <>
        <AppHeader context="Local research" />
        <main id="main-content" className="explore-shell">
          <header className="explore-intro">
            <p className="eyebrow">DOWNFORCE / Local research environment</p>
            <h1>Telemetry research runs locally</h1>
            <p>
              The visitor edition includes the historical archive, analytics,
              detailed replays and frozen intelligence. Native telemetry is not
              bundled in this read-only public snapshot.
            </p>
            <p>
              Clone DOWNFORCE to use the implemented research tools on your own
              machine. Download or import datasets you are entitled to use;
              coverage depends on provider availability and terms. Cloning does
              not include ignored native datasets or guarantee every session.
            </p>
            <p>
              <a href={RUN_LOCALLY_URL}>Run locally</a> ·{" "}
              <Link href="/app">Explore the archive</Link> ·{" "}
              <Link href="/app/replays">Open detailed replays</Link>
            </p>
          </header>
        </main>
      </>
    );
  }
  return (
    <>
      <AppHeader context="Telemetry Lab" />
      <Suspense fallback={<p>Loading telemetry route…</p>}>
        <TelemetryWorkspace />
      </Suspense>
    </>
  );
}
