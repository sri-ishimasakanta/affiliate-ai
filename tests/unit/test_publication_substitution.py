"""app.wordpress.publication_substitution — byte-preserving canonical HTML ->
tracked HTML substitution engine (D-D3)。
"""

from __future__ import annotations

import pytest

from app.article.draft_promotion_canonical import compute_text_hash
from app.wordpress.link_occurrence import compute_occurrence_identity_hash
from app.wordpress.publication_artifact import (
    build_substitution_manifest,
    compute_artifact_hash,
    compute_tracked_html_hash,
    expected_replacement_href,
)
from app.wordpress.publication_substitution import (
    TRACKED_REL,
    PublicationSubstitutionError,
    SelectedSubstitution,
    build_tracked_html,
    reverse_tracked_html,
    validate_tracked_html,
)
from app.wordpress.renderer import RENDERER_VERSION, render_wordpress_html

_TOKEN_A = "TESTTOKENAAAAAAAAAAAA"
_TOKEN_B = "TESTTOKENBBBBBBBBBBBB"


def _render(markdown: str):
    rendered = render_wordpress_html(markdown)
    body_hash = compute_text_hash(markdown)
    return rendered, body_hash


def _select(
    *,
    rendered,
    body_hash: str,
    ordinal: int,
    token: str,
    mapping_id: int,
    target_id: int,
) -> SelectedSubstitution:
    identity = compute_occurrence_identity_hash(
        canonical_body_hash=body_hash,
        renderer_version=RENDERER_VERSION,
        occurrence_ordinal=ordinal,
        original_href=rendered.external_links[ordinal],
    )
    return SelectedSubstitution(
        occurrence_ordinal=ordinal,
        occurrence_identity_hash=identity,
        mapping_id=mapping_id,
        affiliate_link_target_id=target_id,
        token=token,
        target_projection_version=1,
    )


def _build(rendered, body_hash: str, selections):
    return build_tracked_html(
        canonical_html=rendered.html,
        external_links=rendered.external_links,
        canonical_body_hash=body_hash,
        renderer_version=RENDERER_VERSION,
        selections=selections,
    )


def _validate(rendered, body_hash: str, tracked_html: str, manifest) -> None:
    validate_tracked_html(
        canonical_html=rendered.html,
        external_links=rendered.external_links,
        canonical_body_hash=body_hash,
        renderer_version=RENDERER_VERSION,
        tracked_html=tracked_html,
        manifest=manifest,
    )


# ==================== §27: exact selected substitution =====================
def test_zero_substitutions() -> None:
    rendered, body_hash = _render("[a](https://official.example.test/a)\n")
    result = _build(rendered, body_hash, [])
    assert result.manifest == []
    assert result.tracked_html == rendered.html
    _validate(rendered, body_hash, result.tracked_html, result.manifest)


def test_one_substitution() -> None:
    rendered, body_hash = _render("[a](https://official.example.test/a)\n")
    sel = _select(
        rendered=rendered, body_hash=body_hash, ordinal=0, token=_TOKEN_A, mapping_id=1, target_id=1
    )
    result = _build(rendered, body_hash, [sel])
    assert len(result.manifest) == 1
    assert result.tracked_html != rendered.html
    assert expected_replacement_href(_TOKEN_A) in result.tracked_html
    _validate(rendered, body_hash, result.tracked_html, result.manifest)


def test_multiple_substitutions() -> None:
    md = (
        "[a](https://official.example.test/a) "
        "[b](https://official.example.test/b) "
        "[c](https://official.example.test/c)\n"
    )
    rendered, body_hash = _render(md)
    sels = [
        _select(
            rendered=rendered,
            body_hash=body_hash,
            ordinal=0,
            token=_TOKEN_A,
            mapping_id=1,
            target_id=1,
        ),
        _select(
            rendered=rendered,
            body_hash=body_hash,
            ordinal=2,
            token=_TOKEN_B,
            mapping_id=2,
            target_id=2,
        ),
    ]
    result = _build(rendered, body_hash, sels)
    assert len(result.manifest) == 2
    assert [e["occurrence_ordinal"] for e in result.manifest] == [0, 2]
    assert expected_replacement_href(_TOKEN_A) in result.tracked_html
    assert expected_replacement_href(_TOKEN_B) in result.tracked_html
    assert "https://official.example.test/b" in result.tracked_html  # ordinal 1 は無変更
    _validate(rendered, body_hash, result.tracked_html, result.manifest)


def test_non_adjacent_substitutions() -> None:
    md = (
        "# H\n\n[a](https://official.example.test/a)\n\n"
        "some text\n\n[b](https://official.example.test/b)\n\n"
        "more text\n\n[c](https://official.example.test/c)\n"
    )
    rendered, body_hash = _render(md)
    sels = [
        _select(
            rendered=rendered,
            body_hash=body_hash,
            ordinal=0,
            token=_TOKEN_A,
            mapping_id=1,
            target_id=1,
        ),
        _select(
            rendered=rendered,
            body_hash=body_hash,
            ordinal=2,
            token=_TOKEN_B,
            mapping_id=2,
            target_id=2,
        ),
    ]
    result = _build(rendered, body_hash, sels)
    _validate(rendered, body_hash, result.tracked_html, result.manifest)
    assert "some text" in result.tracked_html
    assert "more text" in result.tracked_html


def test_same_url_twice_substitute_first_only() -> None:
    md = "[first](https://official.example.test/x) [second](https://official.example.test/x)\n"
    rendered, body_hash = _render(md)
    sel = _select(
        rendered=rendered, body_hash=body_hash, ordinal=0, token=_TOKEN_A, mapping_id=1, target_id=1
    )
    result = _build(rendered, body_hash, [sel])
    assert len(result.manifest) == 1
    assert result.manifest[0]["occurrence_ordinal"] == 0
    # ordinal 1 (同じ href) は無変更のまま残る。
    assert (
        'href="https://official.example.test/x" target="_blank" rel="noopener noreferrer">second'
        in result.tracked_html
    )
    _validate(rendered, body_hash, result.tracked_html, result.manifest)


def test_same_url_twice_substitute_second_only() -> None:
    md = "[first](https://official.example.test/x) [second](https://official.example.test/x)\n"
    rendered, body_hash = _render(md)
    sel = _select(
        rendered=rendered, body_hash=body_hash, ordinal=1, token=_TOKEN_A, mapping_id=1, target_id=1
    )
    result = _build(rendered, body_hash, [sel])
    assert len(result.manifest) == 1
    assert result.manifest[0]["occurrence_ordinal"] == 1
    assert (
        'href="https://official.example.test/x" target="_blank" rel="noopener noreferrer">first'
        in result.tracked_html
    )
    _validate(rendered, body_hash, result.tracked_html, result.manifest)


def test_same_url_twice_substitute_both_independently() -> None:
    md = "[first](https://official.example.test/x) [second](https://official.example.test/x)\n"
    rendered, body_hash = _render(md)
    sels = [
        _select(
            rendered=rendered,
            body_hash=body_hash,
            ordinal=0,
            token=_TOKEN_A,
            mapping_id=1,
            target_id=1,
        ),
        _select(
            rendered=rendered,
            body_hash=body_hash,
            ordinal=1,
            token=_TOKEN_B,
            mapping_id=2,
            target_id=2,
        ),
    ]
    result = _build(rendered, body_hash, sels)
    assert len(result.manifest) == 2
    assert (
        result.manifest[0]["occurrence_identity_hash"]
        != result.manifest[1]["occurrence_identity_hash"]
    )
    assert expected_replacement_href(_TOKEN_A) in result.tracked_html
    assert expected_replacement_href(_TOKEN_B) in result.tracked_html
    _validate(rendered, body_hash, result.tracked_html, result.manifest)


# ==================== §28: strict non-selected preservation ================
def test_byte_preservation_of_unrelated_content() -> None:
    md = (
        "# Heading One\n\n"
        "Some **bold** and _italic_ text with an entity & more.\n\n"
        "[selected](https://official.example.test/a)\n\n"
        "- item one\n- item two\n\n"
        "| col a | col b |\n| --- | --- |\n| x | y |\n\n"
        "[unselected](https://official.example.test/b)\n\n"
        "[internal](/local/page)\n"
    )
    rendered, body_hash = _render(md)
    sel = _select(
        rendered=rendered, body_hash=body_hash, ordinal=0, token=_TOKEN_A, mapping_id=1, target_id=1
    )
    result = _build(rendered, body_hash, [sel])

    # canonical html の全ての "行" のうち、選択された occurrence の <a> open tag
    # 以外は 1 バイトも変わっていないことを確認する。
    canonical_lines = rendered.html.splitlines()
    tracked_lines = result.tracked_html.splitlines()
    assert len(canonical_lines) == len(tracked_lines)
    changed = [
        i for i, (c, t) in enumerate(zip(canonical_lines, tracked_lines, strict=True)) if c != t
    ]
    assert len(changed) == 1  # 変更されたのは選択したリンクを含む行だけ

    assert "<h2>Heading One</h2>" in result.tracked_html
    assert "<strong>bold</strong>" in result.tracked_html
    assert "<em>italic</em>" in result.tracked_html
    assert "&amp;" in result.tracked_html
    assert "<li>item one</li>" in result.tracked_html
    assert "<table>" in result.tracked_html
    assert "<td>x</td>" in result.tracked_html
    assert 'href="/local/page">internal</a>' in result.tracked_html
    assert (
        'href="https://official.example.test/b" target="_blank" '
        'rel="noopener noreferrer">unselected</a>'
    ) in result.tracked_html
    _validate(rendered, body_hash, result.tracked_html, result.manifest)


# ==================== §29: rel handling ======================================
def test_existing_rel_becomes_exact_approved_tracked_rel() -> None:
    rendered, body_hash = _render("[a](https://official.example.test/a)\n")
    sel = _select(
        rendered=rendered, body_hash=body_hash, ordinal=0, token=_TOKEN_A, mapping_id=1, target_id=1
    )
    result = _build(rendered, body_hash, [sel])
    assert result.manifest[0]["rel_before"] == "noopener noreferrer"
    assert result.manifest[0]["rel_after"] == TRACKED_REL
    assert TRACKED_REL == "sponsored nofollow noopener noreferrer"
    assert f'rel="{TRACKED_REL}"' in result.tracked_html


def test_no_rel_gets_deterministic_tracked_rel() -> None:
    from app.wordpress.publication_substitution import build_tracked_html as _btm

    canonical_html = '<p><a href="https://official.example.test/a" target="_blank">text</a></p>\n'
    external_links = ["https://official.example.test/a"]
    body_hash = "b" * 64
    identity = compute_occurrence_identity_hash(
        canonical_body_hash=body_hash,
        renderer_version=RENDERER_VERSION,
        occurrence_ordinal=0,
        original_href=external_links[0],
    )
    sel = SelectedSubstitution(
        occurrence_ordinal=0,
        occurrence_identity_hash=identity,
        mapping_id=1,
        affiliate_link_target_id=1,
        token=_TOKEN_A,
        target_projection_version=1,
    )
    result = _btm(
        canonical_html=canonical_html,
        external_links=external_links,
        canonical_body_hash=body_hash,
        renderer_version=RENDERER_VERSION,
        selections=[sel],
    )
    assert result.manifest[0]["rel_before"] == ""
    assert result.manifest[0]["rel_after"] == TRACKED_REL
    assert f'target="_blank" rel="{TRACKED_REL}"' in result.tracked_html

    reconstructed = reverse_tracked_html(tracked_html=result.tracked_html, manifest=result.manifest)
    assert reconstructed == canonical_html


def test_reverse_restores_exact_prior_rel_state() -> None:
    rendered, body_hash = _render("[a](https://official.example.test/a)\n")
    sel = _select(
        rendered=rendered, body_hash=body_hash, ordinal=0, token=_TOKEN_A, mapping_id=1, target_id=1
    )
    result = _build(rendered, body_hash, [sel])
    reconstructed = reverse_tracked_html(tracked_html=result.tracked_html, manifest=result.manifest)
    assert reconstructed == rendered.html
    assert 'rel="noopener noreferrer"' in reconstructed


def test_unselected_existing_rel_remains_untouched() -> None:
    md = "[first](https://official.example.test/x) [second](https://official.example.test/x)\n"
    rendered, body_hash = _render(md)
    sel = _select(
        rendered=rendered, body_hash=body_hash, ordinal=0, token=_TOKEN_A, mapping_id=1, target_id=1
    )
    result = _build(rendered, body_hash, [sel])
    # ordinal 1 の rel は元のまま。
    assert result.tracked_html.count('rel="noopener noreferrer"') == 1
    assert result.tracked_html.count(f'rel="{TRACKED_REL}"') == 1


# ==================== §26: parser/tokenizer safety (synthetic) =============
def test_href_attribute_position_first_middle_last() -> None:
    body_hash = "b" * 64
    external_links = ["https://official.example.test/a"]
    identity = compute_occurrence_identity_hash(
        canonical_body_hash=body_hash,
        renderer_version=RENDERER_VERSION,
        occurrence_ordinal=0,
        original_href=external_links[0],
    )
    sel = SelectedSubstitution(
        occurrence_ordinal=0,
        occurrence_identity_hash=identity,
        mapping_id=1,
        affiliate_link_target_id=1,
        token=_TOKEN_A,
        target_projection_version=1,
    )
    variants = [
        '<a href="https://official.example.test/a" target="_blank" rel="noopener noreferrer">t</a>',
        '<a target="_blank" href="https://official.example.test/a" rel="noopener noreferrer">t</a>',
        '<a target="_blank" rel="noopener noreferrer" href="https://official.example.test/a">t</a>',
    ]
    for canonical_html in variants:
        result = build_tracked_html(
            canonical_html=canonical_html,
            external_links=external_links,
            canonical_body_hash=body_hash,
            renderer_version=RENDERER_VERSION,
            selections=[sel],
        )
        assert expected_replacement_href(_TOKEN_A) in result.tracked_html
        assert TRACKED_REL in result.tracked_html
        reconstructed = reverse_tracked_html(
            tracked_html=result.tracked_html, manifest=result.manifest
        )
        assert reconstructed == canonical_html


def test_single_quoted_attributes_supported() -> None:
    body_hash = "b" * 64
    external_links = ["https://official.example.test/a"]
    identity = compute_occurrence_identity_hash(
        canonical_body_hash=body_hash,
        renderer_version=RENDERER_VERSION,
        occurrence_ordinal=0,
        original_href=external_links[0],
    )
    sel = SelectedSubstitution(
        occurrence_ordinal=0,
        occurrence_identity_hash=identity,
        mapping_id=1,
        affiliate_link_target_id=1,
        token=_TOKEN_A,
        target_projection_version=1,
    )
    canonical_html = (
        "<a href='https://official.example.test/a' target='_blank' rel='noopener noreferrer'>t</a>"
    )
    result = build_tracked_html(
        canonical_html=canonical_html,
        external_links=external_links,
        canonical_body_hash=body_hash,
        renderer_version=RENDERER_VERSION,
        selections=[sel],
    )
    assert "target='_blank'" in result.tracked_html  # 無関係な属性の quote style は無変更
    reconstructed = reverse_tracked_html(tracked_html=result.tracked_html, manifest=result.manifest)
    assert reconstructed == canonical_html


def test_extra_attributes_preserved() -> None:
    body_hash = "b" * 64
    external_links = ["https://official.example.test/a"]
    identity = compute_occurrence_identity_hash(
        canonical_body_hash=body_hash,
        renderer_version=RENDERER_VERSION,
        occurrence_ordinal=0,
        original_href=external_links[0],
    )
    sel = SelectedSubstitution(
        occurrence_ordinal=0,
        occurrence_identity_hash=identity,
        mapping_id=1,
        affiliate_link_target_id=1,
        token=_TOKEN_A,
        target_projection_version=1,
    )
    canonical_html = (
        '<a href="https://official.example.test/a" target="_blank" '
        'rel="noopener noreferrer" class="foo" data-x="1">t</a>'
    )
    result = build_tracked_html(
        canonical_html=canonical_html,
        external_links=external_links,
        canonical_body_hash=body_hash,
        renderer_version=RENDERER_VERSION,
        selections=[sel],
    )
    assert 'class="foo"' in result.tracked_html
    assert 'data-x="1"' in result.tracked_html
    reconstructed = reverse_tracked_html(tracked_html=result.tracked_html, manifest=result.manifest)
    assert reconstructed == canonical_html


def test_html_entities_in_surrounding_content_untouched() -> None:
    md = "Tools & tips: [a](https://official.example.test/a) <done>\n"
    rendered, body_hash = _render(md)
    sel = _select(
        rendered=rendered, body_hash=body_hash, ordinal=0, token=_TOKEN_A, mapping_id=1, target_id=1
    )
    result = _build(rendered, body_hash, [sel])
    assert "Tools &amp; tips:" in result.tracked_html
    assert "&lt;done&gt;" in result.tracked_html
    _validate(rendered, body_hash, result.tracked_html, result.manifest)


# ==================== §30: tampering rejection ===============================
def _prepared():
    md = (
        "# Heading\n\n"
        "[selected](https://official.example.test/a) and "
        "[unselected](https://official.example.test/b)\n"
    )
    rendered, body_hash = _render(md)
    sel = _select(
        rendered=rendered, body_hash=body_hash, ordinal=0, token=_TOKEN_A, mapping_id=1, target_id=1
    )
    result = _build(rendered, body_hash, [sel])
    return rendered, body_hash, result


def test_tamper_anchor_text_rejected() -> None:
    rendered, body_hash, result = _prepared()
    tampered = result.tracked_html.replace(">selected<", ">tampered<")
    with pytest.raises(PublicationSubstitutionError):
        _validate(rendered, body_hash, tampered, result.manifest)


def test_tamper_unselected_href_rejected() -> None:
    rendered, body_hash, result = _prepared()
    tampered = result.tracked_html.replace(
        "https://official.example.test/b", "https://evil.example.test/b"
    )
    with pytest.raises(PublicationSubstitutionError):
        _validate(rendered, body_hash, tampered, result.manifest)


def test_tamper_tracked_replacement_href_rejected() -> None:
    rendered, body_hash, result = _prepared()
    tampered = result.tracked_html.replace(_TOKEN_A, "EVILTOKEN0000000000AA")
    with pytest.raises(PublicationSubstitutionError):
        _validate(rendered, body_hash, tampered, result.manifest)


def test_tamper_tracked_rel_rejected() -> None:
    rendered, body_hash, result = _prepared()
    tampered = result.tracked_html.replace(TRACKED_REL, "sponsored")
    with pytest.raises(PublicationSubstitutionError):
        _validate(rendered, body_hash, tampered, result.manifest)


def test_tamper_surrounding_text_rejected() -> None:
    rendered, body_hash, result = _prepared()
    tampered = result.tracked_html.replace("<h2>Heading</h2>", "<h2>Tampered</h2>")
    with pytest.raises(PublicationSubstitutionError):
        _validate(rendered, body_hash, tampered, result.manifest)


def test_tamper_extra_attribute_injected_rejected() -> None:
    rendered, body_hash, result = _prepared()
    tampered = result.tracked_html.replace(
        f'rel="{TRACKED_REL}"', f'rel="{TRACKED_REL}" data-evil="1"'
    )
    with pytest.raises(PublicationSubstitutionError):
        _validate(rendered, body_hash, tampered, result.manifest)


def test_tamper_attribute_removed_rejected() -> None:
    rendered, body_hash, result = _prepared()
    tampered = result.tracked_html.replace(' target="_blank"', "", 1)
    with pytest.raises(PublicationSubstitutionError):
        _validate(rendered, body_hash, tampered, result.manifest)


def test_tamper_injected_element_rejected() -> None:
    rendered, body_hash, result = _prepared()
    injected_tag = (
        '<a href="https://injected.example.test" target="_blank" '
        'rel="noopener noreferrer">injected</a>'
    )
    tampered = result.tracked_html + injected_tag
    with pytest.raises(PublicationSubstitutionError):
        _validate(rendered, body_hash, tampered, result.manifest)


def test_tamper_removed_element_rejected() -> None:
    rendered, body_hash, result = _prepared()
    tampered = result.tracked_html.replace("<h2>Heading</h2>\n", "")
    with pytest.raises(PublicationSubstitutionError):
        _validate(rendered, body_hash, tampered, result.manifest)


def test_tamper_manifest_ordinal_rejected() -> None:
    rendered, body_hash, result = _prepared()
    tampered_manifest = [dict(result.manifest[0])]
    tampered_manifest[0]["occurrence_ordinal"] = 1
    with pytest.raises(PublicationSubstitutionError):
        _validate(rendered, body_hash, result.tracked_html, tampered_manifest)


def test_tamper_manifest_original_href_rejected() -> None:
    rendered, body_hash, result = _prepared()
    tampered_manifest = [dict(result.manifest[0])]
    tampered_manifest[0]["original_href"] = "https://different.example.test/z"
    with pytest.raises(PublicationSubstitutionError):
        _validate(rendered, body_hash, result.tracked_html, tampered_manifest)


def test_tamper_manifest_replacement_href_rejected() -> None:
    rendered, body_hash, result = _prepared()
    tampered_manifest = [dict(result.manifest[0])]
    tampered_manifest[0]["replacement_href"] = "https://bizfluxlab.com/go/OTHERTOKEN0000000000"
    with pytest.raises(PublicationSubstitutionError):
        _validate(rendered, body_hash, result.tracked_html, tampered_manifest)


def test_tamper_manifest_mapping_id_where_correspondence_matters() -> None:
    """pure HTML レベルでは mapping_id 自体を独立検証できない (DB 参照が必要) が、
    duplicate mapping_id (別 occurrence と衝突) は build_tracked_html が拒否する。"""

    md = "[a](https://official.example.test/a) [b](https://official.example.test/b)\n"
    rendered, body_hash = _render(md)
    sel_a = _select(
        rendered=rendered, body_hash=body_hash, ordinal=0, token=_TOKEN_A, mapping_id=1, target_id=1
    )
    sel_b_same_mapping = _select(
        rendered=rendered, body_hash=body_hash, ordinal=1, token=_TOKEN_B, mapping_id=1, target_id=2
    )
    with pytest.raises(PublicationSubstitutionError, match="mapping_id"):
        _build(rendered, body_hash, [sel_a, sel_b_same_mapping])


def test_tamper_duplicate_manifest_entry_rejected() -> None:
    rendered, body_hash, result = _prepared()
    duplicated_manifest = [dict(result.manifest[0]), dict(result.manifest[0])]
    with pytest.raises(PublicationSubstitutionError):
        _validate(rendered, body_hash, result.tracked_html, duplicated_manifest)


# ==================== D-D1 artifact-helper compatibility ====================
def test_manifest_accepted_by_dd1_artifact_helpers() -> None:
    rendered, body_hash, result = _prepared()
    canonicalized = build_substitution_manifest(result.manifest)
    assert canonicalized == result.manifest
    artifact_hash = compute_artifact_hash(
        artifact_schema_version=1,
        article_id=1,
        canonical_body_hash=body_hash,
        renderer_version=RENDERER_VERSION,
        manifest=canonicalized,
    )
    assert len(artifact_hash) == 64
    tracked_hash = compute_tracked_html_hash(result.tracked_html)
    assert len(tracked_hash) == 64


# ==================== out-of-range / consistency guards =====================
def test_out_of_range_ordinal_rejected() -> None:
    rendered, body_hash = _render("[a](https://official.example.test/a)\n")
    identity = compute_occurrence_identity_hash(
        canonical_body_hash=body_hash,
        renderer_version=RENDERER_VERSION,
        occurrence_ordinal=5,
        original_href="https://official.example.test/a",
    )
    sel = SelectedSubstitution(
        occurrence_ordinal=5,
        occurrence_identity_hash=identity,
        mapping_id=1,
        affiliate_link_target_id=1,
        token=_TOKEN_A,
        target_projection_version=1,
    )
    with pytest.raises(PublicationSubstitutionError, match="out of range"):
        _build(rendered, body_hash, [sel])


def test_identity_hash_mismatch_rejected() -> None:
    rendered, body_hash = _render("[a](https://official.example.test/a)\n")
    sel = SelectedSubstitution(
        occurrence_ordinal=0,
        occurrence_identity_hash="f" * 64,
        mapping_id=1,
        affiliate_link_target_id=1,
        token=_TOKEN_A,
        target_projection_version=1,
    )
    with pytest.raises(PublicationSubstitutionError, match="does not match"):
        _build(rendered, body_hash, [sel])


def test_duplicate_ordinal_selection_rejected() -> None:
    rendered, body_hash = _render(
        "[a](https://official.example.test/a) [b](https://official.example.test/b)\n"
    )
    sel1 = _select(
        rendered=rendered, body_hash=body_hash, ordinal=0, token=_TOKEN_A, mapping_id=1, target_id=1
    )
    sel2 = _select(
        rendered=rendered, body_hash=body_hash, ordinal=0, token=_TOKEN_B, mapping_id=2, target_id=2
    )
    with pytest.raises(PublicationSubstitutionError, match="duplicate selection"):
        _build(rendered, body_hash, [sel1, sel2])


# ==================== D-D3-COMMIT §6/§7: anchor-span / external_links ======
# bijection enforcement (count / order / exact href). ``_require_consistent_
# with_external_links`` inside ``build_tracked_html`` already enforces this on
# every call (all tests above implicitly exercise the success path since they
# always pass the real ``rendered.external_links``) -- these tests pin the
# fail-closed rejection path explicitly, and the "target != _blank" matcher
# safety case.
def test_bijection_rejects_more_anchors_than_external_links() -> None:
    canonical_html = (
        '<a href="https://official.example.test/a" target="_blank" '
        'rel="noopener noreferrer">a</a>'
        '<a href="https://official.example.test/b" target="_blank" '
        'rel="noopener noreferrer">b</a>'
    )
    external_links = ["https://official.example.test/a"]  # 実際は 2 個だが 1 個しか渡さない
    with pytest.raises(PublicationSubstitutionError, match="count"):
        build_tracked_html(
            canonical_html=canonical_html,
            external_links=external_links,
            canonical_body_hash="b" * 64,
            renderer_version=RENDERER_VERSION,
            selections=[],
        )


def test_bijection_rejects_fewer_anchors_than_external_links() -> None:
    canonical_html = (
        '<a href="https://official.example.test/a" target="_blank" '
        'rel="noopener noreferrer">a</a>'
    )
    external_links = [
        "https://official.example.test/a",
        "https://official.example.test/b",
    ]
    with pytest.raises(PublicationSubstitutionError, match="count"):
        build_tracked_html(
            canonical_html=canonical_html,
            external_links=external_links,
            canonical_body_hash="b" * 64,
            renderer_version=RENDERER_VERSION,
            selections=[],
        )


def test_bijection_rejects_href_mismatch_at_same_ordinal() -> None:
    canonical_html = (
        '<a href="https://official.example.test/a" target="_blank" '
        'rel="noopener noreferrer">a</a>'
    )
    # count は一致するが、実際のタグの href が external_links の値と食い違う
    # (呼び出し側が別の canonical_html/external_links の組み合わせを渡した場合等)。
    external_links = ["https://official.example.test/DIFFERENT"]
    with pytest.raises(PublicationSubstitutionError, match="does not match"):
        build_tracked_html(
            canonical_html=canonical_html,
            external_links=external_links,
            canonical_body_hash="b" * 64,
            renderer_version=RENDERER_VERSION,
            selections=[],
        )


def test_bijection_rejects_order_swap_even_with_matching_set() -> None:
    """count も href の集合も一致するが、順序が入れ替わっているケース。
    ordinal は document order でしか意味を持たないため、順序不一致も
    fail closed で拒否されなければならない。"""

    canonical_html = (
        '<a href="https://official.example.test/a" target="_blank" '
        'rel="noopener noreferrer">a</a>'
        '<a href="https://official.example.test/b" target="_blank" '
        'rel="noopener noreferrer">b</a>'
    )
    # 実際の document order は [a, b] だが、呼び出し側は [b, a] の順で渡した。
    external_links = [
        "https://official.example.test/b",
        "https://official.example.test/a",
    ]
    with pytest.raises(PublicationSubstitutionError, match="does not match"):
        build_tracked_html(
            canonical_html=canonical_html,
            external_links=external_links,
            canonical_body_hash="b" * 64,
            renderer_version=RENDERER_VERSION,
            selections=[],
        )


def test_non_blank_target_attribute_not_treated_as_external_occurrence() -> None:
    """``target="_blank"`` 以外の target 値を持つ anchor は D-D2/renderer の
    external marker ではないため、external occurrence として扱われてはならない
    (matcher が誤って ordinal をずらさないことの確認、D-D3-COMMIT §7)。"""

    canonical_html = (
        '<a href="/local" target="_self">not-external</a>'
        '<a href="https://official.example.test/a" target="_blank" '
        'rel="noopener noreferrer">a</a>'
    )
    external_links = ["https://official.example.test/a"]
    body_hash = "b" * 64
    identity = compute_occurrence_identity_hash(
        canonical_body_hash=body_hash,
        renderer_version=RENDERER_VERSION,
        occurrence_ordinal=0,
        original_href=external_links[0],
    )
    sel = SelectedSubstitution(
        occurrence_ordinal=0,
        occurrence_identity_hash=identity,
        mapping_id=1,
        affiliate_link_target_id=1,
        token=_TOKEN_A,
        target_projection_version=1,
    )
    result = build_tracked_html(
        canonical_html=canonical_html,
        external_links=external_links,
        canonical_body_hash=body_hash,
        renderer_version=RENDERER_VERSION,
        selections=[sel],
    )
    # target="_self" の anchor は完全無変更のまま。
    assert 'target="_self">not-external</a>' in result.tracked_html
    assert expected_replacement_href(_TOKEN_A) in result.tracked_html
    reconstructed = reverse_tracked_html(
        tracked_html=result.tracked_html, manifest=result.manifest
    )
    assert reconstructed == canonical_html


def test_reverse_tracked_html_full_pipeline_detects_bijection_break_via_validate() -> None:
    """reverse_tracked_html 自体は external_links を受け取らないが、
    validate_tracked_html はこれを canonical_html 経由の forward reconstruction
    (build_tracked_html が bijection を強制する) と突き合わせるため、
    tracked_html 側で anchor 構造が壊れているケースも間接的に fail closed で
    検出できることを end-to-end で確認する。"""

    rendered, body_hash = _render(
        "[a](https://official.example.test/a) [b](https://official.example.test/b)\n"
    )
    sel = _select(
        rendered=rendered, body_hash=body_hash, ordinal=0, token=_TOKEN_A, mapping_id=1, target_id=1
    )
    result = _build(rendered, body_hash, [sel])
    # tracked_html 内の非選択 anchor から target="_blank" を剥がす
    # (= その occurrence が external として認識されなくなる、構造破壊)。
    tampered = result.tracked_html.replace(
        'href="https://official.example.test/b" target="_blank" '
        'rel="noopener noreferrer"',
        'href="https://official.example.test/b"',
    )
    with pytest.raises(PublicationSubstitutionError):
        _validate(rendered, body_hash, tampered, result.manifest)
