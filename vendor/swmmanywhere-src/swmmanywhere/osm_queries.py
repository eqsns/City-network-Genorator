"""Small Overpass bounding-box queries for the pinned OSMnx graph workflow.

The caller still supplies OSMnx's 500 m buffered polygon and performs the native
polygon truncation and topology simplification afterwards. Query envelopes may
download extra corner features, but never shrink the requested polygon.
"""
from collections import OrderedDict
from decimal import Decimal

from osmnx import _overpass, settings as ox_settings

MAX_QUERY_AREA_M2 = 25_000_000


def _bbox_from_coordinates(coordinates):
    """Enclose every coordinate of OSMnx's already rounded query polygon.

    Decimal preserves those coordinates exactly: another float-formatting pass
    must not move a boundary inward and lose an OSM node at the edge.
    """
    values = [Decimal(value) for value in coordinates.split()]
    latitudes, longitudes = values[::2], values[1::2]
    bounds = (min(latitudes), min(longitudes), max(latitudes), max(longitudes))
    return ','.join(format(value, 'f') for value in bounds)


def download_network(polygon, network_type, custom_filter):
    """Yield full way/node responses, compatible with OSMnx's downloader.

    Keep its subdivision and exact tag filters, and let the bounded transport
    handle caching and failures. All referenced nodes are fetched, including
    nodes outside the envelope, so edges and boundary topology remain intact.
    """
    if isinstance(custom_filter, list):
        filters = custom_filter
    elif isinstance(custom_filter, str):
        filters = [custom_filter]
    else:
        filters = [_overpass._get_network_filter(network_type)]

    settings = _overpass._make_overpass_settings()
    # Use the native projected subdivision, with smaller pieces for public
    # servers. Restore global OSMnx configuration before making any requests.
    original_area = ox_settings.max_query_area_size
    try:
        ox_settings.max_query_area_size = min(original_area, MAX_QUERY_AREA_M2)
        pieces = _overpass._make_overpass_polygon_coord_strs(polygon)
    finally:
        ox_settings.max_query_area_size = original_area
    from swmmanywhere.osm_network import configure_workload, notice
    configure_workload(len(pieces) * len(filters))
    label = '河流' if custom_filter else {'walk': '步行道路', 'drive': '行车道路'}.get(network_type, '道路')
    for index, coordinates in enumerate(pieces, 1):
        if len(pieces) > 1:
            notice(f'正在获取 OSM 数据分块 {index}/{len(pieces)}（{label}）')
        bbox = _bbox_from_coordinates(coordinates)
        for way_filter in filters:
            query = f'{settings};(way{way_filter}({bbox});>;);out body qt;'
            yield _overpass._overpass_request(OrderedDict(data=query))
