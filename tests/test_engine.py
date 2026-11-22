"""引擎层单元测试：DP 读法、分差待核边界、确认冲突、归一化校验。"""
from __future__ import annotations

import pytest

from app import engine


def _w(cid, start, end, text, score=0):
    return engine.Word(id=cid, start=start, end=end, text=text, score=score)


# 行尾 4 的共享语料：
#  读法 A：1(0-2,5) + 2(2-4,5)        = 10
#  读法 B：3(0-1,3) + 4(1-4,6)        = 9
#  读法 C：5(0-4,4)                   = 4
def _corpus():
    return [
        _w(1, 0, 2, "天氣", 5),
        _w(2, 2, 4, "晴朗", 5),
        _w(3, 0, 1, "天", 3),
        _w(4, 1, 4, "氣晴朗", 6),
        _w(5, 0, 4, "天氣晴朗", 4),
    ]


def test_best_reading_and_gap_zero_no_review():
    res = engine.compute_result(4, _corpus(), score_gap=0, confirmed_ids=set())
    assert res.feasible
    assert res.total_score == 10
    assert res.ordered_ids == [1, 2]
    assert res.review_ids == set()  # 次优差 1 分，gap=0 时不标待核


def test_gap_boundary_marks_review():
    # gap=1：差 1 分的读法 B 也在范围内，A 中两个词都缺少过近的替代读法
    res = engine.compute_result(4, _corpus(), score_gap=1, confirmed_ids=set())
    assert res.total_score == 10
    assert res.ordered_ids == [1, 2]
    assert res.review_ids == {1, 2}


def test_tie_break_is_deterministic_by_id_tuple():
    words = [
        _w(5, 0, 1, "a", 1),
        _w(2, 1, 2, "b", 1),
        _w(9, 0, 2, "ab", 2),
    ]
    res = engine.compute_result(2, words, 0, set())
    assert res.total_score == 2
    # 同分两条读法：(9,) 与 (5,2)，编号元组字典序小者 (5,2) 胜出
    assert res.ordered_ids == [5, 2]


def test_alternative_reading_cannot_cross_confirmed_boundary():
    # 确认词 3 (0-1) 后，禁用词 4 不能让整词 5 (0-4) 越界顶替
    res = engine.compute_result(4, _corpus(), 10, {3})
    assert res.feasible
    assert res.ordered_ids == [3, 4]
    assert 4 not in res.review_ids  # 去掉 4 后该自由段拼不出来，不标待核


def test_review_scoring_is_per_segment_with_confirmed_word():
    # 行尾 6，确认词 9 占 [2,4)，左右两个自由段各自有近分替代
    words = [
        _w(1, 0, 2, "左甲", 5), _w(2, 0, 2, "左乙", 4),
        _w(9, 2, 4, "中", 10),
        _w(3, 4, 6, "右甲", 5), _w(4, 4, 6, "右乙", 5),
    ]
    res = engine.compute_result(6, words, 1, {9})
    assert res.feasible
    assert res.ordered_ids == [1, 9, 3]
    # 左：甲/乙差 1 在分差内 -> 1 待核；右：3/4 同分 -> 3 待核；9 已确认不标
    assert res.review_ids == {1, 3}


def test_confirm_changes_reading_and_drops_review():
    res = engine.compute_result(4, _corpus(), 1, {3})
    assert res.feasible
    assert res.ordered_ids == [3, 4]
    assert res.total_score == 9
    assert res.review_ids == set()  # 去掉 4 后拼不成整行，不标待核


def test_overlap_confirmation_conflicts():
    res = engine.compute_result(4, _corpus(), 1, {3, 1})
    assert not res.feasible
    assert res.conflict_reason == "overlap"


def test_non_overlap_but_untileable_conflicts():
    words = [_w(1, 0, 1, "a"), _w(2, 1, 3, "bc"), _w(3, 2, 3, "c")]
    res = engine.compute_result(3, words, 0, {3})
    assert not res.feasible
    assert res.conflict_reason == "no_full_tiling"


def test_confirmation_spanning_whole_line_then_nothing_else_fits():
    res = engine.compute_result(4, _corpus(), 1, {5})
    assert res.feasible
    assert res.ordered_ids == [5]
    # 再确认任何其它词都会与 0-4 的词重叠
    assert not engine.compute_result(4, _corpus(), 1, {5, 1}).feasible


def test_normalize_merges_identical_words():
    raw = [
        {"id": 1, "start": 0, "end": 1, "text": "a", "score": 2},
        {"id": 2, "start": 0, "end": 1, "text": "a", "score": 2},
    ]
    words = engine.normalize_candidates(1, raw)
    assert [w.id for w in words] == [1]


def test_normalize_rejects_same_word_with_different_scores():
    raw = [
        {"id": 1, "start": 0, "end": 1, "text": "a", "score": 2},
        {"id": 2, "start": 0, "end": 1, "text": "a", "score": 3},
    ]
    with pytest.raises(engine.ValidationError):
        engine.normalize_candidates(1, raw)


def test_normalize_rejects_bad_positions_and_duplicate_ids():
    with pytest.raises(engine.ValidationError):
        engine.normalize_candidates(3, [
            {"id": 1, "start": 2, "end": 2, "text": "a", "score": 1}
        ])
    with pytest.raises(engine.ValidationError):
        engine.normalize_candidates(3, [
            {"id": 1, "start": 0, "end": 4, "text": "a", "score": 1}
        ])
    with pytest.raises(engine.ValidationError):
        engine.normalize_candidates(3, [
            {"id": 1, "start": 0, "end": 1, "text": "a", "score": 1},
            {"id": 1, "start": 1, "end": 3, "text": "b", "score": 1},
        ])


def test_negative_scores_supported():
    words = [_w(1, 0, 2, "ab", -5), _w(2, 0, 1, "a", -1),
             _w(3, 1, 2, "b", -1)]
    res = engine.compute_result(2, words, 0, set())
    assert res.total_score == -2
    assert res.ordered_ids == [2, 3]


def test_empty_line_has_empty_reading():
    res = engine.compute_result(0, [], 0, set())
    assert res.feasible
    assert res.total_score == 0
    assert res.ordered_ids == []
    assert res.review_ids == set()


# 与 _corpus 相同的一组候选，坐标等比例放大 scale 倍
def _scaled_corpus(scale):
    return [
        _w(1, 0, 2 * scale, "天氣", 5),
        _w(2, 2 * scale, 4 * scale, "晴朗", 5),
        _w(3, 0, 1 * scale, "天", 3),
        _w(4, 1 * scale, 4 * scale, "氣晴朗", 6),
        _w(5, 0, 4 * scale, "天氣晴朗", 4),
    ]


def test_sparse_coordinates_same_result_at_any_scale():
    # 同一组候选坐标等比例放大：最高分读法、同分取舍、分差待核都不变
    for scale in (1, 10**3, 10**6):
        res = engine.compute_result(4 * scale, _scaled_corpus(scale), 1, set())
        assert res.feasible
        assert res.total_score == 10
        assert res.ordered_ids == [1, 2]
        assert res.review_ids == {1, 2}


def test_decode_traversal_does_not_grow_with_coordinate_span():
    # 解码只访问“有词可达”的位置：坐标等比例放大，遍历量不变
    visits = []
    for scale in (1, 10**3, 10**6, 10**9):
        stats = {}
        res = engine.compute_result(
            4 * scale, _scaled_corpus(scale), 1, set(), stats=stats
        )
        assert res.ordered_ids == [1, 2]
        visits.append(stats["positions"])
    assert len(set(visits)) == 1  # 各放大倍数下访问的位置数完全相同


def test_huge_sparse_line_decodes():
    # 行尾 10**9、全行只有 4 个候选：逐坐标遍历的旧实现会卡住，
    # 现在只访问有词的少数位置
    words = [
        _w(1, 0, 400_000_000, "甲", 7),
        _w(2, 400_000_000, 1_000_000_000, "乙", 7),
        _w(3, 0, 1_000_000_000, "丙", 4),
        _w(4, 100_000_000, 200_000_000, "丁", 1),
    ]
    res = engine.compute_result(1_000_000_000, words, 1, set())
    assert res.feasible
    assert res.ordered_ids == [1, 2]
    assert res.total_score == 14
    # 唯一替代读法 3 号差 10 分，超出分差 1：两个词都不待核
    assert res.review_ids == set()
    # 分差放宽到 10：4 分的整词读法进入分差，两个词都待核
    res2 = engine.compute_result(1_000_000_000, words, 10, set())
    assert res2.review_ids == {1, 2}
