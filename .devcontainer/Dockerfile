# Pick a base that fits the project, e.g. mcr.microsoft.com/devcontainers/python:1-3.12-bookworm
FROM mcr.microsoft.com/devcontainers/python:1-3.12-bookworm

# --- Claude Code (native installer; auto-updates). Kept early so project layers below don't reinstall it.
USER vscode
RUN curl -fsSL https://claude.ai/install.sh | bash && mkdir -p /home/vscode/.claude
USER root
ENV PATH=/home/vscode/.local/bin:$PATH \
    CLAUDE_CONFIG_DIR=/home/vscode/.claude

# --- Project setup below (paths are relative to the project root).
# The web app (app/server.py) is standard-library Python; nothing to install.
EXPOSE 8420
