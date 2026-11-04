import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  agentRules: false,
  reactStrictMode: true,
  env: {
    NEXT_PUBLIC_DATA_PROFILE:
      process.env.NEXT_PUBLIC_DATA_PROFILE ??
      (process.env.VERCEL === "1" ? "visitor" : "local"),
  },
};

export default nextConfig;
