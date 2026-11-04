import { AppHeader } from "@/components/app/AppHeader";
import { SessionBrowser } from "@/features/replay/SessionBrowser";

export default function DetailedReplayRegistryPage() {
  return (
    <div className="app-shell app-shell--registry workspace-discovery">
      <AppHeader context="Race workspace / Detailed library" />
      <main className="session-index" id="main-content">
        <section
          className="session-index__intro"
          aria-labelledby="workspace-title"
        >
          <p className="eyebrow">
            Detailed race library / Three ways into a race
          </p>
          <h1 id="workspace-title">Re-enter every decision.</h1>
          <p>
            Follow the race that happened. Understand the estimates. Test the
            decision that did not. Choose from the available detailed races; the
            broader historical record lives in the archive.
          </p>
          <div
            className="session-index__stamp"
            aria-label="Workspace guarantees"
          >
            <span>Observed history</span>
            <span>Model estimates</span>
            <span>Simulated alternatives</span>
          </div>
        </section>
        <section aria-labelledby="available-sessions-title">
          <div className="session-index__section-head">
            <h2 id="available-sessions-title">Choose your race</h2>
            <span>One library. Your choice of workspace.</span>
          </div>
          <SessionBrowser />
        </section>
      </main>
    </div>
  );
}
