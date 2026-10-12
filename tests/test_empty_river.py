"""An empty valid waterway result is distinct from a failed download."""
import bootstrap
import pytest
import requests
from osmnx._errors import InsufficientResponseError
from swmmanywhere import prepare_data


def test_valid_empty_river_allows_topography_based_outfalls(monkeypatch):
    def empty(**kwargs):
        raise InsufficientResponseError('No data elements in server response. Check query location/filters and log.')
    monkeypatch.setattr(prepare_data, '_osm_graph_from_bbox', empty)
    graph = prepare_data.download_river([109.48, 36.59, 109.49, 36.60])
    assert graph.number_of_nodes() == graph.number_of_edges() == 0


@pytest.mark.parametrize('error', [requests.ConnectionError('network failed'),
                                 ValueError('malformed response'),
                                 InsufficientResponseError('different failure')])
def test_river_failures_never_become_empty_success(monkeypatch, error):
    def failed(**kwargs):
        raise error
    monkeypatch.setattr(prepare_data, '_osm_graph_from_bbox', failed)
    with pytest.raises(type(error), match=str(error)):
        prepare_data.download_river([109.48, 36.59, 109.49, 36.60])


def test_empty_roads_still_fail(monkeypatch):
    def empty(**kwargs):
        raise InsufficientResponseError('No data elements in server response.')
    monkeypatch.setattr(prepare_data, '_osm_graph_from_bbox', empty)
    with pytest.raises(InsufficientResponseError):
        prepare_data.download_street([109.48, 36.59, 109.49, 36.60])
