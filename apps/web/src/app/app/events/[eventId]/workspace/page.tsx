import { AppHeader } from "@/components/app/AppHeader";
import { ArchiveWorkspace } from "@/features/catalog/ArchiveWorkspace";

export default async function ArchiveWorkspacePage({
  params,
}: {
  params: Promise<{ eventId: string }>;
}) {
  const { eventId } = await params;
  return (
    <div className="app-shell">
      <AppHeader context="Historical engineering workspace" />
      <ArchiveWorkspace eventId={decodeURIComponent(eventId)} />
    </div>
  );
}
