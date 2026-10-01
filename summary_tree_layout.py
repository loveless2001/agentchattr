"""Which summary-tree nodes to show when an agent reads a channel summary.

Pure math, no I/O. Units are leaf blocks (each leaf summarizes a fixed run of
chat messages). A node is an aligned power-of-two range [lo, hi) of leaves; a
node of size 2**k lives at tree level k, index lo >> k.

The layout is coarse for old history and fine for recent history: a node is
shown whole only while its size is small relative to how far back it starts,
so detail decays with age (the idea behind OptMem's `wake`).
"""


def node_level(lo: int, hi: int) -> int:
    """Tree level of the aligned node [lo, hi)."""
    return (hi - lo).bit_length() - 1


def _tile(total: int, alpha: float) -> list[tuple[int, int]]:
    """Tile [0, total) with aligned power-of-two nodes.

    A node stays whole when it is complete (ends at or before `total`) and its
    size is at most `alpha` times its distance from the present. Larger alpha
    keeps bigger nodes whole, so it yields fewer, coarser lines.
    """
    span = 1
    while span < total:
        span *= 2
    tiles: list[tuple[int, int]] = []
    stack = [(0, span)]
    while stack:
        lo, hi = stack.pop()
        if lo >= total:
            continue
        size = hi - lo
        if size > 1 and (hi > total or size > alpha * (total - lo)):
            mid = lo + size // 2
            stack.append((mid, hi))
            stack.append((lo, mid))
        else:
            tiles.append((lo, hi))
    tiles.sort()
    return tiles


def pick_nodes(total: int, budget: int) -> list[tuple[int, int]]:
    """Nodes to render for `total` leaves within about `budget` lines.

    If every leaf fits, each leaf is its own line. Otherwise binary-search the
    coarsest-acceptable alpha, then spend any lines left over splitting the
    newest splittable node, because recent detail is worth the most.
    """
    if total <= 0:
        return []
    budget = max(1, budget)
    if total <= budget:
        return [(i, i + 1) for i in range(total)]
    lo, hi = 0.0, float(total)
    for _ in range(60):
        mid = (lo + hi) / 2
        if len(_tile(total, mid)) > budget:
            lo = mid
        else:
            hi = mid
    nodes = _tile(total, hi)
    while len(nodes) < budget:
        newest = next(
            (i for i in range(len(nodes) - 1, -1, -1) if nodes[i][1] - nodes[i][0] > 1),
            None,
        )
        if newest is None:
            break
        a, b = nodes[newest]
        mid = (a + b) // 2
        nodes[newest:newest + 1] = [(a, mid), (mid, b)]
    return nodes
