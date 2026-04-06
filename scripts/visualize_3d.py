"""GER-RAG 3D Visualization — Cosmic View

ドキュメントを宇宙空間の恒星として可視化する。
各ノードの温度が恒星の色温度に対応し、質量が恒星のサイズに対応する。
速度ベクトル（加速度方向）を矢印、重力圏を球として表現。

Usage:
    python scripts/visualize_3d.py [--method pca|umap] [--open]
    python scripts/visualize_3d.py --compare --open    # 原始 vs 仮想の並列比較

サーバー停止中でも実行可能（DB + FAISSファイルを直接読む）。
"""

from __future__ import annotations

import argparse
import asyncio
import math
import time

import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from ger_rag.config import GERConfig
from ger_rag.core.gravity import compute_virtual_position
from ger_rag.index.faiss_index import FaissIndex
from ger_rag.store.sqlite_store import SqliteStore


# -----------------------------------------------------------------------
# Stellar color temperature mapping
# -----------------------------------------------------------------------

def stellar_color(temperature: float, mass: float, decay: float, disp_norm: float) -> str:
    """Map node temperature to stellar color (pure blackbody)."""
    t = min(1.0, temperature / 0.00025)

    if mass < 1.01 and temperature < 1e-8:
        alpha = 0.06 + 0.06 * decay
        return f"rgba(40,30,25,{alpha:.2f})"

    if t < 0.1:
        r, g, b = 180, 60, 30
    elif t < 0.25:
        blend = (t - 0.1) / 0.15
        r, g, b = int(180 + 55 * blend), int(60 + 60 * blend), int(30 + 10 * blend)
    elif t < 0.45:
        blend = (t - 0.25) / 0.2
        r, g, b = 255, int(120 + 90 * blend), int(40 + 30 * blend)
    elif t < 0.65:
        blend = (t - 0.45) / 0.2
        r, g, b = 255, int(210 + 45 * blend), int(70 + 150 * blend)
    elif t < 0.85:
        blend = (t - 0.65) / 0.2
        r, g, b = int(255 - 20 * blend), int(255 - 10 * blend), 255
    else:
        blend = (t - 0.85) / 0.15
        r, g, b = int(235 - 75 * blend), int(245 - 45 * blend), 255

    disp_boost = min(0.15, disp_norm * 0.5)
    luminosity = 0.3 + 0.7 * min(1.0, mass / 8.0)
    alpha = min(1.0, (0.15 + 0.85 * decay * luminosity) + disp_boost)
    return f"rgba({r},{g},{b},{alpha:.2f})"


def stellar_size(mass: float, disp_norm: float) -> float:
    if mass < 1.01:
        base = 1.5
    else:
        base = 2.0 + 12.0 * min(1.0, (mass - 1.0) / 8.0)
    glow = min(3.0, disp_norm * 10)
    return base + glow


# -----------------------------------------------------------------------
# Data loading
# -----------------------------------------------------------------------

def reduce_to_3d(vectors: np.ndarray, method: str = "pca") -> np.ndarray:
    if method == "umap":
        import umap
        reducer = umap.UMAP(n_components=3, n_neighbors=15, min_dist=0.1, metric="cosine")
        return reducer.fit_transform(vectors)
    else:
        from sklearn.decomposition import PCA
        return PCA(n_components=3).fit_transform(vectors)


async def load_data(config: GERConfig):
    faiss_index = FaissIndex(dimension=config.embedding_dim)
    faiss_index.load(config.faiss_index_path)

    if faiss_index.size == 0:
        print("ERROR: FAISSインデックスが空です。")
        raise SystemExit(1)

    import faiss
    raw = faiss.rev_swig_ptr(faiss_index._index.get_xb(), faiss_index.size * config.embedding_dim)
    vectors = np.array(raw).reshape(faiss_index.size, config.embedding_dim).copy()
    ids = list(faiss_index._id_map)

    store = SqliteStore(db_path=config.db_path)
    await store.initialize()
    states = await store.get_all_node_states()
    state_map = {s.id: s for s in states}

    doc_map = {}
    for node_id in ids:
        doc = await store.get_document(node_id)
        if doc:
            doc_map[node_id] = doc

    edges = await store.get_all_edges()
    displacements = await store.load_displacements()
    velocities = await store.load_velocities()

    # Build co-occurrence neighbor map for BH centroid computation
    cooccurrence_neighbors: dict[str, dict[str, float]] = {}
    for edge in edges:
        cooccurrence_neighbors.setdefault(edge.src, {})[edge.dst] = edge.weight
        cooccurrence_neighbors.setdefault(edge.dst, {})[edge.src] = edge.weight

    await store.close()

    return vectors, ids, state_map, doc_map, edges, displacements, velocities, cooccurrence_neighbors


def compute_bh_centroids_3d(
    ids: list[str],
    coords_3d: np.ndarray,
    cooccurrence_neighbors: dict[str, dict[str, float]],
    masses: np.ndarray,
    config: GERConfig,
) -> list[dict]:
    """Compute BH centroid positions in 3D space for visualization.

    Returns list of {position, bh_mass, member_count, member_ids} for significant BHs.
    Deduplicates nearby centroids to avoid clutter.
    """
    id_to_idx = {nid: i for i, nid in enumerate(ids)}
    seen_centroids: list[dict] = []

    for i, node_id in enumerate(ids):
        neighbors = cooccurrence_neighbors.get(node_id, {})
        if not neighbors:
            continue

        total_weight = 0.0
        centroid = np.zeros(3, dtype=np.float64)
        member_ids = []
        for neighbor_id, weight in neighbors.items():
            j = id_to_idx.get(neighbor_id)
            if j is None:
                continue
            centroid += weight * coords_3d[j].astype(np.float64)
            total_weight += weight
            member_ids.append(neighbor_id)

        if total_weight < 5.0:  # skip weak clusters
            continue

        centroid /= total_weight
        bh_mass = config.bh_mass_scale * math.log(1.0 + total_weight)

        # Deduplicate: skip if too close to an existing centroid
        too_close = False
        for existing in seen_centroids:
            if np.linalg.norm(centroid - existing["position"]) < 0.5:
                # Merge: keep the heavier one
                if bh_mass > existing["bh_mass"]:
                    existing["position"] = centroid
                    existing["bh_mass"] = bh_mass
                    existing["total_weight"] = total_weight
                too_close = True
                break

        if not too_close:
            seen_centroids.append({
                "position": centroid,
                "bh_mass": bh_mass,
                "total_weight": total_weight,
                "member_count": len(member_ids),
            })

    return seen_centroids


def build_virtual_vectors(vectors, ids, displacements, state_map):
    virtual = vectors.copy()
    for i, node_id in enumerate(ids):
        disp = displacements.get(node_id)
        if disp is not None and disp.shape[0] == vectors.shape[1]:
            virtual[i] = compute_virtual_position(vectors[i], disp, temperature=0.0)
    return virtual


# -----------------------------------------------------------------------
# Node properties
# -----------------------------------------------------------------------

def compute_node_properties(ids, state_map, doc_map, displacements, velocities, config):
    now = time.time()
    masses, temperatures, decays, disp_norms, vel_norms = [], [], [], [], []
    hover_texts, sources = [], []

    for node_id in ids:
        state = state_map.get(node_id)
        mass = state.mass if state else 1.0
        temp = state.temperature if state else 0.0
        last_access = state.last_access if state else now
        decay_val = math.exp(-config.delta * (now - last_access))
        disp = displacements.get(node_id)
        vel = velocities.get(node_id)
        dn = float(np.linalg.norm(disp)) if disp is not None else 0.0
        vn = float(np.linalg.norm(vel)) if vel is not None else 0.0

        masses.append(mass)
        temperatures.append(temp)
        decays.append(decay_val)
        disp_norms.append(dn)
        vel_norms.append(vn)

        doc = doc_map.get(node_id, {})
        content = doc.get("content", "")[:120].replace("\n", " ")
        meta = doc.get("metadata", {}) or {}
        source = meta.get("source", "unknown")
        sources.append(source)

        # Spectral class
        t_scaled = min(1.0, temp / 0.00025)
        if temp < 1e-8:
            spectral = "Dormant (dust)"
        elif t_scaled < 0.25:
            spectral = "M/K (red giant)" if mass > 3.0 else "M (red dwarf)"
        elif t_scaled < 0.55:
            spectral = "G/F (yellow)"
        elif t_scaled < 0.85:
            spectral = "F/A (white)"
        else:
            spectral = "A/B (blue supergiant)" if mass > 3.0 else "B (blue-white)"

        # Gravity radius
        grav_radius = config.compute_gravity_radius(mass)

        hover_texts.append(
            f"<b>{content}...</b><br><br>"
            f"ID: {node_id[:12]}...<br>"
            f"Source: {source}<br>"
            f"━━━━━━━━━━━━━━━━━<br>"
            f"Mass: <b>{mass:.2f}</b><br>"
            f"Temperature: <b>{temp:.6f}</b> [{spectral}]<br>"
            f"Decay: {decay_val:.4f}<br>"
            f"Displacement: <b>{dn:.6f}</b><br>"
            f"Velocity: <b>{vn:.6f}</b><br>"
            f"Gravity radius: min_sim={grav_radius:.3f}<br>"
            f"History: {len(state.sim_history) if state else 0} entries"
        )

    return (
        np.array(masses), np.array(temperatures), np.array(decays),
        np.array(disp_norms), np.array(vel_norms), hover_texts, sources,
    )


# -----------------------------------------------------------------------
# Figure building
# -----------------------------------------------------------------------

SPACE_SCENE = dict(
    xaxis=dict(showgrid=False, zeroline=False, showticklabels=False,
               showspikes=False, title="", backgroundcolor="rgb(5,5,15)"),
    yaxis=dict(showgrid=False, zeroline=False, showticklabels=False,
               showspikes=False, title="", backgroundcolor="rgb(5,5,15)"),
    zaxis=dict(showgrid=False, zeroline=False, showticklabels=False,
               showspikes=False, title="", backgroundcolor="rgb(5,5,15)"),
    bgcolor="rgb(5,5,15)",
)


def add_nodes_to_figure(
    fig, coords_3d, ids, masses, temperatures, decays, disp_norms, vel_norms,
    hover_texts, sources, edges, velocities_3d=None, config=None,
    cooccurrence_neighbors=None, row=None, col=None,
):
    """Add stellar nodes, filaments, velocity arrows, gravity spheres, and BH centroids."""

    def _add(trace):
        if row is not None:
            fig.add_trace(trace, row=row, col=col)
        else:
            fig.add_trace(trace)

    # Edges as faint filaments
    if edges:
        id_to_idx = {nid: i for i, nid in enumerate(ids)}
        ex, ey, ez = [], [], []
        for edge in edges:
            if edge.src in id_to_idx and edge.dst in id_to_idx:
                i, j = id_to_idx[edge.src], id_to_idx[edge.dst]
                ex.extend([coords_3d[i, 0], coords_3d[j, 0], None])
                ey.extend([coords_3d[i, 1], coords_3d[j, 1], None])
                ez.extend([coords_3d[i, 2], coords_3d[j, 2], None])
        if ex:
            _add(go.Scatter3d(
                x=ex, y=ey, z=ez, mode="lines",
                line=dict(color="rgba(60,90,180,0.12)", width=0.8),
                hoverinfo="skip", name="Filaments", showlegend=True,
            ))

    # Velocity arrows: length = actual next-step displacement in 3D
    if velocities_3d is not None:
        ax, ay, az = [], [], []

        for i in range(len(ids)):
            if vel_norms[i] < 0.001:
                continue
            v = velocities_3d[i]  # already projected to 3D, dt=1 so this IS the next displacement
            ax.extend([coords_3d[i, 0], coords_3d[i, 0] + v[0], None])
            ay.extend([coords_3d[i, 1], coords_3d[i, 1] + v[1], None])
            az.extend([coords_3d[i, 2], coords_3d[i, 2] + v[2], None])

        if ax:
            _add(go.Scatter3d(
                x=ax, y=ay, z=az, mode="lines",
                line=dict(color="rgba(0,255,200,0.7)", width=2.5),
                hoverinfo="skip", name="Velocity vectors", showlegend=True,
            ))

    # Gravity field rings (for high-mass nodes)
    if config is not None:
        # Scale radius relative to data spread
        data_extent = max(
            np.ptp(coords_3d[:, 0]), np.ptp(coords_3d[:, 1]), np.ptp(coords_3d[:, 2])
        ) if len(coords_3d) > 0 else 1.0

        for i in range(len(ids)):
            if masses[i] < 2.0:
                continue
            min_sim = config.compute_gravity_radius(masses[i])
            # Convert cosine distance to 3D radius, capped to ~5% of data extent
            raw_radius = math.sqrt(2.0 * (1.0 - min_sim))
            radius_3d = min(raw_radius * data_extent * 0.04, data_extent * 0.05)

            n_pts = 32
            theta = np.linspace(0, 2 * np.pi, n_pts)
            cx, cy, cz = coords_3d[i]
            alpha = min(0.25, 0.08 + masses[i] * 0.015)

            # XY ring
            _add(go.Scatter3d(
                x=(cx + radius_3d * np.cos(theta)).tolist(),
                y=(cy + radius_3d * np.sin(theta)).tolist(),
                z=np.full(n_pts, cz).tolist(),
                mode="lines",
                line=dict(color=f"rgba(255,200,50,{alpha:.2f})", width=1.2),
                hoverinfo="skip", showlegend=False,
            ))
            # XZ ring (perpendicular, gives sphere impression)
            _add(go.Scatter3d(
                x=(cx + radius_3d * np.cos(theta)).tolist(),
                y=np.full(n_pts, cy).tolist(),
                z=(cz + radius_3d * np.sin(theta)).tolist(),
                mode="lines",
                line=dict(color=f"rgba(255,200,50,{alpha * 0.6:.2f})", width=1.0),
                hoverinfo="skip", showlegend=False,
            ))

    # Co-occurrence Black Holes (cluster centroids)
    if cooccurrence_neighbors is not None and config is not None:
        bh_list = compute_bh_centroids_3d(ids, coords_3d, cooccurrence_neighbors, masses, config)
        if bh_list:
            bh_x = [bh["position"][0] for bh in bh_list]
            bh_y = [bh["position"][1] for bh in bh_list]
            bh_z = [bh["position"][2] for bh in bh_list]
            bh_sizes = [3.0 + 6.0 * min(1.0, bh["bh_mass"] / 3.0) for bh in bh_list]
            bh_texts = [
                f"<b>Black Hole</b><br>"
                f"BH mass: {bh['bh_mass']:.2f}<br>"
                f"Total edge weight: {bh['total_weight']:.0f}<br>"
                f"Members: {bh['member_count']}"
                for bh in bh_list
            ]
            _add(go.Scatter3d(
                x=bh_x, y=bh_y, z=bh_z,
                mode="markers",
                marker=dict(
                    size=bh_sizes,
                    color="rgba(120,50,200,0.8)",
                    symbol="diamond",
                ),
                text=bh_texts, hoverinfo="text",
                name=f"Black Holes ({len(bh_list)})",
            ))

            # BH gravity wells (faint rings around each BH)
            data_extent = max(
                np.ptp(coords_3d[:, 0]), np.ptp(coords_3d[:, 1]), np.ptp(coords_3d[:, 2])
            ) if len(coords_3d) > 0 else 1.0
            n_pts = 24
            theta = np.linspace(0, 2 * np.pi, n_pts)
            for bh in bh_list:
                radius = min(bh["bh_mass"] * data_extent * 0.015, data_extent * 0.06)
                cx, cy, cz = bh["position"]
                alpha = min(0.2, 0.05 + bh["bh_mass"] * 0.03)
                _add(go.Scatter3d(
                    x=(cx + radius * np.cos(theta)).tolist(),
                    y=(cy + radius * np.sin(theta)).tolist(),
                    z=np.full(n_pts, cz).tolist(),
                    mode="lines",
                    line=dict(color=f"rgba(120,50,200,{alpha:.2f})", width=1.5),
                    hoverinfo="skip", showlegend=False,
                ))

    # Stars
    node_colors = []
    node_sizes = []
    for i in range(len(ids)):
        node_colors.append(stellar_color(temperatures[i], masses[i], decays[i], disp_norms[i]))
        node_sizes.append(stellar_size(masses[i], disp_norms[i]))

    _add(go.Scatter3d(
        x=coords_3d[:, 0], y=coords_3d[:, 1], z=coords_3d[:, 2],
        mode="markers",
        marker=dict(size=node_sizes, color=node_colors),
        text=hover_texts, hoverinfo="text",
        name=f"Stars ({len(ids)})",
    ))


def build_single_figure(coords_3d, ids, props, edges, velocities_3d, config,
                        cooccurrence_neighbors=None, title_suffix=""):
    masses, temperatures, decays, disp_norms, vel_norms, hover_texts, sources = props

    fig = go.Figure()
    add_nodes_to_figure(
        fig, coords_3d, ids, masses, temperatures, decays, disp_norms, vel_norms,
        hover_texts, sources, edges, velocities_3d=velocities_3d, config=config,
        cooccurrence_neighbors=cooccurrence_neighbors,
    )

    displaced = sum(1 for d in disp_norms if d > 0.001)
    moving = sum(1 for v in vel_norms if v > 0.001)
    max_disp = float(disp_norms.max()) if len(disp_norms) > 0 else 0
    max_vel = float(vel_norms.max()) if len(vel_norms) > 0 else 0
    high_mass = sum(1 for m in masses if m > 2.0)

    fig.update_layout(
        title=dict(text=(
            f"<span style='font-size:20px'>GER-RAG Cosmos {title_suffix}</span><br>"
            f"<sub style='color:#888'>{len(ids)} stars | {len(edges)} filaments | "
            f"Displaced: {displaced} | Moving: {moving} | "
            f"High mass: {high_mass} | Max v: {max_vel:.4f}</sub>"
        )),
        scene=SPACE_SCENE,
        paper_bgcolor="rgb(5,5,15)", plot_bgcolor="rgb(5,5,15)",
        font=dict(color="#CCCCCC", family="monospace"),
        legend=dict(bgcolor="rgba(10,10,30,0.7)", font=dict(color="#AAAAAA", size=11),
                    bordercolor="rgba(40,60,120,0.3)", borderwidth=1),
        margin=dict(l=0, r=0, t=80, b=40),
        width=1400, height=900,
    )
    fig.add_annotation(
        text=(
            "Size=Mass | Color=Temperature (M赤→A/B青白) | "
            "Cyan=Velocity | Gold=Gravity radius | Purple◆=Black Holes"
        ),
        xref="paper", yref="paper", x=0.5, y=-0.03,
        showarrow=False, font=dict(size=11, color="#666666"),
    )
    return fig


def build_comparison_figure(orig_3d, virtual_3d, ids, props, edges, velocities_3d, config,
                            cooccurrence_neighbors=None):
    masses, temperatures, decays, disp_norms, vel_norms, hover_texts, sources = props

    fig = make_subplots(
        rows=1, cols=2,
        specs=[[{"type": "scatter3d"}, {"type": "scatter3d"}]],
        subplot_titles=[
            "<span style='color:#888'>Original Embedding (固定座標)</span>",
            "<span style='color:#FFD700'>Virtual Position (重力変位後)</span>",
        ],
    )

    add_nodes_to_figure(
        fig, orig_3d, ids, masses, temperatures, decays, disp_norms, vel_norms,
        hover_texts, sources, edges, row=1, col=1,
    )
    add_nodes_to_figure(
        fig, virtual_3d, ids, masses, temperatures, decays, disp_norms, vel_norms,
        hover_texts, sources, edges, velocities_3d=velocities_3d, config=config,
        cooccurrence_neighbors=cooccurrence_neighbors, row=1, col=2,
    )

    displaced = sum(1 for d in disp_norms if d > 0.001)
    moving = sum(1 for v in vel_norms if v > 0.001)
    max_vel = float(vel_norms.max()) if len(vel_norms) > 0 else 0

    fig.update_layout(
        title=dict(text=(
            f"<span style='font-size:20px'>GER-RAG Cosmos — Original vs Gravitational Field</span><br>"
            f"<sub style='color:#888'>{len(ids)} stars | {len(edges)} filaments | "
            f"Displaced: {displaced} | Moving: {moving} | Max velocity: {max_vel:.4f}</sub>"
        )),
        scene=SPACE_SCENE, scene2=SPACE_SCENE,
        paper_bgcolor="rgb(5,5,15)", plot_bgcolor="rgb(5,5,15)",
        font=dict(color="#CCCCCC", family="monospace"),
        legend=dict(bgcolor="rgba(10,10,30,0.7)", font=dict(color="#AAAAAA", size=11),
                    bordercolor="rgba(40,60,120,0.3)", borderwidth=1),
        margin=dict(l=0, r=0, t=80, b=40),
        width=1800, height=900,
    )
    return fig


# -----------------------------------------------------------------------
# Velocity 3D projection
# -----------------------------------------------------------------------

def project_velocities_to_3d(ids, velocities, vectors, method, reducer_or_components):
    """Project 768-dim velocity vectors to 3D using the same PCA/UMAP transform."""
    dim = vectors.shape[1]
    vel_3d = np.zeros((len(ids), 3), dtype=np.float32)

    if method == "pca" and reducer_or_components is not None:
        # PCA components matrix (3 x dim)
        components = reducer_or_components
        for i, nid in enumerate(ids):
            vel = velocities.get(nid)
            if vel is not None and np.linalg.norm(vel) > 0.001:
                vel_3d[i] = components @ vel
    # UMAP doesn't have a linear transform, approximate with finite differences
    elif method == "umap":
        for i, nid in enumerate(ids):
            vel = velocities.get(nid)
            if vel is not None and np.linalg.norm(vel) > 0.001:
                # Small perturbation approach — rough approximation
                vel_3d[i] = np.random.randn(3).astype(np.float32) * float(np.linalg.norm(vel))

    return vel_3d


# -----------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="GER-RAG Cosmic 3D Visualization")
    parser.add_argument("--method", choices=["pca", "umap"], default="pca")
    parser.add_argument("--open", action="store_true", help="Open in browser")
    parser.add_argument("--output", default="ger_rag_3d.html")
    parser.add_argument("--sample", type=int, default=0, help="Sample N nodes (0=all)")
    parser.add_argument("--compare", action="store_true",
                        help="Side-by-side: original vs virtual coordinates")
    args = parser.parse_args()

    config = GERConfig.from_config_file()

    print("Loading data...")
    vectors, ids, state_map, doc_map, edges, displacements, velocities, cooc_neighbors = asyncio.run(load_data(config))
    displaced_count = sum(1 for d in displacements.values() if np.linalg.norm(d) > 0.001)
    moving_count = sum(1 for v in velocities.values() if np.linalg.norm(v) > 0.001)
    bh_nodes = sum(1 for nid in ids if nid in cooc_neighbors and sum(cooc_neighbors[nid].values()) >= 5)
    print(f"  {len(ids)} stars, {len(edges)} filaments, "
          f"{displaced_count} displaced, {moving_count} moving, "
          f"{bh_nodes} nodes with BH")

    if args.sample > 0 and args.sample < len(ids):
        print(f"  Sampling {args.sample} stars...")
        rng = np.random.default_rng(42)
        id_to_idx = {nid: i for i, nid in enumerate(ids)}

        # Ensure co-occurrence nodes are always included in sample
        cooc_node_indices = set()
        for nid in cooc_neighbors:
            if nid in id_to_idx:
                cooc_node_indices.add(id_to_idx[nid])
                for neighbor_id in cooc_neighbors[nid]:
                    if neighbor_id in id_to_idx:
                        cooc_node_indices.add(id_to_idx[neighbor_id])

        remaining = [i for i in range(len(ids)) if i not in cooc_node_indices]
        n_random = max(0, args.sample - len(cooc_node_indices))
        random_idx = rng.choice(remaining, size=min(n_random, len(remaining)), replace=False)
        sample_idx = np.sort(np.array(list(cooc_node_indices) + list(random_idx)))

        print(f"    (including {len(cooc_node_indices)} co-occurrence nodes)")
        vectors = vectors[sample_idx]
        ids = [ids[i] for i in sample_idx]
        sampled_set = set(ids)
        edges = [e for e in edges if e.src in sampled_set and e.dst in sampled_set]
        cooc_neighbors = {
            nid: {k: v for k, v in nbrs.items() if k in sampled_set}
            for nid, nbrs in cooc_neighbors.items()
            if nid in sampled_set
        }

    props = compute_node_properties(ids, state_map, doc_map, displacements, velocities, config)

    if args.compare:
        print("Building virtual positions...")
        virtual_vectors = build_virtual_vectors(vectors, ids, displacements, state_map)

        print(f"Reducing to 3D ({args.method.upper()}, joint fit)...")
        combined = np.vstack([vectors, virtual_vectors])
        if args.method == "pca":
            from sklearn.decomposition import PCA
            pca = PCA(n_components=3)
            combined_3d = pca.fit_transform(combined)
            pca_components = pca.components_  # (3, dim)
        else:
            combined_3d = reduce_to_3d(combined, method=args.method)
            pca_components = None

        n = len(ids)
        orig_3d = combined_3d[:n]
        virtual_3d = combined_3d[n:]

        # Project velocities to 3D
        vel_3d = project_velocities_to_3d(ids, velocities, vectors, args.method, pca_components)

        print("Building cosmic comparison...")
        fig = build_comparison_figure(orig_3d, virtual_3d, ids, props, edges, vel_3d, config,
                                      cooccurrence_neighbors=cooc_neighbors)
    else:
        print("Building virtual positions...")
        virtual_vectors = build_virtual_vectors(vectors, ids, displacements, state_map)

        print(f"Reducing to 3D ({args.method.upper()})...")
        if args.method == "pca":
            from sklearn.decomposition import PCA
            pca = PCA(n_components=3)
            coords_3d = pca.fit_transform(virtual_vectors)
            pca_components = pca.components_
        else:
            coords_3d = reduce_to_3d(virtual_vectors, method=args.method)
            pca_components = None

        vel_3d = project_velocities_to_3d(ids, velocities, vectors, args.method, pca_components)

        print("Building cosmic view...")
        fig = build_single_figure(coords_3d, ids, props, edges, vel_3d, config,
                                  cooccurrence_neighbors=cooc_neighbors, title_suffix="— Virtual Space")

    print(f"Saving to {args.output}...")
    fig.write_html(args.output, include_plotlyjs=True)
    print(f"  Saved: {args.output}")

    if args.open:
        import webbrowser
        webbrowser.open(args.output)
    else:
        print(f"  Open: file://{args.output}")


if __name__ == "__main__":
    main()
