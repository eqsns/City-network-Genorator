from collections import OrderedDict
from decimal import Decimal
import re

import bootstrap
import osmnx as ox
from osmnx import _overpass, projection, settings
import pytest
from shapely.geometry import Point, Polygon, box

from swmmanywhere import osm_queries


@pytest.fixture
def captured(monkeypatch):
    calls = []
    response = {'elements': [
        {'type': 'way', 'id': 1, 'nodes': [2, 3], 'tags': {'highway': 'residential', 'lanes': '2'}},
        {'type': 'node', 'id': 2, 'lat': 32.0, 'lon': 118.0},
        {'type': 'node', 'id': 3, 'lat': 40.0, 'lon': 125.0},
    ]}
    def request(data):
        calls.append(data)
        return response
    monkeypatch.setattr(_overpass, '_overpass_request', request)
    return calls, response


def query_bounds(query):
    match = re.search(r'\((-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)\);>', query)
    assert match, query
    return tuple(Decimal(value) for value in match.groups())


def assert_covers(query, coordinate_string):
    south, west, north, east = query_bounds(query)
    coordinates = [Decimal(value) for value in coordinate_string.split()]
    assert all(south <= latitude <= north for latitude in coordinates[::2])
    assert all(west <= longitude <= east for longitude in coordinates[1::2])


def capped_native_pieces(monkeypatch, polygon):
    with monkeypatch.context() as patch:
        patch.setattr(settings, 'max_query_area_size', min(settings.max_query_area_size, 25_000_000))
        return _overpass._make_overpass_polygon_coord_strs(polygon)


def test_decimal_envelope_does_not_round_inward(monkeypatch, captured):
    coordinates = '32.00000000001 -118.00000000001 32.99999999999 -117.99999999999 32.5 -118.2 32.00000000001 -118.00000000001'
    monkeypatch.setattr(_overpass, '_make_overpass_polygon_coord_strs', lambda polygon: [coordinates])
    list(osm_queries.download_network(box(-118.2, 32, -118, 33), 'drive', None))
    assert_covers(captured[0][0]['data'], coordinates)


def test_real_polygon_uses_its_complete_envelope(monkeypatch, captured):
    polygon = Polygon([(118.75, 32.01), (118.79, 32.03), (118.77, 32.05), (118.745, 32.035)])
    native = capped_native_pieces(monkeypatch, polygon)
    results = list(osm_queries.download_network(polygon, 'walk', None))
    assert len(results) == len(native)
    for request, coordinates in zip(captured[0], native, strict=True):
        assert_covers(request['data'], coordinates)
        assert 'poly:' not in request['data']


def test_native_subdivision_keeps_every_piece(monkeypatch, captured):
    monkeypatch.setattr(settings, 'max_query_area_size', 20_000_000)
    polygon = box(118.745, 32.010, 118.835, 32.090)
    pieces = _overpass._make_overpass_polygon_coord_strs(polygon)
    assert len(pieces) > 1
    results = list(osm_queries.download_network(polygon, 'drive', None))
    assert len(results) == len(pieces)
    for request, coordinates in zip(captured[0], pieces, strict=True):
        assert_covers(request['data'], coordinates)


def test_large_region_uses_25_square_km_native_subdivision(monkeypatch, captured):
    monkeypatch.setattr(settings, 'max_query_area_size', 2_500_000_000)
    polygon = box(109.443226, 36.569573, 109.531631, 36.617569)
    assert len(_overpass._make_overpass_polygon_coord_strs(polygon)) == 1
    pieces = capped_native_pieces(monkeypatch, polygon)
    assert len(pieces) > 1
    results = list(osm_queries.download_network(polygon, 'walk', None))
    assert len(results) == len(pieces)
    assert settings.max_query_area_size == 2_500_000_000
    for request, coordinates in zip(captured[0], pieces, strict=True):
        assert_covers(request['data'], coordinates)


@pytest.mark.parametrize('original_area', [2_500_000_000, 10_000_000])
@pytest.mark.parametrize('failure', [False, True])
def test_subdivision_area_cap_is_restored_on_success_or_failure(monkeypatch, original_area, failure):
    monkeypatch.setattr(settings, 'max_query_area_size', original_area)
    observed = []
    def subdivide(polygon):
        observed.append(('subdivide', settings.max_query_area_size))
        if failure:
            raise RuntimeError('projection failed')
        return ['32.01 118.75 32.02 118.76 32.01 118.75']
    def request(data):
        observed.append(('request', settings.max_query_area_size))
        return {'elements': []}
    monkeypatch.setattr(_overpass, '_make_overpass_polygon_coord_strs', subdivide)
    monkeypatch.setattr(_overpass, '_overpass_request', request)
    download = osm_queries.download_network(box(118.75, 32.01, 118.76, 32.02), 'drive', None)
    if failure:
        with pytest.raises(RuntimeError, match='projection failed'):
            list(download)
        assert observed == [('subdivide', min(original_area, 25_000_000))]
    else:
        list(download)
        assert observed == [('subdivide', min(original_area, 25_000_000)), ('request', original_area)]
    assert settings.max_query_area_size == original_area


@pytest.mark.parametrize('network_type', ['walk', 'drive'])
def test_network_filter_is_unchanged(network_type, captured):
    list(osm_queries.download_network(box(118.75, 32.01, 118.76, 32.02), network_type, None))
    query = captured[0][0]['data']
    assert f'way{_overpass._get_network_filter(network_type)}(' in query


@pytest.mark.parametrize('filters', ['["waterway"]', ['["waterway"]', '["power"="line"]']])
def test_custom_filters_are_preserved(filters, captured):
    list(osm_queries.download_network(box(118.75, 32.01, 118.76, 32.02), 'unused', filters))
    expected = filters if isinstance(filters, list) else [filters]
    assert len(captured[0]) == len(expected)
    for request, way_filter in zip(captured[0], expected, strict=True):
        assert f'way{way_filter}(' in request['data']


def test_workload_uses_all_pieces_and_filters_before_downloading(monkeypatch, captured):
    from swmmanywhere import osm_network
    observed = []
    coordinates = '32.01 118.75 32.02 118.76 32.01 118.75'
    monkeypatch.setattr(_overpass, '_make_overpass_polygon_coord_strs', lambda polygon: [coordinates] * 49)
    monkeypatch.setattr(osm_network, 'configure_workload', lambda count: observed.append((count, len(captured[0]))))
    results = list(osm_queries.download_network(box(118.75, 32.01, 118.76, 32.02), 'unused',
                                                ['["waterway"]', '["power"="line"]']))
    assert observed == [(98, 0)]
    assert len(results) == 98


def test_full_nodes_and_tags_are_requested_without_output_clipping(captured):
    results = list(osm_queries.download_network(box(118.75, 32.01, 118.76, 32.02), 'drive', None))
    request = captured[0][0]
    assert isinstance(request, OrderedDict)
    assert request['data'].endswith(';>;);out body qt;')
    assert 'out geom' not in request['data']
    assert 'out skel' not in request['data']
    assert '[bbox:' not in request['data']
    # Return every received element, including referenced nodes beyond the bbox.
    assert results[0] is captured[1]
    assert results[0]['elements'][0]['tags']['lanes'] == '2'
    assert results[0]['elements'][-1]['lat'] == 40.0


def test_graph_from_bbox_keeps_native_500m_buffer(monkeypatch):
    bbox = (118.745, 32.010, 118.835, 32.090)
    original = box(*bbox)
    projected, crs = projection.project_geometry(original)
    expected, _ = projection.project_geometry(projected.buffer(500), crs=crs, to_latlong=True)
    received = []
    queries = []
    def download(polygon, network_type, custom_filter):
        received.append(polygon)
        yield from osm_queries.download_network(polygon, network_type, custom_filter)
    monkeypatch.setattr(_overpass, '_download_overpass_network', download)
    monkeypatch.setattr(_overpass, '_overpass_request', lambda data: queries.append(data) or {'elements': []})
    with pytest.raises(ValueError, match='No data elements'):
        ox.graph_from_bbox(bbox, network_type='drive', truncate_by_edge=True)
    assert len(received) == 1
    # Native bbox_to_poly starts at another corner: compare the same geometry,
    # independent of vertex order and insignificant reprojection differences.
    assert received[0].hausdorff_distance(expected) < 1e-9
    pieces = capped_native_pieces(monkeypatch, expected)
    assert len(pieces) > 1
    for query, coordinates in zip(queries, pieces, strict=True):
        assert_covers(query['data'], coordinates)
    bounds = [tuple(map(float, query_bounds(query['data']))) for query in queries]
    south = min(bounds_[0] for bounds_ in bounds)
    west = min(bounds_[1] for bounds_ in bounds)
    north = max(bounds_[2] for bounds_ in bounds)
    east = max(bounds_[3] for bounds_ in bounds)
    assert west < bbox[0] and south < bbox[1] and east > bbox[2] and north > bbox[3]


@pytest.mark.parametrize('truncate_by_edge', [False, True])
def test_envelope_corner_extras_are_clipped_without_changing_network(monkeypatch, truncate_by_edge):
    """Run the native graph builder, clipping and simplification in both cases.

    The envelope downloads an additional way at a rounded buffer corner. Its
    nodes must not leak into the graph, while ordinary boundary nodes and the
    bend/intersection inside the requested region must retain native topology.
    """
    bbox = (118.75, 32.01, 118.76, 32.02)
    projected, crs = projection.project_geometry(box(*bbox))
    buffered, _ = projection.project_geometry(projected.buffer(500), crs=crs, to_latlong=True)
    west, south, _, _ = buffered.bounds
    extra_coordinates = [(west + 0.00005, south + 0.00005), (west + 0.0001, south + 0.0001)]
    assert all(not buffered.covers(Point(coordinates)) for coordinates in extra_coordinates)
    coordinates = {
        1: (118.748, 32.015), 2: (118.752, 32.0152), 3: (118.755, 32.015),
        4: (118.758, 32.015), 5: (118.762, 32.015),
        6: (118.755, 32.012), 7: (118.755, 32.018),
        90: extra_coordinates[0], 91: extra_coordinates[1],
    }
    ways = [
        {'type': 'way', 'id': 100, 'nodes': [1, 2, 3, 4, 5], 'tags': {'highway': 'residential', 'lanes': '2'}},
        {'type': 'way', 'id': 101, 'nodes': [6, 3, 7], 'tags': {'highway': 'residential'}},
        {'type': 'way', 'id': 190, 'nodes': [90, 91], 'tags': {'highway': 'residential'}},
    ]
    native_downloader = _overpass._download_overpass_network
    def request(data):
        query = data['data']
        # Emulate the only response difference: bbox selects the extra corner
        # way, polygon does not. Both responses include all referenced nodes.
        selected = ways[:-1] if 'poly:' in query else ways
        referenced = {node for way in selected for node in way['nodes']}
        nodes = [{'type': 'node', 'id': node, 'lon': coordinates[node][0], 'lat': coordinates[node][1]}
                 for node in sorted(referenced)]
        return {'elements': [*selected, *nodes]}
    monkeypatch.setattr(_overpass, '_overpass_request', request)
    expected = ox.graph_from_bbox(bbox, network_type='drive', retain_all=True,
                                  truncate_by_edge=truncate_by_edge)
    monkeypatch.setattr(_overpass, '_download_overpass_network', osm_queries.download_network)
    actual = ox.graph_from_bbox(bbox, network_type='drive', retain_all=True,
                                truncate_by_edge=truncate_by_edge)
    assert dict(actual.nodes(data=True)) == dict(expected.nodes(data=True))
    assert dict(actual.edges.items()) == dict(expected.edges.items())
    assert 90 not in actual and 91 not in actual
    assert 2 not in actual and 4 not in actual  # Degree-two bend nodes simplify.
    assert 3 in actual  # The four-way intersection is retained.
    assert (1 in actual) == truncate_by_edge  # Native treatment of boundary nodes.
    assert native_downloader is not osm_queries.download_network
