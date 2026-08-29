# GateX Intelligence source intake

This repository owns the runnable source collectors. The GateX web application
owns intake persistence, report generation, review, and publication. The
collectors never publish a report directly.

## Source profiles

Public source identity is kept inside sealed profiles. Do not add account names,
identifiers, article text, credentials, or private URLs to plaintext files in
this repository.

- `source-a`: verified and enabled. Its incremental Sogou cursor starts from the
  existing sealed checkpoint so a migration does not replay the full archive.
  Its TikHub backfill cursor is independent and resumable.
- `source-b`: disabled and fail-closed. The public display identity is known,
  and a candidate TikHub username is sealed, but the TikHub profile has not been
  verified. Both collection paths reject the profile until the profile API
  returns the exact username and display-name pair and `enabled` is set.

## Delivery contract

Every article becomes one `gatex-intelligence-intake/v1` envelope. The schema is
in `schemas/gatex-intelligence-intake.schema.json`. Stable article identity and
content hashes produce deterministic `externalId` and `idempotencyKey` values.
Only the stable WeChat URL parameters, source and author attribution, publication
time, deletion state, hashes, and short excerpts enter the source/admin ledger.
An active envelope also carries `privateDocument` with canonical UTF-8 text,
its SHA-256, and an exact text MIME type. That field is generation-only: the
Worker moves it to private object storage, removes it from persisted JSON, and
exposes it only through the authenticated generation callback. The Worker gives
that object a fixed 14-day generation retention window and its scheduled cleanup
deletes the object after expiry. It is not a new long-term source bundle. The
pre-existing sealed snapshot archive remains a separate workflow.

The current two adapters emit `language=zh`, `accessScope=member`,
`provenanceType=source_channel`, and `source.kind=social`. The reusable envelope
schema also permits its documented `en` and alternate access-scope values. The
local validator rejects incompatible aliases before dry-run can pass.

`externalId` is derived only from the canonical article URL and is therefore
stable across Sogou and TikHub. `contentHash` and `privateDocument.sha256` are
the SHA-256 of the canonical full text and may differ when provider extraction
differs. GateX merges those observations by `(channelKey,
externalId)` into one durable generation job and by canonical URL into one
source record; a changed hash remains visible as a new observation.

The envelope records an explicit `accepted` or `withdrawn` source status and
requests an original GateX scan. It requires independent fact
checking, attribution, a maximum 180-character source excerpt, draft-only
creation, and human approval. Removed sources are marked `withdrawn`, force
`triggerDraft=false`, omit `privateDocument`, delete or block any existing
generation object, and remain available as a deletion audit event.

The only allowed delivery target is:

`https://gatex.fund/api/integrations/intelligence/intake`

`dry-run` validates and counts envelopes without opening a network connection.
`post` requires `GATEX_INTELLIGENCE_INTAKE_SECRET` and sends the idempotency key in
both the envelope and request header. The delivery client rejects every redirect
so its bearer secret is never replayed to a redirected URL.
Scheduled `post` mode validates that secret and the exact endpoint before source
collection, including runs that discover zero new articles.

## Incremental collection

`.github/workflows/intelligence-source-incremental.yml` reuses the hardened
Sogou provider and encrypted checkpoint. Its plaintext capture exists only in
the temporary runner directory and is erased on exit. Scheduled execution is gated by the
repository variable `ENABLE_INTELLIGENCE_SOURCE_INGEST=true`. Manual dispatch
defaults to `dry-run`. Delivery completes before the encrypted cursor advances,
so a failed POST can be retried without losing the article.
All checkpoint-writing workflows share one non-cancelling concurrency group so
the existing archive, incremental intake, and manual backfill cannot race a push.

## Historical backfill

`.github/workflows/intelligence-source-backfill.yml` is manual only. It verifies
the sealed TikHub profile on every page, fetches at most 50 details, preserves a
pending list and `next_offset`, and commits the encrypted cursor only after all
GateX POSTs succeed. Dry-run never advances the cursor. It requires a
`TIKHUB_WECHAT_TOKEN` authorized for TikHub WeChat MP V2 profile, article list,
and article detail endpoints.

Collection, identity, and delivery failures fail the Actions job with a generic
stage and error class. They do not advance the encrypted checkpoint, so the same
source can be replayed safely. Intelligence failure diagnostics contain only a
sealed generic summary; they never package the full runner capture.

## Activation checklist

1. Deploy the GateX intake endpoint and its database migration.
2. Add `GATEX_INTELLIGENCE_INTAKE_SECRET` as a GitHub Actions secret.
3. Set `GATEX_INTELLIGENCE_INTAKE_URL` to the exact allowed endpoint.
4. Manually run each enabled profile in `dry-run` mode.
5. Set `ENABLE_INTELLIGENCE_SOURCE_INGEST=true` only after dry-run validation.
6. Keep source-b disabled until its exact TikHub username is independently
   verified and sealed.

Source-b identity preflight is read-only and does not require the profile to be
enabled:

```bash
gh workflow run intelligence-source-identity-preflight.yml \
  --repo yt-feng/gatex-fund \
  -f profile=source-b
```

After that job reports an exact username and display-name match, update and seal
the profile as verified/enabled, then validate both collection paths without
advancing either cursor:

```bash
gh workflow run intelligence-source-incremental.yml \
  --repo yt-feng/gatex-fund \
  -f profile=source-b \
  -f mode=dry-run

gh workflow run intelligence-source-backfill.yml \
  --repo yt-feng/gatex-fund \
  -f profile=source-b \
  -f maximum_items=1 \
  -f mode=dry-run
```

These commands require GitHub secrets `RUNTIME_AGE_IDENTITY` and
`TIKHUB_WECHAT_TOKEN`. The latter must include WeChat Search V2 and WeChat MP V2
profile, article-list, and article-detail access. POST delivery additionally
requires `GATEX_INTELLIGENCE_INTAKE_SECRET` and the exact
`GATEX_INTELLIGENCE_INTAKE_URL` variable.

No workflow in this change has been dispatched, pushed, or deployed.
