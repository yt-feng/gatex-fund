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

The primary archive uses the verified TikHub source profile. It checks the
latest article page on every run, skips stable document identities already in
the archive checkpoint, and preserves pending candidates and a separate scan
cursor when the ten-article daily limit is reached. The existing archive state
is retained during this migration. Complete API text, rich HTML, raw API detail,
cover images and inline images are sealed in the existing export format before
the checkpoint advances. The primary workflow requires `TIKHUB_WECHAT_TOKEN`;
an absent credential fails explicitly instead of skipping collection.

When a discovery candidate requires verification, it is deferred while later
candidates are checked for attributable articles. Known anchors after that
block cannot hide later candidates. Discovery stops once the configured number
of new attributable documents is available, and complete articles can be saved
as a partial batch. The encrypted checkpoint
advances only for articles whose identity, content, and assets are complete;
deferred candidates stay eligible for a later run. After the batch and
checkpoint are committed, the workflow reports a `partial` outcome with a
warning and deferred count. A typed verification block during discovery is
retried twice, after 30 and 90 seconds, from the original checkpoint and a fresh
batch directory. This does not retry detail or asset failures, or republish a
completed batch. Verification blocks that exhaust these attempts with no
completed new items, and other collection or persistence failures, still fail
explicitly. Failed attempt files remain inside the private diagnostic area.
Each discovery attempt has a three-minute deadline and uses the same bounded
retries when it stalls. The deadline ends when discovery completes or detail
collection begins. Exhausted deadlines fail with a `timeout` status, preserving
the original checkpoint and leaving time to seal diagnostics before job expiry.
Tool downloads retry transient HTTP and transport failures twice, after five
and fifteen seconds; every successful download still requires its pinned hash.
The primary and secondary archive profiles share one generic provider release.
When upgrading it, update both `sealed/provider-overlay.py.age` and
`sealed/archive-sources/source-b/provider-overlay.py.age` with the same verified
encrypted provider. Keep their configurations and checkpoints separate. CI
checks that these two provider files match; separate intelligence adapters are
outside this contract.
Browser recovery rejects error and verification pages before requesting their
response bodies, and skips redirect and verification response bodies entirely.
After a verification failure, remaining discovery candidates use normal requests
without repeating browser recovery during that pass. The encrypted regression
suite is `sealed/provider-recovery-tests.py.age`; decrypt it beside the provider
in a private working directory before running it with Python.
Resuming a partial batch bypasses the known-article early stop. Discovered
articles beyond the per-run limit retain stable pending identities until
completed, even when temporarily absent from a later discovery page.

An additional encrypted profile may run through the pinned
`secondary-snapshot` workflow. It uses a separate checkpoint and vault, runs
in archive-only mode, and shares the same non-cancelling concurrency group as
the primary snapshot so encrypted state updates are serialized. Its schedule
is independently gated by the `ENABLE_SECONDARY_SNAPSHOT` repository variable;
manual dispatch remains available from the default branch.
It uses the independently verified `source-b-archive` TikHub profile. That
archive profile is separate from the disabled generic source-b intake profile.
Article navigation URLs are normalized to HTTPS with only the preserved document
identity and optional signature. Detail requests alternate between H5 and native
endpoints for at most four attempts within the run's call and time budgets.
Profile and page requests allow three attempts. Credential, balance and identity
errors stop the run. Account, document, full-body and image checks remain required.
Temporarily unavailable articles retain durable retry metadata with exponential
cooldown, while healthy articles and the independent scan cursor can progress.
Partial runs report an upstream warning after encrypted state and archive
persistence. An identity-verified upstream outage may save zero new articles and
its retry checkpoint as an explicit partial result; an unverified empty result,
captcha or persistence failure still fails.

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

The primary and secondary archive workflows use their verified API profiles
without installing or launching a browser. Legacy browser-provider state is
accepted only through an encrypted checkpoint.

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
