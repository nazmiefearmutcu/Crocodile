from scripts.benchmark_raw_capture import benchmark_capture


def test_counter_coverage_uses_source_decisions_not_repeated_replay(recorded_json_session):
    reader, _ = recorded_json_session
    result = benchmark_capture(reader, repeats=2)
    assert result["received_frames"] == 5
    assert result["new_depth_events"] == 3
    assert result["stale_or_duplicate_depth_frames"] == 2
    assert result["fsynced_records"] == reader.manifest.fsynced_ordinal
    assert result["queued_records"] == result["written_records"] == result["fsynced_records"]
    assert result["snapshot_limit_per_side"] == 1000
    assert result["replay_records_total"] == 2 * result["canonical_records_per_pass"]
    assert result["replay_records_per_second"] > 0
