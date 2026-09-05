import { EntityProfile } from "@/features/analytics/EntityProfile";

export default async function Page({
  params,
}: {
  params: Promise<{ circuitId: string }>;
}) {
  return <EntityProfile kind="circuits" id={(await params).circuitId} />;
}
