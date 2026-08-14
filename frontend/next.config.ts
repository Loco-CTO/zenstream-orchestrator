import type { NextConfig } from "next";

const isDevelopment = process.env.NODE_ENV === "development";
const developmentApiUrl =
	process.env.NEXT_PUBLIC_ORCHESTRATOR_API_URL ??
	process.env.ORCHESTRATOR_API_URL ??
	"http://localhost:9090";
const publicWebUrl =
	process.env.NEXT_PUBLIC_ZENSTREAM_WEB_URL ??
	(isDevelopment ? "http://localhost:9086" : "");

const nextConfig: NextConfig = {
	...(isDevelopment ? {} : { output: "export" as const }),
	env: {
		NEXT_PUBLIC_ORCHESTRATOR_API_URL: isDevelopment ? developmentApiUrl : "",
		NEXT_PUBLIC_ZENSTREAM_WEB_URL: publicWebUrl,
	},
	// Keep Turbopack's mutable development manifests away from production builds.
	distDir: isDevelopment ? ".next-dev" : ".next",
	turbopack: { root: __dirname },
	trailingSlash: true,
};

export default nextConfig;
