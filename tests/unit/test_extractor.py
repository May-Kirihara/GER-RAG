from ger_rag.core.extractor import extract_candidates


def test_returns_empty_for_noise_only():
    transcript = "\n".join(["ok", "了解", "ありがとう", "----", "  "])
    assert extract_candidates(transcript) == []


def test_user_preference_is_top_ranked():
    transcript = (
        "ok\n"
        "ユーザー: pip禁止。uvを使ってください\n"
        "今日はいい天気ですね\n"
    )
    candidates = extract_candidates(transcript, max_candidates=3)
    assert len(candidates) >= 1
    top = candidates[0]
    assert "uv" in top.content
    assert top.suggested_source == "user"
    assert "preference" in top.suggested_tags


def test_failure_line_is_extracted_with_troubleshooting_tag():
    transcript = "失敗: numpyにor演算子でValueError。原因と解決を記録"
    candidates = extract_candidates(transcript, max_candidates=3)
    assert len(candidates) == 1
    c = candidates[0]
    assert "troubleshooting" in c.suggested_tags
    assert any("失敗" in r or "エラー" in r for r in c.reasons)


def test_min_score_filters_weak_candidates():
    transcript = "今日は晴れています"  # numeric-free, no keyword
    assert extract_candidates(transcript) == []


def test_dedup_identical_lines():
    line = "失敗: 同じValueErrorに2回ハマったので原因をメモ"
    transcript = "\n".join([line, line, line])
    candidates = extract_candidates(transcript, max_candidates=5)
    assert len(candidates) == 1


def test_max_candidates_caps_output():
    transcript = "\n".join(
        f"決定 #{i}: 採用方針について {i*100} を確認" for i in range(10)
    )
    candidates = extract_candidates(transcript, max_candidates=3)
    assert len(candidates) == 3


def test_length_filter_drops_overlong_segments():
    transcript = "決定: " + ("a" * 10_000)
    assert extract_candidates(transcript, max_chars=200) == []
