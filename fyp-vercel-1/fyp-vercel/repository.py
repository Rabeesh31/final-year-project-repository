"""
repository.py - Project model + ProjectRepository (the data-structure layer's public face).

The API layer only talks to ProjectRepository. Every search goes through the custom
indexes; there are no SQL queries or list scans for filtering.

  DynamicArray -> master list of all Project objects
  HashMap      -> id -> Project, domain -> ids, technology -> ids
  Trie         -> title words -> ids   (prefix / keyword search)
  AVLTree      -> year -> ids          (year range queries)
  MaxHeap      -> top-K domains by project count
"""
import re
import time
from ds import DynamicArray, HashMap, Trie, AVLTree, top_k


class Project:
    FIELDS = ("id", "title", "abstract", "domain", "technologies", "year", "source_url",
              "uploaded_by", "file_name", "file_path", "featured", "verified",
              "created_at", "updated_at")

    def __init__(self, **kw):
        for f in self.FIELDS:
            setattr(self, f, kw.get(f))
        self.technologies = list(self.technologies or [])
        self.featured, self.verified = bool(self.featured), bool(self.verified)

    def to_dict(self):
        return {f: getattr(self, f) for f in self.FIELDS}


def _words(title):
    return set(re.findall(r"[a-z0-9]+", title.lower()))


class ProjectRepository:
    def __init__(self):
        self.projects = DynamicArray()      # master list
        self.by_id = HashMap()              # id -> Project             O(1)
        self.trie = Trie()                  # title words -> ids
        self.domain_idx = HashMap()         # domain -> [ids]
        self.tech_idx = HashMap()           # tech (lowercase) -> [ids]
        self.tech_names = HashMap()         # tech (lowercase) -> display name
        self.year_idx = AVLTree()           # year -> [ids]
        self.next_id = 1

    # ---- index maintenance -------------------------------------------------
    def _index(self, p):
        for w in _words(p.title):
            self.trie.insert(w, p.id)
        self._map_add(self.domain_idx, p.domain, p.id)
        for t in p.technologies:
            self._map_add(self.tech_idx, t.lower(), p.id)
            self.tech_names.put(t.lower(), t)
        self.year_idx.insert(p.year, p.id)

    def _unindex(self, p):
        for w in _words(p.title):
            self.trie.delete(w, p.id)
        self._map_del(self.domain_idx, p.domain, p.id)
        for t in p.technologies:
            self._map_del(self.tech_idx, t.lower(), p.id)
        self.year_idx.remove(p.year, p.id)

    @staticmethod
    def _map_add(m, key, pid):
        lst = m.get(key)
        if lst is None:
            m.put(key, [pid])
        elif pid not in lst:
            lst.append(pid)

    @staticmethod
    def _map_del(m, key, pid):
        lst = m.get(key)
        if lst and pid in lst:
            lst.remove(pid)
            if not lst:
                m.delete(key)

    # ---- CRUD ----------------------------------------------------------------
    def load(self, data):
        """Rebuild all indexes from a saved dict (called at start-up)."""
        p = Project(**data)
        self.projects.append(p); self.by_id.put(p.id, p); self._index(p)
        self.next_id = max(self.next_id, p.id + 1)
        return p

    def add(self, data, uploaded_by, created_at=None):
        now = created_at or time.time()
        p = Project(id=self.next_id, uploaded_by=uploaded_by, created_at=now, updated_at=now, **data)
        self.next_id += 1
        self.projects.append(p); self.by_id.put(p.id, p); self._index(p)
        return p

    def get(self, pid):
        return self.by_id.get(pid)

    def update(self, pid, changes):
        p = self.get(pid)
        if not p: return None
        self._unindex(p)                    # remove old keys, apply, re-index
        for k, v in changes.items():
            setattr(p, k, v)
        p.updated_at = time.time()
        self._index(p)
        return p

    def delete(self, pid):
        p = self.get(pid)
        if not p: return None
        self._unindex(p)
        self.projects.remove(p); self.by_id.delete(pid)
        return p

    # ---- search --------------------------------------------------------------
    def search(self, title="", domain="", tech="", year_from=None, year_to=None,
               predicate=None, featured_first=False):
        """Each filter yields a set of ids from its index; we intersect them."""
        cand = None

        def narrow(ids):
            nonlocal cand
            cand = ids if cand is None else cand & ids

        for tok in re.findall(r"[a-z0-9]+", title.lower()):   # every word is a prefix
            narrow(self.trie.starts_with(tok))
        if domain:
            narrow(set(self.domain_idx.get(domain, [])))
        if tech:
            narrow(set(self.tech_idx.get(tech.lower(), [])))
        if year_from is not None or year_to is not None:
            narrow(set(self.year_idx.range(year_from if year_from is not None else -10**9,
                                           year_to if year_to is not None else 10**9)))
        rows = list(self.projects) if cand is None else [self.by_id.get(i) for i in cand]
        if predicate:
            rows = [p for p in rows if predicate(p)]
        rows.sort(key=lambda p: (not p.featured if featured_first else 0, -p.created_at))
        return rows

    # ---- analytics -----------------------------------------------------------
    def analytics(self, year_from=None, year_to=None, k=5):
        lo = year_from if year_from is not None else -10**9
        hi = year_to if year_to is not None else 10**9
        ids = set(self.year_idx.range(lo, hi))
        counts = HashMap()
        for i in ids:
            d = self.by_id.get(i).domain
            counts.put(d, (counts.get(d) or 0) + 1)
        per_domain = sorted(([d, c] for d, c in counts.items()), key=lambda x: -x[1])
        per_year = self.year_idx.items(lo, hi)
        recent = sorted((self.by_id.get(i) for i in ids), key=lambda p: -p.created_at)
        month_ago = time.time() - 30 * 86400
        return {
            "total": len(ids),
            "domains": len(per_domain),
            "years_covered": len(per_year),
            "last_30_days": sum(1 for p in recent if p.created_at >= month_ago),
            "per_domain": per_domain,
            "per_year": [[y, c] for y, c in per_year],
            "top_domains": [[d, c] for c, d in top_k([(c, d) for d, c in per_domain], k)],  # max-heap
            "recent": [p.to_dict() for p in recent[:5]],
        }

    def meta(self):
        return {"domains": sorted(self.domain_idx.keys()),
                "technologies": sorted(v for _, v in self.tech_names.items()),
                "years": [y for y, _ in self.year_idx.items()]}
