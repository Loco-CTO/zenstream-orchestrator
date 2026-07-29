import type { NextConfig } from "next";

const isDevelopment = process.env.NODE_ENV === "development";

const nextConfig: NextConfig = {
  ...(isDevelopment ? {} : { output: "export" as const }),
  // Keep Turbopack's mutable development manifests away from production builds.
  distDir: isDevelopment ? ".next-dev" : ".next",
  turbopack: { root: __dirname },
  trailingSlash: true,
  ...(isDevelopment ? {
    async rewrites() {
      return [
      {
        source: "/api/:path*",
        destination: `${process.env.ORCHESTRATOR_API_URL ?? "http://127.0.0.1:9090"}/api/:path*`,
      },
    ];
    },
  } : {}),
};

export default nextConfig;
