FROM node:22-alpine

WORKDIR /app

COPY Project/app/frontend/package.json Project/app/frontend/package-lock.json ./
RUN npm ci

COPY Project/app/frontend ./

EXPOSE 5173

CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "5173"]
