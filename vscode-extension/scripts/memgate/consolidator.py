"""Consolidator — TF-IDF keyword extraction and MMR representative selection."""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from memgate.store import MemoryStore


@dataclass
class ClusterSummary:
    """Summary of a topic cluster."""

    cluster_id: int
    keywords: list[str]
    representative_ids: list[str]
    centroid_embedding: np.ndarray
    n_memories: int
    updated_at: float = 0.0


# Simple tokenizer — splits on non-alphanumeric, lowercases
_SPLIT_RE = re.compile(r"[^a-z0-9]+")

# Common English stopwords to filter out
_STOPWORDS = frozenset(
    "a an the is are was were be been being have has had do does did "
    "will would shall should may might can could am not no nor and but "
    "or if then else for to of in on at by with from as this that it "
    "its he she they them their his her we our you your my me him us "
    "so up out about into over after before between through during just "
    "also very much more most than too all any each every both few many "
    "some such only own same other another new old get got".split()
)


def _tokenize(text: str) -> list[str]:
    """Tokenize text into lowercase alpha-numeric tokens."""
    return [
        w for w in _SPLIT_RE.split(text.lower())
        if w and len(w) > 2 and w not in _STOPWORDS
    ]


class Consolidator:
    """Cluster-level extractive summarization.

    - TF-IDF keyword extraction (no new deps: uses re, Counter, math.log)
    - MMR representative selection (relevance to centroid x diversity)
    """

    def __init__(
        self,
        keywords_per_cluster: int = 10,
        representatives_per_cluster: int = 5,
    ) -> None:
        self._keywords_k = keywords_per_cluster
        self._representatives_k = representatives_per_cluster

    def extract_keywords(
        self,
        texts: list[str],
        corpus: list[list[str]] | None = None,
        top_k: int | None = None,
    ) -> list[str]:
        """Extract top-k keywords from texts using TF-IDF.

        Args:
            texts: List of text strings in the cluster.
            corpus: Optional pre-tokenized corpus for IDF. If None, uses texts as corpus.
            top_k: Number of keywords to return. Defaults to self._keywords_k.

        Returns:
            List of keyword strings sorted by TF-IDF score.
        """
        top_k = top_k or self._keywords_k
        if not texts:
            return []

        # Tokenize cluster texts
        cluster_tokens = []
        for t in texts:
            cluster_tokens.extend(_tokenize(t))

        if not cluster_tokens:
            return []

        # TF: term frequency in the cluster
        tf = Counter(cluster_tokens)
        total_tokens = len(cluster_tokens)

        # IDF: use corpus if provided, else use individual texts as documents
        if corpus is None:
            corpus = [_tokenize(t) for t in texts]

        n_docs = len(corpus)
        doc_freq: Counter[str] = Counter()
        for doc_tokens in corpus:
            unique_tokens = set(doc_tokens)
            for token in unique_tokens:
                doc_freq[token] += 1

        # TF-IDF scores
        tfidf: dict[str, float] = {}
        for term, count in tf.items():
            tf_score = count / total_tokens
            df = doc_freq.get(term, 0)
            idf_score = math.log((n_docs + 1) / (df + 1)) + 1  # smoothed IDF
            tfidf[term] = tf_score * idf_score

        # Sort by score descending
        sorted_terms = sorted(tfidf.items(), key=lambda x: x[1], reverse=True)
        return [term for term, _ in sorted_terms[:top_k]]

    def select_representatives(
        self,
        embeddings: np.ndarray,
        centroid: np.ndarray,
        n: int | None = None,
    ) -> list[int]:
        """Select diverse representative memories using MMR.

        Maximal Marginal Relevance: picks items that are both relevant to
        the centroid and diverse from already-selected items.

        Args:
            embeddings: (N, D) array of memory embeddings.
            centroid: (D,) centroid embedding.
            n: Number of representatives to select.

        Returns:
            List of indices into the embeddings array.
        """
        n = n or self._representatives_k
        if len(embeddings) == 0:
            return []
        n = min(n, len(embeddings))

        # Normalize
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True) + 1e-10
        normed = embeddings / norms
        centroid_norm = centroid / (np.linalg.norm(centroid) + 1e-10)

        # Relevance to centroid
        relevance = normed @ centroid_norm  # (N,)

        selected: list[int] = []
        remaining = set(range(len(embeddings)))

        # Lambda for balancing relevance vs diversity
        lam = 0.7

        for _ in range(n):
            best_idx = -1
            best_score = -float("inf")

            for idx in remaining:
                rel = float(relevance[idx])

                # Max similarity to already-selected
                if selected:
                    sims = [
                        float(np.dot(normed[idx], normed[s]))
                        for s in selected
                    ]
                    max_sim = max(sims)
                else:
                    max_sim = 0.0

                # MMR score
                mmr = lam * rel - (1 - lam) * max_sim
                if mmr > best_score:
                    best_score = mmr
                    best_idx = idx

            if best_idx >= 0:
                selected.append(best_idx)
                remaining.discard(best_idx)

        return selected

    def consolidate_cluster(
        self,
        memories: list[dict],
        cluster_id: int,
        corpus_texts: list[str] | None = None,
    ) -> ClusterSummary | None:
        """Consolidate a cluster: extract keywords + select representatives.

        Args:
            memories: List of memory dicts with 'id', 'content', 'embedding'.
            cluster_id: Cluster identifier.
            corpus_texts: Optional full corpus texts for IDF calculation.

        Returns:
            ClusterSummary or None if too few memories.
        """
        if len(memories) < 2:
            return None

        texts = [m["content"] for m in memories]
        embeddings = np.array([m["embedding"] for m in memories], dtype=np.float32)
        centroid = embeddings.mean(axis=0)

        # Keywords
        corpus = None
        if corpus_texts:
            corpus = [_tokenize(t) for t in corpus_texts]
        keywords = self.extract_keywords(texts, corpus=corpus, top_k=self._keywords_k)

        # Representatives
        rep_indices = self.select_representatives(
            embeddings, centroid, n=self._representatives_k
        )
        representative_ids = [memories[i]["id"] for i in rep_indices]

        return ClusterSummary(
            cluster_id=cluster_id,
            keywords=keywords,
            representative_ids=representative_ids,
            centroid_embedding=centroid,
            n_memories=len(memories),
        )
