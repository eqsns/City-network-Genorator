"""Render a SWMManywhere model as a PNG network map."""

from __future__ import annotations

import argparse
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager


def render_network(model_dir: Path, title: str, output: Path | None = None) -> dict:
    model_dir = Path(model_dir)
    output = output or model_dir / "network.png"
    edges = gpd.read_parquet(model_dir / "edges.geoparquet")
    nodes = gpd.read_parquet(model_dir / "nodes.geoparquet")
    inp_path = next(model_dir.glob("model_*.inp"))
    inp = inp_path.read_text(encoding="utf-8")
    outfall_section = inp.split("[OUTFALLS]", 1)[1].split("[", 1)[0]
    outfall_ids = {
        line.split()[0].removesuffix("_outfall")
        for line in outfall_section.splitlines()
        if line.strip() and not line.startswith(";")
    }
    outfalls = nodes[nodes["id"].astype(str).isin(outfall_ids)]
    font_path = Path(r"C:\Windows\Fonts\simhei.ttf")
    if font_path.exists():
        font_manager.fontManager.addfont(str(font_path))
        plt.rcParams["font.family"] = "SimHei"
        plt.rcParams["axes.unicode_minus"] = False
    fig, ax = plt.subplots(figsize=(13, 10), dpi=170)
    fig.patch.set_facecolor("#f6f8f8")
    ax.set_facecolor("#f6f8f8")
    edges.plot(ax=ax, linewidth=0.48, color="#177c88", alpha=0.8)
    if len(outfalls):
        outfalls.plot(ax=ax, markersize=7, color="#e97848", alpha=0.93, zorder=3)
    ax.set_title(title + " · 合成排水管网", fontsize=18, loc="left", pad=18)
    ax.set_xlabel("东坐标（米）" if font_path.exists() else "Easting (m)")
    ax.set_ylabel("北坐标（米）" if font_path.exists() else "Northing (m)")
    ax.set_aspect("equal")
    ax.ticklabel_format(style="plain", useOffset=False)
    ax.grid(alpha=0.14, linewidth=0.5)
    subtitle = (
        f"{len(edges):,} 条街道管段  ·  {len(outfalls):,} 个排口  ·  "
        "SWMManywhere 合成模型，非现状实测管网"
        if font_path.exists()
        else f"{len(edges):,} street conduits · {len(outfalls):,} outfalls · Synthetic model"
    )
    ax.text(0, -0.11, subtitle, transform=ax.transAxes, fontsize=10, color="#52636b")
    fig.subplots_adjust(left=0.11, right=0.97, top=0.90, bottom=0.16)
    fig.savefig(output, dpi=170, facecolor=fig.get_facecolor())
    plt.close(fig)
    return {
        "image": str(output),
        "street_conduits": len(edges),
        "nodes": len(nodes),
        "outfalls": len(outfalls),
        "crs": edges.crs.to_epsg() if edges.crs else None,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("model_dir", type=Path)
    parser.add_argument("--title", default="南京主城区核心范围")
    args = parser.parse_args()
    print(render_network(args.model_dir, args.title))
