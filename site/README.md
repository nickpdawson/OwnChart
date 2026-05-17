# OwnChart landing page

Static landing page for `www.ownchart.me`. No build step; pure HTML + CSS.

## Structure

```
site/
├── index.html      Landing page
├── screenshots.html Screenshots tour (web + iOS)
├── privacy.html    Privacy page (mirrored from PRIVACY.md)
├── styles.css      All styling
├── legal.css       Additional styles for /privacy and /screenshots
├── favicon.svg     Browser tab icon (four-node mark)
├── og-image.svg    Social preview (1200×630)
├── _headers        Edge security headers (same syntax as Cloudflare Pages)
├── _redirects      Short-link redirects to GitHub doc paths
├── robots.txt
├── sitemap.xml
└── screenshots/    Web + iOS shots used by index.html and screenshots.html
```

## Deploying via Cloudflare Workers Static Assets

The site is deployed as a Cloudflare **Worker** (not Pages), using
`wrangler.jsonc` at the repo root. Authentication uses your local
wrangler OAuth token cached at `~/Library/Preferences/.wrangler/`.

From the repo root:

```sh
npx wrangler deploy
```

That uploads everything under `site/` to the `ownchart` Worker. New
content is uploaded; unchanged files are deduped by content hash
(wrangler will say "No updated asset files to upload" in that case —
the manifest still gets refreshed, so the deploy is live).

After deploy, verify:

```sh
curl -sI https://www.ownchart.me/             # should be 200 with a fresh etag
curl -sI https://ownchart.nd-1cb.workers.dev/ # should match
```

The custom domain (`www.ownchart.me`, `ownchart.me`) is wired in the
Cloudflare dashboard. The apex-to-www redirect is a zone-level
Redirect Rule, not a `_redirects` entry (the Static Assets
`_redirects` format can't match by hostname).

## Iterating

`site/` lives on the `dev` branch like the rest of the repo. Push to
`dev` for source-of-truth; **deploys are explicit** (`npx wrangler
deploy`) and gated by PM approval per project doctrine. Do not
auto-deploy on push.

## OG image (one open item)

The current `og-image.svg` is a vector. Most platforms (Slack,
Discord, LinkedIn, X) handle SVG OG images fine, but some older
crawlers prefer PNG. If a clean PNG is preferred, render the SVG once
at 1200×630 and drop it in as `og-image.png`, then update the two
`<meta>` tags in `index.html` to point at the PNG.
