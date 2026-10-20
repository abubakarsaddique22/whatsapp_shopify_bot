# The 25 required test cases (Blueprint 11.1)

Generated from `tests/case_map.py` by `uv run python scripts\test_map.py`. Do not edit by hand: `tests/unit/test_case_map.py` fails when this page is out of date.

Types: U unit, I integration (fakes), E evaluation set (real model), A adversarial.

| ID | Case | Type | Phase | Tests | Evaluation set | Open |
|----|------|------|-------|-------|----------------|------|
| T01 | Customer asks the current price of a product | I, E | 3, 9 | 3 in `tests/integration/test_graph_flow.py` | evals/product_qa.jsonl (scripts/run_eval.py --set golden) | - |
| T02 | Customer asks if a watch is in stock | I, E | 3 | 2 in `tests/integration/test_graph_flow.py` | evals/product_qa.jsonl (scripts/run_eval.py --set golden) | - |
| T03 | Customer asks which colours exist | I, E | 3 | 3 in `tests/integration/test_graph_flow.py` | evals/product_qa.jsonl (scripts/run_eval.py --set golden) | - |
| T04 | Customer asks about size or dimensions | I, E | 3 | 13 in `tests/integration/test_graph_flow.py`, `tests/integration/test_specs_images.py`, `tests/integration/test_tools.py`, `tests/unit/catalog/test_specs.py` | evals/product_qa.jsonl (scripts/run_eval.py --set golden) | the eval set has 0 size questions: the dev store has no Case size option yet |
| T05 | Customer requests a specific product image | I | 7 | 2 in `tests/integration/test_image_flow.py` | - | - |
| T06 | Customer asks about the refund policy | I, E | 8 | 2 in `tests/integration/test_graph_flow.py` | evals/rag_golden.jsonl (run_eval.py --set retrieval; tests/llm/test_retrieval.py) | - |
| T07 | Customer asks about the privacy policy | I, E | 8 | 1 in `tests/integration/test_graph_flow.py` | evals/rag_golden.jsonl (run_eval.py --set retrieval; tests/llm/test_retrieval.py) | - |
| T08 | English text message | E | 5 | 2 in `tests/unit/agent/test_router_eval.py` | evals/router_golden.jsonl (run_eval.py --set language; tests/llm/test_language.py) | - |
| T09 | Roman Urdu text message | E | 5 | 2 in `tests/unit/agent/test_router_eval.py` | evals/router_golden.jsonl (run_eval.py --set language; tests/llm/test_language.py) | router language 94.2% vs the 95% gate; golden labels need a native review |
| T10 | Urdu voice message | I, E | 6 | 1 in `tests/integration/test_worker_voice.py` | evals/voice_manifest.csv (scripts/bakeoff.py) | voice bake-off not run: evals/voice_manifest.csv has no clips yet (Phase 6 Step 6) |
| T11 | Punjabi voice message | E | 6 | 2 in `tests/integration/test_worker_voice.py` | evals/voice_manifest.csv (scripts/bakeoff.py) | voice bake-off not run: evals/voice_manifest.csv has no clips yet (Phase 6 Step 6); the Punjabi decision is still open |
| T12 | English voice message | I, E | 6 | 1 in `tests/integration/test_worker_voice.py` | evals/voice_manifest.csv (scripts/bakeoff.py) | voice bake-off not run: evals/voice_manifest.csv has no clips yet (Phase 6 Step 6) |
| T13 | Ambiguous product query | I, E | 3, 9 | 4 in `tests/integration/test_graph_flow.py`, `tests/integration/test_search.py`, `tests/integration/test_tools.py` | evals/natural_qa.jsonl (run_eval.py --set natural) | - |
| T14 | Shopify returns no matching product | I | 3 | 4 in `tests/integration/test_search.py`, `tests/integration/test_tools.py` | - | - |
| T15 | Shopify API temporarily unavailable | I | 3, 9 | 5 in `tests/drills/test_failure_drills.py`, `tests/integration/test_graph_flow.py`, `tests/integration/test_tools.py` | - | - |
| T16 | A webhook event is delivered twice (WhatsApp and Shopify) | I | 4, 10 | 2 in `tests/integration/test_sync_events.py`, `tests/integration/test_whatsapp_webhook.py` | run_eval.py --set duplicates (t16_webhook_twice + chaos scenarios) | - |
| T17 | A product's price or inventory changes | I | 10 | 2 in `tests/integration/test_sync_repair.py` | - | - |
| T18 | The RAG index contains stale information | I | 8, 10 | 1 in `tests/integration/test_rag_ingest.py` | - | - |
| T19 | Customer tries to get another customer's private data | A | 11 | 2 in `tests/adversarial/test_t19_private_data.py` | evals/adversarial.jsonl (run_eval.py --set security; tests/llm/test_security_router.py) | - |
| T20 | Malicious message attempts prompt injection | A | 11 | 4 in `tests/adversarial/test_t20_injection.py` | evals/adversarial.jsonl (run_eval.py --set security; tests/llm/test_security_router.py) | - |
| T21 | A retrieved document contains malicious instructions | A | 11 | 7 in `tests/adversarial/test_t21_ingest.py`, `tests/adversarial/test_t21_poisoned_documents.py`, `tests/integration/test_security_events.py` | evals/adversarial.jsonl (run_eval.py --set security; tests/llm/test_security_router.py) | adv-048 (instruction inside fake tags) is a known gap, see docs/threat_model.md |
| T22 | Speech transcription is unclear | I | 6 | 1 in `tests/integration/test_worker_voice.py` | - | - |
| T23 | A product has no suitable image | I | 7 | 1 in `tests/integration/test_image_flow.py` | - | - |
| T24 | WhatsApp media delivery fails | I | 7 | 5 in `tests/integration/test_image_flow.py`, `tests/integration/test_sender_images.py` | - | - |
| T25 | Customer sends an unsupported attachment | I | 4, 6 | 3 in `tests/integration/test_worker_sender.py`, `tests/integration/test_worker_voice.py` | - | - |

## Tests per case

### T01: Customer asks the current price of a product

Expected: Exact live price and currency; changing the price changes the answer.

- `tests/integration/test_graph_flow.py::test_t01_the_price_is_the_live_price_from_the_tool`
- `tests/integration/test_graph_flow.py::test_t01_changing_the_live_price_changes_what_the_composer_sees`
- `tests/integration/test_graph_flow.py::test_t01_a_price_in_english`

### T02: Customer asks if a watch is in stock

Expected: Every stock state answered correctly; no invented numbers.

- `tests/integration/test_graph_flow.py::test_t02_each_stock_state_is_answered_from_the_tool`
- `tests/integration/test_graph_flow.py::test_t02_an_invented_stock_number_is_rejected`

### T03: Customer asks which colours exist

Expected: Only real colours; 'colour not offered' differs from 'out of stock'.

- `tests/integration/test_graph_flow.py::test_t03_only_the_real_colours_are_listed`
- `tests/integration/test_graph_flow.py::test_t03_a_colour_that_does_not_exist_is_rejected`
- `tests/integration/test_graph_flow.py::test_t03_a_colour_that_is_not_offered_is_not_called_out_of_stock`

### T04: Customer asks about size or dimensions

Expected: Values from metafields or options; if absent, 'not available'.

- `tests/integration/test_graph_flow.py::test_t04_a_size_from_the_specs_is_answered`
- `tests/integration/test_graph_flow.py::test_t04_a_missing_spec_is_not_invented`
- `tests/integration/test_tools.py::test_t04_specs_value_comes_from_the_metafield`
- `tests/integration/test_tools.py::test_t04_missing_spec_is_not_available_not_a_guess`
- `tests/integration/test_tools.py::test_t04_hidden_product_specs_are_not_found`
- `tests/integration/test_specs_images.py::test_t04_asked_spec_comes_from_the_metafield`
- `tests/integration/test_specs_images.py::test_t04_case_size_comes_from_the_variant_options`
- `tests/integration/test_specs_images.py::test_t04_missing_value_is_not_available`
- `tests/integration/test_specs_images.py::test_t04_hidden_or_unknown_product_is_not_found`
- `tests/unit/catalog/test_specs.py::test_t04_metafield_value_is_returned_as_the_merchant_wrote_it`
- `tests/unit/catalog/test_specs.py::test_t04_case_size_comes_from_the_options_when_there_is_no_metafield`
- `tests/unit/catalog/test_specs.py::test_t04_absent_value_is_not_available_never_guessed`
- `tests/unit/catalog/test_specs.py::test_t04_nothing_at_all_is_not_available`

### T05: Customer requests a specific product image

Expected: Correct product or variant image is sent through the fake WhatsApp client.

- `tests/integration/test_image_flow.py::test_t05_colour_picture_is_queued_and_sent_through_the_fake_client`
- `tests/integration/test_image_flow.py::test_t05_colour_without_its_own_picture_gets_the_main_one_and_says_so`

### T06: Customer asks about the refund policy

Expected: Grounded answer with the policy's numbers; abstains if no policy exists.

- `tests/integration/test_graph_flow.py::test_t06_a_refund_answer_uses_the_numbers_of_the_policy`
- `tests/integration/test_graph_flow.py::test_t06_without_a_policy_the_bot_abstains_and_does_not_invent_days`

### T07: Customer asks about the privacy policy

Expected: Public policy summarised; no personal data.

- `tests/integration/test_graph_flow.py::test_t07_the_privacy_policy_is_summarised_without_personal_data`

### T08: English text message

Expected: Language detected en; reply in English.

- `tests/unit/agent/test_router_eval.py::test_t08_english_message_gets_an_english_reply_language`
- `tests/unit/agent/test_router_eval.py::test_t08_t09_over_the_whole_golden_set`

### T09: Roman Urdu text message

Expected: Language detected roman_ur; reply in Roman Urdu.

- `tests/unit/agent/test_router_eval.py::test_t09_roman_urdu_message_gets_a_roman_urdu_reply_language`
- `tests/unit/agent/test_router_eval.py::test_t08_t09_over_the_whole_golden_set`

### T10: Urdu voice message

Expected: Transcript reaches the router; reply is Roman Urdu text.

- `tests/integration/test_worker_voice.py::test_t10_urdu_voice_note_reaches_the_router`

### T11: Punjabi voice message

Expected: Handled per bake-off result; if unclear, a clarification is sent.

- `tests/integration/test_worker_voice.py::test_t11_punjabi_voice_note_is_handled_when_it_is_clear`
- `tests/integration/test_worker_voice.py::test_t11_unclear_punjabi_gets_a_clarification`

### T12: English voice message

Expected: Reply in English text.

- `tests/integration/test_worker_voice.py::test_t12_english_voice_note_reaches_the_router`

### T13: Ambiguous product query

Expected: One clarifying question naming candidates; no guessing.

- `tests/integration/test_graph_flow.py::test_t13_an_ambiguous_product_gets_one_question_naming_the_candidates`
- `tests/integration/test_graph_flow.py::test_t13_the_answer_to_the_question_is_looked_up_without_a_new_guess`
- `tests/integration/test_tools.py::test_t13_ambiguous_search_returns_candidates_and_no_price`
- `tests/integration/test_search.py::test_t13_close_matches_are_ambiguous_and_name_the_candidates`

### T14: Shopify returns no matching product

Expected: Honest 'not found'; draft and unpublished products behave the same.

- `tests/integration/test_tools.py::test_t14_unknown_product_is_ok_with_kind_none`
- `tests/integration/test_tools.py::test_t14_hidden_product_is_never_found`
- `tests/integration/test_search.py::test_t14_unknown_product_is_none`
- `tests/integration/test_search.py::test_t14_hidden_product_behaves_like_a_missing_one`

### T15: Shopify API temporarily unavailable

Expected: Safe message; stock state UNKNOWN; no invented values.

- `tests/integration/test_tools.py::test_t15_shopify_down_stock_is_unknown_never_a_guess`
- `tests/integration/test_tools.py::test_t15_shopify_down_price_has_no_value_at_all`
- `tests/integration/test_graph_flow.py::test_t15_when_shopify_is_down_the_bot_says_so_and_invents_no_price`
- `tests/drills/test_failure_drills.py::test_shopify_down_price_is_not_guessed`
- `tests/drills/test_failure_drills.py::test_shopify_down_an_invented_price_never_reaches_the_customer`

### T16: A webhook event is delivered twice (WhatsApp and Shopify)

Expected: One inbox row, one reply; second Shopify event ignored.

- `tests/integration/test_whatsapp_webhook.py::test_t16_duplicate_wamid_gives_one_row`
- `tests/integration/test_sync_events.py::test_t16_second_delivery_of_the_same_event_is_ignored`

### T17: A product's price or inventory changes

Expected: Next answer shows the new live value; mirror updated after the webhook.

- `tests/integration/test_sync_repair.py::test_t17_a_price_change_in_shopify_reaches_the_mirror_through_the_worker`
- `tests/integration/test_sync_repair.py::test_t17_a_sold_out_item_is_seen_through_the_worker`

### T18: The RAG index contains stale information

Expected: After a policy change and sync the new text is used; the old version is inactive.

- `tests/integration/test_rag_ingest.py::test_t18_a_changed_policy_gets_a_new_version_and_the_old_one_goes_inactive`

### T19: Customer tries to get another customer's private data

Expected: Refusal, no data tool called, handoff offered.

- `tests/adversarial/test_t19_private_data.py::test_t19_the_message_is_refused_and_no_tool_runs`
- `tests/adversarial/test_t19_private_data.py::test_t19_when_the_customer_accepts_a_person_takes_over`

### T20: Malicious message attempts prompt injection

Expected: No leak; security event logged; reply is a refusal or a normal on-topic reply.

- `tests/adversarial/test_t20_injection.py::test_t20_an_attack_gets_the_fixed_refusal_and_a_strike`
- `tests/adversarial/test_t20_injection.py::test_t20_a_composer_that_obeys_is_stopped_by_the_validator`
- `tests/adversarial/test_t20_injection.py::test_t20_a_composer_that_obeys_twice_gets_the_safe_reply_and_a_handoff`
- `tests/adversarial/test_t20_injection.py::test_t20_false_authority_does_not_change_a_price`

### T21: A retrieved document contains malicious instructions

Expected: Instructions ignored; document flagged; answer unaffected.

- `tests/adversarial/test_t21_ingest.py::test_t21_a_poisoned_document_is_flagged_and_not_indexed`
- `tests/adversarial/test_t21_ingest.py::test_t21_an_attack_added_to_a_real_policy_keeps_the_last_good_version`
- `tests/integration/test_security_events.py::test_t21_a_poisoned_product_text_is_not_indexed_and_the_good_version_stays`
- `tests/adversarial/test_t21_poisoned_documents.py::test_t21_a_poisoned_passage_is_data_and_the_answer_is_unaffected`
- `tests/adversarial/test_t21_poisoned_documents.py::test_t21_a_composer_that_obeys_the_passage_is_stopped_by_the_validator`
- `tests/adversarial/test_t21_poisoned_documents.py::test_t21_a_composer_that_obeys_twice_gets_the_safe_reply_and_a_handoff`
- `tests/adversarial/test_t21_poisoned_documents.py::test_t21_an_instruction_to_leak_a_secret_inside_a_passage_does_not_leak`

### T22: Speech transcription is unclear

Expected: Polite request to repeat; no answer built on the doubtful text.

- `tests/integration/test_worker_voice.py::test_t22_a_doubtful_transcript_is_not_kept_and_not_answered`

### T23: A product has no suitable image

Expected: Says no image is available and gives the product link.

- `tests/integration/test_image_flow.py::test_t23_product_without_a_picture_gets_a_reply_with_the_link`

### T24: WhatsApp media delivery fails

Expected: Retry; fallback to a text link; failure logged.

- `tests/integration/test_image_flow.py::test_t24_refused_picture_that_cannot_be_fetched_becomes_a_text_link_and_is_logged`
- `tests/integration/test_image_flow.py::test_t24_temporary_error_is_retried_once_then_the_text_link_goes_out`
- `tests/integration/test_sender_images.py::test_t24_failed_picture_becomes_a_text_link`
- `tests/integration/test_sender_images.py::test_t24_temporary_error_is_retried_once_then_text_link`
- `tests/integration/test_sender_images.py::test_t24_picture_that_cannot_be_converted_becomes_a_text_link`

### T25: Customer sends an unsupported attachment

Expected: Polite text reply; no crash; the conversation continues.

- `tests/integration/test_worker_sender.py::test_t25_unsupported_type_gets_a_polite_reply`
- `tests/integration/test_worker_voice.py::test_t25_a_media_file_that_is_not_audio_gets_the_polite_reply`
- `tests/integration/test_worker_voice.py::test_t25_audio_without_a_media_id_gets_the_polite_reply`

