import type { Metadata } from "next";
import type { ReactNode } from "react";

import "@fontsource-variable/manrope/wght.css";
import "@fontsource/barlow-condensed/600.css";
import "@fontsource/barlow-condensed/700.css";
import "@fontsource/jetbrains-mono/400.css";
import "@fontsource/jetbrains-mono/500.css";
import "@fontsource/jetbrains-mono/600.css";
import "./globals.css";
import "./landing-correction.css";
import "./replay.css";
import "./art-direction.css";
import "./catalog.css";
import "./analytics.css";

export const metadata: Metadata = {
  title: "DOWNFORCE — Formula 1 race intelligence",
  description:
    "Engineering-grade Formula 1 historical replay, ML intelligence, and probabilistic strategy simulation.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
