"""GER-RAG 3D Visualization — Cosmic View

ドキュメントを宇宙空間の恒星として可視化する。
各ノードの温度が恒星の色温度に対応し、質量が恒星のサイズに対応する。

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
# 恒星のスペクトル型:  M(赤) → K(橙) → G(黄) → F(白) → A(白青) → B(青白)
# temperature=0 は冷えた暗い星、高温ほど明るく青白い
# -----------------------------------------------------------------------

def stellar_color(temperature: float, mass: float, decay: float, disp_norm: float) -> str:
    """Map node temperature to stellar color (pure blackbody).

    Like real stars:
      High mass + Low temp  = Red giant (赤色巨星) — 大きくて赤い安定した星
      High mass + High temp = Blue supergiant (青色超巨星) — 大きくて青白い不安定な星
      Low mass  + Low temp  = Red dwarf (赤色矮星) — 小さくて暗い
      Low mass  + High temp = White dwarf (白色矮星) — 小さくて白い

    Actual temperature range: 0 ~ 0.00023 (median ~0.00002)
    """
    # Scale temperature to 0-1 using actual data range
    # 0 = cold, 0.00005 = warm, 0.0001+ = hot, 0.0002+ = blazing
    t = min(1.0, temperature / 0.00025)

    # Dormant: never queried, barely visible background dust
    if mass < 1.01 and temperature < 1e-8:
        alpha = 0.06 + 0.06 * decay
        return f"rgba(40,30,25,{alpha:.2f})"

    # Blackbody color gradient (temperature only → color)
    if t < 0.1:
        # M type: deep red (赤色矮星/赤色巨星)
        r, g, b = 180, 60, 30
    elif t < 0.25:
        blend = (t - 0.1) / 0.15
        # M→K: red to orange
        r = int(180 + 55 * blend)
        g = int(60 + 60 * blend)
        b = int(30 + 10 * blend)
    elif t < 0.45:
        blend = (t - 0.25) / 0.2
        # K→G: orange to yellow
        r = 255
        g = int(120 + 90 * blend)
        b = int(40 + 30 * blend)
    elif t < 0.65:
        blend = (t - 0.45) / 0.2
        # G→F: yellow to warm white
        r = 255
        g = int(210 + 45 * blend)
        b = int(70 + 150 * blend)
    elif t < 0.85:
        blend = (t - 0.65) / 0.2
        # F→A: warm white to white
        r = int(255 - 20 * blend)
        g = int(255 - 10 * blend)
        b = 255
    else:
        blend = (t - 0.85) / 0.15
        # A→B: white to blue-white (青色超巨星)
        r = int(235 - 75 * blend)
        g = int(245 - 45 * blend)
        b = 255

    # Displacement glow: displaced nodes have a slight brightness boost
    disp_boost = min(0.15, disp_norm * 0.5)

    # Alpha from decay (recently accessed = bright) + mass luminosity
    luminosity = 0.3 + 0.7 * min(1.0, mass / 8.0)
    alpha = min(1.0, (0.15 + 0.85 * decay * luminosity) + disp_boost)

    return f"rgba({r},{g},{b},{alpha:.2f})"


def stellar_size(mass: float, disp_norm: float) -> float:
    """Map mass to star size. High mass = giant star."""
    if mass < 1.01:
        base = 1.5  # Dormant dust
    else:
        base = 2.0 + 12.0 * min(1.0, (mass - 1.0) / 8.0)
    # Displaced nodes get subtle glow
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
        pca = PCA(n_components=3)
        return pca.fit_transform(vectors)


async def load_data(config: GERConfig):
    faiss_index = FaissIndex(dimension=config.embedding_dim)
    faiss_index.load(config.faiss_index_path)

    if faiss_index.size == 0:
        print("ERROR: FAISSインデックスが空です。")
        raise SystemExit(1)

    import faiss
    vectors = faiss.rev_swig_ptr(faiss_index._index.get_xb(), faiss_index.size * config.embedding_dim)
    vectors = np.array(vectors).reshape(faiss_index.size, config.embedding_dim).copy()
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
    await store.close()

    return vectors, ids, state_map, doc_map, edges, displacements


def build_virtual_vectors(vectors, ids, displacements, state_map):
    virtual = vectors.copy()
    for i, node_id in enumerate(ids):
        disp = displacements.get(node_id)
        if disp is not None and disp.shape[0] == vectors.shape[1]:
            virtual[i] = compute_virtual_position(vectors[i], disp, temperature=0.0)
    return virtual


# -----------------------------------------------------------------------
# Node property computation
# -----------------------------------------------------------------------

def compute_node_properties(ids, state_map, doc_map, displacements, config):
    now = time.time()
    masses, temperatures, decays, disp_norms = [], [], [], []
    hover_texts, sources = [], []

    for node_id in ids:
        state = state_map.get(node_id)
        mass = state.mass if state else 1.0
        temp = state.temperature if state else 0.0
        last_access = state.last_access if state else now
        decay_val = math.exp(-config.delta * (now - last_access))
        disp = displacements.get(node_id)
        dn = float(np.linalg.norm(disp)) if disp is not None else 0.0

        masses.append(mass)
        temperatures.append(temp)
        decays.append(decay_val)
        disp_norms.append(dn)

        doc = doc_map.get(node_id, {})
        content = doc.get("content", "")[:120].replace("\n", " ")
        meta = doc.get("metadata", {}) or {}
        source = meta.get("source", "unknown")
        sources.append(source)

        # Spectral class label (based on actual data range)
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

        hover_texts.append(
            f"<b>{content}...</b><br><br>"
            f"ID: {node_id[:12]}...<br>"
            f"Source: {source}<br>"
            f"━━━━━━━━━━━━━━━━━<br>"
            f"Mass: <b>{mass:.2f}</b><br>"
            f"Temperature: <b>{temp:.6f}</b> [{spectral}]<br>"
            f"Decay: {decay_val:.4f}<br>"
            f"Displacement: <b>{dn:.6f}</b><br>"
            f"History: {len(state.sim_history) if state else 0} entries"
        )

    return (
        np.array(masses), np.array(temperatures), np.array(decays),
        np.array(disp_norms), hover_texts, sources,
    )


# -----------------------------------------------------------------------
# Figure building
# -----------------------------------------------------------------------

SPACE_SCENE = dict(
    xaxis=dict(
        showgrid=False, zeroline=False, showticklabels=False,
        showspikes=False, title="",
        backgroundcolor="rgb(5,5,15)",
        gridcolor="rgba(30,30,60,0.3)",
    ),
    yaxis=dict(
        showgrid=False, zeroline=False, showticklabels=False,
        showspikes=False, title="",
        backgroundcolor="rgb(5,5,15)",
        gridcolor="rgba(30,30,60,0.3)",
    ),
    zaxis=dict(
        showgrid=False, zeroline=False, showticklabels=False,
        showspikes=False, title="",
        backgroundcolor="rgb(5,5,15)",
        gridcolor="rgba(30,30,60,0.3)",
    ),
    bgcolor="rgb(5,5,15)",
)


def add_nodes_to_figure(
    fig, coords_3d, ids, masses, temperatures, decays, disp_norms,
    hover_texts, sources, edges, row=None, col=None,
):
    """Add stellar nodes and gravitational filament edges."""
    # Edges as faint filaments
    if edges:
        id_to_idx = {nid: i for i, nid in enumerate(ids)}
        edge_x, edge_y, edge_z = [], [], []
        for edge in edges:
            if edge.src in id_to_idx and edge.dst in id_to_idx:
                i, j = id_to_idx[edge.src], id_to_idx[edge.dst]
                edge_x.extend([coords_3d[i, 0], coords_3d[j, 0], None])
                edge_y.extend([coords_3d[i, 1], coords_3d[j, 1], None])
                edge_z.extend([coords_3d[i, 2], coords_3d[j, 2], None])

        if edge_x:
            trace = go.Scatter3d(
                x=edge_x, y=edge_y, z=edge_z,
                mode="lines",
                line=dict(color="rgba(60,90,180,0.12)", width=0.8),
                hoverinfo="skip",
                name="Gravitational filaments",
                showlegend=True,
            )
            if row is not None:
                fig.add_trace(trace, row=row, col=col)
            else:
                fig.add_trace(trace)

    # Stars (all nodes as single trace for performance, colored by temperature)
    node_colors = []
    node_sizes = []
    for i in range(len(ids)):
        node_colors.append(
            stellar_color(temperatures[i], masses[i], decays[i], disp_norms[i])
        )
        node_sizes.append(
            stellar_size(masses[i], disp_norms[i])
        )

    trace = go.Scatter3d(
        x=coords_3d[:, 0],
        y=coords_3d[:, 1],
        z=coords_3d[:, 2],
        mode="markers",
        marker=dict(
            size=node_sizes,
            color=node_colors,
        ),
        text=hover_texts,
        hoverinfo="text",
        name=f"Stars ({len(ids)})",
    )
    if row is not None:
        fig.add_trace(trace, row=row, col=col)
    else:
        fig.add_trace(trace)


def build_single_figure(coords_3d, ids, props, edges, config, title_suffix=""):
    masses, temperatures, decays, disp_norms, hover_texts, sources = props

    fig = go.Figure()
    add_nodes_to_figure(
        fig, coords_3d, ids, masses, temperatures, decays, disp_norms,
        hover_texts, sources, edges,
    )

    displaced = sum(1 for d in disp_norms if d > 0.001)
    max_disp = float(disp_norms.max()) if len(disp_norms) > 0 else 0
    hot = sum(1 for t in temperatures if t > 0.001)
    high_mass = sum(1 for m in masses if m > 2.0)

    fig.update_layout(
        title=dict(text=(
            f"<span style='font-size:20px'>GER-RAG Cosmos {title_suffix}</span><br>"
            f"<sub style='color:#888'>{len(ids)} stars | {len(edges)} filaments | "
            f"Displaced: {displaced} | Hot: {hot} | "
            f"High mass: {high_mass} | Max displacement: {max_disp:.4f}</sub>"
        )),
        scene=SPACE_SCENE,
        paper_bgcolor="rgb(5,5,15)",
        plot_bgcolor="rgb(5,5,15)",
        font=dict(color="#CCCCCC", family="monospace"),
        legend=dict(
            bgcolor="rgba(10,10,30,0.7)", font=dict(color="#AAAAAA", size=11),
            bordercolor="rgba(40,60,120,0.3)", borderwidth=1,
        ),
        margin=dict(l=0, r=0, t=80, b=40),
        width=1400, height=900,
    )
    fig.add_annotation(
        text=(
            "Size = Mass (質量: 赤色巨星←→矮星)  |  "
            "Color = Temperature (M赤 → K橙 → G黄 → F白 → A/B青白)  |  "
            "Brightness = Decay × Luminosity"
        ),
        xref="paper", yref="paper", x=0.5, y=-0.03,
        showarrow=False, font=dict(size=11, color="#666666"),
    )
    return fig


def build_comparison_figure(orig_3d, virtual_3d, ids, props, edges, config):
    masses, temperatures, decays, disp_norms, hover_texts, sources = props

    fig = make_subplots(
        rows=1, cols=2,
        specs=[[{"type": "scatter3d"}, {"type": "scatter3d"}]],
        subplot_titles=[
            "<span style='color:#888'>Original Embedding (固定座標)</span>",
            "<span style='color:#FFD700'>Virtual Position (重力変位後)</span>",
        ],
    )

    add_nodes_to_figure(
        fig, orig_3d, ids, masses, temperatures, decays, disp_norms,
        hover_texts, sources, edges, row=1, col=1,
    )
    add_nodes_to_figure(
        fig, virtual_3d, ids, masses, temperatures, decays, disp_norms,
        hover_texts, sources, edges, row=1, col=2,
    )

    displaced = sum(1 for d in disp_norms if d > 0.001)
    max_disp = float(disp_norms.max()) if len(disp_norms) > 0 else 0
    hot = sum(1 for t in temperatures if t > 0.001)

    fig.update_layout(
        title=dict(text=(
            f"<span style='font-size:20px'>GER-RAG Cosmos — Original vs Gravitational Field</span><br>"
            f"<sub style='color:#888'>{len(ids)} stars | {len(edges)} filaments | "
            f"Displaced: {displaced} | Hot: {hot} | "
            f"Max displacement: {max_disp:.4f}</sub>"
        )),
        scene=SPACE_SCENE,
        scene2=SPACE_SCENE,
        paper_bgcolor="rgb(5,5,15)",
        plot_bgcolor="rgb(5,5,15)",
        font=dict(color="#CCCCCC", family="monospace"),
        legend=dict(
            bgcolor="rgba(10,10,30,0.7)", font=dict(color="#AAAAAA", size=11),
            bordercolor="rgba(40,60,120,0.3)", borderwidth=1,
        ),
        margin=dict(l=0, r=0, t=80, b=40),
        width=1800, height=900,
    )
    return fig


# -----------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="GER-RAG Cosmic 3D Visualization")
    parser.add_argument(
        "--method", choices=["pca", "umap"], default="pca",
        help="Dimensionality reduction method (default: pca)",
    )
    parser.add_argument("--open", action="store_true", help="Open in browser")
    parser.add_argument("--output", default="ger_rag_3d.html", help="Output HTML file")
    parser.add_argument("--sample", type=int, default=0, help="Sample N nodes (0=all)")
    parser.add_argument("--compare", action="store_true",
                        help="Side-by-side: original vs virtual coordinates")
    args = parser.parse_args()

    config = GERConfig()

    print("Loading data...")
    vectors, ids, state_map, doc_map, edges, displacements = asyncio.run(load_data(config))
    displaced_count = sum(1 for d in displacements.values() if np.linalg.norm(d) > 0.001)
    print(f"  {len(ids)} stars, {len(edges)} filaments, {displaced_count} displaced")

    if args.sample > 0 and args.sample < len(ids):
        print(f"  Sampling {args.sample} stars...")
        rng = np.random.default_rng(42)
        sample_idx = rng.choice(len(ids), size=args.sample, replace=False)
        sample_idx.sort()
        vectors = vectors[sample_idx]
        ids = [ids[i] for i in sample_idx]
        sampled_set = set(ids)
        edges = [e for e in edges if e.src in sampled_set and e.dst in sampled_set]

    props = compute_node_properties(ids, state_map, doc_map, displacements, config)

    if args.compare:
        print("Building virtual positions...")
        virtual_vectors = build_virtual_vectors(vectors, ids, displacements, state_map)

        print(f"Reducing to 3D ({args.method.upper()}, joint fit)...")
        combined = np.vstack([vectors, virtual_vectors])
        combined_3d = reduce_to_3d(combined, method=args.method)
        n = len(ids)
        orig_3d = combined_3d[:n]
        virtual_3d = combined_3d[n:]

        print("Building cosmic comparison...")
        fig = build_comparison_figure(orig_3d, virtual_3d, ids, props, edges, config)
    else:
        print("Building virtual positions...")
        virtual_vectors = build_virtual_vectors(vectors, ids, displacements, state_map)

        print(f"Reducing to 3D ({args.method.upper()})...")
        coords_3d = reduce_to_3d(virtual_vectors, method=args.method)

        print("Building cosmic view...")
        fig = build_single_figure(coords_3d, ids, props, edges, config, "— Virtual Space")

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
