# Products: N4 Productized Knowledge + N5 Digital Product Automation

This page covers the local product lifecycle only:

candidate → spec + assets → checks → human quality review → human content approval →
reproducible build → release candidate → human release gate

**Nothing is published, listed, uploaded or sold.** Distribution is a separate workstream (see
[n-track-plan.md](n-track-plan.md)). No distribution provider is assumed.

- Code:
  - `app/products/spec.py` (spec, traceability, redaction, quality)
  - `app/products/records.py` (human records)
  - `app/products/build.py` (build, manifest, validation, staleness)
  - `app/products/candidates.py` (knowledge candidates)
- CLI: `scripts/manage_products.py`
- Policy: `app/config/product_policy.json`
- Tests: `tests/unit/test_products.py`
- Products (committed): `products/<id>/`
  - `product.json`
  - `assets/`
  - `CHANGELOG.md`
  - `records.json` (append-only human records)
- Release candidates (git-ignored): `reports/products/<id>/<version>/`
  - `<id>-<version>.zip`
  - `candidate.json`

## N4: from evidence to a product

- `manage_products.py candidates` lists knowledge candidates. Each one states:
  - its target user;
  - the problem it addresses;
  - the intended outcome;
  - its evidence (decisions, complete phases, doc phrases), checked for presence;
  - whether it is reusable.

  Site-specific material (for example this site's runbook) is never a product.
- A product is **an evidence-backed generalization, never a copy of the development log.**
  - Every asset lists the `doc:` / `decision:` / `phase:` sources it generalizes; `doc:` sources
    can pin a phrase.
  - Sources live in the spec, not in the reader-facing text.
- `check <id>` treats each of these as an error:
  - a missing source, or a pinned phrase that is gone;
  - secrets or internal values (the same patterns as the note checks);
  - site-specific terms, from the policy list plus the WordPress host in the local settings (the
    host is read at check time and never written);
  - unfinished markers;
  - revenue claims without records;
  - undeclared files;
  - site-specific assets;
  - a `distribution` field;
  - invalid JSON config examples;
  - a missing required field.

  Causal or hype wording and internal phase codes are warnings.
- The **content hash** covers the spec (without the version) and the asset contents.
- The human gates are ordered, and each is bound to that hash:
  1. `review <id> --by <name> --confirm-all`: the quality review. It confirms these items:
     - target user is clear;
     - useful without this site;
     - claims match sources;
     - no internal or personal data;
     - no unobserved results.
  2. `approve-content <id> --content-hash <sha> --by <name>`: ready for release. This is **not**
     a sale or a publication.

## N5: keeping products current and reproducible

- `plan <id>` compares the current sources and assets with the last released manifest:
  - `stale` means a source changed, so the affected assets need review;
  - `content_changed` means an asset or the spec changed, and suggests a bump.

  The human bumps the version (semver) and adds a `## <version>` section to `CHANGELOG.md`.
- `build <id>` writes a release candidate.
  - The zip has sorted entries, a fixed 1980-01-01 timestamp, fixed permissions and LF text, so
    the same inputs give the same bytes.
  - `MANIFEST.json` has no timestamps. It records:
    - file hashes;
    - source hashes;
    - the content hash;
    - the build input hash.
  - Validation checks:
    - only declared files;
    - text files only;
    - a size limit;
    - a redaction scan of every file;
    - the changelog section;
    - that the version is newer than the last release.
- `verify <id>` rebuilds and compares the bytes.
- `approve-release <id> --manifest-hash <sha> --by <name>` is the human release gate. It
  requires:
  - a content approval for the same content hash;
  - a release candidate that still matches a rebuild;
  - a clean validation;
  - a version not already released.

  It records the manifest in `records.json`. Distribution is still a separate human decision.

## Review packet (for the human H4 / H5 decisions)

`manage_products.py packet <id>` writes `reports/products/<id>/<version>/review-packet.md`. It
covers:

- target user, problem and outcome;
- the included files (bytes and hashes);
- the evidence / source mapping, with whether each source resolves;
- reusable knowledge and the excluded site-specific material;
- the redaction and quality results;
- the reproducibility check;
- the known limitations;
- the expected distribution format;
- what to check for the H4 quality review, the H4 content approval and the H5 release approval,
  with the exact commands and hashes.

It approves nothing.

## Current state (2026-09-30)

All four products are 0.1.0. Each checks with 0 errors and 0 warnings, has a reproducible
release candidate and has a review packet.

| Product | Assets | Sources traced |
|---|---|---|
| `approval-gated-automation-kit` | 20-item checklist, five-stage guide, approval-policy config example | 8 |
| `actionable-alerting-playbook` | alerting guide, alerting checklist | 4 |
| `project-state-handoff-template` | "current state" report template, how-to guide | 4 |
| `small-sample-measurement-guide` | small-sample guide, "before you write a number" checklist | 3 |

Every spec now states its `known_limitations`.

**PENDING human, for every product:** the quality review and content approval (H4) and the
release gate (H5). Claude does not record them.

## First paid product (2026-10-01)

[first-paid-product.md](first-paid-product.md) ranks the candidates for a first paid piece (the
top one is `approval-gated-automation-kit`). It also draws the free / paid boundary, drafts a
paid note structure, and lists the gate for publishing a paid piece. That gate does **not** wait
for N3 unless the copy uses N3 numbers. Nothing is priced, listed, approved or sold.

On 2026-10-01 all four release candidates still rebuilt to the same bytes (`verify`), and none
is stale. The review packet of `approval-gated-automation-kit` 0.1.0 was regenerated (manifest
`8c551ed0…`).

On 2026-10-01 the human chose `approval-gated-automation-kit` 0.2.0 for a note paid article (initial
price 500 JPY as an operating value, replacing the earlier 980 JPY plan; license in `assets/license.md`). 0.2.0 adds content-hash file
approval to the checklist (now 25 items), guide and config example. It checks with 0 errors and
0 warnings, and its build is reproducible (manifest `65ac6fbb…`). H4 / H5 are still pending. The asset
kind `license` was added.
