# Daily complete editions

The complete-edition pipeline is separate from generic intelligence intake and
uses only the verified source identity authorized by the site owner. The source
archive remains sealed. Provider names, article bodies, translations, prompts
and delivery responses are not written to Git history or public Action artifacts.

`scheduled-snapshot` durably queues each newly archived approved source before
advancing its archive checkpoint. An unsuccessful enqueue leaves the checkpoint
unchanged, so a subsequent archive run can replay it idempotently. The enabled
publication workflow runs after a successful primary snapshot and at 04:15 UTC
every day. It also supports manual runs with a bounded edition count.

Configure these repository variables after the Worker endpoints are deployed:

- `ENABLE_TECHNOLOGY_FRONTIERS_PUBLICATION=true`
- `GATEX_TECHNOLOGY_SOURCE_BIZ_SHA256`: SHA-256 of the approved publisher identity.
- `GATEX_TRANSLATION_MODEL`: optional text model, default `gpt-4o-mini`.

The workflow uses the dedicated `GATEX_TECHNOLOGY_PUBLICATION_SECRET` and the
`APIMART_API_KEY` repository secrets. The Worker keeps only the SHA-256 hash of
the dedicated publication credential. Generic intake retains its existing secret
and configuration. The local Python adapter supports the old intake credential
as a fallback for compatibility. It does not require storage-admin credentials.
Its service path is `/api/integrations/technology-frontiers` on the GateX site.

The queue keeps ordered source lines, translated-block progress and independent
image-task receipts privately. Every source line must appear once, in order, in
the English edition. A second bilingual editorial pass checks terminology and
qualifier scope against the unchanged source-line map before marking the private
translation as reviewed. The renderer retains recognized comparison tables as
native table cells. A truncated response, missing coverage, untranslated text,
invalid cover or unreadable PDF prevents publication. An unfinished image task
is reused by the next run. Completed reports are published only after the PDF
and cover are stored and verified; replay preserves the first publication time.

The PDF uses native typography, the full translated body, one editorial/source
note inside the body and a detailed standalone final disclaimer page. Its cover
art is text-free so the website can render accessible, responsive native titles.
A disclaimer states the scope of the publication and does not override mandatory
law or imply that every possible liability is excluded.

The daily run processes at most five pending editions by default. A valid empty
queue reports zero; it does not generate filler or repeatedly reissue old articles.
Failures remain pending, fail the Action visibly, and do not prevent other queued
editions in that run from completing. This workflow does not perform subscriber
mail delivery; the website's consent-based daily digest handles new publications.

Run offline verification:

```sh
python3 scripts/test_technology_frontiers_daily.py
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m snapshot_pipeline.cli guard --root .
```

The APIMart `/v1/chat/completions` route defaults to streaming, so the adapter
explicitly requests `stream: false` for its validated JSON responses. See the
[provider reference](https://docs.apimart.ai/en/api-reference/texts/general/chat-completions).
