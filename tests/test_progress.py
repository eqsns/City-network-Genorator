from pathlib import Path
from types import SimpleNamespace
import pytest
import bootstrap
from swmmanywhere import preprocessing


def addresses(tmp_path):
    return SimpleNamespace(bbox_paths=SimpleNamespace(**{k:tmp_path/(k+'.dat') for k in ['elevation','building','street','river']}))


def test_cached_download_stages_and_optional_callback(tmp_path,monkeypatch):
    paths=addresses(tmp_path)
    for p in vars(paths.bbox_paths).values():p.touch()
    for name in ['elevation','building','street','river']:
        monkeypatch.setattr(preprocessing,'prepare_'+name,lambda *a,**k:None)
    events=[]
    preprocessing.run_downloads([118.77,32.02,118.78,32.03],paths,progress_callback=lambda *args:events.append(args))
    assert [e[1] for e in events]==[5,15,25,35,45]
    assert all('缓存' in e[2] for e in events[:4])
    preprocessing.run_downloads([118.77,32.02,118.78,32.03],paths)


def test_network_error_identifies_source(tmp_path,monkeypatch):
    paths=addresses(tmp_path)
    for name in ['elevation','building']:
        monkeypatch.setattr(preprocessing,'prepare_'+name,lambda *a,**k:None)
    def unavailable(*args,**kwargs):raise TimeoutError('connection timeout')
    monkeypatch.setattr(preprocessing,'prepare_street',unavailable)
    with pytest.raises(RuntimeError,match='OSM 道路信息处理失败'):
        preprocessing.run_downloads([118.77,32.02,118.78,32.03],paths)


def test_no_inp_is_failure(tmp_path,monkeypatch):
    import generator
    monkeypatch.setattr(generator.engine,'load_config',lambda *a:{})
    monkeypatch.setattr(generator.engine,'swmmanywhere',lambda *a,**k:(tmp_path/'graph.parquet',None))
    with pytest.raises(RuntimeError,match='未生成 SWMM 输入文件'):
        generator.main(tmp_path/'config.yml','empty')
