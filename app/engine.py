"""单行校对核心算法。

合法读法：从位置 0 出发，若干词首尾相接（前一个词的 end 等于后一个词的
start），恰好到达行尾 line_end，并包含全部已确认词。

总分最高的读法通过分段动态规划求得；确认词把整行切成若干自由段，每段独立
做位置 DP。判定某词是否“待核”时，在其所属自由段禁用该词再算一次最优分，
若仍能拼出整行且总分差距不超过 score_gap，则存在一条分差内的合法读法不含
此词，标记为待核。
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Set, Tuple


@dataclass(frozen=True)
class Word:
    id: int
    start: int
    end: int
    text: str
    score: int


@dataclass
class ValidationError(Exception):
    detail: str


def normalize_candidates(
    line_end: int, raw: Sequence[dict]
) -> List[Word]:
    """校验并归一化候选。

    - 编号必须唯一；
    - 起止位置必须满足 0 <= start < end <= line_end，文字非空；
    - （起止位置, 文字）相同即为同一个词：分数也相同时合并去重，分数不同
      则视为输入矛盾，拒绝建任务。
    """
    if not raw:
        raise ValidationError("至少需要一个候选词")
    if len(raw) > 200:
        raise ValidationError("候选词最多 200 个")

    seen_ids: Set[int] = set()
    by_key: Dict[Tuple[int, int, str], Word] = {}
    words: List[Word] = []

    for item in raw:
        cid = item["id"]
        if cid in seen_ids:
            raise ValidationError(f"候选编号重复: {cid}")
        seen_ids.add(cid)

        start, end, text, score = (
            item["start"],
            item["end"],
            item["text"],
            item["score"],
        )
        if start < 0:
            raise ValidationError(f"候选 {cid} 的 start 不能为负")
        if start >= end:
            raise ValidationError(
                f"候选 {cid} 不合法: 需满足 start < end（start={start}, end={end}）"
            )
        if end > line_end:
            raise ValidationError(
                f"候选 {cid} 超出行尾: end={end} > line_end={line_end}"
            )
        if not text:
            raise ValidationError(f"候选 {cid} 的文字不能为空")

        key = (start, end, text)
        if key in by_key:
            existing = by_key[key]
            if existing.score != score:
                raise ValidationError(
                    f"候选 {cid} 与候选 {existing.id} 位置和文字相同但分数不同，"
                    "同一个词不能有两个分数"
                )
            # 完全相同的词，合并去重，保留首次出现的编号
            continue
        w = Word(id=cid, start=start, end=end, text=text, score=score)
        by_key[key] = w
        words.append(w)

    return words


def _better(cand: Tuple[int, Tuple[int, ...]],
            current: Optional[Tuple[int, Tuple[int, ...]]]
            ) -> Tuple[int, Tuple[int, ...]]:
    """分数优先取高；同分按编号元组字典序取小，保证结果确定性。"""
    if current is None:
        return cand
    if cand[0] > current[0]:
        return cand
    if cand[0] == current[0] and cand[1] < current[1]:
        return cand
    return current


def segment_best(
    lo: int,
    hi: int,
    by_start: Dict[int, List[Word]],
    forbidden: Set[int],
    boundary_breaks: Optional[Set[int]] = None,
    stats: Optional[dict] = None,
) -> Optional[Tuple[int, Tuple[int, ...]]]:
    """在自由段 [lo, hi) 内求最优拼合；不可达返回 None。

    返回 (总分, 按顺序的候选编号元组)。
    forbidden 中的词不可使用；boundary_breaks 中的位置是确认词边界，
    任何跨过边界的词也不可使用（替代读法不能越过已确认词）。

    位置 DP 只访问“有词可达”的位置：用最小堆按坐标升序弹出，每个
    可达位置入堆、弹出各一次，每条候选边松弛一次。遍历量与候选词数
    成正比，与坐标跨度无关——同一组候选的坐标等比例放大多少倍，
    访问的位置数都不变（stats["positions"] 可观测，供测试与演示）。
    """
    if lo == hi:
        return (0, ())
    breaks = boundary_breaks or set()
    dp: Dict[int, Tuple[int, Tuple[int, ...]]] = {lo: (0, ())}
    heap = [lo]
    while heap:
        pos = heapq.heappop(heap)
        if stats is not None:
            stats["positions"] = stats.get("positions", 0) + 1
        state = dp[pos]
        for w in by_start.get(pos, ()):
            if w.id in forbidden or w.end > hi:
                continue
            # 词内部不能穿过确认词边界
            if any(lo < b < w.end for b in breaks):
                continue
            cand = (state[0] + w.score, state[1] + (w.id,))
            nxt = dp.get(w.end)
            if nxt is None:
                # 首次到达该位置：入堆。w.end > pos，而堆按升序弹出，
                # 故它弹出时所有前驱（坐标都更小）已松弛完，dp[w.end] 即最终值
                dp[w.end] = cand
                heapq.heappush(heap, w.end)
            else:
                dp[w.end] = _better(cand, nxt)
    return dp.get(hi)


@dataclass
class Result:
    feasible: bool
    total_score: Optional[int]
    ordered_ids: List[int]
    review_ids: Set[int]
    conflict_reason: Optional[str] = None
    conflict_detail: Optional[str] = None


def compute_result(
    line_end: int,
    words: Sequence[Word],
    score_gap: int,
    confirmed_ids: Set[int],
    stats: Optional[dict] = None,
) -> Result:
    """依据当前已生效确认项重算读法与待核标记。

    stats 不为 None 时，把各自由段位置 DP 的遍历量累计进去
    （观测用，不影响结果）。
    """
    by_id = {w.id: w for w in words}
    try:
        confirmed = sorted(
            (by_id[cid] for cid in confirmed_ids),
            key=lambda w: (w.start, w.end, w.id),
        )
    except KeyError as exc:
        return Result(False, None, [], set(), "unknown_word",
                      f"已确认词不存在: {exc.args[0]}")

    # 确认词之间位置不得重叠
    for prev, cur in zip(confirmed, confirmed[1:]):
        if prev.end > cur.start:
            return Result(
                False, None, [], set(),
                "overlap",
                f"确认词 {prev.id}（{prev.start}-{prev.end}）与 "
                f"{cur.id}（{cur.start}-{cur.end}）位置重叠，无法同时成立",
            )

    by_start: Dict[int, List[Word]] = {}
    for w in words:
        by_start.setdefault(w.start, []).append(w)

    # 确认词的结束位置是自由段之间的硬边界，段内选词不得跨越
    breaks = {w.end for w in confirmed}

    # 确认词把整行切成自由段，逐段求最优拼合
    # free_segments: (lo, hi, 该段最优分, 该段最优编号序列)，与自由段一一对应
    free_segments: List[Tuple[int, int, int, Tuple[int, ...]]] = []
    pieces: List[Tuple[int, Tuple[int, ...]]] = []
    cursor = 0
    for cw in confirmed:
        seg = segment_best(cursor, cw.start, by_start, set(), breaks, stats)
        if seg is None:
            return Result(
                False, None, [], set(),
                "no_full_tiling",
                f"区间 [{cursor}, {cw.start}) 无法拼合到确认词 {cw.id}",
            )
        free_segments.append((cursor, cw.start, seg[0], seg[1]))
        pieces.append(seg)
        pieces.append((cw.score, (cw.id,)))
        cursor = cw.end
    tail = segment_best(cursor, line_end, by_start, set(), breaks, stats)
    if tail is None:
        return Result(
            False, None, [], set(),
            "no_full_tiling",
            f"区间 [{cursor}, {line_end}) 无法拼合到行尾",
        )
    free_segments.append((cursor, line_end, tail[0], tail[1]))
    pieces.append(tail)

    total = sum(p[0] for p in pieces)
    ordered_ids: List[int] = []
    for _, ids in pieces:
        ordered_ids.extend(ids)

    # 待核判定：对读法中的每个非确认词，禁用后重算其所属自由段
    review: Set[int] = set()
    for cid in ordered_ids:
        if cid in confirmed_ids:
            continue  # 已确认词必然出现在每条合法读法中，无需待核
        w = by_id[cid]
        seg_idx = next(
            i for i, (lo, hi, _, _) in enumerate(free_segments)
            if lo <= w.start and w.end <= hi
        )
        lo, hi, seg_best_score, _ = free_segments[seg_idx]
        alt = segment_best(lo, hi, by_start, {cid}, breaks, stats)
        if alt is None:
            continue  # 不含此词就拼不成整行：不存在缺少它的合法读法
        alt_total = total - seg_best_score + alt[0]
        if total - alt_total <= score_gap:
            review.add(cid)

    return Result(True, total, ordered_ids, review)
