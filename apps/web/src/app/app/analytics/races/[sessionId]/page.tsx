import { RaceAnalytics } from "@/features/analytics/RaceAnalytics";

export default async function Page({
  params,
}: {
  params: Promise<{ sessionId: string }>;
}) {
  return <RaceAnalytics sessionId={(await params).sessionId} />;
}
