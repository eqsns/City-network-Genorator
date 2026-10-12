"""Reproduce the user's geometry failure from complete local inputs only."""
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import bootstrap
import shapely
from swmmanywhere import parameters, preprocessing
from swmmanywhere.filepaths import FilePaths
from swmmanywhere.graph_utilities import graphfcns


def main():
    source = Path(sys.argv[1]).resolve()
    job = json.loads((source / 'app_job.json').read_text(encoding='utf-8'))
    work = ROOT / '.cache' / '道路几何故障复现' / str(time.time_ns())
    work.mkdir(parents=True)
    inputs = {key: source / 'bbox_1' / 'download' / filename for key, filename in
              dict(street='street.json', river='river.json', elevation='elevation.tif', building='building.geoparquet').items()}
    addresses = FilePaths(work, 'audit', job['bbox'], **inputs)
    graph = preprocessing.create_starting_graph(addresses)
    params = parameters.get_full_parameters()
    steps = ('assign_id', 'fix_geometries', 'remove_non_pipe_allowable_links',
             'calculate_streetcover', 'remove_parallel_edges', 'to_undirected')
    for step in steps:
        graph = graphfcns[step](graph, addresses=addresses, **params)
    length = params['subcatchment_derivation'].max_street_length
    failures = []
    for u, v, data in graph.edges(data=True):
        geom = data['geometry']
        try:
            shapely.segmentize(geom, length)
        except Exception as exc:
            failures.append(dict(u=u, v=v, id=data.get('id'), osmid=data.get('osmid'),
                                 edge_type=data.get('edge_type'), wkt=geom.wkt,
                                 length=geom.length, coordinate_count=int(shapely.get_num_coordinates(geom)),
                                 reason=shapely.is_valid_reason(geom), error=str(exc)))
    report = dict(source=str(source), bbox=job['bbox'], graph_nodes=len(graph.nodes),
                  graph_edges=len(graph.edges), failed_edges=len(failures), failures=failures)
    (work / 'audit.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(dict(report, report=str(work / 'audit.json')), ensure_ascii=False), flush=True)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
