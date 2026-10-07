import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Self-contained server in .next/standalone for a small Docker image.
  output: "standalone",
  poweredByHeader: false,
};

export default nextConfig;
