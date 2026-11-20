FROM node:22-bookworm-slim AS web
WORKDIR /build/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY run.py README.txt LICENSE.txt ./
COPY config ./config
COPY data ./data
COPY design ./design
COPY --from=web /build/frontend/dist ./frontend/dist
EXPOSE 8000
CMD ["python", "run.py", "run", "--host", "0.0.0.0"]
