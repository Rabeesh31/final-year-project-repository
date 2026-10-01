"""
ds.py - Hand-written data structures (no library containers do the real work).

  DynamicArray : append O(1) amortised, remove O(n), index O(1)
  HashMap      : put/get/delete O(1) average (separate chaining, auto-resize)
  Trie         : insert/delete O(L), prefix search O(L + matches)   (L = word length)
  AVLTree      : insert/delete/search O(log n), range query O(log n + k)
  MaxHeap      : push/pop O(log n), build-heap O(n)
"""


# ---------------------------------------------------------------- DynamicArray
class DynamicArray:
    """Growable array: doubles its capacity when full (master list of projects)."""

    def __init__(self):
        self._cap, self._n = 4, 0
        self._a = [None] * self._cap

    def __len__(self):
        return self._n

    def __iter__(self):
        for i in range(self._n):
            yield self._a[i]

    def __getitem__(self, i):
        if not 0 <= i < self._n:
            raise IndexError(i)
        return self._a[i]

    def append(self, x):                      # O(1) amortised
        if self._n == self._cap:
            self._cap *= 2
            b = [None] * self._cap
            for i in range(self._n):
                b[i] = self._a[i]
            self._a = b
        self._a[self._n] = x
        self._n += 1

    def remove(self, x):                      # O(n): find, then shift left
        for i in range(self._n):
            if self._a[i] is x:
                for j in range(i, self._n - 1):
                    self._a[j] = self._a[j + 1]
                self._n -= 1
                self._a[self._n] = None
                return True
        return False


# --------------------------------------------------------------------- HashMap
class HashMap:
    """Separate-chaining hash table. Keys: int or str. djb2 hash for strings."""

    def __init__(self, cap=16):
        self._b = [[] for _ in range(cap)]
        self._n = 0

    @staticmethod
    def _hash(key):
        if isinstance(key, int):
            return key
        h = 5381
        for ch in str(key):
            h = ((h << 5) + h + ord(ch)) & 0xFFFFFFFF
        return h

    def _bucket(self, key):
        return self._b[self._hash(key) % len(self._b)]

    def put(self, key, value):                # O(1) average
        for pair in self._bucket(key):
            if pair[0] == key:
                pair[1] = value
                return
        self._bucket(key).append([key, value])
        self._n += 1
        if self._n / len(self._b) > 0.75:
            self._resize()

    def get(self, key, default=None):         # O(1) average
        for k, v in self._bucket(key):
            if k == key:
                return v
        return default

    def delete(self, key):                    # O(1) average
        b = self._bucket(key)
        for i, (k, _) in enumerate(b):
            if k == key:
                b.pop(i)
                self._n -= 1
                return True
        return False

    def _resize(self):                        # O(n), rare
        old = self._b
        self._b = [[] for _ in range(len(old) * 2)]
        self._n = 0
        for bucket in old:
            for k, v in bucket:
                self.put(k, v)

    def __contains__(self, key):
        return any(k == key for k, _ in self._bucket(key))

    def __len__(self):
        return self._n

    def items(self):
        for bucket in self._b:
            for k, v in bucket:
                yield k, v

    def keys(self):
        for k, _ in self.items():
            yield k


# ------------------------------------------------------------------------ Trie
class _TrieNode:
    __slots__ = ("children", "ids")

    def __init__(self):
        self.children = {}   # char -> node (a small dict per node is fine)
        self.ids = []        # project ids for words that END at this node


class Trie:
    """Prefix tree: word -> list of project ids."""

    def __init__(self):
        self.root = _TrieNode()

    def insert(self, word, pid):              # O(L)
        node = self.root
        for ch in word:
            node = node.children.setdefault(ch, _TrieNode())
        if pid not in node.ids:
            node.ids.append(pid)

    def delete(self, word, pid):              # O(L) - also prunes empty branches
        path, node = [], self.root
        for ch in word:
            if ch not in node.children:
                return
            path.append((node, ch))
            node = node.children[ch]
        if pid in node.ids:
            node.ids.remove(pid)
        for parent, ch in reversed(path):
            child = parent.children[ch]
            if child.ids or child.children:
                break
            del parent.children[ch]

    def starts_with(self, prefix):            # O(L + size of subtree)
        node = self.root
        for ch in prefix:
            node = node.children.get(ch)
            if node is None:
                return set()
        out, stack = set(), [node]
        while stack:                          # iterative DFS of the subtree
            n = stack.pop()
            out.update(n.ids)
            stack.extend(n.children.values())
        return out


# --------------------------------------------------------------------- AVLTree
class _AVLNode:
    __slots__ = ("key", "ids", "left", "right", "height")

    def __init__(self, key, pid):
        self.key, self.ids = key, [pid]
        self.left = self.right = None
        self.height = 1


def _h(n): return n.height if n else 0
def _upd(n): n.height = 1 + max(_h(n.left), _h(n.right))
def _bf(n): return _h(n.left) - _h(n.right)


def _rot_r(y):
    x = y.left; y.left = x.right; x.right = y
    _upd(y); _upd(x)
    return x


def _rot_l(x):
    y = x.right; x.right = y.left; y.left = x
    _upd(x); _upd(y)
    return y


def _balance(n):
    _upd(n)
    b = _bf(n)
    if b > 1:                                 # left heavy
        if _bf(n.left) < 0:
            n.left = _rot_l(n.left)
        return _rot_r(n)
    if b < -1:                                # right heavy
        if _bf(n.right) > 0:
            n.right = _rot_r(n.right)
        return _rot_l(n)
    return n


def _insert(n, key, pid):
    if n is None:
        return _AVLNode(key, pid)
    if key < n.key:
        n.left = _insert(n.left, key, pid)
    elif key > n.key:
        n.right = _insert(n.right, key, pid)
    else:
        if pid not in n.ids:
            n.ids.append(pid)
        return n
    return _balance(n)


def _pop_min(n):
    if n.left is None:
        return n.right, n
    n.left, m = _pop_min(n.left)
    return _balance(n), m


def _remove(n, key, pid):
    if n is None:
        return None
    if key < n.key:
        n.left = _remove(n.left, key, pid)
    elif key > n.key:
        n.right = _remove(n.right, key, pid)
    else:
        if pid in n.ids:
            n.ids.remove(pid)
        if n.ids:
            return n                          # other projects share this year
        if n.left is None: return n.right
        if n.right is None: return n.left
        n.right, m = _pop_min(n.right)        # replace with in-order successor
        n.key, n.ids = m.key, m.ids
    return _balance(n)


class AVLTree:
    """Self-balancing BST. key = year, value = list of project ids."""

    def __init__(self):
        self.root = None

    def insert(self, key, pid):               # O(log n)
        self.root = _insert(self.root, key, pid)

    def remove(self, key, pid):               # O(log n)
        self.root = _remove(self.root, key, pid)

    def search(self, key):                    # O(log n)
        n = self.root
        while n:
            if key == n.key: return list(n.ids)
            n = n.left if key < n.key else n.right
        return []

    def range(self, lo, hi):                  # O(log n + k)
        out = []

        def walk(n):
            if n is None: return
            if lo < n.key: walk(n.left)
            if lo <= n.key <= hi: out.extend(n.ids)
            if n.key < hi: walk(n.right)
        walk(self.root)
        return out

    def items(self, lo=-10**9, hi=10**9):     # in-order (key, count) pairs, O(k)
        out = []

        def walk(n):
            if n is None: return
            if lo < n.key: walk(n.left)
            if lo <= n.key <= hi: out.append((n.key, len(n.ids)))
            if n.key < hi: walk(n.right)
        walk(self.root)
        return out


# --------------------------------------------------------------------- MaxHeap
class MaxHeap:
    """Array-based binary max-heap of comparable tuples, e.g. (count, name)."""

    def __init__(self, items=None):
        self._a = list(items) if items else []
        for i in range(len(self._a) // 2 - 1, -1, -1):   # build-heap O(n)
            self._down(i)

    def __len__(self):
        return len(self._a)

    def push(self, x):                        # O(log n)
        self._a.append(x)
        self._up(len(self._a) - 1)

    def pop(self):                            # O(log n)
        top, last = self._a[0], self._a.pop()
        if self._a:
            self._a[0] = last
            self._down(0)
        return top

    def _up(self, i):
        a = self._a
        while i > 0:
            p = (i - 1) // 2
            if a[p] >= a[i]: break
            a[p], a[i] = a[i], a[p]
            i = p

    def _down(self, i):
        a, n = self._a, len(self._a)
        while True:
            l, r, m = 2 * i + 1, 2 * i + 2, i
            if l < n and a[l] > a[m]: m = l
            if r < n and a[r] > a[m]: m = r
            if m == i: break
            a[m], a[i] = a[i], a[m]
            i = m


def top_k(pairs, k):
    """Top-k (count, label) pairs using a max-heap: O(n + k log n)."""
    h = MaxHeap(pairs)
    return [h.pop() for _ in range(min(k, len(h)))]
