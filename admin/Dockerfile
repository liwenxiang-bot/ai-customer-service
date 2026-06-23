# syntax=docker/dockerfile:1
# Build context = repository root.

FROM node:20-alpine AS build
WORKDIR /admin
RUN npm i -g pnpm@10
COPY admin/package.json admin/pnpm-lock.yaml* ./
RUN pnpm install --no-frozen-lockfile
COPY admin/ ./
RUN pnpm build

FROM nginx:1.27-alpine
COPY admin/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /admin/dist /usr/share/nginx/html
EXPOSE 80
