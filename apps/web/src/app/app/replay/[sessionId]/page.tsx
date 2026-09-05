import { ReplayWorkspace } from "@/features/replay/ReplayWorkspace";

export default async function ReplayPage({
  params,
}: {
  params: Promise<{ sessionId: string }>;
}) {
  const { sessionId } = await params;
  return <ReplayWorkspace sessionId={decodeURIComponent(sessionId)} />;
}
