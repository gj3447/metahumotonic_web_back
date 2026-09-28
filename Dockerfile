FROM node:24.21.0-bookworm-slim AS build
WORKDIR /build
COPY ts/package.json ts/package-lock.json ts/.node-version ./
COPY ts/scripts ./scripts
RUN npm ci --no-audit --no-fund
COPY ts/tsconfig.json ./
COPY ts/src ./src
COPY ts/test ./test
COPY ts/config ./config
RUN npm run build && npm prune --omit=dev --no-audit --no-fund

FROM node:24.21.0-bookworm-slim AS runtime
ENV NODE_ENV=production
WORKDIR /app
COPY --from=build --chown=node:node /build/package.json ./
COPY --from=build --chown=node:node /build/node_modules ./node_modules
COPY --from=build --chown=node:node /build/dist/src ./dist/src
COPY --from=build --chown=node:node /build/dist/config ./dist/config
COPY --from=build --chown=node:node /build/scripts/platform-db.mjs ./scripts/platform-db.mjs
USER node
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD node -e "fetch('http://127.0.0.1:8000/ready').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
CMD ["node", "dist/src/main.js"]
