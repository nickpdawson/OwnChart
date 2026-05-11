/** @type {import('next').NextConfig} */
const nextConfig = {
  // Doctrine: no third-party telemetry by default.
  // (NEXT_TELEMETRY_DISABLED is also set in the Dockerfile to be belt-and-suspenders.)
  output: "standalone",
  reactStrictMode: true,
  poweredByHeader: false,
  experimental: {
    typedRoutes: true,
    // Brief generation hits Anthropic Opus and routinely takes 30-60s.
    // Default rewrite proxy timeout is 30s, which silently cuts the
    // response and surfaces as `socket hang up` / HTTP 500 in the
    // browser even though the api fully completed. Bump to 120s.
    proxyTimeout: 120_000,
  },
  async rewrites() {
    // The web container talks to the api container over the docker network.
    // Browser-originated /api/* calls are proxied to the FastAPI service,
    // which keeps cookies same-origin and avoids CORS entirely.
    const apiBase = process.env.OWNCHART_API_INTERNAL_URL || "http://api:8000";
    return [
      {
        source: "/api/:path*",
        destination: `${apiBase}/api/:path*`,
      },
      {
        source: "/healthz",
        destination: `${apiBase}/healthz`,
      },
      {
        source: "/readyz",
        destination: `${apiBase}/readyz`,
      },
      {
        source: "/openapi.json",
        destination: `${apiBase}/openapi.json`,
      },
    ];
  },
};

export default nextConfig;
