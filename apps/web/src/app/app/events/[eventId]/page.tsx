import { AppHeader } from "@/components/app/AppHeader";
import { RaceOverview } from "@/features/catalog/RaceOverview";

export default async function RaceOverviewPage({
  params,
}: {
  params: Promise<{ eventId: string }>;
}) {
  const { eventId } = await params;
  return (
    <div className="app-shell">
      <AppHeader context="Race overview / Data coverage" />
      <RaceOverview eventId={decodeURIComponent(eventId)} />
    </div>
  );
}
