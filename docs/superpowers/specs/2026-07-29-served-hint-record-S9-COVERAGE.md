# Served-hint record — S9 coverage map

**Increment:** S6 (last DoD gate — §9 executes against a live database).
**Guide:** `docs/superpowers/specs/2026-07-27-served-hint-record-design-V5-FROZEN.md` §9
(starts ~line 1078; 32 check rows).
**Status vocabulary:** `COVERED` | `ADDED` | `NOT-APPLICABLE`.

Counts (this map): **COVERED 17 / ADDED 15 / NOT-APPLICABLE 0**.

Notes carried into this map (not re-opened design):

- **J-05 / row 27.** Guide asserts `indexdef LIKE '%WHERE run_id IS NOT NULL%'`.
  `pg_get_indexdef` parenthesises the NullTest, so that literal never matches.
  Adopted assertion: `pg_index.indpred IS NOT NULL` (§11 Q9 panel preference).
  See ERRATA.
- **J-06 / rows 19 & 20.** §9's combined grant row was contradictory (local reader
  both must and must not INSERT). Split and covered by S2 grants tests.
- **D-2 query columns.** Rows that name `query_text` are read against BUILD-CHARTER
  D-2: storage is `query_hmac` by default; raw `query_text` only when
  `ARB_HINT_READ_QUERY_RAW=1`. Cap + `query_truncated` still apply.

| §9 check (quote enough to identify it) | Covering test (`file::test_name`) | Status |
|---|---|---|
| **(a) COMMIT, bus:** a record-intent entry processed by `HintReadConsumer` produces a `hint_read` row visible from a **second**, independent connection | `tests/arb_memory/test_hint_read_section9.py::test_section9_a_commit_bus_visible_from_second_connection` | ADDED |
| **(a) COMMIT, local:** `_record_local_read` on the production-shape (autocommit) connection produces a `hint_read` row visible from a **second**, independent connection | `tests/arb_memory/test_hint_read_search_wiring.py::test_section9_a_commit_local_visible_from_second_connection` | COVERED |
| **(b) ISOLATION, bus:** forcing the record-intent XADD to raise leaves `handle_read_request`'s reply byte-identical / raises nothing to the caller | `tests/arb_memory/test_hint_read_section9.py::test_section9_b_isolation_bus_xadd_failure_reply_byte_identical` (byte-identical hits/status); also `tests/arb_memory/test_hint_read_producer.py::test_recording_xadd_failure_never_fails_read` | ADDED |
| **(b) ISOLATION, local** — (i) non-autocommit conn_factory and (ii) spy `execute()` raises on `hint_read` INSERT, success + rejection paths | `tests/arb_memory/test_hint_read_search_wiring.py::test_section9_b_isolation_local_non_autocommit_success_and_rejection`; `tests/arb_memory/test_hint_read_search_wiring.py::test_section9_b_isolation_local_spy_insert_raises_success_and_rejection` | COVERED |
| Successful `memory_search()` e2e produces one `hint_read` + one `hint_read_hit` per hit with NON-NULL `hint_id` / `vector_distance` (H-02) | `tests/arb_memory/test_hint_read_search_wiring.py::test_h02_e2e_memory_search_records_real_retrieve_hit_shape` | COVERED |
| Redelivered stream entry does not double-record, and the row survives (is not dead-lettered) | `tests/arb_memory/test_hint_read_consumer.py::test_redelivery_is_idempotent` | COVERED |
| Clean redelivery → `HintReadSink.write` `"duplicate"`, no extra hits, not dead-lettered (H-10) | `tests/arb_memory/test_hint_read_consumer.py::test_redelivery_is_idempotent` | COVERED |
| Two deployments / different prefixes, same Redis id → two distinct rows / `read_id`s / `stream_entry_id`s (G-09, H-04) | `tests/arb_memory/test_hint_read_consumer.py::test_h04_cross_prefix_distinct_rows` | COVERED |
| Consumer commit precedes ack; kill between them leaves PEL; redelivery reaches same idempotent end state | `tests/arb_memory/test_hint_read_section9.py::test_section9_commit_precedes_ack_pel_redelivery_idempotent` | ADDED |
| Consumer tests run at **`autocommit=False`**, production-equivalent | `tests/arb_memory/test_hint_read_section9.py::test_section9_consumer_runs_at_autocommit_false` | ADDED |
| Malformed record-intent → `hint_read_deadletter` **before** `_ack`, `stream_entry_id` fully-qualified (H-04), not silently dropped (G-05) | `tests/arb_memory/test_hint_read_consumer.py::test_consumer_malformed_entry_deadletters`; `tests/arb_memory/test_hint_read_consumer.py::test_deadletter_malformed_and_redelivery_idempotent` | COVERED |
| Redelivery of already-dead-lettered entry does not produce a second deadletter row (G-05) | `tests/arb_memory/test_hint_read_consumer.py::test_deadletter_malformed_and_redelivery_idempotent` | COVERED |
| **Local tier reuses the cached connection for recording** — no second connect per read | `tests/arb_memory/test_hint_read_section9.py::test_section9_local_reuses_cached_connection` | ADDED |
| Forced failure on the SECOND `hint_read_hit` insert rolls back the PARENT (G-06) | `tests/arb_memory/test_hint_read_local_recorder.py::test_record_local_read_atomicity_rolls_back_parent_on_second_hit_failure` | COVERED |
| Local tier records every rejected/errored read — query-too-long, missing key, rate-limited, genuine handler exception | `tests/arb_memory/test_hint_read_section9.py::test_section9_local_records_every_rejection_class` (all four); partial prior cover in `test_over_cap_query_rejected_but_recorded_truncated` | ADDED |
| G-08 / H-08: same class ≤1 row / 60s; different class → own row; next window → second; **spy INSERT attempt count** | `tests/arb_memory/test_hint_read_search_wiring.py::test_g08_bounding_same_class_one_row_different_classes_two` (row counts); `tests/arb_memory/test_hint_read_section9.py::test_section9_g08_spy_insert_attempts_and_next_window` (attempt count + next window) | ADDED |
| Over-long query stored truncated, `query_truncated=true`, length equals cap — **both doors** (G-12); D-2 raw opt-in for text (local half pre-existed; bus half added in S6) | local: `tests/arb_memory/test_hint_read_search_wiring.py::test_over_cap_query_rejected_but_recorded_truncated`; bus: `tests/arb_memory/test_hint_read_section9.py::test_section9_over_long_query_truncated_on_bus_door` | ADDED |
| Deny-proof seeds ambient PUBLIC grants first, then proves revocation, for `hint_read`/`hint_read_hit` (and deadletter) (G-07) | `tests/arb_memory/test_hint_read_grants.py::test_local_reader_grants_revoke_seeded_public_on_all_three_hint_read_tables`; `tests/arb_memory/test_hint_read_grants.py::test_mcp_grants_revoke_seeded_public_on_all_three_hint_read_tables` | COVERED |
| Local reader role can `INSERT` into `hint_read`/`hint_read_hit` but CANNOT `SELECT` (seeded deny) | `tests/arb_memory/test_hint_read_grants.py::test_local_reader_can_insert_hint_read_but_not_select` | COVERED |
| `vault_export_role` cannot `INSERT` into `hint_read`/`hint_read_hit` (G-03) | `tests/arb_memory/test_hint_read_grants.py::test_vault_export_role_has_no_hint_read_access_after_local_reader_grants` | COVERED |
| Bus consumer role (real non-owner `CREATE ROLE` + `SET ROLE`) can `INSERT`/`SELECT` on the three tables **and** `nextval()` the deadletter sequence via real INSERT (H-01) | `tests/arb_memory/test_hint_read_grants.py::test_consumer_role_can_insert_hint_read_deadletter_via_set_role` | COVERED |
| Public-door role and local-reader role (non-owner pattern) have neither `INSERT` nor `SELECT` on the three tables (H-06) | `tests/arb_memory/test_hint_read_grants.py::test_mcp_grants_revoke_seeded_public_on_all_three_hint_read_tables`; `tests/arb_memory/test_hint_read_grants.py::test_local_reader_grants_revoke_seeded_public_on_all_three_hint_read_tables` | COVERED |
| Local reader still cannot write `hints`/`artefacts` | `tests/arb_memory/test_hint_read_section9.py::test_section9_local_reader_cannot_write_hints_or_artefacts_after_writer_grants` (also baseline `tests/arb_memory/test_local_reader_grants.py::test_local_reader_privileges`) | ADDED |
| Genuine empty result records `outcome='ok'`, `hit_count=0` | local: `tests/arb_memory/test_hint_read_section9.py::test_section9_empty_result_records_ok_zero_hits_local`; bus: `tests/arb_memory/test_hint_read_section9.py::test_section9_empty_result_records_ok_zero_hits_bus` | ADDED |
| Recording issues exactly one Redis command (record-intent XADD) and zero DB round-trips beyond the read (G-11 / H-09) | `tests/arb_memory/test_hint_read_section9.py::test_section9_g11_recording_is_one_xadd_zero_extra_db` | ADDED |
| `run_id`/`seat_id` are `NULL` on every row this slice writes, on both tiers | `tests/arb_memory/test_hint_read_section9.py::test_section9_run_id_seat_id_null_both_tiers` | ADDED |
| Partial index on `run_id` and `seat_id` — **via `pg_index.indpred IS NOT NULL`** (J-05 fix; not guide LIKE) | `tests/arb_memory/test_hint_read_section9.py::test_section9_partial_indexes_via_pg_index_indpred` | ADDED |
| `door` correctly distinguishes bus from local rows | `tests/arb_memory/test_hint_read_section9.py::test_section9_door_distinguishes_bus_from_local`; also `tests/arb_memory/test_hint_read_local_recorder.py::test_record_local_read_door_follows_argument` | ADDED |
| `withheld` on a hit matches `store.retrieve`'s returned value, not `artefact is None` | `tests/arb_memory/test_hint_read_section9.py::test_section9_withheld_matches_retrieve_not_artefact_none`; also `tests/arb_memory/test_hint_read_producer.py::test_withheld_round_trips_from_outer_level` | ADDED |
| `schema.sql` applied twice succeeds and leaves identical state including `hint_read_deadletter` | `tests/arb_memory/test_hint_read_schema.py::test_schema_sql_reappliable_with_hint_read_tables` | COVERED |
| `run_hint_read_purge()` retention role can `DELETE`+`SELECT` on `hint_read` | `tests/arb_memory/test_hint_read_retention.py::test_retention_role_can_delete_hint_read_via_set_role` | COVERED |
| `run_hint_read_purge()` never deletes from `hint_read_deadletter` | `tests/arb_memory/test_hint_read_retention.py::test_purge_expired_never_deletes_hint_read_deadletter` | COVERED |

## Placement choices

New checks live in `tests/arb_memory/test_hint_read_section9.py` unless a sibling file
already owned the mechanism (S1 schema, S2 grants, S3 recorder, S3b wiring, S4a
producer, S4b consumer, S5 retention). The over-long **local** half and G-08
**row-count** half stay in `test_hint_read_search_wiring.py`; S6 adds the bus half
and the spy-attempt / next-window half.

## Mutation pairs (ADDED tests only)

| Mutation (what broke) | Test that went red |
|---|---|
| Full-width `hint_read_run_idx` (drop `WHERE run_id IS NOT NULL`) | `test_section9_partial_indexes_via_pg_index_indpred` |
| `_conn` always reconnects | `test_section9_local_reuses_cached_connection` |
| Empty hits recorded as `outcome='error'` | `test_section9_empty_result_records_ok_zero_hits_local` |
| Second `redis.xadd` after the first | `test_section9_g11_recording_is_one_xadd_zero_extra_db` |
| `query_truncated` forced `"0"` | `test_section9_over_long_query_truncated_on_bus_door` |
| `apply_hint_read_local_writer_grants` also `GRANT INSERT ON hints` | `test_section9_local_reader_cannot_write_hints_or_artefacts_after_writer_grants` |
| G-08 bound removed (`should_record = True` always) | `test_section9_g08_spy_insert_attempts_and_next_window` |
| Skip recording when `rejection_class == "rate_limited"` | `test_section9_local_records_every_rejection_class` |
| Sink stores non-NULL `run_id`/`seat_id` defaults | `test_section9_run_id_seat_id_null_both_tiers` |
| Record-intent `except` re-raises | `test_section9_b_isolation_bus_xadd_failure_reply_byte_identical` |
| Sink hardcodes `door='local'` | `test_section9_door_distinguishes_bus_from_local` |
| `store.retrieve` always emits `withheld=False` | `test_section9_withheld_matches_retrieve_not_artefact_none` |
| Sink returns `"recorded"` without INSERT | `test_section9_a_commit_bus_visible_from_second_connection`, `test_section9_commit_precedes_ack_pel_redelivery_idempotent`, `test_section9_consumer_runs_at_autocommit_false` |
| Bus producer `hit_count` forced `"1"` | `test_section9_empty_result_records_ok_zero_hits_bus` |
