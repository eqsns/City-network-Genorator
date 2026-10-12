"""Preprocessing module for SWMManywhere.

A module to call downloads, preprocess these downloads into formats suitable
for graphfcns, and some other utilities (such as creating a project folder
structure or create the starting graph from rivers/streets).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import networkx as nx

from swmmanywhere import geospatial_utilities as go
from swmmanywhere import prepare_data
from swmmanywhere.filepaths import FilePaths
from swmmanywhere.logging import logger
from swmmanywhere.progress import report
from swmmanywhere.utilities import load_graph, read_df, save_graph, write_df


def prepare_precipitation(
    bbox: tuple[float, float, float, float],
    addresses: FilePaths,
    api_keys: dict[str, str],
    target_crs: str,
    source_crs: str = "EPSG:4326",
):
    """Download and reproject precipitation data."""
    if addresses.bbox_paths.precipitation.exists():
        return
    logger.info(f"downloading precipitation to {addresses.bbox_paths.precipitation}")
    precip = prepare_data.download_precipitation(
        bbox, api_keys["cds_username"], api_keys["cds_api_key"]
    )
    precip = precip.reset_index()
    precip = go.reproject_df(precip, source_crs, target_crs)

    write_df(precip, addresses.bbox_paths.precipitation)


def prepare_elevation(
    bbox: tuple[float, float, float, float], addresses: FilePaths, target_crs: str
):
    """Download and reproject elevation data."""
    if addresses.bbox_paths.elevation.exists():
        return
    logger.info(f"downloading elevation to {addresses.bbox_paths.elevation}")
    with tempfile.TemporaryDirectory(dir=".") as temp_dir:
        fid = Path(temp_dir) / "elevation.tif"
        prepare_data.download_elevation(
            fid,
            bbox,
        )
        go.reproject_raster(target_crs, fid, addresses.bbox_paths.elevation)


def prepare_building(
    bbox: tuple[float, float, float, float], addresses: FilePaths, target_crs: str
):
    """Download and reproject building data."""
    if addresses.bbox_paths.building.exists():
        return

    logger.info(f"downloading buildings to {addresses.bbox_paths.building}")
    prepare_data.download_buildings_bbox(addresses.bbox_paths.building, bbox)

    buildings = read_df(addresses.bbox_paths.building)
    buildings = buildings.to_crs(target_crs)

    write_df(buildings, addresses.bbox_paths.building)


def prepare_street(
    bbox: tuple[float, float, float, float],
    addresses: FilePaths,
    target_crs: str,
    source_crs: str = "EPSG:4326",
    network_types=["drive"],
):
    """Download and reproject street graph.

    Download the street graph within the bbox and reproject it to the UTM zone.
    The street graph is downloaded for all network types in network_types. The
    street graph is saved to the addresses.bbox_paths.street directory.

    Args:
        bbox (tuple[float, float, float, float]): Bounding box coordinates in
            the format (minx, miny, maxx, maxy) in EPSG:4326.
        addresses (FilePaths): Class containing the addresses of the directories.
        target_crs (str): Target CRS to reproject the graph to.
        source_crs (str): Source CRS of the graph.
        network_types (list): List of network types to download. For duplicate
            edges, nx.compose_all selects the attributes in priority of last to
            first. In likelihood, you want to ensure that the last network in
            the list is `drive`, so as to retain information about `lanes`,
            which is needed to calculate impervious area.
    """
    if addresses.bbox_paths.street.exists():
        return
    logger.info(f"downloading network to {addresses.bbox_paths.street}")
    if "drive" in network_types and network_types[-1] != "drive":
        logger.warning(
            """The last network type should be `drive` to retain 
                        `lanes` attribute, needed to calculate impervious area.
                        Moving it to the last position."""
        )
        network_types.pop("drive")
        network_types.append("drive")
    networks = []
    for network_type in network_types:
        network = prepare_data.download_street(bbox, network_type=network_type)
        nx.set_edge_attributes(network, network_type, "network_type")
        networks.append(network)
    street_network = nx.compose_all(networks)

    # Reproject graph
    street_network = go.reproject_graph(street_network, source_crs, target_crs)

    save_graph(street_network, addresses.bbox_paths.street)


def prepare_river(
    bbox: tuple[float, float, float, float],
    addresses: FilePaths,
    target_crs: str,
    source_crs: str = "EPSG:4326",
):
    """Download and reproject river graph."""
    if addresses.bbox_paths.river.exists():
        return
    logger.info(f"downloading river network to {addresses.bbox_paths.river}")
    river_network = prepare_data.download_river(bbox)
    river_network = go.reproject_graph(river_network, source_crs, target_crs)
    save_graph(river_network, addresses.bbox_paths.river)


def run_downloads(
    bbox: tuple[float, float, float, float],
    addresses: FilePaths,
    network_types=["drive"],
    progress_callback=None,
):
    """Run the data downloads.

    Run the precipitation, elevation, building, street and river network
    downloads. If the data already exists, do not download it again. Reprojects
    data to the UTM zone.

    Args:
        bbox (tuple[float, float, float, float]): Bounding box coordinates in
            the format (minx, miny, maxx, maxy) in EPSG:4326.
        addresses (FilePaths): Class containing the addresses of the directories.
        network_types (list): List of network types to download.
    """
    target_crs = go.get_utm_epsg(bbox[0], bbox[1])

    # Download precipitation data
    # Currently commented because it doesn't work
    # prepare_precipitation(bbox, addresses, api_keys, target_crs)

    steps = [
        ('elevation', '高程数据', 5, prepare_elevation, {}),
        ('building', '建筑轮廓', 15, prepare_building, {}),
        ('street', 'OSM 道路信息', 25, prepare_street, {'network_types': network_types}),
        ('river', 'OSM 河流信息', 35, prepare_river, {}),
    ]
    for name, label, percent, function, kwargs in steps:
        cached = getattr(addresses.bbox_paths, name).exists()
        message = ('正在读取缓存的' if cached else '正在获取') + label
        report(progress_callback, name, percent, message)
        try:
            from swmmanywhere.osm_network import download_status
            with download_status(lambda text: report(progress_callback, name, percent, text, checkpoint=False)):
                function(bbox, addresses, target_crs, **kwargs)
        except Exception as exc:
            raise RuntimeError(f'{label}处理失败：{exc}') from exc
    report(progress_callback, 'graph', 45, '基础数据已就绪，正在建立管网')


def create_starting_graph(addresses: FilePaths):
    """Create the starting graph.

    Create the starting graph by combining the street and river networks.

    Args:
        addresses (FilePaths): Class containing the addresses of the directories.

    Returns:
        nx.Graph: Combined street and river network.
    """
    river = load_graph(addresses.bbox_paths.river)
    nx.set_edge_attributes(river, "river", "edge_type")
    street = load_graph(addresses.bbox_paths.street)
    nx.set_edge_attributes(street, "street", "edge_type")
    return nx.compose(river, street)
