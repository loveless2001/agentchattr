"""Which summary-tree nodes to show for a stretch of channel history.

Pure math, no I/O. Units are leaves (one chat message each). A part (k, j)
is the tree node at level k, index j: it covers leaves [j·2^k, (j+1)·2^k).

The layout is OptChat's fold (spec §5.2), done from scratch per call: start
with every leaf, then, while the lines are over a byte budget, replace the
most due pair of adjacent sibling parts by their parent. A pair at level l
starting at leaf `start` is due (total - start) / 2^(l+2): oldest relative to
its size first, so detail fades with age while each level keeps about as many
lines.
"""

import heapq


def fold(total: int, budget: int, cost) -> list[tuple[int, int]]:
    """Parts tiling leaves [0, total), oldest first, within about `budget` bytes.

    cost(k, j) -> (bytes, shown): what part (k, j)'s line takes, and whether
    it is a real line. Pairs whose parent is shown are merged first; a parent
    that is not shown is merged into only when nothing else is left, so the
    budget holds even while much of the tree is still being written.
    """
    if total <= 0:
        return []
    sizes: dict[tuple[int, int], tuple[int, bool]] = {}

    def size(k: int, j: int) -> tuple[int, bool]:
        if (k, j) not in sizes:
            sizes[k, j] = cost(k, j)
        return sizes[k, j]

    view = {(0, j) for j in range(total)}
    used = sum(size(0, j)[0] for j in range(total))
    heap: list[tuple] = []

    def offer(k: int, i: int):
        """Queue merging (k, 2i) and (k, 2i+1) into (k+1, i), if both are shown."""
        if ((i + 1) << (k + 1)) > total or (k, 2 * i) not in view or (k, 2 * i + 1) not in view:
            return
        start = i << (k + 1)
        due = (total - start) / (1 << (k + 2))
        heapq.heappush(heap, (not size(k + 1, i)[1], -due, start, k, i))

    for i in range(total // 2):
        offer(0, i)
    while used > budget and heap:
        *_, k, i = heapq.heappop(heap)
        a, b = (k, 2 * i), (k, 2 * i + 1)
        view -= {a, b}
        view.add((k + 1, i))
        used += size(k + 1, i)[0] - size(*a)[0] - size(*b)[0]
        offer(k + 1, i >> 1)
    return sorted(view, key=lambda part: part[1] << part[0])
