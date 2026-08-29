# 3.13, not "latest". coincurve (libsecp256k1, a runtime dependency since the
# hand-rolled BIP-340 arithmetic was deleted) publishes wheels for cp39-cp313
# only -- no abi3, no cp314. A newer base image still BUILDS here, but only by
# compiling coincurve from source. See the note beside the dependency in
# pyproject.toml before changing this line.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

# --only-binary :all: is a guard, not an optimisation. Every dependency in this
# image has a wheel for cp313 on both amd64 and arm64; if that ever stops being
# true the build FAILS here instead of quietly compiling from source and
# producing a slower, larger image nobody inspected. It is also what lets this
# stage run without build-essential.
RUN pip install --no-cache-dir --only-binary :all: '.[arb-memory,arb-files,arb-email]'

ENTRYPOINT ["python", "-m", "arb_memory"]
