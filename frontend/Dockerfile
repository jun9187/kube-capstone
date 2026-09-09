FROM nginx:1.27-alpine

COPY templates/default.conf.template /etc/nginx/templates/default.conf.template
COPY index.html /usr/share/nginx/html/index.html
COPY app.js /usr/share/nginx/html/app.js

ENV API_HOST=api
ENV API_PORT=8000

EXPOSE 80
