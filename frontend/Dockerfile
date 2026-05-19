FROM node:18-alpine AS builder

WORKDIR /app

ARG BACKEND_INTERNAL_URL
ARG UPLOAD_PROXY_TIMEOUT_MS
ENV BACKEND_INTERNAL_URL=${BACKEND_INTERNAL_URL}
ENV UPLOAD_PROXY_TIMEOUT_MS=${UPLOAD_PROXY_TIMEOUT_MS}

COPY package*.json ./
RUN npm ci

COPY . .
RUN npm run build

FROM node:18-alpine AS runner

WORKDIR /app

ARG BACKEND_INTERNAL_URL
ARG UPLOAD_PROXY_TIMEOUT_MS

ENV NODE_ENV=production
ENV BACKEND_INTERNAL_URL=${BACKEND_INTERNAL_URL}
ENV UPLOAD_PROXY_TIMEOUT_MS=${UPLOAD_PROXY_TIMEOUT_MS}

COPY --from=builder /app/package*.json ./
RUN npm ci --omit=dev && npm cache clean --force
COPY --from=builder /app/.next ./.next
COPY --from=builder /app/public ./public
COPY --from=builder /app/next.config.mjs ./next.config.mjs

EXPOSE 3000

CMD ["npm", "run", "start"]