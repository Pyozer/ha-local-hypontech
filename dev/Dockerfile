# Clean environment to run the repository checks (see dev/check.sh) without installing anything
# on the host. The sources are not copied in: docker-compose.yaml mounts them read-only.
FROM python:3.12-slim

# The tool versions are shared with the CI workflow.
COPY requirements.txt /tmp/requirements.txt
RUN apt-get update \
    && apt-get install -y --no-install-recommends shellcheck \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r /tmp/requirements.txt

# The sources are read-only: no bytecode next to them, and the coverage data goes to /tmp.
ENV PYTHONDONTWRITEBYTECODE=1 \
    COVERAGE_FILE=/tmp/.coverage

WORKDIR /repo
CMD ["dev/check.sh"]
