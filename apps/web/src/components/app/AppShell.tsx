import { ExploreArchive } from "@/features/catalog/ExploreArchive";

import { AppHeader } from "./AppHeader";

export function AppShell() {
  return (
    <div className="app-shell app-shell--registry workspace-discovery">
      <AppHeader context="Race workspace / Archive" />
      <ExploreArchive />
    </div>
  );
}
