import json
import threading
import time
import urllib.error
import urllib.request
import pytest
import requests
from app import JobStore, make_server
from map_service import MapService, PNG, USER_AGENT, cache_metadata


def response(status=200, content=PNG+b'tile', **headers):
    result=requests.Response()
    result.status_code=status;result._content=content
    result.headers.update(headers)
    return result


def test_persistent_cache_and_identified_requests(tmp_path,monkeypatch):
    calls=[]
    def get(url,**kwargs):
        calls.append((url,kwargs))
        return response(**{'Cache-Control':'max-age=3600','ETag':'one'})
    monkeypatch.setattr(requests,'get',get)
    data,ttl=MapService(tmp_path).tile(3,4,2,'http://127.0.0.1:1234/')
    assert data.startswith(PNG) and 3500<ttl<=3600
    assert MapService(tmp_path).tile(3,4,2)[0]==data
    assert len(calls)==1
    assert calls[0][0]=='https://tile.openstreetmap.org/3/4/2.png'
    assert calls[0][1]['headers']['User-Agent']==USER_AGENT
    assert calls[0][1]['headers']['Referer']=='http://127.0.0.1:1234/'


def test_expired_tiles_revalidate_with_etag(tmp_path,monkeypatch):
    service=MapService(tmp_path)
    monkeypatch.setattr(requests,'get',lambda *a,**k:response(**{'Cache-Control':'max-age=0','ETag':'abc'}))
    original=service.tile(3,4,2)[0]
    calls=[]
    def revalidate(url,**kwargs):
        calls.append(kwargs)
        return response(304,b'',**{'Cache-Control':'max-age=600'})
    monkeypatch.setattr(requests,'get',revalidate)
    assert service.tile(3,4,2)[0]==original
    assert calls[0]['headers']['If-None-Match']=='abc'


def test_cache_defaults_and_no_store(tmp_path,monkeypatch):
    assert cache_metadata({},100)['expires']==100+7*86400
    assert cache_metadata({'Cache-Control':'max-age=200','Age':'50'},100)['expires']==250
    assert cache_metadata({'Cache-Control':'no-cache'},100)['expires']==100
    monkeypatch.setattr(requests,'get',lambda *a,**k:response(**{'Cache-Control':'no-store'}))
    assert MapService(tmp_path).tile(3,4,2)[0].startswith(PNG)
    assert not list((tmp_path/'map-cache').rglob('*.png'))


def test_failure_records_actionable_reason_without_caching(tmp_path,monkeypatch):
    def unavailable(*a,**k):raise requests.ConnectTimeout('private debug information')
    monkeypatch.setattr(requests,'get',unavailable)
    service=MapService(tmp_path)
    with pytest.raises(RuntimeError,match='超时'):service.tile(3,4,2)
    assert service.status()['failed']==1
    assert '超时' in (tmp_path/'network.log').read_text(encoding='utf-8')
    assert not list((tmp_path/'map-cache').rglob('*.png'))


def test_invalid_image_is_not_cached(tmp_path,monkeypatch):
    monkeypatch.setattr(requests,'get',lambda *a,**k:response(content=b'<html>Blocked</html>'))
    with pytest.raises(RuntimeError,match='PNG'):MapService(tmp_path).tile(3,4,2)
    assert not list((tmp_path/'map-cache').rglob('*.png'))


@pytest.mark.parametrize('coordinates',[(-1,0,0),(20,0,0),(3,-1,2),(3,8,2),(3,4,8)])
def test_coordinate_bounds(coordinates):
    with pytest.raises(ValueError):MapService.validate(*coordinates)


def test_local_tile_endpoint_requires_token(tmp_path,monkeypatch):
    calls=[]
    def get(*a,**k):calls.append(1);return response()
    monkeypatch.setattr(requests,'get',get)
    store=JobStore(tmp_path)
    server=make_server(store)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    url=f'http://127.0.0.1:{server.server_port}/api/tiles/3/4/2.png'
    try:
        with pytest.raises(urllib.error.HTTPError) as error:urllib.request.urlopen(url)
        assert error.value.code==403 and not calls
        result=urllib.request.urlopen(url+'?token='+store.token)
        assert result.headers['Content-Type']=='image/png'
        assert 'private' in result.headers['Cache-Control']
        assert result.read().startswith(PNG)
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(url.replace('/3/4/2.png','/3/99/2.png')+'?token='+store.token)
        assert error.value.code==400 and len(calls)==1
    finally:server.shutdown();server.server_close()
