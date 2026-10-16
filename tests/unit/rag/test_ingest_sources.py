"""Phase 8, step 4: how rag/ingest.py turns policies and pages into sources (no database)."""

import pytest

from app.core.errors import ConfigError
from app.domain.catalog import Page, Policy
from app.rag.index import SourceDocument
from app.rag.ingest import (
    build_sources,
    document_hash,
    load_sources_from_dir,
    page_source,
    policy_source,
)


def make_policy(kind: str = "REFUND_POLICY", title: str = "Refund policy") -> Policy:
    return Policy(
        type=kind, title=title, body="## Returns\nWithin 7 days.", url="https://s.example/p"
    )


def make_page(title: str, handle: str) -> Page:
    return Page(id="gid://shopify/Page/1", title=title, handle=handle, body="Some text.")


# ---- policies


@pytest.mark.parametrize(
    ("kind", "doc_type"),
    [
        ("REFUND_POLICY", "policy_refund"),
        ("SHIPPING_POLICY", "policy_shipping"),
        ("PRIVACY_POLICY", "policy_privacy"),
        ("TERMS_OF_SERVICE", "page"),  # other policies are plain pages
    ],
)
def test_policy_type_gives_the_doc_type(kind, doc_type):
    source = policy_source(make_policy(kind))
    assert source.doc_type == doc_type
    assert source.source_key == f"policy:{kind}"
    assert source.source == "shopify_policy"


def test_policy_type_is_trimmed_and_upper_cased():
    assert policy_source(make_policy(" refund_policy ")).source_key == "policy:REFUND_POLICY"


def test_policy_keeps_its_official_link_and_text():
    source = policy_source(make_policy())
    assert source.source_url == "https://s.example/p"
    assert source.body == "## Returns\nWithin 7 days."


def test_policy_without_a_title_gets_one_from_its_type():
    assert policy_source(make_policy("TERMS_OF_SERVICE", "  ")).title == "Terms of service"


# ---- pages


@pytest.mark.parametrize(
    ("title", "handle"),
    [
        ("FAQ", "faq"),
        ("Frequently Asked Questions", "help"),
        ("Help", "frequently-asked-questions"),
        ("Aam sawal", "help"),
    ],
)
def test_a_page_about_questions_is_a_faq(title, handle):
    assert page_source(make_page(title, handle), "shop.example").doc_type == "faq"


@pytest.mark.parametrize(
    ("title", "handle"), [("Warranty", "warranty-info"), ("Size guide", "size")]
)
def test_any_other_page_is_a_plain_page(title, handle):
    assert page_source(make_page(title, handle), "shop.example").doc_type == "page"


def test_page_link_is_built_from_the_shop_host_and_handle():
    source = page_source(make_page("FAQ", "faq"), "shop.example")
    assert source.source_url == "https://shop.example/pages/faq"
    assert source.source_key == "page:gid://shopify/Page/1"


def test_page_has_no_link_without_a_shop_host():
    assert page_source(make_page("FAQ", "faq"), None).source_url is None


def test_build_sources_lists_policies_first_then_pages():
    sources = build_sources([make_policy()], [make_page("FAQ", "faq")], "shop.example")
    assert [s.source_key for s in sources] == ["policy:REFUND_POLICY", "page:gid://shopify/Page/1"]


# ---- the document hash


def make_doc(**changes) -> SourceDocument:
    values = {
        "source_key": "test:a",
        "doc_type": "policy_refund",
        "source": "shopify_policy",
        "title": "Refund policy",
        "source_url": "https://s.example/p",
        "body": "ignored by the hash",
        "language": "en",
    }
    return SourceDocument(**(values | changes))


def test_same_document_and_text_give_the_same_hash():
    assert document_hash(make_doc(), "text") == document_hash(make_doc(), "text")


@pytest.mark.parametrize(
    "changes",
    [
        {"doc_type": "policy_shipping"},
        {"title": "Returns"},
        {"source_url": "https://s.example/other"},
        {"language": "ur"},
    ],
)
def test_changing_a_field_changes_the_hash(changes):
    assert document_hash(make_doc(**changes), "text") != document_hash(make_doc(), "text")


def test_changing_the_text_changes_the_hash():
    assert document_hash(make_doc(), "text one") != document_hash(make_doc(), "text two")


# ---- documents from a folder (local development and the golden-set corpus)


def test_folder_documents_get_type_title_and_text_from_the_file(tmp_path):
    (tmp_path / "policy_refund.md").write_text("# Refund policy\n## Returns\nOk.", encoding="utf-8")
    (tmp_path / "faq.md").write_text("# Questions\nQ: Hi?\nA: Hello.", encoding="utf-8")
    (tmp_path / "about_us.md").write_text("We sell watches.", encoding="utf-8")

    docs = {d.source_key: d for d in load_sources_from_dir(tmp_path)}

    refund = docs["file:policy_refund"]
    assert (refund.doc_type, refund.title) == ("policy_refund", "Refund policy")
    assert refund.body.strip() == "## Returns\nOk."  # the "# Title" line is not repeated
    assert docs["file:faq"].doc_type == "faq"
    about = docs["file:about_us"]
    assert (about.doc_type, about.title) == ("page", "About us")  # no "# " line: title from name
    assert about.body == "We sell watches."


def test_an_empty_folder_is_a_clear_error(tmp_path):
    with pytest.raises(ConfigError):
        load_sources_from_dir(tmp_path)
