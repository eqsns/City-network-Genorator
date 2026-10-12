"""Regression coverage for coincident OSM nodes and street segmentation."""
from copy import deepcopy

import bootstrap
import networkx as nx
import pytest
import shapely
from shapely.errors import GEOSException

from swmmanywhere.graphfcns import network_cleaning_graphfcns as cleaning
from swmmanywhere.parameters import SubcatchmentDerivation


def add_road(graph, u, v, coordinates, road_id, **attributes):
    geometry = shapely.LineString(coordinates)
    if coordinates:
        graph.add_node(u, x=coordinates[0][0], y=coordinates[0][1])
        graph.add_node(v, x=coordinates[-1][0], y=coordinates[-1][1])
    graph.add_edge(u, v, id=road_id, geometry=geometry,
                   length=geometry.length, edge_type="street", **attributes)


def split(graph):
    return cleaning.split_long_edges()(
        graph, subcatchment_derivation=SubcatchmentDerivation(max_street_length=60)
    )


def node_at(graph, point):
    return next(n for n, data in graph.nodes(data=True)
                if (data["x"], data["y"]) == point)


def assert_valid_segments(graph):
    for u, v, data in graph.edges(data=True):
        geometry = data["geometry"]
        assert geometry.is_valid
        assert 0 < geometry.length <= 60
        assert data["length"] == pytest.approx(geometry.length)
        coordinates = list(geometry.coords)
        assert len(coordinates) == 2
        assert set(coordinates) == {
            (graph.nodes[u]["x"], graph.nodes[u]["y"]),
            (graph.nodes[v]["x"], graph.nodes[v]["y"]),
        }


@pytest.mark.parametrize("repeat_count", [2, 3, 5])
def test_zero_length_road_does_not_abort_or_steal_valid_road_attributes(repeat_count):
    graph = nx.MultiGraph(crs="EPSG:32651", place="regression")
    # Insert the collapsed road first: filtering only the geometry array would
    # incorrectly assign its ID and other attributes to the following road.
    add_road(graph, "z1", "z2", [(0, 0)] * repeat_count,
             "collapsed", lanes=9, marker="do-not-use")
    add_road(graph, "a", "b", [(0, 0), (180, 0)],
             "valid-road", lanes=2, marker="retained")
    before = deepcopy(graph)

    result = split(graph)

    assert result.number_of_edges() == 3
    assert nx.is_connected(result)
    assert result.graph == graph.graph
    assert sum(d["length"] for _, _, d in result.edges(data=True)) == pytest.approx(180)
    for _, _, data in result.edges(data=True):
        assert data["id"] == "valid-road"
        assert data["lanes"] == 2
        assert data["marker"] == "retained"
    assert_valid_segments(result)
    assert nx.utils.graphs_equal(graph, before)


def test_coincident_distinct_nodes_keep_both_roads_connected():
    graph = nx.MultiGraph()
    add_road(graph, "left", "middle-a", [(-120, 0), (0, 0)], "west")
    add_road(graph, "middle-a", "middle-b", [(0, 0), (0, 0)], "collapsed")
    add_road(graph, "middle-b", "right", [(0, 0), (120, 0)], "east")

    result = split(graph)

    assert result.number_of_edges() == 4
    assert result.number_of_nodes() == 5
    assert nx.is_connected(result)
    assert nx.shortest_path_length(result, node_at(result, (-120, 0)),
                                   node_at(result, (120, 0)), weight="length") == 240
    assert {d["id"] for _, _, d in result.edges(data=True)} == {"west", "east"}
    assert result.degree[node_at(result, (0, 0))] == 2
    assert_valid_segments(result)


def test_repeated_intermediate_points_preserve_bent_road_geometry():
    graph = nx.MultiGraph()
    coordinates = [(0, 0), (0, 0), (60, 0), (60, 0), (60, 60), (60, 60)]
    add_road(graph, 1, 2, coordinates, "bent-road")

    result = split(graph)

    assert result.number_of_edges() == 2
    assert nx.is_connected(result)
    assert sum(d["length"] for _, _, d in result.edges(data=True)) == pytest.approx(120)
    assert node_at(result, (60, 0)) in result
    assert_valid_segments(result)


@pytest.mark.parametrize("same_node_id", [False, True])
def test_positive_length_closed_road_is_retained(same_node_id):
    graph = nx.MultiGraph()
    coordinates = [(0, 0), (60, 0), (60, 60), (0, 60), (0, 0)]
    add_road(graph, "a", "a" if same_node_id else "b", coordinates, "ring")

    result = split(graph)

    assert result.number_of_edges() == result.number_of_nodes() == 4
    assert nx.is_connected(result)
    assert all(degree == 2 for _, degree in result.degree())
    assert sum(d["length"] for _, _, d in result.edges(data=True)) == pytest.approx(240)
    assert_valid_segments(result)


def test_very_short_but_positive_road_is_not_treated_as_collapsed():
    graph = nx.MultiGraph()
    add_road(graph, 1, 2, [(0, 0), (1e-100, 0)], "short")

    result = split(graph)

    assert result.number_of_edges() == 1
    assert next(iter(result.edges(data=True)))[2]["length"] == 1e-100
    assert_valid_segments(result)


@pytest.mark.parametrize("kind", ["empty-graph", "empty-line", "collapsed-line", "mixed"])
def test_all_degenerate_inputs_return_empty_graph_without_geos_crash(kind):
    graph = nx.MultiGraph(crs="EPSG:32651")
    if kind in {"empty-line", "mixed"}:
        add_road(graph, 1, 2, [], "empty")
    if kind in {"collapsed-line", "mixed"}:
        add_road(graph, 3, 4, [(1, 2), (1, 2)], "collapsed")

    result = split(graph)

    assert result.number_of_edges() == result.number_of_nodes() == 0
    assert result.graph == graph.graph


def test_nan_geometry_is_not_silently_filtered_as_zero_length():
    graph = nx.MultiGraph()
    with pytest.warns(RuntimeWarning, match="invalid value"):
        add_road(graph, 1, 2, [(0, 0), (float("nan"), 1)], "nonfinite")
    # Invalid coordinates require an actionable failure, never a partial graph
    # reported as success. The source road ID must remain in that failure.
    with pytest.raises(ValueError, match="nonfinite"):
        split(graph)


def test_unrelated_geos_error_is_propagated_instead_of_dropping_road(monkeypatch):
    graph = nx.MultiGraph()
    add_road(graph, 1, 2, [(0, 0), (120, 0)], "valid-road")

    def failed(geometries, max_length):
        raise GEOSException("unexpected geometry failure")

    monkeypatch.setattr(cleaning.shapely, "segmentize", failed)
    with pytest.raises(GEOSException, match="unexpected geometry failure"):
        split(graph)


def test_non_linestring_geometry_is_not_silently_treated_as_zero_length():
    graph = nx.MultiGraph()
    graph.add_edge(1, 2, id="not-a-road", geometry=shapely.Point(0, 0), length=0)
    with pytest.raises((TypeError, ValueError, GEOSException)):
        split(graph)
