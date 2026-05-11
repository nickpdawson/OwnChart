# OwnChart web container — Next.js 15 standalone build
FROM node:22-bookworm-slim AS deps
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY package.json package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

FROM node:22-bookworm-slim AS builder
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM node:22-bookworm-slim AS runtime
WORKDIR /app

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000

# node:22 base image already has user `node` at uid 1000.
# We reuse it so files written into the bind-mounted /data are owned by
# administrator on the Maverick host (also uid 1000) — no chown dance needed.
USER node

COPY --from=builder --chown=node:node /app/.next/standalone ./
COPY --from=builder --chown=node:node /app/.next/static ./.next/static
COPY --from=builder --chown=node:node /app/public ./public

EXPOSE 3000
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD node -e "fetch('http://localhost:3000/login').then(r=>{if(r.status>=500)process.exit(1)}).catch(()=>process.exit(1))"

CMD ["node", "server.js"]
