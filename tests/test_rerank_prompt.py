import search


def test_default_rerank_prompt_is_shape_only():
    assert search.RERANK_PROMPT == search.SHAPE_RERANK_PROMPT
    assert "只看主杆件骨架" in search.RERANK_PROMPT
    assert "忽略荷载" in search.RERANK_PROMPT
    assert "支座符号细节" in search.RERANK_PROMPT
    assert "荷载位置和方向" not in search.RERANK_PROMPT


def test_legacy_rerank_prompt_kept_for_comparison():
    assert search.LEGACY_RERANK_PROMPT != search.SHAPE_RERANK_PROMPT
    assert "荷载位置和方向" in search.LEGACY_RERANK_PROMPT


def test_final_rerank_score_keeps_load_and_shape_blend():
    assert search.compute_final_rerank_score(1.0, 0.2) == 0.6
    assert search.compute_final_rerank_score(0.1, 0.95) == 0.525
    assert search.compute_final_rerank_score(0.5, 2.0) == 0.75
