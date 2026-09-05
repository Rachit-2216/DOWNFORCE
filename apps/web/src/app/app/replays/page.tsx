import { AppHeader } from "@/components/app/AppHeader";
import { SessionBrowser } from "@/features/replay/SessionBrowser";

export default function DetailedReplayRegistryPage() {
  return (
    <div className="app-shell app-shell--registry">
      <AppHeader context="Detailed replay / Frozen ML V1 corpus" />
      <main className="session-index" id="main-content">
        <section
          className="session-index__intro"
          aria-labelledby="workspace-title"
        >
          <p className="eyebrow">13 detailed sessions / Canonical replay</p>
          <h1 id="workspace-title">Re-enter every decision.</h1>
          <p>
            These telemetry-era races power deterministic replay, ML
            intelligence and probabilistic strategy analysis. They are distinct
            from the broader archive.
          </p>
          <div
            className="session-index__stamp"
            aria-label="Workspace guarantees"
          >
            <span>RaceState(t)</span>
            <span>ML V1 frozen</span>
            <span>No future leakage</span>
          </div>
        </section>
        <section aria-labelledby="available-sessions-title">
          <div className="session-index__section-head">
            <h2 id="available-sessions-title">Detailed sessions</h2>
            <span>Verified local datasets</span>
          </div>
          <SessionBrowser />
        </section>
      </main>
    </div>
  );
}
