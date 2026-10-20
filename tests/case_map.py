"""Phase 12, Step 6: the map of the 25 required test cases (Blueprint 11.1) to the tests that prove
them. Not a test module itself (no `test_` prefix): tests/unit/test_case_map.py checks it, and
scripts/test_map.py turns it into docs/test_cases.md.

Rules the check enforces:
* exactly T01 to T25, each with at least one pytest test that really exists (file + function);
* every test function named `test_tNN_...` anywhere under tests/ is listed under that TNN, so a
  new T-test can never be forgotten here;
* the docs page is generated from this file and must be up to date.

`evals` names the evaluation set behind the 'E' type (needs a real model, Phase 12 Step 4), and
`open` says honestly what is still missing for that case. A case with an `open` note is mapped,
but not finished.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Case:
    title: str
    types: str  # Blueprint 11.1: U unit, I integration (fakes), E evaluation set, A adversarial
    phases: str
    expected: str
    tests: tuple[str, ...]  # "tests/<path>.py::<function>"
    evals: tuple[str, ...] = field(default=())
    open: str | None = None


GRAPH = "tests/integration/test_graph_flow.py"
TOOLS = "tests/integration/test_tools.py"
SEARCH = "tests/integration/test_search.py"
SPECS = "tests/integration/test_specs_images.py"
UNIT_SPECS = "tests/unit/catalog/test_specs.py"
IMAGES = "tests/integration/test_image_flow.py"
SENDER_IMAGES = "tests/integration/test_sender_images.py"
VOICE = "tests/integration/test_worker_voice.py"
ROUTER_EVAL = "tests/unit/agent/test_router_eval.py"
DRILLS = "tests/drills/test_failure_drills.py"

PRODUCT_QA = "evals/product_qa.jsonl (scripts/run_eval.py --set golden)"
RAG_GOLDEN = "evals/rag_golden.jsonl (run_eval.py --set retrieval; tests/llm/test_retrieval.py)"
ROUTER_GOLDEN = "evals/router_golden.jsonl (run_eval.py --set language; tests/llm/test_language.py)"
VOICE_BAKEOFF = "evals/voice_manifest.csv (scripts/bakeoff.py)"
ADVERSARIAL = (
    "evals/adversarial.jsonl (run_eval.py --set security; tests/llm/test_security_router.py)"
)
NO_CLIPS = "voice bake-off not run: evals/voice_manifest.csv has no clips yet (Phase 6 Step 6)"

CASES: dict[str, Case] = {
    "T01": Case(
        "Customer asks the current price of a product",
        "I, E",
        "3, 9",
        "Exact live price and currency; changing the price changes the answer.",
        (
            f"{GRAPH}::test_t01_the_price_is_the_live_price_from_the_tool",
            f"{GRAPH}::test_t01_changing_the_live_price_changes_what_the_composer_sees",
            f"{GRAPH}::test_t01_a_price_in_english",
        ),
        evals=(PRODUCT_QA,),
    ),
    "T02": Case(
        "Customer asks if a watch is in stock",
        "I, E",
        "3",
        "Every stock state answered correctly; no invented numbers.",
        (
            f"{GRAPH}::test_t02_each_stock_state_is_answered_from_the_tool",
            f"{GRAPH}::test_t02_an_invented_stock_number_is_rejected",
        ),
        evals=(PRODUCT_QA,),
    ),
    "T03": Case(
        "Customer asks which colours exist",
        "I, E",
        "3",
        "Only real colours; 'colour not offered' differs from 'out of stock'.",
        (
            f"{GRAPH}::test_t03_only_the_real_colours_are_listed",
            f"{GRAPH}::test_t03_a_colour_that_does_not_exist_is_rejected",
            f"{GRAPH}::test_t03_a_colour_that_is_not_offered_is_not_called_out_of_stock",
        ),
        evals=(PRODUCT_QA,),
    ),
    "T04": Case(
        "Customer asks about size or dimensions",
        "I, E",
        "3",
        "Values from metafields or options; if absent, 'not available'.",
        (
            f"{GRAPH}::test_t04_a_size_from_the_specs_is_answered",
            f"{GRAPH}::test_t04_a_missing_spec_is_not_invented",
            f"{TOOLS}::test_t04_specs_value_comes_from_the_metafield",
            f"{TOOLS}::test_t04_missing_spec_is_not_available_not_a_guess",
            f"{TOOLS}::test_t04_hidden_product_specs_are_not_found",
            f"{SPECS}::test_t04_asked_spec_comes_from_the_metafield",
            f"{SPECS}::test_t04_case_size_comes_from_the_variant_options",
            f"{SPECS}::test_t04_missing_value_is_not_available",
            f"{SPECS}::test_t04_hidden_or_unknown_product_is_not_found",
            f"{UNIT_SPECS}::test_t04_metafield_value_is_returned_as_the_merchant_wrote_it",
            f"{UNIT_SPECS}::test_t04_case_size_comes_from_the_options_when_there_is_no_metafield",
            f"{UNIT_SPECS}::test_t04_absent_value_is_not_available_never_guessed",
            f"{UNIT_SPECS}::test_t04_nothing_at_all_is_not_available",
        ),
        evals=(PRODUCT_QA,),
        open="the eval set has 0 size questions: the dev store has no Case size option yet",
    ),
    "T05": Case(
        "Customer requests a specific product image",
        "I",
        "7",
        "Correct product or variant image is sent through the fake WhatsApp client.",
        (
            f"{IMAGES}::test_t05_colour_picture_is_queued_and_sent_through_the_fake_client",
            f"{IMAGES}::test_t05_colour_without_its_own_picture_gets_the_main_one_and_says_so",
        ),
    ),
    "T06": Case(
        "Customer asks about the refund policy",
        "I, E",
        "8",
        "Grounded answer with the policy's numbers; abstains if no policy exists.",
        (
            f"{GRAPH}::test_t06_a_refund_answer_uses_the_numbers_of_the_policy",
            f"{GRAPH}::test_t06_without_a_policy_the_bot_abstains_and_does_not_invent_days",
        ),
        evals=(RAG_GOLDEN,),
    ),
    "T07": Case(
        "Customer asks about the privacy policy",
        "I, E",
        "8",
        "Public policy summarised; no personal data.",
        (f"{GRAPH}::test_t07_the_privacy_policy_is_summarised_without_personal_data",),
        evals=(RAG_GOLDEN,),
    ),
    "T08": Case(
        "English text message",
        "E",
        "5",
        "Language detected en; reply in English.",
        (
            f"{ROUTER_EVAL}::test_t08_english_message_gets_an_english_reply_language",
            f"{ROUTER_EVAL}::test_t08_t09_over_the_whole_golden_set",
        ),
        evals=(ROUTER_GOLDEN,),
    ),
    "T09": Case(
        "Roman Urdu text message",
        "E",
        "5",
        "Language detected roman_ur; reply in Roman Urdu.",
        (
            f"{ROUTER_EVAL}::test_t09_roman_urdu_message_gets_a_roman_urdu_reply_language",
            f"{ROUTER_EVAL}::test_t08_t09_over_the_whole_golden_set",
        ),
        evals=(ROUTER_GOLDEN,),
        open="router language 94.2% vs the 95% gate; golden labels need a native review",
    ),
    "T10": Case(
        "Urdu voice message",
        "I, E",
        "6",
        "Transcript reaches the router; reply is Roman Urdu text.",
        (f"{VOICE}::test_t10_urdu_voice_note_reaches_the_router",),
        evals=(VOICE_BAKEOFF,),
        open=NO_CLIPS,
    ),
    "T11": Case(
        "Punjabi voice message",
        "E",
        "6",
        "Handled per bake-off result; if unclear, a clarification is sent.",
        (
            f"{VOICE}::test_t11_punjabi_voice_note_is_handled_when_it_is_clear",
            f"{VOICE}::test_t11_unclear_punjabi_gets_a_clarification",
        ),
        evals=(VOICE_BAKEOFF,),
        open=NO_CLIPS + "; the Punjabi decision is still open",
    ),
    "T12": Case(
        "English voice message",
        "I, E",
        "6",
        "Reply in English text.",
        (f"{VOICE}::test_t12_english_voice_note_reaches_the_router",),
        evals=(VOICE_BAKEOFF,),
        open=NO_CLIPS,
    ),
    "T13": Case(
        "Ambiguous product query",
        "I, E",
        "3, 9",
        "One clarifying question naming candidates; no guessing.",
        (
            f"{GRAPH}::test_t13_an_ambiguous_product_gets_one_question_naming_the_candidates",
            f"{GRAPH}::test_t13_the_answer_to_the_question_is_looked_up_without_a_new_guess",
            f"{TOOLS}::test_t13_ambiguous_search_returns_candidates_and_no_price",
            f"{SEARCH}::test_t13_close_matches_are_ambiguous_and_name_the_candidates",
            f"{SEARCH}::test_t13_a_title_inside_another_title_is_not_guessed_with_the_typo_rule",
        ),
        evals=("evals/natural_qa.jsonl (run_eval.py --set natural)",),
    ),
    "T14": Case(
        "Shopify returns no matching product",
        "I",
        "3",
        "Honest 'not found'; draft and unpublished products behave the same.",
        (
            f"{TOOLS}::test_t14_unknown_product_is_ok_with_kind_none",
            f"{TOOLS}::test_t14_hidden_product_is_never_found",
            f"{SEARCH}::test_t14_unknown_product_is_none",
            f"{SEARCH}::test_t14_hidden_product_behaves_like_a_missing_one",
        ),
    ),
    "T15": Case(
        "Shopify API temporarily unavailable",
        "I",
        "3, 9",
        "Safe message; stock state UNKNOWN; no invented values.",
        (
            f"{TOOLS}::test_t15_shopify_down_stock_is_unknown_never_a_guess",
            f"{TOOLS}::test_t15_shopify_down_price_has_no_value_at_all",
            f"{GRAPH}::test_t15_when_shopify_is_down_the_bot_says_so_and_invents_no_price",
            f"{DRILLS}::test_shopify_down_price_is_not_guessed",
            f"{DRILLS}::test_shopify_down_an_invented_price_never_reaches_the_customer",
        ),
    ),
    "T16": Case(
        "A webhook event is delivered twice (WhatsApp and Shopify)",
        "I",
        "4, 10",
        "One inbox row, one reply; second Shopify event ignored.",
        (
            "tests/integration/test_whatsapp_webhook.py::test_t16_duplicate_wamid_gives_one_row",
            "tests/integration/test_sync_events.py"
            "::test_t16_second_delivery_of_the_same_event_is_ignored",
        ),
        evals=("run_eval.py --set duplicates (t16_webhook_twice + chaos scenarios)",),
    ),
    "T17": Case(
        "A product's price or inventory changes",
        "I",
        "10",
        "Next answer shows the new live value; mirror updated after the webhook.",
        (
            "tests/integration/test_sync_repair.py"
            "::test_t17_a_price_change_in_shopify_reaches_the_mirror_through_the_worker",
            "tests/integration/test_sync_repair.py"
            "::test_t17_a_sold_out_item_is_seen_through_the_worker",
        ),
    ),
    "T18": Case(
        "The RAG index contains stale information",
        "I",
        "8, 10",
        "After a policy change and sync the new text is used; the old version is inactive.",
        (
            "tests/integration/test_rag_ingest.py"
            "::test_t18_a_changed_policy_gets_a_new_version_and_the_old_one_goes_inactive",
        ),
    ),
    "T19": Case(
        "Customer tries to get another customer's private data",
        "A",
        "11",
        "Refusal, no data tool called, handoff offered.",
        (
            "tests/adversarial/test_t19_private_data.py"
            "::test_t19_the_message_is_refused_and_no_tool_runs",
            "tests/adversarial/test_t19_private_data.py"
            "::test_t19_when_the_customer_accepts_a_person_takes_over",
        ),
        evals=(ADVERSARIAL,),
    ),
    "T20": Case(
        "Malicious message attempts prompt injection",
        "A",
        "11",
        "No leak; security event logged; reply is a refusal or a normal on-topic reply.",
        (
            "tests/adversarial/test_t20_injection.py"
            "::test_t20_an_attack_gets_the_fixed_refusal_and_a_strike",
            "tests/adversarial/test_t20_injection.py"
            "::test_t20_a_composer_that_obeys_is_stopped_by_the_validator",
            "tests/adversarial/test_t20_injection.py"
            "::test_t20_a_composer_that_obeys_twice_gets_the_safe_reply_and_a_handoff",
            "tests/adversarial/test_t20_injection.py"
            "::test_t20_false_authority_does_not_change_a_price",
        ),
        evals=(ADVERSARIAL,),
    ),
    "T21": Case(
        "A retrieved document contains malicious instructions",
        "A",
        "11",
        "Instructions ignored; document flagged; answer unaffected.",
        (
            "tests/adversarial/test_t21_ingest.py"
            "::test_t21_a_poisoned_document_is_flagged_and_not_indexed",
            "tests/adversarial/test_t21_ingest.py"
            "::test_t21_an_attack_added_to_a_real_policy_keeps_the_last_good_version",
            "tests/integration/test_security_events.py"
            "::test_t21_a_poisoned_product_text_is_not_indexed_and_the_good_version_stays",
            "tests/adversarial/test_t21_poisoned_documents.py"
            "::test_t21_a_poisoned_passage_is_data_and_the_answer_is_unaffected",
            "tests/adversarial/test_t21_poisoned_documents.py"
            "::test_t21_a_composer_that_obeys_the_passage_is_stopped_by_the_validator",
            "tests/adversarial/test_t21_poisoned_documents.py"
            "::test_t21_a_composer_that_obeys_twice_gets_the_safe_reply_and_a_handoff",
            "tests/adversarial/test_t21_poisoned_documents.py"
            "::test_t21_an_instruction_to_leak_a_secret_inside_a_passage_does_not_leak",
        ),
        evals=(ADVERSARIAL,),
        open="adv-048 (instruction inside fake tags) is a known gap, see docs/threat_model.md",
    ),
    "T22": Case(
        "Speech transcription is unclear",
        "I",
        "6",
        "Polite request to repeat; no answer built on the doubtful text.",
        (f"{VOICE}::test_t22_a_doubtful_transcript_is_not_kept_and_not_answered",),
    ),
    "T23": Case(
        "A product has no suitable image",
        "I",
        "7",
        "Says no image is available and gives the product link.",
        (f"{IMAGES}::test_t23_product_without_a_picture_gets_a_reply_with_the_link",),
    ),
    "T24": Case(
        "WhatsApp media delivery fails",
        "I",
        "7",
        "Retry; fallback to a text link; failure logged.",
        (
            f"{IMAGES}"
            "::test_t24_refused_picture_that_cannot_be_fetched_becomes_a_text_link_and_is_logged",
            f"{IMAGES}::test_t24_temporary_error_is_retried_once_then_the_text_link_goes_out",
            f"{SENDER_IMAGES}::test_t24_failed_picture_becomes_a_text_link",
            f"{SENDER_IMAGES}::test_t24_temporary_error_is_retried_once_then_text_link",
            f"{SENDER_IMAGES}::test_t24_picture_that_cannot_be_converted_becomes_a_text_link",
        ),
    ),
    "T25": Case(
        "Customer sends an unsupported attachment",
        "I",
        "4, 6",
        "Polite text reply; no crash; the conversation continues.",
        (
            "tests/integration/test_worker_sender.py"
            "::test_t25_unsupported_type_gets_a_polite_reply",
            f"{VOICE}::test_t25_a_media_file_that_is_not_audio_gets_the_polite_reply",
            f"{VOICE}::test_t25_audio_without_a_media_id_gets_the_polite_reply",
        ),
    ),
}


def render_markdown() -> str:
    """docs/test_cases.md, generated (scripts/test_map.py). Do not edit the page by hand."""
    lines = [
        "# The 25 required test cases (Blueprint 11.1)",
        "",
        "Generated from `tests/case_map.py` by `uv run python scripts\\test_map.py`. "
        "Do not edit by hand: `tests/unit/test_case_map.py` fails when this page is out of date.",
        "",
        "Types: U unit, I integration (fakes), E evaluation set (real model), A adversarial.",
        "",
        "| ID | Case | Type | Phase | Tests | Evaluation set | Open |",
        "|----|------|------|-------|-------|----------------|------|",
    ]
    for case_id, case in CASES.items():
        files = sorted({ref.split("::")[0] for ref in case.tests})
        tests = f"{len(case.tests)} in " + ", ".join(f"`{f}`" for f in files)
        evals = "; ".join(case.evals) or "-"
        lines.append(
            f"| {case_id} | {case.title} | {case.types} | {case.phases} | {tests} | {evals} | "
            f"{case.open or '-'} |"
        )
    lines += ["", "## Tests per case", ""]
    for case_id, case in CASES.items():
        lines.append(f"### {case_id}: {case.title}")
        lines.append("")
        lines.append(f"Expected: {case.expected}")
        lines.append("")
        lines += [f"- `{ref}`" for ref in case.tests]
        lines.append("")
    return "\n".join(lines)
