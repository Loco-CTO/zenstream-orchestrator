import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "export",
  trailingSlash: true,
  async rewrites() {
    if (process.env.NODE_ENV !== "development") return [];
    return [
      {
        source: "/api/:path*",
        destination: `${process.env.ORCHESTRATOR_API_URL ?? "http://127.0.0.1:9090"}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
