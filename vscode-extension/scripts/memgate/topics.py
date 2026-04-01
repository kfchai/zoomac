"""TopicModel — self-organizing clusters from MLP hidden layer activations."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class TopicCluster:
    """A discovered topic cluster."""

    cluster_id: int
    terms: list[str]
    centroid: np.ndarray
    coherence: float
    neuron_indices: list[int]


class TopicModel:
    """Self-organizing topic model using MLP hidden layer activations.

    The MLP's hidden layer (h1) naturally develops feature detectors.
    This class performs greedy clustering on cached h1 activations to
    discover emergent topic groups.
    """

    def __init__(
        self,
        top_neurons: int = 5,
        overlap_threshold: float = 0.5,
    ) -> None:
        self._top_neurons = top_neurons
        self._overlap_threshold = overlap_threshold
        self._clusters: list[TopicCluster] = []
        self._next_cluster_id = 0

    def extract(self, h1_cache: dict[str, np.ndarray]) -> list[TopicCluster]:
        """Extract topic clusters from h1 activation cache.

        Uses greedy clustering based on dominant neuron overlap (Jaccard >= threshold).

        Args:
            h1_cache: Dict mapping content prefix -> h1 activation vector.

        Returns:
            List of discovered TopicCluster objects.
        """
        if len(h1_cache) < 3:
            return self._clusters

        # Get top-N neuron indices for each term
        term_neurons: dict[str, set[int]] = {}
        for term, h1 in h1_cache.items():
            top_idx = np.argsort(np.abs(h1))[-self._top_neurons:]
            term_neurons[term] = set(top_idx.tolist())

        # Greedy clustering by Jaccard overlap
        unassigned = set(term_neurons.keys())
        clusters: list[TopicCluster] = []

        while unassigned:
            # Pick a seed
            seed = next(iter(unassigned))
            seed_neurons = term_neurons[seed]
            cluster_terms = [seed]
            unassigned.discard(seed)

            # Find overlapping terms
            for term in list(unassigned):
                intersection = seed_neurons & term_neurons[term]
                union = seed_neurons | term_neurons[term]
                jaccard = len(intersection) / len(union) if union else 0
                if jaccard >= self._overlap_threshold:
                    cluster_terms.append(term)
                    unassigned.discard(term)

            if len(cluster_terms) >= 2:
                # Compute centroid and coherence
                vecs = np.array([h1_cache[t] for t in cluster_terms])
                centroid = vecs.mean(axis=0)

                # Coherence = mean pairwise cosine similarity
                norms = np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-10
                normed = vecs / norms
                sim_matrix = normed @ normed.T
                n = len(cluster_terms)
                if n > 1:
                    # Extract upper triangle
                    mask = np.triu(np.ones((n, n), dtype=bool), k=1)
                    coherence = float(sim_matrix[mask].mean())
                else:
                    coherence = 1.0

                # Dominant neurons = union of all term neurons
                all_neurons = set()
                for t in cluster_terms:
                    all_neurons |= term_neurons[t]

                cluster_id = self._next_cluster_id
                self._next_cluster_id += 1

                clusters.append(
                    TopicCluster(
                        cluster_id=cluster_id,
                        terms=cluster_terms,
                        centroid=centroid,
                        coherence=coherence,
                        neuron_indices=sorted(all_neurons),
                    )
                )

        self._clusters = clusters
        return clusters

    def assign(self, h1_activations: np.ndarray) -> tuple[int | None, str | None]:
        """Assign an h1 activation vector to the closest existing cluster.

        Args:
            h1_activations: h1 activation vector from MLP forward pass.

        Returns:
            (cluster_id, label) or (None, None) if no clusters exist.
        """
        if not self._clusters:
            return None, None

        best_sim = -1.0
        best_cluster = None

        h1_norm = np.linalg.norm(h1_activations)
        if h1_norm < 1e-10:
            return None, None

        for cluster in self._clusters:
            c_norm = np.linalg.norm(cluster.centroid)
            if c_norm < 1e-10:
                continue
            sim = float(np.dot(h1_activations, cluster.centroid) / (h1_norm * c_norm))
            if sim > best_sim:
                best_sim = sim
                best_cluster = cluster

        if best_cluster is not None and best_sim > 0.3:
            label = f"topic_{best_cluster.cluster_id}"
            if best_cluster.terms:
                # Use first term as label hint
                label = best_cluster.terms[0][:50]
            return best_cluster.cluster_id, label

        return None, None

    @property
    def clusters(self) -> list[TopicCluster]:
        return self._clusters

    @property
    def n_clusters(self) -> int:
        return len(self._clusters)
