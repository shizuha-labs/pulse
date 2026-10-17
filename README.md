# Shizuha Pulse

Tasks, projects, workflows, alerts and agent work coordination for the Shizuha platform.

## Deploy the platform

Use the [platform deployment repository](https://github.com/shizuha-labs/deploy)
for the coordinated installation of ID, Hive, Pulse, Connect, Wiki and Atlas.
Service repositories provide application source; deployment configuration owns
hostnames, credentials, storage and inter-service trust.

Django API and React frontend; PostgreSQL, Redis and Shizuha ID.

Configuration is read by [`taskmanager/settings.py`](taskmanager/settings.py). Supply deployment-specific
credentials and endpoints through the environment. Existing production-cluster
manifests and internal CI workflows are not an external installation recipe.

Pulse-specific environment variables are documented in [self-host configuration](docs/self-host-configuration.md).

## Development

Backend dependencies are declared in `pyproject.toml` and `uv.lock`;
frontend dependencies are in `frontend/package.json` and its lockfile.
The repository's `.forgejo/workflows/premerge.yml` documents its test recipe.
Run tests against isolated databases and service dependencies.

## License

Original code is available under the [MIT License](LICENSE). Third-party
components retain their respective licenses and notices. Dependency licenses
are not replaced by this repository's license.
