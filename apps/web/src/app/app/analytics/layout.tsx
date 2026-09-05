import type { ReactNode } from "react";

import { AnalyticsLayout } from "@/features/analytics/AnalyticsLayout";

export default function Layout({ children }: { children: ReactNode }) {
  return <AnalyticsLayout>{children}</AnalyticsLayout>;
}
