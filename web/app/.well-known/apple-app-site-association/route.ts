import { NextResponse } from "next/server";

// LINK-01 (Universal Links / apple-app-site-association)
//
// The iOS app declares Associated Domains for BOTH hosts it ships against:
//   applinks:app.ownchart.me
//   applinks:ownchart.dzsec.net
// Both are served by this single Next.js app (single-origin contract), so
// one route handler covers both hosts. Apple's CDN fetches
//   https://<host>/.well-known/apple-app-site-association
// and it MUST be served as HTTP 200 with Content-Type: application/json,
// no redirect and no auth, or Universal Links silently fail to register.
//
// TEAM ID: currently the PERSONAL Apple Developer account. This MUST be
// swapped to the Shift Human LLC team ID once Nick supplies it (plan task
// 1.5). It is the Apple Developer Team ID that prefixes the app's bundle
// identifier to form the full appID. If this is wrong, iOS will refuse to
// associate the domain and deep links open in Safari instead of the app.
export const APPLE_TEAM_ID = "3ELWNV29J2";

// The app's bundle identifier. appID = <TEAM_ID>.<BUNDLE_ID>.
const APPLE_BUNDLE_ID = "me.ownchart.ios";

// Route handlers are static-eligible; pin dynamic so the header/body are
// always emitted exactly as written (no build-time caching surprises).
export const dynamic = "force-static";

const AASA = {
  applinks: {
    details: [
      {
        appIDs: [`${APPLE_TEAM_ID}.${APPLE_BUNDLE_ID}`],
        components: [
          {
            "/": "/invite/*",
            comment: "invite deep links",
          },
        ],
      },
    ],
  },
  // Deliberately NO webcredentials: OwnChart does not use Shared Web
  // Credentials / Password AutoFill association.
};

export function GET() {
  return NextResponse.json(AASA, {
    status: 200,
    headers: {
      "Content-Type": "application/json",
      // Apple's CDN caches this; a modest TTL keeps propagation reasonable
      // while still letting us fix a bad appID within a day.
      "Cache-Control": "public, max-age=3600",
    },
  });
}
