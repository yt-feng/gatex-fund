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

Scheduled collection is active through the pinned `scheduled-snapshot`
workflow. Its daily schedule is gated by the `ENABLE_SNAPSHOT` repository
variable and remains disabled when that variable is absent. A separate CI
workflow performs synthetic offline tests on every source change.

The optional GateX Intelligence source adapters are documented in
[`docs/intelligence-source-intake.md`](docs/intelligence-source-intake.md).
They use separate sealed profiles and cursors from the existing snapshot
workflow, and their intake path does not persist a new full-source bundle.

Source changes are authored and validated locally before they are pushed. The
scheduled workflow may commit only encrypted checkpoints and encrypted export
bundles; it does not rewrite the local source tree or publish plaintext data.

## Runtime inputs

`RUNTIME_AGE_IDENTITY` is always required to open the sealed runtime payloads.
The `token_env` field inside the sealed configuration may be omitted or set to
`null` for a credentialless provider. If it names an environment variable, the
runner requires a non-empty value for that variable before loading the provider.

`EGRESS_PROXY_URI` is optional unless the sealed runtime binds its dynamic
token to `SNAPSHOT_EGRESS_PROXY`. When present, the runner starts a compatible
local client from `sslocal` or `ss-local`, removes the relay URI from the child
environment, and exposes only a loopback SOCKS5 address to the provider process.
The address is also bound to `SNAPSHOT_EGRESS_PROXY` before the runner evaluates
the sealed `token_env` binding, so a provider can require that local endpoint.
The scheduled workflow installs a compatible client on its hosted runner. The
URI belongs in an encrypted repository secret and must not be added to
configuration files, command arguments, logs, or commits.

Sealed providers may request a browser runtime for page-level verification.
The scheduled workflow installs a pinned Playwright/Chromium pair and runs it
under Xvfb. Browser state is accepted only through the encrypted checkpoint.

## Repository layout

```text
.github/workflows/       Pinned offline CI and scheduled runner
recipients/              Public encryption recipients
sealed/                  Encrypted runtime payloads and checkpoint
src/snapshot_pipeline/   Provider-neutral runner and audit code
tests/fixtures/          Synthetic offline fixtures
vault/                   Encrypted export bundles
workflow-templates/      Historical deployment templates
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
