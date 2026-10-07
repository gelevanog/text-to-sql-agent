"""Schema retrieval: pick the tables, semantic views and metrics a question needs, add the tables that connect them,
and render a compact schema context for the prompt (never the whole schema, unless the full-dump ablation asks).

Scoring is BM25 over one document per table, view and metric (names, descriptions, synonyms, column names and
sample values), on the question expanded with the semantic layer's synonyms. An optional embedding model can be
fused in (`TALLY_EMBEDDINGS=openrouter`); nothing runs locally.
"""

from __future__ import annotations

import math
import re
from collections import Counter, deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from tally.schema.catalog import Catalog, TableInfo

_STOP = frozenset(
    "a an and are as at be by did do does for from had has have how i in is it its last me my of on or our per show "
    "that the their them then there these this to us was we were what when where which who why will with you your "
    "give list tell many much than vs versus between each all any one".split()
)


def tokenize(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", text.lower().replace("_", " "))
    return [_stem(w) for w in words if w not in _STOP and len(w) > 1]


def _stem(word: str) -> str:
    for suffix, keep in (("ies", "y"), ("sses", "ss"), ("ing", ""), ("ed", ""), ("es", ""), ("s", "")):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3 and not word.endswith("ss"):
            return word[: -len(suffix)] + keep
    return word


@dataclass(frozen=True)
class Doc:
    kind: str  # table | view | metric
    name: str
    text: str


class BM25:
    def __init__(self, docs: Sequence[Doc], k1: float = 1.4, b: float = 0.6) -> None:
        self.docs = list(docs)
        self.k1, self.b = k1, b
        self.tokens = [Counter(tokenize(d.text)) for d in self.docs]
        self.lengths = [sum(t.values()) for t in self.tokens]
        self.avg = sum(self.lengths) / max(len(self.lengths), 1)
        df: Counter[str] = Counter()
        for tokens in self.tokens:
            df.update(tokens.keys())
        n = len(self.docs)
        self.idf = {term: math.log(1 + (n - f + 0.5) / (f + 0.5)) for term, f in df.items()}

    def scores(self, query: Iterable[str]) -> list[float]:
        terms = list(query)
        result: list[float] = []
        for tokens, length in zip(self.tokens, self.lengths, strict=True):
            score = 0.0
            for term in terms:
                tf = tokens.get(term, 0)
                if tf:
                    norm = tf * (self.k1 + 1) / (tf + self.k1 * (1 - self.b + self.b * length / self.avg))
                    score += self.idf.get(term, 0.0) * norm
            result.append(score)
        return result


EmbedFn = Callable[[list[str]], list[list[float]]]


@dataclass
class RetrievedContext:
    tables: list[str]
    views: list[str]
    metrics: list[str]
    joins: list[tuple[str, str]]
    scores: dict[str, float] = field(default_factory=dict)
    text: str = ""
    mode: str = "retrieval"

    def summary(self) -> dict[str, object]:
        return {
            "tables": self.tables,
            "views": self.views,
            "metrics": self.metrics,
            "joins": [f"{a} = {b}" for a, b in self.joins],
            "mode": self.mode,
        }


class SchemaRetriever:
    def __init__(
        self,
        catalog: Catalog,
        *,
        top_tables: int = 6,
        top_metrics: int = 3,
        top_views: int = 2,
        embed: EmbedFn | None = None,
        embedding_weight: float = 0.5,
    ) -> None:
        self.catalog = catalog
        self.top_tables = top_tables
        self.top_metrics = top_metrics
        self.top_views = top_views
        self.docs = self._documents()
        self.index = BM25(self.docs)
        self.embed = embed
        self.embedding_weight = embedding_weight
        self._doc_vectors: list[list[float]] | None = None
        self.graph = self._join_graph()

    # ---- documents --------------------------------------------------------------------------------------------
    def _documents(self) -> list[Doc]:
        layer = self.catalog.layer
        docs: list[Doc] = []
        for table in self.catalog.tables.values():
            docs.append(Doc("table", table.name, self._table_text(table)))
        for view in self.catalog.views.values():
            docs.append(Doc("view", view.name, self._table_text(view)))
        for name, metric in layer.metrics.items():
            parts = [
                name.replace("_", " "),
                metric.description,
                " ".join(metric.synonyms),
                metric.view or "",
                metric.sql,
            ]
            docs.append(Doc("metric", name, " ".join(parts)))
        return docs

    def _table_text(self, table: TableInfo) -> str:
        parts = [table.name.replace("_", " "), table.name, table.description, " ".join(table.synonyms)]
        parts.extend(self.catalog.layer.synonyms.get(table.name, []))
        for column in table.visible_columns:
            parts.append(column.name.replace("_", " "))
            parts.append(column.description)
            parts.extend(column.samples[:12])
        return " ".join(p for p in parts if p)

    def _join_graph(self) -> dict[str, set[str]]:
        graph: dict[str, set[str]] = {name: set() for name in self.catalog.tables}
        for left, right in self.catalog.joins:
            a, b = left.split(".")[0], right.split(".")[0]
            if a in graph and b in graph and a != b:
                graph[a].add(b)
                graph[b].add(a)
        return graph

    # ---- retrieval --------------------------------------------------------------------------------------------
    def query_terms(self, question: str) -> list[str]:
        terms = tokenize(question)
        for canonical in self.catalog.layer.expand_synonyms(question):
            terms.extend(tokenize(canonical))
        return terms

    def score(self, question: str) -> list[tuple[Doc, float]]:
        bm25 = self.index.scores(self.query_terms(question))
        top = max(bm25) if bm25 and max(bm25) > 0 else 1.0
        fused = [s / top for s in bm25]
        if self.embed is not None:
            if self._doc_vectors is None:
                self._doc_vectors = self.embed([d.text[:1500] for d in self.docs])
            query_vector = self.embed([question])[0]
            cosines = [_cosine(query_vector, v) for v in self._doc_vectors]
            low, high = min(cosines), max(cosines)
            span = (high - low) or 1.0
            fused = [f + self.embedding_weight * (c - low) / span for f, c in zip(fused, cosines, strict=True)]
        return sorted(zip(self.docs, fused, strict=True), key=lambda pair: -pair[1])

    def retrieve(self, question: str, *, extra_tables: Iterable[str] = (), full: bool = False) -> RetrievedContext:
        if full:
            context = RetrievedContext(
                tables=sorted(self.catalog.tables),
                views=sorted(self.catalog.views),
                metrics=sorted(self.catalog.layer.metrics),
                joins=list(self.catalog.joins),
                mode="full",
            )
            context.text = render_context(self.catalog, context)
            return context
        ranked = self.score(question)
        best = ranked[0][1] if ranked else 0.0
        floor = 0.2 * best
        tables: list[str] = []
        views: list[str] = []
        metrics: list[str] = []
        scores: dict[str, float] = {}
        for doc, value in ranked:
            if value <= 0 or value < floor:
                continue
            if doc.kind == "table" and len(tables) < self.top_tables:
                tables.append(doc.name)
            elif doc.kind == "view" and len(views) < self.top_views:
                views.append(doc.name)
            elif doc.kind == "metric" and len(metrics) < self.top_metrics:
                metrics.append(doc.name)
            else:
                continue
            scores[f"{doc.kind}:{doc.name}"] = round(value, 3)
        for metric in metrics:
            view = self.catalog.layer.metrics[metric].view
            if view and view in self.catalog.views and view not in views:
                views.append(view)
        for name in extra_tables:
            if name in self.catalog.tables and name not in tables:
                tables.append(name)
            elif name in self.catalog.views and name not in views:
                views.append(name)
        tables = self._connect(tables)
        joins = [(a, b) for a, b in self.catalog.joins if a.split(".")[0] in tables and b.split(".")[0] in tables]
        joins.extend(self._view_joins(views, tables))
        context = RetrievedContext(tables=tables, views=views, metrics=metrics, joins=joins, scores=scores)
        context.text = render_context(self.catalog, context)
        return context

    def _connect(self, tables: list[str], max_extra: int = 3) -> list[str]:
        """Add the intermediate tables on the shortest join path between every pair of selected tables."""
        selected = list(tables)
        extra = 0
        for i, source in enumerate(tables):
            for target in tables[i + 1 :]:
                path = self._path(source, target)
                for node in path[1:-1]:
                    if node not in selected and extra < max_extra:
                        selected.append(node)
                        extra += 1
        return selected

    def _path(self, source: str, target: str) -> list[str]:
        previous: dict[str, str | None] = {source: None}
        queue = deque([source])
        while queue:
            node = queue.popleft()
            if node == target:
                path = [node]
                while (parent := previous[path[-1]]) is not None:
                    path.append(parent)
                return path[::-1]
            for neighbour in sorted(self.graph.get(node, ())):
                if neighbour not in previous:
                    previous[neighbour] = node
                    queue.append(neighbour)
        return []

    def _view_joins(self, views: list[str], tables: list[str]) -> list[tuple[str, str]]:
        """A view column with the same name as a base table's foreign key joins the same way."""
        refs = {left.split(".")[1]: right for left, right in self.catalog.joins}
        joins: list[tuple[str, str]] = []
        for view in views:
            for column in self.catalog.views[view].columns:
                ref = refs.get(column.name)
                if ref and ref.split(".")[0] in tables:
                    joins.append((f"{view}.{column.name}", ref))
        return joins


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def _column_line(
    column_name: str,
    type_: str,
    *,
    pk: bool = False,
    ref: str | None = None,
    desc: str = "",
    samples: Sequence[str] = (),
) -> str:
    line = f"  {column_name} {type_}".rstrip()
    if pk:
        line += " PK"
    if ref:
        line += f" -> {ref}"
    if samples:
        shown = ", ".join(f"'{s}'" for s in samples[:12])
        line += f" values: {shown}"
    if desc:
        line += f" -- {desc}"
    return line


def render_context(catalog: Catalog, context: RetrievedContext) -> str:
    layer = catalog.layer
    out: list[str] = []
    if context.views:
        out.append("### Semantic views (select from them like tables; they already apply the business rules)")
        for name in context.views:
            view = catalog.views[name]
            out.append(f"{name} -- {view.description}")
            out.extend(_column_line(c.name, c.type, desc=c.description, samples=c.samples) for c in view.columns)
        out.append("")
    out.append("### Tables")
    for name in context.tables:
        table = catalog.tables[name]
        header = f"{name} ({table.row_count:,} rows)"
        if table.description:
            header += f" -- {table.description}"
        out.append(header)
        out.extend(
            _column_line(c.name, c.type, pk=c.primary_key, ref=c.references, desc=c.description, samples=c.samples)
            for c in table.visible_columns
        )
    if context.metrics:
        out.append("")
        out.append("### Metrics")
        for name in context.metrics:
            metric = layer.metrics[name]
            where = f" over {metric.view}" if metric.view else ""
            out.append(f"- {name} = {metric.sql}{where} -- {metric.description}")
    if context.joins:
        out.append("")
        out.append("### Join paths")
        out.extend(f"- {a} = {b}" for a, b in context.joins)
    if layer.rules:
        out.append("")
        out.append("### Business rules")
        out.extend(f"- {' '.join(rule.split())}" for rule in layer.rules)
    return "\n".join(out)
