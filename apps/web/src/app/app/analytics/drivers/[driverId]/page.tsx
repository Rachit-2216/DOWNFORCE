import { EntityProfile } from "@/features/analytics/EntityProfile";

export default async function Page({
  params,
}: {
  params: Promise<{ driverId: string }>;
}) {
  return <EntityProfile kind="drivers" id={(await params).driverId} />;
}
