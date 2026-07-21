/** @type {import('next').NextConfig} */
const nextConfig = {
  // Doctrine: no third-party telemetry by default.
  // (NEXT_TELEMETRY_DISABLED is also set in the Dockerfile to be belt-and-suspenders.)
  output: "standalone",
  reactStrictMode: true,
  poweredByHeader: false,
  experimental: {
    typedRoutes: true,
    // Brief + Episode Intelligence hit Anthropic Opus. EI in particular
    // ships ~25k input tokens (planner JSON + unparsed clinical notes)
    // and routinely takes 50-70s on the LLM call alone, plus planner
    // overhead. The default 30s and even the prior 120s cap were
    // hitting at the worst possible moment — Opus had finished and
    // the api had already committed the model_run, but the persistence
    // path got cancelled by the proxy timeout, leaving an orphan
    // model_run with no conversation message saved. Caught 2026-05-14
    // 03:17 UTC. Bumped to 240s for headroom.
    proxyTimeout: 240_000,
  },
  async headers() {
    // P1-8b security headers. Applied to every response the web tier
    // serves. Deliberately conservative on CSP: we set frame-ancestors /
    // object-src / base-uri (safe for Next.js) but do NOT attempt a full
    // script-src lockdown in this pass — Next's inline bootstrap + chunk
    // loading would need nonces/hashes wired through, a separate change.
    //
    // HSTS is sent unconditionally: browsers only honor it over HTTPS, so
    // it's a no-op for a plain-HTTP localhost dev instance and correct for
    // every TLS-terminated deployment.
    const securityHeaders = [
      {
        key: "Strict-Transport-Security",
        value: "max-age=63072000; includeSubDomains",
      },
      { key: "X-Content-Type-Options", value: "nosniff" },
      { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
      {
        key: "Permissions-Policy",
        value: "camera=(), microphone=(), geolocation=()",
      },
      {
        key: "Content-Security-Policy",
        value: "frame-ancestors 'none'; object-src 'none'; base-uri 'self'",
      },
    ];
    return [{ source: "/:path*", headers: securityHeaders }];
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
