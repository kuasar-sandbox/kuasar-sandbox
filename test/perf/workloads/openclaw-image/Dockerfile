FROM node:22-slim

# Install system utilities: Python 3, git, ripgrep, procps
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-minimal \
    git \
    ripgrep \
    procps \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install openclaw globally
RUN npm install -g openclaw@2026.8.1 --omit=dev --no-audit --no-fund

# Create benchmark workspace with a git repository and code files
WORKDIR /workspace/repo
RUN git config --global user.email "agent@openclaw.local" && \
    git config --global user.name "OpenClaw Agent" && \
    git init && \
    mkdir -p src tests && \
    echo 'def add(a, b): return a + b\ndef sub(a, b): return a - b\ndef mul(a, b): return a * b\ndef div(a, b): return a / b if b != 0 else None' > src/calc.py && \
    echo 'import unittest\nfrom src.calc import add, sub, mul, div\n\nclass TestCalc(unittest.TestCase):\n    def test_add(self): self.assertEqual(add(2, 3), 5)\n    def test_sub(self): self.assertEqual(sub(5, 2), 3)\n    def test_mul(self): self.assertEqual(mul(3, 4), 12)\n    def test_div(self): self.assertEqual(div(10, 2), 5)\n\nif __name__ == "__main__": unittest.main()' > tests/test_calc.py && \
    git add . && \
    git commit -m "initial commit"

COPY run_openclaw_session.mjs /opt/run_openclaw.mjs
RUN chmod +x /opt/run_openclaw.mjs

WORKDIR /workspace
