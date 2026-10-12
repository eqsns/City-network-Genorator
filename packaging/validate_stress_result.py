"""Read-only integrity checks for a completed CNG model directory.

Run with .venv-build/Scripts/python.exe packaging/validate_stress_result.py MODEL.
The only writes are SWMM diagnostics in a disposable temporary copy. No hydraulic
simulation, download, artifact repair, or modification of the model is performed.
JSON is written to stdout; exit status is 0 for passing checks, 1 for failure.
An unavailable native SWMM library is explicitly reported as a skipped check.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import math
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bootstrap  # noqa: E402,F401 - same vendored dependency path as the app


def parse_inp(text):
    sections = defaultdict(list)
    section = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(';'):
            continue
        if line.startswith('['):
            section = line.split(']', 1)[0][1:].upper()
            continue
        if section:
            tokens = re.findall(r'"[^"]*"|[^\s;]+|;.*', line)
            sections[section].append([token.strip('"') for token in tokens
                                      if not token.startswith(';')])
    return sections


def reject_constant(value):
    raise ValueError(f'Non-finite JSON number: {value}')


def load_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'), parse_constant=reject_constant)


def finite_coordinates(geometry):
    if not isinstance(geometry, dict):
        return False
    if geometry.get('type') == 'GeometryCollection':
        parts = geometry.get('geometries', [])
        return bool(parts) and all(finite_coordinates(part) for part in parts)

    def visit(value):
        if not isinstance(value, list) or not value:
            return False
        if isinstance(value[0], (float, int)):
            return len(value) >= 2 and all(isinstance(v, (float, int)) and
                                          not isinstance(v, bool) and math.isfinite(v)
                                          for v in value)
        return all(visit(part) for part in value)

    return visit(geometry.get('coordinates'))


def native_worker(inp):
    """Run only in the temporary copy supplied by the parent subprocess call."""
    temporary_root = (Path(__file__).resolve().parents[1] / '.cache').resolve()
    if (inp.name != 'model.inp' or inp.parent.parent != temporary_root
            or not inp.parent.name.startswith('cng-swmm-open-') or Path.cwd().resolve() != inp.parent):
        return {'status': 'failed', 'reason': 'Native worker requires an isolated temporary copy.'}
    try:
        from swmm.toolkit import solver
    except (ImportError, OSError) as exc:
        return {'status': 'unavailable', 'reason': f'{type(exc).__name__}: {exc}'}
    result = {'status': 'failed', 'simulation_run': False}
    work = inp.parent
    try:
        code = solver.swmm_open(str(inp), str(work / 'check.rpt'), str(work / 'check.out'))
        if code not in (None, 0):
            raise RuntimeError(f'swmm_open returned {code}')
        result.update(status='passed', version=solver.swmm_get_version())
    except Exception as exc:
        result['reason'] = f'{type(exc).__name__}: {exc}'
    finally:
        try:
            solver.swmm_close()
        except Exception as exc:
            result.update(status='failed', close_error=str(exc))
    report = work / 'check.rpt'
    if report.exists():
        text = report.read_text(encoding='utf-8', errors='replace')
        result['report_errors'] = [line.strip() for line in text.splitlines()
                                   if re.search(r'\bERROR\s+\d+', line)][:20]
        result['report_warnings'] = [line.strip() for line in text.splitlines()
                                     if re.search(r'\bWARNING\b', line)][:20]
        if result['report_errors']:
            result['status'] = 'failed'
    return result


def validate(model, native_timeout=120):
    import geopandas as gpd
    import numpy as np
    import pandas as pd
    import shapely

    started = time.monotonic()
    report = {'model': str(model), 'passed': False, 'checks': [], 'counts': {},
              'warnings': [], 'native_swmm': {'status': 'not_run'}}

    def check(name, ok, **details):
        report['checks'].append(dict(name=name, passed=bool(ok), **details))
        return bool(ok)

    def attempt(name, function):
        try:
            return function()
        except Exception as exc:
            check(name, False, error=f'{type(exc).__name__}: {exc}')
            return None

    def finish():
        report['passed'] = all(item['passed'] for item in report['checks'])
        report['seconds'] = round(time.monotonic() - started, 3)
        return report

    inps = sorted(model.glob('model_*.inp'))
    names = ['edges.geoparquet', 'nodes.geoparquet', 'subcatchments.geoparquet',
             'network.geojson', 'network.png', 'stats.json', 'storm.dat', 'SWMM-model.zip']
    missing = [name for name in names if not (model / name).is_file()
               or (model / name).stat().st_size == 0]
    if not check('required_nonempty_artifacts', not missing and len(inps) == 1
                 and inps[0].stat().st_size > 0, missing=missing,
                 inp_files=[path.name for path in inps]):
        return finish()
    inp = inps[0]
    report['file_bytes'] = {path.name: path.stat().st_size
                            for path in [inp, *(model / name for name in names)]}
    frames = {}
    for kind in ('edges', 'nodes', 'subcatchments'):
        frame = attempt(f'{kind}_readable', lambda k=kind: gpd.read_parquet(model / f'{k}.geoparquet'))
        if frame is not None:
            frames[kind] = frame
            report['counts'][kind] = len(frame)
    if len(frames) != 3:
        return finish()
    edges, nodes, subs = (frames[kind] for kind in ('edges', 'nodes', 'subcatchments'))

    def parquet_checks():
        for kind, frame in frames.items():
            coordinates = shapely.get_coordinates(frame.geometry.array)
            check(f'{kind}_finite_nonempty_geometry', len(frame) > 0 and
                  not frame.geometry.isna().any() and not frame.geometry.is_empty.any()
                  and np.isfinite(coordinates).all())
            invalid = ~frame.geometry.is_valid
            if invalid.any():
                report.setdefault('geometry_warnings', {})[kind] = {
                    'invalid_count': int(invalid.sum()),
                    'reason_sample': shapely.is_valid_reason(frame.geometry[invalid].array[:3]).tolist()}
                report['warnings'].append(f'{kind}: {int(invalid.sum())} topologically invalid geometries.')
            check(f'{kind}_unique_ids', 'id' in frame and frame['id'].notna().all()
                  and frame['id'].astype(str).is_unique)
            check(f'{kind}_projected_crs', frame.crs is not None and frame.crs.is_projected
                  and frame.crs == edges.crs,
                  crs=(f'EPSG:{frame.crs.to_epsg()}' if frame.crs and frame.crs.to_epsg()
                       else str(frame.crs)))
        for kind, columns in {'edges': ('diameter', 'length'),
                              'nodes': ('x', 'y', 'surface_elevation', 'chamber_floor_elevation'),
                              'subcatchments': ('area', 'width', 'slope', 'rc')}.items():
            frame = frames[kind]
            for column in columns:
                values = pd.to_numeric(frame[column], errors='coerce').to_numpy(dtype=float)
                valid = np.isfinite(values)
                if column in ('diameter', 'length', 'area', 'width'):
                    valid &= values > 0
                elif column == 'slope':
                    valid &= values >= 0
                elif column == 'rc':
                    valid &= (values >= 0) & (values <= 100 + 1e-8)
                check(f'{kind}_{column}_usable', valid.all(), invalid_count=int((~valid).sum()))
        node_ids = set(nodes.id.astype(str))
        endpoints = set(edges.u.astype(str)) | set(edges.v.astype(str))
        check('parquet_endpoint_references', endpoints <= node_ids,
              unknown_sample=sorted(endpoints - node_ids)[:10])
        check('parquet_no_self_loops', not (edges.u.astype(str) == edges.v.astype(str)).any())

    attempt('parquet_checks_completed', parquet_checks)
    sections = parse_inp(inp.read_text(encoding='utf-8-sig'))

    def inp_checks():
        for section in ('CONDUITS', 'OUTFALLS', 'STORAGE', 'SUBCATCHMENTS', 'XSECTIONS', 'RAINGAGES'):
            rows = sections[section]
            check(f'inp_{section.lower()}_unique_nonempty', bool(rows) and
                  len({row[0] for row in rows}) == len(rows))
        outfalls = {row[0] for row in sections['OUTFALLS']}
        check('inp_conduit_count', len(sections['CONDUITS']) == len(edges) + len(outfalls),
              actual=len(sections['CONDUITS']), expected=len(edges) + len(outfalls))
        report['counts'].update(inp_conduits=len(sections['CONDUITS']), outfalls=len(outfalls),
                                inp_subcatchments=len(sections['SUBCATCHMENTS']))
        source_nodes = set(nodes.id.astype(str))
        native_nodes = {row[0] for name in ('STORAGE', 'JUNCTIONS', 'DIVIDERS', 'OUTFALLS')
                        for row in sections[name]}
        check('inp_node_ids', {row[0] for row in sections['STORAGE']} == source_nodes
              and not (outfalls & source_nodes)
              and {name.removesuffix('_outfall') for name in outfalls} <= source_nodes)
        unknown = {value for row in sections['CONDUITS'] for value in row[1:3]} - native_nodes
        check('inp_endpoint_references', not unknown, unknown_sample=sorted(unknown)[:10])
        source_links = {str(row.id): (str(row.u), str(row.v)) for row in edges.itertuples()}
        inp_links = {row[0]: (row[1], row[2]) for row in sections['CONDUITS']}
        check('inp_source_link_ids_and_endpoints', all(inp_links.get(key) == value
                                                     for key, value in source_links.items()))
        expected_extra = {(name.removesuffix('_outfall'), name) for name in outfalls}
        actual_extra = [value for key, value in inp_links.items() if key not in source_links]
        check('inp_outfall_connectors', len(actual_extra) == len(expected_extra)
              and set(actual_extra) == expected_extra)
        sub_ids = {row[0] for row in sections['SUBCATCHMENTS']}
        retained_subs = set(subs.loc[subs.id.astype(str).isin(source_nodes), 'id'].astype(str))
        check('inp_retained_subcatchments', sub_ids == {name + '-sub' for name in retained_subs})
        rain_ids = {row[0] for row in sections['RAINGAGES']}
        valid_subcatchment_outlets = native_nodes | sub_ids
        check('inp_subcatchment_references', all(row[1] in rain_ids and row[2] in valid_subcatchment_outlets
                                                for row in sections['SUBCATCHMENTS']))
        xsections = {row[0]: row for row in sections['XSECTIONS']}
        check('inp_cross_sections', set(xsections) == set(inp_links) and all(
            row[1].upper() == 'CIRCULAR' and math.isfinite(float(row[2])) and float(row[2]) > 0
            for row in xsections.values()))
        check('inp_coordinates_finite', bool(sections['COORDINATES']) and all(
            len(row) >= 3 and math.isfinite(float(row[1])) and math.isfinite(float(row[2]))
            for name in ('COORDINATES', 'VERTICES', 'POLYGONS') for row in sections[name]))

    attempt('inp_checks_completed', inp_checks)

    def output_checks():
        stats = load_json(model / 'stats.json')
        outfall_ids = {row[0].removesuffix('_outfall') for row in sections['OUTFALLS']}
        counts = {'pipe': len(edges), 'node': len(nodes), 'outfall': len(outfall_ids)}
        check('stats_counts_and_crs', stats.get('street_conduits') == counts['pipe']
              and stats.get('nodes') == counts['node'] and stats.get('outfalls') == counts['outfall']
              and stats.get('crs') == edges.crs.to_epsg(), stats=stats)
        geojson = load_json(model / 'network.geojson')
        features = geojson['features']
        actual = Counter(feature['properties']['kind'] for feature in features)
        check('geojson_type_and_counts', geojson.get('type') == 'FeatureCollection'
              and actual == counts, actual=dict(actual), expected=counts)
        expected_ids = {'pipe': set(edges.id.astype(str)), 'node': set(nodes.id.astype(str)),
                        'outfall': outfall_ids}
        for kind, ids in expected_ids.items():
            selected = [feature for feature in features if feature['properties']['kind'] == kind]
            actual_ids = [str(feature['properties'].get('id')) for feature in selected]
            check(f'geojson_{kind}_ids', len(actual_ids) == len(set(actual_ids)) and set(actual_ids) == ids)
        check('geojson_finite_geometries', all(finite_coordinates(feature.get('geometry'))
                                              for feature in features))
        report['counts']['geojson_features'] = len(features)

    attempt('output_checks_completed', output_checks)

    def rain_and_zip_checks():
        rain_paths = [row[5] for row in sections['RAINGAGES'] if len(row) >= 6 and row[4].upper() == 'FILE']
        portable = len(rain_paths) == len(sections['RAINGAGES']) and bool(rain_paths) and all(
            value == 'storm.dat' and not PureWindowsPath(value).is_absolute() for value in rain_paths)
        check('rain_relative_path', portable, paths=rain_paths)
        with zipfile.ZipFile(model / 'SWMM-model.zip') as archive:
            bad = archive.testzip()
            check('zip_crc', bad is None, corrupt_member=bad)
            members = archive.namelist()
            check('zip_expected_members', len(members) == len(set(members)) and
                  {inp.name, 'storm.dat', '说明.txt'} == set(members), members=members)
            check('zip_model_matches', archive.read(inp.name) == inp.read_bytes())
            check('zip_rain_matches', archive.read('storm.dat') == (model / 'storm.dat').read_bytes())
        return portable

    portable = attempt('rain_zip_checks_completed', rain_and_zip_checks)
    if portable:
        try:
            environment = os.environ.copy()
            environment.update(PYTHONIOENCODING='utf-8', PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1')
            # The parent removes the copy only after the native process exits,
            # so Windows DLL/file handles cannot prevent cleanup.
            # Codex's Windows sandbox TEMP can point at a virtualized directory
            # that a new process cannot enter. Keep this disposable copy in the
            # workspace cache, never in or beneath the model being inspected.
            temporary_root = Path(__file__).resolve().parents[1] / '.cache'
            temporary_root.mkdir(exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='cng-swmm-open-', dir=temporary_root) as temporary:
                work = Path(temporary).resolve()
                shutil.copyfile(inp, work / 'model.inp')
                shutil.copyfile(model / 'storm.dat', work / 'storm.dat')
                process = subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()),
                                          '--native-worker', str(work / 'model.inp')],
                                         capture_output=True, cwd=work,
                                         encoding='utf-8', errors='replace', env=environment,
                                         timeout=native_timeout,
                                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            payloads = [line.removeprefix('CNG_NATIVE_JSON=') for line in process.stdout.splitlines()
                        if line.startswith('CNG_NATIVE_JSON=')]
            if process.returncode != 0 or not payloads:
                raise RuntimeError(f'Native check exited {process.returncode}: {process.stderr[-2000:]}')
            result = json.loads(payloads[-1])
            report['native_swmm'] = result
            if result['status'] == 'unavailable':
                report['warnings'].append('Native SWMM library unavailable; model-open check skipped.')
            else:
                check('native_swmm_open', result['status'] == 'passed')
        except Exception as exc:
            report['native_swmm'] = {'status': 'failed', 'reason': f'{type(exc).__name__}: {exc}'}
            check('native_swmm_open', False, error=str(exc))
    else:
        report['native_swmm'] = {'status': 'skipped', 'reason': 'Rain/ZIP validation failed.'}
    return finish()


def main():
    if len(sys.argv) == 3 and sys.argv[1] == '--native-worker':
        print('CNG_NATIVE_JSON=' + json.dumps(native_worker(Path(sys.argv[2]).resolve()), ensure_ascii=False))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('model', type=Path, help='Completed bbox_N/model_N directory')
    parser.add_argument('--native-timeout', type=float, default=120, help='SWMM open timeout in seconds')
    args = parser.parse_args()
    try:
        result = validate(args.model.resolve(), args.native_timeout)
    except Exception as exc:
        result = {'model': str(args.model.resolve()), 'passed': False,
                  'error': f'{type(exc).__name__}: {exc}'}
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
