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

## Current state (2026-09-30)

- Knowledge candidates: 4 reusable and eligible, plus 1 site-specific (not a product).
- First product: `approval-gated-automation-kit` 0.1.0. It is a Japanese guide bundle: a
  20-item checklist, a guide to the five automation stages, and an approval-policy config
  example.
  - Check: 0 errors and 0 warnings, 8 sources traced.
  - A release candidate is built and reproducible (`verify` true).
- **PENDING human:**
  - the quality review;
  - the content approval (H4);
  - the release gate (H5).

  Claude does not record these.
