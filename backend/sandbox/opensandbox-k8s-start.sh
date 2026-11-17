echo -ne "\033]0;OpenSandbox-server-Kubernetes\007"
pushd "$(dirname "$0")" > /dev/null || exit 1

# The server is the backend's opensandbox-server group: (cd .. && uv sync --group opensandbox-server)
OPENSANDBOX_SERVER_API_KEY=my-super-secret-local-key-123 ../.venv/bin/opensandbox-server --config ./sandbox.k8s.firecracker.toml
