import { ExploreArchive } from "@/features/catalog/ExploreArchive";

import { AppHeader } from "./AppHeader";

export function AppShell() {
  return (
    <div className="app-shell app-shell--registry">
      <AppHeader context="Historical data platform / Explore" />
      <ExploreArchive />
    </div>
  );
}
