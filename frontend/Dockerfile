# Development Dockerfile for Shizuha Pulse Frontend
FROM node:20-alpine

WORKDIR /app

# Install the reviewed local UI package and public locked dependencies.
COPY package*.json ./
COPY vendor/ ./vendor/
RUN npm ci --no-audit --no-fund

# Copy source code
COPY . .

# Expose Vite dev server port
EXPOSE 80

# Start development server
CMD ["npm", "run", "dev"]
