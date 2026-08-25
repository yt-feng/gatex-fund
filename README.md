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

The scheduled workflow runs in a temporary directory with restrictive file
permissions. Plaintext is removed before the job exits. Only encrypted state
and encrypted export bundles are committed.

Collection is not active in the public scaffold. The workflow is stored under
`workflow-templates/` and must be promoted explicitly after an adapter review.
The only active workflow performs synthetic offline tests.

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
```

## Decrypting an export

Install the pinned encryption tool and supply the offline identity explicitly:

```bash
python3 scripts/install_age.py --bin-dir .local/bin
.local/bin/age -d -i /path/to/offline-identity vault/xx/example.tar.age > bundle.tar
tar -tf bundle.tar
```

Private identities must never be added to this repository.
