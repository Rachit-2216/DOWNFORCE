import { EntityProfile } from "@/features/analytics/EntityProfile";

export default async function Page({
  params,
}: {
  params: Promise<{ constructorId: string }>;
}) {
  return (
    <EntityProfile kind="constructors" id={(await params).constructorId} />
  );
}
