FROM python:3.12-slim

WORKDIR /app

# Install uv for fast dependency management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency files first for Docker layer caching
COPY pyproject.toml uv.lock* ./

# Install dependencies
RUN uv sync --frozen --no-dev || uv sync --no-dev

# Copy application code
COPY . .

# Run the agent in start mode (production)
CMD ["uv", "run", "python", "main.py", "start"]
