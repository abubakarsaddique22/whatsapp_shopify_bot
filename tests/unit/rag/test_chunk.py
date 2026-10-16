"""Phase 8, step 2: chunk_document (policies, pages and FAQ)."""

from app.integrations.shopify.mapper import html_to_text
from app.rag.chunk import chunk_document, estimate_tokens
from app.rag.clean import clean_text


def _paths_and_texts(chunks):  # type: ignore[no-untyped-def]
    return [(c.heading_path, c.text) for c in chunks]


# ---- token estimate


def test_token_estimate_is_about_one_and_a_third_per_word() -> None:
    assert estimate_tokens("one two three") == 4
    assert estimate_tokens("word " * 20) == 26
    assert estimate_tokens("") == 1  # never zero


# ---- policies and pages


def test_one_chunk_per_heading_with_the_title_in_the_path() -> None:
    body = (
        "## Returns\nYou can return items within 14 days.\n\n"
        "## Refunds\nRefunds take 5 to 7 working days."
    )
    chunks = chunk_document("policy_refund", "Refund policy", body)
    assert _paths_and_texts(chunks) == [
        ("Refund policy > Returns", "You can return items within 14 days."),
        ("Refund policy > Refunds", "Refunds take 5 to 7 working days."),
    ]
    assert [c.ordinal for c in chunks] == [0, 1]


def test_text_before_the_first_heading_belongs_to_the_title() -> None:
    chunks = chunk_document("policy_shipping", "Shipping policy", "We ship all over Pakistan.")
    assert _paths_and_texts(chunks) == [("Shipping policy", "We ship all over Pakistan.")]


def test_lines_of_a_section_stay_together_in_order() -> None:
    body = "## Delivery time\nKarachi: 2 days\nLahore: 3 days\nOther cities: 5 days"
    [chunk] = chunk_document("policy_shipping", "Shipping", body)
    assert chunk.text == "Karachi: 2 days\nLahore: 3 days\nOther cities: 5 days"


def test_a_heading_with_no_text_makes_no_chunk() -> None:
    body = "## Returns\n## Refunds\nRefunds take 5 days."
    chunks = chunk_document("policy_refund", "Refund policy", body)
    assert _paths_and_texts(chunks) == [("Refund policy > Refunds", "Refunds take 5 days.")]


def test_nested_heading_levels_build_a_nested_path_when_the_mapper_keeps_them() -> None:
    body = "# Returns\n## Window\n14 days.\n## Condition\nUnused.\n# Refunds\nIn 5 days."
    chunks = chunk_document("policy_refund", "Refund policy", body)
    assert [c.heading_path for c in chunks] == [
        "Refund policy > Returns > Window",
        "Refund policy > Returns > Condition",
        "Refund policy > Refunds",
    ]


def test_a_heading_equal_to_the_title_is_not_repeated() -> None:
    chunks = chunk_document("page", "Warranty", "## Warranty\nTwo years.")
    assert [c.heading_path for c in chunks] == ["Warranty"]


def test_no_title_gives_the_heading_as_path_or_none() -> None:
    chunks = chunk_document("page", "", "Intro line.\n## Care\nKeep it dry.")
    assert [c.heading_path for c in chunks] == [None, "Care"]


def test_empty_body_gives_no_chunks() -> None:
    assert chunk_document("page", "Title", "") == []
    assert chunk_document("faq", "Title", "") == []


def test_a_long_section_is_cut_into_chunks_that_fit() -> None:
    lines = [f"line{i} " + " ".join(["word"] * 19) for i in range(40)]  # 20 words = 26 tokens
    body = "## Terms\n" + "\n".join(lines)
    chunks = chunk_document("page", "Policy", body, max_tokens=100, overlap_tokens=40)
    assert len(chunks) > 5
    assert all(c.token_count <= 100 + 40 for c in chunks)  # up to the overlap more
    assert all(c.heading_path == "Policy > Terms" for c in chunks)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    joined = "\n".join(c.text for c in chunks)
    assert all(line in joined for line in lines)  # nothing is lost


def test_a_short_last_line_is_repeated_at_the_start_of_the_next_chunk() -> None:
    lines = [f"line{i} aaa bbb ccc ddd eee fff ggg" for i in range(6)]  # 8 words = 11 tokens
    chunks = chunk_document("page", "P", "\n".join(lines), max_tokens=25, overlap_tokens=40)
    assert chunks[0].text == "\n".join(lines[:2])
    assert chunks[1].text.startswith(lines[1])


def test_no_overlap_when_the_limit_is_zero() -> None:
    lines = [f"line{i} aaa bbb ccc ddd eee fff ggg" for i in range(6)]
    chunks = chunk_document("page", "P", "\n".join(lines), max_tokens=25, overlap_tokens=0)
    assert "\n".join(c.text for c in chunks) == "\n".join(lines)


def test_a_very_long_paragraph_is_cut_by_sentence() -> None:
    sentence = " ".join(["word"] * 29) + " end."  # 30 words = 39 tokens
    chunks = chunk_document("page", "P", " ".join([sentence] * 5), max_tokens=100, overlap_tokens=0)
    assert len(chunks) == 3
    assert all(c.token_count <= 100 for c in chunks)
    assert all(c.text.count("end.") >= 1 for c in chunks)


def test_a_sentence_with_no_full_stop_is_cut_by_words() -> None:
    chunks = chunk_document("page", "P", " ".join(["word"] * 400), max_tokens=100, overlap_tokens=0)
    assert all(c.token_count <= 100 for c in chunks)
    assert sum(len(c.text.split()) for c in chunks) == 400


def test_real_mapper_output_is_chunked_by_its_headings() -> None:
    html = (
        "<h2>Returns</h2><p>Return within 14 days.</p>"
        "<h2>Refunds</h2><p>Refund in 5 working days.</p><ul><li>Original payment</li></ul>"
    )
    body = clean_text(html_to_text(html))
    chunks = chunk_document("policy_refund", "Refund policy", body)
    assert _paths_and_texts(chunks) == [
        ("Refund policy > Returns", "Return within 14 days."),
        ("Refund policy > Refunds", "Refund in 5 working days.\n- Original payment"),
    ]


# ---- hashes


def test_same_words_under_the_same_heading_give_the_same_hash() -> None:
    first = chunk_document("page", "T", "## A\nSame words.")[0]
    second = chunk_document("page", "T", "## A\nSame words.")[0]
    assert first.content_hash == second.content_hash
    assert len(first.content_hash) == 64


def test_the_hash_changes_with_the_text_and_with_the_heading() -> None:
    base = chunk_document("page", "T", "## A\nRefund in 14 days.")[0]
    other_text = chunk_document("page", "T", "## A\nRefund in 15 days.")[0]
    other_heading = chunk_document("page", "T", "## B\nRefund in 14 days.")[0]
    assert base.content_hash != other_text.content_hash
    assert base.content_hash != other_heading.content_hash


# ---- FAQ


def test_faq_headings_that_are_questions_give_one_chunk_each() -> None:
    body = (
        "## How long is delivery?\nIt takes 3 to 5 days.\n"
        "## Can I return a watch?\nYes, within 14 days."
    )
    chunks = chunk_document("faq", "FAQ", body)
    assert _paths_and_texts(chunks) == [
        ("FAQ", "Q: How long is delivery?\nA: It takes 3 to 5 days."),
        ("FAQ", "Q: Can I return a watch?\nA: Yes, within 14 days."),
    ]


def test_faq_question_lines_without_headings() -> None:
    body = "How long is delivery?\nIt takes 3 to 5 days.\nDo you ship abroad?\nNo.\nNot yet."
    chunks = chunk_document("faq", "FAQ", body)
    assert [c.text for c in chunks] == [
        "Q: How long is delivery?\nA: It takes 3 to 5 days.",
        "Q: Do you ship abroad?\nA: No.\nNot yet.",
    ]


def test_faq_with_q_and_a_prefixes() -> None:
    body = "Q: Delivery time\nA: 3 days\nQ. Refund\nA: 14 days"
    chunks = chunk_document("faq", "FAQ", body)
    assert [c.text for c in chunks] == [
        "Q: Delivery time\nA: 3 days",
        "Q: Refund\nA: 14 days",
    ]


def test_faq_urdu_question_mark_is_a_question() -> None:
    body = "ڈیلیوری کتنے دن میں ہوتی ہے\u061f\nتین سے پانچ دن۔"
    [chunk] = chunk_document("faq", "FAQ", body)
    assert chunk.text.startswith("Q: ڈیلیوری کتنے دن میں ہوتی ہے\u061f\nA: ")


def test_faq_headings_without_question_marks_are_taken_as_questions() -> None:
    body = "## Delivery time\n3 to 5 days.\n## Warranty\nTwo years."
    chunks = chunk_document("faq", "FAQ", body)
    assert [c.text for c in chunks] == [
        "Q: Delivery time\nA: 3 to 5 days.",
        "Q: Warranty\nA: Two years.",
    ]


def test_faq_section_labels_between_questions_are_not_answers() -> None:
    body = (
        "Intro text.\n## Shipping\nHow long is delivery?\n3 days.\n## Returns\nCan I return?\nYes."
    )
    chunks = chunk_document("faq", "FAQ", body)
    assert [c.text for c in chunks] == [
        "Q: How long is delivery?\nA: 3 days.",
        "Q: Can I return?\nA: Yes.",
    ]


def test_faq_question_without_an_answer_makes_no_chunk() -> None:
    chunks = chunk_document("faq", "FAQ", "Is it waterproof?\nWhat is the warranty?\nTwo years.")
    assert [c.text for c in chunks] == ["Q: What is the warranty?\nA: Two years."]


def test_faq_with_no_structure_is_cut_like_a_page() -> None:
    chunks = chunk_document("faq", "FAQ", "Call us any time. We reply fast.")
    assert _paths_and_texts(chunks) == [("FAQ", "Call us any time. We reply fast.")]


def test_a_long_faq_answer_is_cut_and_every_piece_repeats_the_question() -> None:
    answer = "\n".join(f"step{i} " + " ".join(["word"] * 19) for i in range(10))
    chunks = chunk_document("faq", "FAQ", f"How do I return a watch?\n{answer}", max_tokens=100)
    assert len(chunks) > 1
    assert all(c.text.startswith("Q: How do I return a watch?\nA: ") for c in chunks)
    assert all(c.token_count <= 100 for c in chunks)
