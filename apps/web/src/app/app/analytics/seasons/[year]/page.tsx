import { notFound } from "next/navigation";

import { SeasonAnalytics } from "@/features/analytics/SeasonAnalytics";

export default async function Page({
  params,
}: {
  params: Promise<{ year: string }>;
}) {
  const year = Number((await params).year);
  if (!Number.isInteger(year) || year < 2000 || year > 2026) notFound();
  return <SeasonAnalytics year={year} />;
}
