# syntax=docker/dockerfile:1.7
# ACVM operations console — multi-stage build, Next.js standalone output, non-root runtime.
#
#   docker build -t acvm-console \
#     --build-arg NEXT_PUBLIC_ACVM_API=https://acvm.example.com/api/v1 .
#   docker run --rm -p 3000:3000 acvm-console
#
# NEXT_PUBLIC_* are inlined at build time, so the API URL is a build argument, not a runtime env.
ARG NODE_IMAGE=node:24-alpine

# ---------------------------------------------------------------- deps
FROM ${NODE_IMAGE} AS deps
WORKDIR /app
RUN apk add --no-cache libc6-compat
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

# ---------------------------------------------------------------- build
FROM ${NODE_IMAGE} AS builder
WORKDIR /app
ARG NEXT_PUBLIC_ACVM_API=http://localhost:8000/api/v1
ARG NEXT_PUBLIC_ACVM_MOCK=0
ENV NEXT_PUBLIC_ACVM_API=${NEXT_PUBLIC_ACVM_API} \
    NEXT_PUBLIC_ACVM_MOCK=${NEXT_PUBLIC_ACVM_MOCK} \
    NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

# ---------------------------------------------------------------- runtime
FROM ${NODE_IMAGE} AS runner
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0
RUN addgroup -S -g 1001 nodejs && adduser -S -u 1001 -G nodejs nextjs
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
COPY --from=builder --chown=nextjs:nodejs /app/public ./public
USER nextjs
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD wget -qO- http://127.0.0.1:3000/robots.txt >/dev/null || exit 1
CMD ["node", "server.js"]
