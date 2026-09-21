# ProcessGuard AI — frontend image
# Build stage compiles the Vite bundle; runtime stage serves it with the
# UNPRIVILEGED nginx image (non-root by default, listens on 8080) so the
# whole stack runs as non-root containers.
#
# Prompt 7: at container start the entrypoint injects the OIDC configuration
# (provider URL the BROWSER should use + client id) into a small runtime
# config file, so the same built bundle works in any environment without a
# rebuild (Vite env vars are build-time; this is the runtime equivalent).
FROM node:22-alpine AS build
WORKDIR /build

COPY package.json package-lock.json* ./
RUN npm install

COPY . .
RUN npm run build

# ---------------------------------------------------------------------------
FROM nginxinc/nginx-unprivileged:1.27-alpine AS final

USER root

COPY --from=build /build/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY docker-entrypoint.d/40-oidc-config.sh /docker-entrypoint.d/40-oidc-config.sh
RUN chmod +x /docker-entrypoint.d/40-oidc-config.sh \
    && chown -R 101:0 /usr/share/nginx/html /etc/nginx/conf.d \
    && chmod -R g+rwX /usr/share/nginx/html /etc/nginx/conf.d

# Image default USER 101 (non-root) applies again below this line.
USER 101

EXPOSE 8080
