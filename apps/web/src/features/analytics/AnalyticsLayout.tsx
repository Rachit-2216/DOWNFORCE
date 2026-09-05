import type { ReactNode } from "react";

import { AppHeader } from "@/components/app/AppHeader";

export function AnalyticsLayout({ children }: { children: ReactNode }) {
  return (
    <div className="app-shell app-shell--analytics">
      <AppHeader context="Historical archive / Analytics" />
      {children}
    </div>
  );
}
