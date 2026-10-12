"""Run SWMManywhere with a Windows-safe Overture release lookup."""

from __future__ import annotations

import argparse
import json
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import requests
import bootstrap
from swmmanywhere import prepare_data
from swmmanywhere import graph_utilities
from swmmanywhere import swmmanywhere as engine
from swmmanywhere.logging import set_verbose
from swmmanywhere.progress import report

from plot_nanjing import render_network


def overture_release() -> tuple[str, str]:
    cache_file = Path.cwd() / ".cache" / "overture_release.json"
    release = None
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if datetime.now() - datetime.fromisoformat(cached["timestamp"]) < timedelta(hours=72):
                release = cached["release"]
        except (ValueError, KeyError, TypeError):
            pass
    if not release or not re.fullmatch(r"\d{4}-\d{2}-\d{2}\.\d+", release):
        response = requests.get("https://stac.overturemaps.org/catalog.json", timeout=30)
        response.raise_for_status()
        links = response.json()["links"]
        releases = [
            match.group(0)
            for link in links
            if link.get("rel") == "child"
            for match in [re.search(r"\d{4}-\d{2}-\d{2}\.\d+", link.get("href", ""))]
            if match
        ]
        if not releases:
            raise RuntimeError("无法从 Overture 目录找到建筑数据版本")
        release = max(releases)
        cache_file.parent.mkdir(exist_ok=True)
        cache_file.write_text(json.dumps({"release": release, "timestamp": datetime.now().isoformat()}), encoding="utf-8")
    return "us-west-2", f"overturemaps-us-west-2/release/{release}/theme=buildings/type=building/"


def main(config_path: Path, title: str, progress_callback=None) -> dict:
    prepare_data._get_latest_s3_url = overture_release
    set_verbose(True)
    # Keep progress messages without writing every intermediate graph to disk.
    graph_utilities.verbose = lambda: False
    engine.verbose = lambda: False
    config = engine.load_config(config_path)
    import osmnx as ox
    ox.settings.requests_timeout = 180
    inp, _ = engine.swmmanywhere(config, progress_callback=progress_callback)
    inp = Path(inp)
    if inp.suffix.lower() != ".inp" or not inp.exists():
        raise RuntimeError("未生成 SWMM 输入文件；请检查边界范围内的道路和河流数据")
    storm_source = Path(engine.__file__).parent / "defs" / "storm.dat"
    storm_target = inp.parent / "storm.dat"
    shutil.copyfile(storm_source, storm_target)
    text = inp.read_text(encoding="utf-8")
    inp.write_text(text.replace(str(storm_source), str(storm_target)), encoding="utf-8")
    report(progress_callback, 'render', 94, '正在绘制管网图并转换地图坐标')
    from artifacts import prepare_artifacts
    stats = prepare_artifacts(inp.parent, title)
    report(progress_callback, 'verify', 99, '正在检查模型及导出文件')
    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=Path)
    parser.add_argument("--title", required=True)
    args = parser.parse_args()
    main(args.config, args.title)
