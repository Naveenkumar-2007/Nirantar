# Nirantar dashboard + customer pay page (Next.js standalone output), non-root.
FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts && cp -r node_modules /prod_modules && npm ci --ignore-scripts

FROM node:22-alpine AS build
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

FROM node:22-alpine
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0
# the server needs only `node`: drop the package managers (and their dependency trees) from the runtime image
RUN addgroup -S -g 10001 nirantar && adduser -S -u 10001 -G nirantar nirantar \
    && rm -rf /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/corepack /usr/local/bin/npm \
       /usr/local/bin/npx /usr/local/bin/corepack /opt/yarn-* /usr/local/bin/yarn /usr/local/bin/yarnpkg
COPY --from=build --chown=nirantar:nirantar /app/.next/standalone ./
COPY --from=build --chown=nirantar:nirantar /app/.next/static ./.next/static
COPY --from=build --chown=nirantar:nirantar /app/public ./public
USER nirantar
EXPOSE 3000
CMD ["node", "server.js"]
