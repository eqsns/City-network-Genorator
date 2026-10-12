"""Portable result files and WGS84 map layers."""
import json
import re
import shutil
import zipfile
from pathlib import Path


def portable_rain_path(text):
    return re.sub(r'(\bFILE\s+)(?:"[^"\r\n]*storm\.dat"|[^\r\n]*?storm\.dat)(?=\s|$)',
                  r'\1"storm.dat"', text, flags=re.I)


def prepare_artifacts(directory, title, draw_png=True):
    import geopandas as gpd
    from plot_nanjing import render_network
    directory = Path(directory)
    inp = next(directory.glob('model_*.inp'))
    text = inp.read_text(encoding='utf-8')
    # SWMM rain files can contain quoted absolute paths from the original engine.
    text = portable_rain_path(text)
    inp.write_text(text, encoding='utf-8')
    if not (directory/'storm.dat').exists():
        import swmmanywhere
        shutil.copy2(Path(swmmanywhere.__file__).parent/'defs'/'storm.dat', directory/'storm.dat')
    edges = gpd.read_parquet(directory/'edges.geoparquet')
    nodes = gpd.read_parquet(directory/'nodes.geoparquet')
    if edges.empty:
        raise RuntimeError('范围内未形成有效管段，请调整范围')
    section = text.split('[OUTFALLS]',1)[1].split('[',1)[0]
    ids = {line.split()[0].removesuffix('_outfall') for line in section.splitlines() if line.strip() and not line.lstrip().startswith(';')}
    outfalls = nodes[nodes['id'].astype(str).isin(ids)]
    features = []
    for frame, kind in [(edges,'pipe'),(nodes,'node'),(outfalls,'outfall')]:
        keep = [c for c in ['id','diameter','length','chamber_floor','surface_elevation','geometry'] if c in frame.columns]
        data = json.loads(frame[keep].to_crs(4326).to_json(drop_id=True, na='null'))
        for f in data['features']:
            f['properties']['kind'] = kind
        features.extend(data['features'])
    (directory/'network.geojson').write_text(json.dumps({'type':'FeatureCollection','features':features},ensure_ascii=False,separators=(',',':'),allow_nan=False),encoding='utf-8')
    if draw_png:
        render_network(directory,title)
    stats = dict(street_conduits=len(edges),nodes=len(nodes),outfalls=len(outfalls),crs=edges.crs.to_epsg())
    (directory/'stats.json').write_text(json.dumps(stats),encoding='utf-8')
    with zipfile.ZipFile(directory/'SWMM-model.zip','w',zipfile.ZIP_DEFLATED) as archive:
        for path in [inp,directory/'storm.dat']:
            archive.write(path,path.name)
        archive.writestr('说明.txt','将文件解压到同一目录，再使用 SWMM 打开 .inp。该模型为公开数据推导的合成管网。')
    return stats
