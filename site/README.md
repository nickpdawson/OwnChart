# OwnChart landing page

Static landing page for `www.ownchart.me`. No build step; pure HTML + CSS.

## Structure

```
site/
├── index.html      Landing page
├── styles.css      All styling
├── favicon.svg     Browser tab icon (four-node mark)
├── og-image.svg    Social preview (1200×630)
├── _headers        Cloudflare Pages security headers
├── _redirects      Short-link redirects to GitHub doc paths
├── robots.txt
└── sitemap.xml
```

## Deploying via Cloudflare Pages (one-time setup)

1. Cloudflare dashboard → **Workers & Pages** → **Create application** → **Pages** → **Connect to Git**.
2. Authorize Cloudflare to access `nickpdawson/OwnChart`.
3. Build settings:
   - **Production branch:** `main`
   - **Framework preset:** None
   - **Build command:** *(leave empty)*
   - **Build output directory:** `site`
   - **Root directory:** *(leave empty)*
4. Deploy. First build takes ~30 seconds.
5. Once it's live, **Custom domains** → add `www.ownchart.me` and `ownchart.me`. Cloudflare auto-configures DNS if the domain is on the same account.

## Iterating

Every push to `main` that touches `site/` triggers a fresh deploy. Preview deployments are generated for non-main branches automatically.

## OG image (one open item)

The current `og-image.svg` is a vector. Most platforms (Slack, Discord, LinkedIn, X) handle SVG OG images fine, but some older crawlers prefer PNG. If a clean PNG is preferred, render the SVG once at 1200×630 and drop it in as `og-image.png`, then update the two `<meta>` tags in `index.html` to point at the PNG. The HTML already references `og-image.png`; if no PNG exists, browsers will fall back to the SVG via `og:image` extension fallback.

## When the demo goes live

The `Try the demo` / `Open the demo` buttons link to `https://demo.ownchart.me/` and carry a small `coming soon` chip. To flip live:

1. Confirm `demo.ownchart.me` is responding.
2. In `index.html`, delete both `<span class="chip">coming soon</span>` occurrences.
3. Push to `main`. Cloudflare rebuilds in ~30s.
