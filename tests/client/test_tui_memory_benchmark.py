from scripts.tui_memory_bench import parse_processes, process_tree, summarize


def test_current_rss_is_normalized_and_tree_excludes_unrelated_processes():
    records = parse_processes("10 1 100 python launcher\n11 10 50 hex-tui\n12 11 200 python acp\n20 1 999 other\n")
    tree = process_tree(records, 10)
    assert [p["pid"] for p in tree] == [10, 11, 12]
    assert sum(p["rss_bytes"] for p in tree) == 350 * 1024
    assert records[10]["command"] == "python launcher"


def test_peak_is_simultaneous_not_sum_of_independent_peaks():
    samples = [
        {"tree_rss_bytes": 120, "processes": [{"pid": 1, "rss_bytes": 100}, {"pid": 2, "rss_bytes": 20}]},
        {"tree_rss_bytes": 110, "processes": [{"pid": 1, "rss_bytes": 10}, {"pid": 2, "rss_bytes": 100}]},
    ]
    report = summarize(samples)
    assert report["simultaneous_tree_peak_rss_bytes"] == 120
    assert report["sampled_process_peaks_rss_bytes"] == {"1": 100, "2": 100}
    assert report["last_tree_rss_bytes"] == 110


def test_missing_root_and_empty_samples_are_supported():
    assert process_tree(parse_processes("2 1 20 python"), 100) == []
    assert summarize([])["simultaneous_tree_peak_rss_bytes"] == 0
