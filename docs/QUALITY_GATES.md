# Quality gates and characterization map

This document records the no-behavior-change test foundation for the ship-readiness work.
The inventory was taken from baseline `9ad843506a1734c436054398f1372f91f005a77a`.

## Baseline inventory

- `tests/test_workflow.py`: 129 test functions, now grouped under `tests/workflow/`.
- `tests/test_cli.py`: 29 test functions, now grouped under `tests/cli/`.
- The split tree collects 249 pytest cases; names and parameterization are preserved.
- All 1,485 baseline assertion nodes are unchanged; fixture-only edits are limited to two workflow cases and five CLI helper call sites.
- The largest new behavior module is 768 lines; no moved test module exceeds 800 lines.
- No explicit `pytest.mark.skip`, `skipif`, `xfail`, or `pytest.skip()` was present in either baseline file.
- `test_default_desktop_profile_is_rejected_before_browser_startup` has Windows, macOS, and other-platform branches; it is not skipped.
- The workflow suite includes synthetic headed Chromium cases. CI must keep its Xvfb display for headed Chromium on Ubuntu.
- Delayed Home/Detail hydration and DOM-replacement cases remain ordinary tests; none are converted to skips.
- The baseline CI workflow runs Ubuntu only. Windows offline CI parity remains a later release gate.

## Shared synthetic fixtures

`tests/fixtures/browser.py` provides reusable Home and Detail HTML builders, a route map that fulfills only local fixture pages and synthetic profile routes while aborting every other request, a temporary local account/profile builder, and workflow browser helpers. The workflow-specific autouse render-settle fixture is loaded by `tests/workflow/conftest.py`, not global test configuration. CLI account/result builders live in `tests/fixtures/cli.py`.

One scheduled-output integration case launches persistent Chromium headed and checks the same sanitized output contract. Other targeted boundary cases remain headed. All page content and profile directories are synthetic and temporary.

## Regression checklist

- [ ] Home / For You and actor verification reject wrong or unavailable context.
- [ ] Canonical permalink, author, and body provenance gate Detail navigation.
- [ ] Detail verification and Back revalidation preserve the selected identity.
- [ ] `READ_IN_PROGRESS` remains distinct from a new below-viewport lookahead at the final scroll boundary.
- [ ] Scroll attempts require measured feed movement and remain within the requested cap.
- [ ] Pending, deduplication, retry, and checkpoint tests preserve safe prior state and reject incomplete tails.
- [ ] Profile-lock contention, cancellation, timeout, browser shutdown, and checkpoint failure stay fail-closed.
- [ ] Every managed launch durably arms its guard under the profile lock; only verified context and Playwright shutdown can disarm it. Persistence failures, interrupted processes, malformed guard state, shutdown failures, recovery, and concurrent access remain fail-closed.
- [ ] Cleanup failure injection distinguishes context close from Playwright stop; the Windows offline stress test confirms headed Chromium children and locks are released after successful shutdown.
- [ ] Scheduled stdout and status contain only fixed codes and allowlisted aggregate counters.
- [ ] Re-run `uv run ruff check .`, `uv run pytest -q --maxfail=1`, and `git diff --check` on the exact review head.
- [ ] Confirm exact-head GitHub Actions success; report Windows and Linux lanes separately when both exist.
- [ ] Do not infer live Threads compatibility from local fixtures; owner UAT remains required.

## Production release gates

- CI runs the offline suite on both `ubuntu-latest` and `windows-latest` with
  Python 3.13 and `uv sync --locked`. Ubuntu installs Chromium with system
  dependencies and uses Xvfb; Windows installs the same pinned browser and
  runs the headed fixtures directly. Pytest's reported skip counts are
  captured per lane and must not be hidden with broad skip markers.
- The Windows lane also runs the PowerShell wrapper against a fake `uv`
  executable and verifies both bounded argument forwarding and exit-code
  propagation. It does not launch the application, read an account, or start
  Chromium through the wrapper.
- `scripts/check_structure.py` compares changed production modules with the PR
  base. New modules may not exceed 600 lines, new orchestration functions may
  not exceed 150 lines, and larger existing files/functions may not grow.
  Function spans are measured from the Python AST. The gate is incremental so
  unrelated baseline modules do not prevent a focused change from being
  reviewed.
- `git diff --check` runs against the PR base and exact commit. Review the
  workflow result for the same full commit SHA proposed for merge.
- `COMPLETE` in `operations/last_run.json` means a checkpoint was committed;
  incomplete Home coverage and skipped rows remain visible in cycle counters
  and fixed status fields. The status file is an atomic latest-run replacement,
  not a history store.
- `docs/RELEASE_CHECKLIST.md` is the ship/no-ship gate. Windows UAT, issue #23
  disposition, rollback rehearsal, and a separate Task Scheduler decision are
  owner evidence and cannot be inferred from CI.

## Baseline test-name mapping

Function names are unchanged. Each baseline node ID maps to the destination module below.

### Workflow

- `tests/test_workflow.py::test_relevance_classifier_is_conservative` → `tests/workflow/test_home_observation.py`
- `tests/test_workflow.py::test_job_seeking_and_hiring_terms_are_literal_and_independent` → `tests/workflow/test_home_observation.py`
- `tests/test_workflow.py::test_offline_chromium_cli_reads_once_and_resumes_without_double_counting` → `tests/workflow/test_home_observation.py`
- `tests/test_workflow.py::test_scheduled_offline_chromium_cli_suppresses_row_and_scroll_diagnostics` → `tests/workflow/test_scheduled_output.py`
- `tests/test_workflow.py::test_scheduled_offline_chromium_cli_suppresses_failure_diagnostics` → `tests/workflow/test_scheduled_output.py`
- `tests/test_workflow.py::test_scheduled_offline_chromium_cli_suppresses_reader_scroll_failure_details` → `tests/workflow/test_scheduled_output.py`
- `tests/test_workflow.py::test_first_viewport_relevance_scrolls_then_reads_and_restores_home` → `tests/workflow/test_home_observation.py`
- `tests/test_workflow.py::test_nested_feed_scroll_is_observed_and_no_movement_stops` → `tests/workflow/test_home_observation.py`
- `tests/test_workflow.py::test_non_article_permalink_cards_sample_and_verify_detail` → `tests/workflow/test_detail_back.py`
- `tests/test_workflow.py::test_detail_target_with_nested_and_adjacent_post_links_is_verified` → `tests/workflow/test_detail_back.py`
- `tests/test_workflow.py::test_detail_without_landmark_or_identity_links_uses_unique_matching_body` → `tests/workflow/test_detail_back.py`
- `tests/test_workflow.py::test_back_accepts_selected_post_after_vertical_layout_shift` → `tests/workflow/test_detail_back.py`
- `tests/test_workflow.py::test_detail_verification_failures_return_home_and_keep_retry_pending` → `tests/workflow/test_detail_back.py`
- `tests/test_workflow.py::test_detail_dom_replacement_does_not_mix_root_and_body_provenance` → `tests/workflow/test_detail_back.py`
- `tests/test_workflow.py::test_empty_detail_is_reported_unavailable_and_home_progress_is_saved` → `tests/workflow/test_checkpoint_pending.py`
- `tests/test_workflow.py::test_stale_pending_allows_one_verified_fallback_and_saves_remaining_pending` → `tests/workflow/test_checkpoint_pending.py`
- `tests/test_workflow.py::test_stale_pending_detail_failure_keeps_both_entries_for_retry` → `tests/workflow/test_checkpoint_pending.py`
- `tests/test_workflow.py::test_current_pending_beats_a_fresh_fallback` → `tests/workflow/test_checkpoint_pending.py`
- `tests/test_workflow.py::test_stale_pending_does_not_reopen_completed_posts_or_navigate_without_actionability` → `tests/workflow/test_checkpoint_pending.py`
- `tests/test_workflow.py::test_stale_pending_does_not_mask_incomplete_home_observations` → `tests/workflow/test_checkpoint_pending.py`
- `tests/test_workflow.py::test_failed_fallback_retries_and_completed_post_stays_deduped_across_ten_cycles` → `tests/workflow/test_checkpoint_pending.py`
- `tests/test_workflow.py::test_completed_fallback_is_not_opened_again_on_unchanged_feed` → `tests/workflow/test_checkpoint_pending.py`
- `tests/test_workflow.py::test_q_stops_pending_detail_without_back_or_checkpoint` → `tests/workflow/test_checkpoint_pending.py`
- `tests/test_workflow.py::test_delayed_home_feed_after_profile_return_is_sampled` → `tests/workflow/test_scan_hydration.py`
- `tests/test_workflow.py::test_transient_home_and_detail_hydration_recover_once` → `tests/workflow/test_scan_hydration.py`
- `tests/test_workflow.py::test_observation_rechecks_readiness_missed_before_wait_registration` → `tests/workflow/test_scan_hydration.py`
- `tests/test_workflow.py::test_streamed_row_survives_discovery_deadline_overrun_once` → `tests/workflow/test_scan_hydration.py`
- `tests/test_workflow.py::test_viewport_scan_cancellation_disposes_candidate_handles` → `tests/workflow/test_scan_hydration.py`
- `tests/test_workflow.py::test_attended_wait_can_extend_then_stop_without_checkpoint` → `tests/workflow/test_scan_hydration.py`
- `tests/test_workflow.py::test_delayed_wheel_and_detail_hydration_reports_changed_return_context` → `tests/workflow/test_scan_hydration.py`
- `tests/test_workflow.py::test_empty_scan_rescans_new_feed_but_changed_back_anchor_stops_without_checkpoint` → `tests/workflow/test_scan_hydration.py`
- `tests/test_workflow.py::test_feed_barrier_waits_through_body_hydration_and_dom_change` → `tests/workflow/test_scan_hydration.py`
- `tests/test_workflow.py::test_failed_selected_detail_saves_home_and_keeps_retry_pending` → `tests/workflow/test_detail_failures.py`
- `tests/test_workflow.py::test_detached_scroll_anchor_without_feed_displacement_stops_without_checkpoint` → `tests/workflow/test_detail_failures.py`
- `tests/test_workflow.py::test_detail_return_with_inactive_for_you_stops_without_checkpoint` → `tests/workflow/test_home_readiness.py`
- `tests/test_workflow.py::test_home_waits_for_delayed_strict_card_hydration` → `tests/workflow/test_home_readiness.py`
- `tests/test_workflow.py::test_home_readiness_skips_offscreen_permalink_for_visible_valid_card` → `tests/workflow/test_home_readiness.py`
- `tests/test_workflow.py::test_initial_feed_readiness_deadline_stops_without_checkpoint` → `tests/workflow/test_home_readiness.py`
- `tests/test_workflow.py::test_mixed_candidate_rows_do_not_block_a_verified_selected_post` → `tests/workflow/test_row_provenance.py`
- `tests/test_workflow.py::test_detail_author_provenance_mismatch_returns_home_with_retry_pending` → `tests/workflow/test_row_provenance.py`
- `tests/test_workflow.py::test_virtualized_detail_target_is_rediscovered_before_locator_click` → `tests/workflow/test_row_provenance.py`
- `tests/test_workflow.py::test_virtualized_replacement_with_new_identity_is_never_clicked` → `tests/workflow/test_row_provenance.py`
- `tests/test_workflow.py::test_replaced_home_row_gets_a_new_observation_id` → `tests/workflow/test_row_provenance.py`
- `tests/test_workflow.py::test_workflow_skips_detail_when_no_home_candidate_is_actionable` → `tests/workflow/test_row_provenance.py`
- `tests/test_workflow.py::test_checkpoint_save_failure_never_returns_a_saved_result` → `tests/workflow/test_row_provenance.py`
- `tests/test_workflow.py::test_unresolved_post_root_does_not_block_a_verified_selected_post` → `tests/workflow/test_row_provenance.py`
- `tests/test_workflow.py::test_ambiguous_pressable_row_recovers_only_its_timestamp_author_and_body` → `tests/workflow/test_row_provenance.py`
- `tests/test_workflow.py::test_card_provenance_component_verifies_canonical_url_author_and_body` → `tests/workflow/test_row_provenance.py`
- `tests/test_workflow.py::test_unresolved_ambiguous_row_does_not_block_a_verified_peer_checkpoint` → `tests/workflow/test_row_provenance.py`
- `tests/test_workflow.py::test_removed_ambiguous_row_gets_a_new_observation_identity` → `tests/workflow/test_row_provenance.py`
- `tests/test_workflow.py::test_closed_session_during_scan_is_classified_without_checkpoint` → `tests/workflow/test_scan_recovery.py`
- `tests/test_workflow.py::test_transient_execution_context_scan_error_is_retried_after_reverification` → `tests/workflow/test_scan_recovery.py`
- `tests/test_workflow.py::test_transient_scan_row_failure_retries_without_replaying_verified_posts` → `tests/workflow/test_scan_recovery.py`
- `tests/test_workflow.py::test_read_body_retry_uses_carried_verified_row_after_native_scroll` → `tests/workflow/test_scan_recovery.py`
- `tests/test_workflow.py::test_unrevalidated_scan_row_failure_stops_without_checkpoint` → `tests/workflow/test_scan_stops.py`
- `tests/test_workflow.py::test_invalid_workflow_transition_is_not_mapped_to_browser_loss` → `tests/workflow/test_scan_stops.py`
- `tests/test_workflow.py::test_other_playwright_scan_error_is_not_mapped_to_browser_loss` → `tests/workflow/test_scan_stops.py`
- `tests/test_workflow.py::test_detail_does_not_auto_scroll_an_offscreen_target` → `tests/workflow/test_scan_stops.py`
- `tests/test_workflow.py::test_detail_body_mismatch_returns_home_without_claiming_read` → `tests/workflow/test_scan_stops.py`
- `tests/test_workflow.py::test_overlapping_scan_completes_coverage_before_detail_and_checkpoint` → `tests/workflow/test_scan_stops.py`
- `tests/test_workflow.py::test_for_you_click_must_leave_an_active_marker_or_route` → `tests/workflow/test_session_lifecycle.py`
- `tests/test_workflow.py::test_actor_verification_scopes_sidebar_profile_text_and_fails_closed` → `tests/workflow/test_session_lifecycle.py`
- `tests/test_workflow.py::test_scroll_attempt_is_not_counted_without_observed_feed_movement` → `tests/workflow/test_session_lifecycle.py`
- `tests/test_workflow.py::test_default_desktop_profile_is_rejected_before_browser_startup` → `tests/workflow/test_session_lifecycle.py`
- `tests/test_workflow.py::test_direct_browser_workflow_rejects_busy_profile_and_releases_on_failure` → `tests/workflow/test_session_lifecycle.py`
- `tests/test_workflow.py::test_only_managed_browser_workflow_is_a_public_profile_entry_point` → `tests/workflow/test_session_lifecycle.py`
- `tests/test_workflow.py::test_concurrent_browser_workflows_reject_same_profile_before_checkpoint_access` → `tests/workflow/test_session_lifecycle.py`
- `tests/test_workflow.py::test_browser_workflows_for_different_profiles_can_run_concurrently` → `tests/workflow/test_session_lifecycle.py`
- `tests/test_workflow.py::test_browser_runtime_timeout_during_launch_releases_profile_lock` → `tests/workflow/test_session_lifecycle.py`
- `tests/test_workflow.py::test_browser_runtime_timeout_during_playwright_startup_attempts_shutdown` → `tests/workflow/test_session_lifecycle.py`
- `tests/test_workflow.py::test_browser_runtime_timeout_during_active_detail_closes_context_and_releases_lock` → `tests/workflow/test_session_lifecycle.py`
- `tests/test_workflow.py::test_external_browser_cancellation_propagates_after_cleanup` → `tests/workflow/test_session_lifecycle.py`
- `tests/test_workflow.py::test_cancellation_during_browser_cleanup_holds_lock_until_shutdown` → `tests/workflow/test_session_lifecycle.py`
- `tests/test_workflow.py::test_browser_cleanup_failure_is_reported_after_successful_cycle` → `tests/workflow/test_session_lifecycle.py`
- `tests/test_workflow.py::test_browser_workflow_detail_requires_tty_before_profile_or_browser_access` → `tests/workflow/test_session_lifecycle.py`
- `tests/test_workflow.py::test_relevance_search_finds_visible_post_after_two_observed_advances` → `tests/workflow/test_selection_provenance.py`
- `tests/test_workflow.py::test_overlapping_wheel_finds_intermediate_relevant_post` → `tests/workflow/test_selection_provenance.py`
- `tests/test_workflow.py::test_post_text_preview_escapes_terminal_and_bidi_controls` → `tests/workflow/test_selection_provenance.py`
- `tests/test_workflow.py::test_post_text_preview_escapes_unicode_format_characters` → `tests/workflow/test_selection_provenance.py`
- `tests/test_workflow.py::test_unverified_candidate_does_not_block_later_verified_selection` → `tests/workflow/test_selection_provenance.py`
- `tests/test_workflow.py::test_timestamp_permalink_accepts_safe_trailing_slash_and_ignores_bad_secondary` → `tests/workflow/test_selection_provenance.py`
- `tests/test_workflow.py::test_noncanonical_link_reasons_are_aggregate_private_and_skipped` → `tests/workflow/test_selection_provenance.py`
- `tests/test_workflow.py::test_query_text_does_not_make_a_profile_link_look_like_a_post_path` → `tests/workflow/test_selection_provenance.py`
- `tests/test_workflow.py::test_bounded_fallback_discovery_handles_dense_synthetic_timestamp_fixture` → `tests/workflow/test_selection_provenance.py`
- `tests/test_workflow.py::test_home_and_detail_canonicalize_multiline_inline_body_equally_without_logging` → `tests/workflow/test_selection_provenance.py`
- `tests/test_workflow.py::test_visible_body_segments_keep_utf16_surrogate_pairs_intact` → `tests/workflow/test_body_progress.py`
- `tests/test_workflow.py::test_partially_clipped_range_stays_missing_after_forward_wheels` → `tests/workflow/test_body_progress.py`
- `tests/test_workflow.py::test_partially_clipped_unicode_ranges_wait_until_fully_visible` → `tests/workflow/test_body_progress.py`
- `tests/test_workflow.py::test_body_range_depth_limit_marks_read_incomplete` → `tests/workflow/test_body_progress.py`
- `tests/test_workflow.py::test_lookahead_rows_are_separate_from_visited_body_gaps` → `tests/workflow/test_body_progress.py`
- `tests/test_workflow.py::test_body_progress_completes_once_when_remaining_ranges_become_visible` → `tests/workflow/test_body_progress.py`
- `tests/test_workflow.py::test_max_scrolls_12_recovers_body_after_next_scroll_and_checkpoints` → `tests/workflow/test_scan_budget.py`
- `tests/test_workflow.py::test_partial_body_does_not_block_a_verified_selected_post` → `tests/workflow/test_scan_budget.py`
- `tests/test_workflow.py::test_max_scrolls_12_checkpoints_with_a_new_incomplete_post` → `tests/workflow/test_scan_budget.py`
- `tests/test_workflow.py::test_body_text_changing_during_the_home_read_stays_unverified` → `tests/workflow/test_scan_budget.py`
- `tests/test_workflow.py::test_irrelevant_posts_exhaust_scroll_budget_as_completed_read_only_search` → `tests/workflow/test_scan_budget.py`
- `tests/test_workflow.py::test_total_scan_budget_checkpoints_verified_rows_and_zero_verified_still_fails` → `tests/workflow/test_scan_budget.py`
- `tests/test_workflow.py::test_modern_landmark_free_div_feed_verifies_cards_and_detail` → `tests/workflow/test_feed_observation.py`
- `tests/test_workflow.py::test_visible_card_without_permalink_is_skipped_without_fingerprint` → `tests/workflow/test_feed_observation.py`
- `tests/test_workflow.py::test_delayed_permalink_hydration_recovers_before_unsupported_stop` → `tests/workflow/test_feed_observation.py`
- `tests/test_workflow.py::test_body_fallback_span_rows_ignore_pressable_non_post_and_nurture_verified_post` → `tests/workflow/test_feed_observation.py`
- `tests/test_workflow.py::test_progressive_body_fallback_emits_row_before_reading_next_with_y_jitter` → `tests/workflow/test_feed_observation.py`
- `tests/test_workflow.py::test_three_verified_rows_stream_around_rejected_candidate_and_checkpoint_selection` → `tests/workflow/test_feed_observation.py`
- `tests/test_workflow.py::test_offscreen_nested_row_is_deferred_then_read_and_changed_back_anchor_stops` → `tests/workflow/test_feed_scrolling.py`
- `tests/test_workflow.py::test_tall_body_requires_overlapping_wheels_before_one_read` → `tests/workflow/test_feed_scrolling.py`
- `tests/test_workflow.py::test_pressable_fragment_progress_keeps_identity_through_native_wheels_and_detail_back` → `tests/workflow/test_feed_scrolling.py`
- `tests/test_workflow.py::test_moved_wheels_without_new_content_stop_with_a_bounded_gap` → `tests/workflow/test_feed_scrolling.py`
- `tests/test_workflow.py::test_pending_pressable_waits_for_missing_permalink_and_rescans` → `tests/workflow/test_feed_scrolling.py`
- `tests/test_workflow.py::test_unresolved_hydrating_pressable_is_skipped_after_verified_selection` → `tests/workflow/test_feed_scrolling.py`
- `tests/test_workflow.py::test_observed_unverified_row_does_not_block_a_measured_native_wheel` → `tests/workflow/test_feed_scrolling.py`
- `tests/test_workflow.py::test_ephemeral_gap_upgrade_keeps_below_fold_row_as_lookahead` → `tests/workflow/test_coverage_boundary.py`
- `tests/test_workflow.py::test_continuous_below_viewport_lookahead_does_not_block_fallback` → `tests/workflow/test_coverage_boundary.py`
- `tests/test_workflow.py::test_encountered_incomplete_body_keeps_fallback_blocked_after_scrolls` → `tests/workflow/test_coverage_boundary.py`
- `tests/test_workflow.py::test_last_scroll_boundary_distinguishes_partial_body_from_below_viewport_lookahead` → `tests/workflow/test_coverage_boundary.py`
- `tests/test_workflow.py::test_carried_candidate_status_is_kept_per_row` → `tests/workflow/test_coverage_boundary.py`
- `tests/test_workflow.py::test_relevant_visible_post_is_selected_when_initial_scroll_is_at_feed_end` → `tests/workflow/test_coverage_boundary.py`
- `tests/test_workflow.py::test_unverified_display_row_does_not_block_verified_selected_detail` → `tests/workflow/test_unverified_selection.py`
- `tests/test_workflow.py::test_two_row_cap_completes_with_skips_without_wheel` → `tests/workflow/test_unverified_selection.py`
- `tests/test_workflow.py::test_unmarked_row_body_hydration_is_rescanned_without_early_ready` → `tests/workflow/test_unverified_selection.py`
- `tests/test_workflow.py::test_final_feed_gate_failure_cannot_print_ready` → `tests/workflow/test_unverified_selection.py`
- `tests/test_workflow.py::test_unverified_keyword_mismatch_never_becomes_a_verified_match` → `tests/workflow/test_unverified_selection.py`
- `tests/test_workflow.py::test_unmarked_rows_use_one_overlapping_wheel_and_checkpoint_skips` → `tests/workflow/test_unverified_selection.py`
- `tests/test_workflow.py::test_explicit_observed_text_preview_contains_only_scoped_unverified_body` → `tests/workflow/test_unverified_selection.py`
- `tests/test_workflow.py::test_unverified_relevance_is_skipped_while_verified_rows_checkpoint` → `tests/workflow/test_unverified_selection.py`
- `tests/test_workflow.py::test_nested_scrollport_excludes_future_rows_and_uses_its_own_coordinates` → `tests/workflow/test_scroll_geometry.py`
- `tests/test_workflow.py::test_document_scroll_keeps_viewport_clip_and_reads_row_below_viewport` → `tests/workflow/test_scroll_geometry.py`
- `tests/test_workflow.py::test_scroll_engine_sends_small_native_wheels_and_measures_the_gesture` → `tests/workflow/test_scroll_geometry.py`
- `tests/test_workflow.py::test_sticky_row_with_scrolltop_change_is_not_reading_progress` → `tests/workflow/test_scroll_geometry.py`
- `tests/test_workflow.py::test_native_wheel_loads_new_visible_post_then_detail_back_and_checkpoint` → `tests/workflow/test_scroll_geometry.py`

### CLI

- `tests/test_cli.py::test_account_cli_keeps_profile_reference_and_actor_out_of_output` → `tests/cli/test_cli_commands.py`
- `tests/test_cli.py::test_account_cli_create_profile_registers_a_new_directory` → `tests/cli/test_cli_commands.py`
- `tests/test_cli.py::test_account_cli_creates_a_local_unverified_account` → `tests/cli/test_cli_commands.py`
- `tests/test_cli.py::test_nurture_rejects_an_unverified_local_account_before_browser_startup` → `tests/cli/test_cli_commands.py`
- `tests/test_cli.py::test_nurture_cli_requires_explicit_headed_mode` → `tests/cli/test_cli_commands.py`
- `tests/test_cli.py::test_show_post_text_requires_tty_before_data_or_browser_startup` → `tests/cli/test_cli_text_output.py`
- `tests/test_cli.py::test_detail_requires_tty_before_data_or_browser_startup` → `tests/cli/test_cli_text_output.py`
- `tests/test_cli.py::test_detail_cli_supports_short_and_legacy_run_without_duplicate_opt_in_text` → `tests/cli/test_cli_text_output.py`
- `tests/test_cli.py::test_cli_run_uses_the_workflow_lock_once` → `tests/cli/test_cli_commands.py`
- `tests/test_cli.py::test_runtime_budget_and_prompt_suppression_reach_short_and_legacy_workflows` → `tests/cli/test_cli_commands.py`
- `tests/test_cli.py::test_runtime_budget_must_be_positive_and_finite` → `tests/cli/test_cli_commands.py`
- `tests/test_cli.py::test_scheduled_failures_write_sanitized_status` → `tests/cli/test_cli_scheduled_output.py`
- `tests/test_cli.py::test_scheduled_success_records_only_safe_counts` → `tests/cli/test_cli_scheduled_output.py`
- `tests/test_cli.py::test_scheduled_uncheckpointed_cycle_is_stopped_with_nonzero_exit` → `tests/cli/test_cli_scheduled_output.py`
- `tests/test_cli.py::test_scheduled_missing_terms_hint_does_not_include_account_alias` → `tests/cli/test_cli_scheduled_output.py`
- `tests/test_cli.py::test_scheduled_runs_require_runtime_and_reject_text_opt_ins` → `tests/cli/test_cli_scheduled_output.py`
- `tests/test_cli.py::test_show_post_text_cli_passes_opt_in_only_after_tty_check` → `tests/cli/test_cli_text_output.py`
- `tests/test_cli.py::test_show_observed_text_requires_tty_before_data_or_browser_startup` → `tests/cli/test_cli_text_output.py`
- `tests/test_cli.py::test_show_observed_text_is_a_separate_tty_opt_in` → `tests/cli/test_cli_text_output.py`
- `tests/test_cli.py::test_attended_readiness_prompt_keeps_async_loop_responsive` → `tests/cli/test_cli_commands.py`
- `tests/test_cli.py::test_nurture_summary_includes_only_safe_aggregate_coverage_diagnostics` → `tests/cli/test_cli_text_output.py`
- `tests/test_cli.py::test_project_registers_only_the_short_tb_script` → `tests/cli/test_cli_commands.py`
- `tests/test_cli.py::test_module_runner_keeps_legacy_command_tree_available` → `tests/cli/test_cli_commands.py`
- `tests/test_cli.py::test_short_add_login_and_ls_commands` → `tests/cli/test_cli_commands.py`
- `tests/test_cli.py::test_short_terms_command_persists_normalized_terms_separately` → `tests/cli/test_cli_commands.py`
- `tests/test_cli.py::test_short_run_uses_saved_terms_and_documented_default_budgets` → `tests/cli/test_cli_commands.py`
- `tests/test_cli.py::test_one_time_run_terms_override_without_changing_saved_terms` → `tests/cli/test_cli_commands.py`
- `tests/test_cli.py::test_short_run_reports_missing_account_actor_and_terms` → `tests/cli/test_cli_commands.py`
- `tests/test_cli.py::test_short_run_login_required_error_is_safe` → `tests/cli/test_cli_commands.py`
