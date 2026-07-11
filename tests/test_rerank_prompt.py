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
