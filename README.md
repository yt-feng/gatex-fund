# Scheduled Source Archive

A resumable scheduled snapshot runner. Runtime configuration, provider logic,
checkpoints, and generated datasets are sealed before they enter the
repository.

## Public boundary

- Source-specific configuration is stored only as an encrypted payload.
- Provider-specific code is stored only as an encrypted overlay.
- Checkpoints are encrypted with a runtime recipient.
- Export bundles are encrypted with a separate offline recipient.
- Workflow logs expose only stage names, status classes, and counts.
- Tests use synthetic fixtures under `example.invalid` and require no secrets.
- Credentials are optional unless the sealed configuration names a token environment variable.
- An optional relay URI can be converted to a short-lived loopback SOCKS5 endpoint.

The scheduled workflow runs in a temporary directory with restrictive file
permissions. Plaintext is removed before the job exits. Only encrypted state
and encrypted export bundles are committed.

Collection is not active in the public scaffold. The workflow is stored under
`workflow-templates/` and must be promoted explicitly after an adapter review.
Its daily schedule is also gated by the `ENABLE_SNAPSHOT` repository variable,
which is disabled when the variable is absent. The only active workflow
performs synthetic offline tests.

## Runtime inputs

`RUNTIME_AGE_IDENTITY` is always required to open the sealed runtime payloads.
The `token_env` field inside the sealed configuration may be omitted or set to
`null` for a credentialless provider. If it names an environment variable, the
runner requires a non-empty value for that variable before loading the provider.

`EGRESS_PROXY_URI` is optional. When present, the runner starts a compatible
local client from `sslocal` or `ss-local`, removes the relay URI from the child
environment, and exposes only a loopback SOCKS5 address to the provider process.
The address is also bound to `SNAPSHOT_EGRESS_PROXY` before the runner evaluates
the sealed `token_env` binding, so a provider can require that local endpoint.
The inactive workflow template installs a compatible client on its hosted
runner. The URI belongs in an encrypted repository secret and must not be added
to configuration files, command arguments, logs, or commits.

## Repository layout

```text
.github/workflows/       Pinned synthetic offline CI
recipients/              Public encryption recipients
sealed/                  Encrypted runtime payloads and checkpoint
src/snapshot_pipeline/   Provider-neutral runner and audit code
tests/fixtures/          Synthetic offline fixtures
vault/                   Encrypted export bundles
workflow-templates/      Inactive workflow template
```

## Local validation

```bash
python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m snapshot_pipeline.cli guard --root .
bash -n scripts/run_scheduled.sh
```

## Decrypting an export

Install the pinned encryption tool and supply the offline identity explicitly:

```bash
python3 scripts/install_age.py --bin-dir .local/bin
.local/bin/age -d -i /path/to/offline-identity vault/xx/example.tar.age > bundle.tar
tar -tf bundle.tar
```

Private identities must never be added to this repository.
