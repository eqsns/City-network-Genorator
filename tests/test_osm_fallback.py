from types import SimpleNamespace
import json
import socket
from threading import Event
from queue import Queue
import time
import bootstrap
import requests
import pytest
from swmmanywhere import prepare_data as data
from swmmanywhere import osm_network as net


def response(status=200, result=None, headers=None):
    value = requests.Response()
    value.status_code = status
    value._content = json.dumps(result if result is not None else {'elements': []}).encode()
    value._content_consumed = True
    value.headers.update(headers or {})
    return value


@pytest.fixture
def transport(monkeypatch):
    calls=[]
    monkeypatch.setattr(net.osm_cache,'retrieve',lambda key:None)
    monkeypatch.setattr(net.osm_cache,'save',lambda *args:None)
    monkeypatch.setattr(net._http,'_config_dns',lambda *args:None)
    monkeypatch.setattr(net.time,'sleep',lambda seconds:calls.append(('wait',seconds)))
    class Session:
        def __init__(self):self.headers={}
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def get(self,url,**kwargs):
            if url.endswith('/interpreter'):
                return self.query(url,**kwargs)
            calls.append(('status',url,kwargs,self.headers.copy()))
            return SimpleNamespace(status_code=200,text='Rate limit: 0\nCurrently running queries:',raise_for_status=lambda:None)
        def post(self,url,**kwargs):
            return self.query(url,**kwargs)
        def query(self,url,**kwargs):
            calls.append(('query',url,kwargs,self.headers.copy()))
            if 'overpass-api.de' in url:raise requests.exceptions.SSLError('temporary EOF')
            return response()
    monkeypatch.setattr(net.requests,'Session',Session)
    monkeypatch.setattr(net.settings,'overpass_url','https://overpass-api.de/api')
    return calls,Session


@pytest.fixture
def fake_clock(monkeypatch):
    clock=SimpleNamespace(now=100.0,waits=[])
    def sleep(seconds):
        clock.waits.append(seconds)
        clock.now+=seconds
    monkeypatch.setattr(net,'time',SimpleNamespace(monotonic=lambda:clock.now,sleep=sleep))
    return clock


def test_transport_fallback_remembers_working_service_and_reports(transport):
    calls,_=transport
    client=net.OverpassClient()
    events=[]
    old_dns=socket.getaddrinfo
    with net.download_status(events.append):
        assert client.request({'data':'example'})=={'elements':[]}
    assert client.working==net.MIRRORS[0]
    assert len([c for c in calls if c[0]=='query'])==2
    assert any('SSL 连接中断' in e for e in events)
    assert any('下载完成' in e for e in events)
    assert socket.getaddrinfo is old_dns
    status=next(c for c in calls if c[0]=='status')
    assert status[3]['User-Agent']==net.USER_AGENT
    calls.clear()
    client.request({'data':'other'})
    assert len([c for c in calls if c[0]=='query'])==1
    query=next(c for c in calls if c[0]=='query')
    assert query[2]['timeout']==(10,70)
    assert query[2]['stream'] is True
    assert not any(c[0]=='status' for c in calls)


def test_status_and_queries_identify_cng_without_library_web_referer(transport,monkeypatch):
    calls,_=transport
    # OSMnx's library defaults are not the identity or browser origin of this
    # desktop app. Neither status nor interpreter should inherit that origin.
    monkeypatch.setattr(net._http,'_get_http_headers',lambda **kwargs:{
        'User-Agent':'OSMnx Python package',
        'Referer':'https://github.com/gboeing/osmnx',
    })
    net.OverpassClient().request({'data':'[out:json];way(1);out;'})
    sent=[call[3] for call in calls if call[0] in ('status','query')]
    assert any(call[0]=='status' for call in calls)
    assert any(call[0]=='query' for call in calls)
    assert len({headers['User-Agent'] for headers in sent})==1
    for headers in sent:
        assert headers['User-Agent'].startswith('CNG/')
        assert 'github.com/ImperialCollegeLondon/SWMManywhere' in headers['User-Agent']
        assert not any(key.lower()=='referer' for key in headers)
        assert headers['Accept']=='application/json'


def test_no_data_is_not_retried_on_another_server(monkeypatch):
    calls=[]
    original=net._overpass._overpass_request
    def query(**kwargs):
        calls.append(1)
        raise ValueError('no data')
    monkeypatch.setattr(data.ox,'graph_from_bbox',query)
    with pytest.raises(ValueError):data._osm_graph_from_bbox(bbox=[1,2,3,4])
    assert len(calls)==1
    assert net._overpass._overpass_request is original


def test_unrestricted_status_never_enters_recursive_wait():
    assert net.slot_wait('Rate limit: 0\nCurrently running queries (pid, space):')==0
    assert net.slot_wait('Rate limit: 2\n2 slots available now.')==0
    assert net.slot_wait('Currently running queries:')==60


def test_all_unreachable_stops_after_two_attempts_per_service(transport,monkeypatch,fake_clock):
    calls,session=transport
    def unavailable(self,url,**kwargs):
        calls.append(('failed',url))
        raise requests.ConnectTimeout()
    monkeypatch.setattr(session,'get',unavailable)
    monkeypatch.setattr(session,'post',unavailable)
    with pytest.raises(requests.ConnectionError,match='服务均未连接成功'):
        net.OverpassClient().request({'data':'example'})
    assert len(calls)==6
    assert sum(fake_clock.waits)==60


def test_throttled_service_cools_down_without_delaying_fallback(transport,monkeypatch,fake_clock):
    calls,session=transport
    queries=[]
    def busy(self,url,**kwargs):
        queries.append((url,fake_clock.now))
        return response(429,headers={'Retry-After':'120'})
    monkeypatch.setattr(session,'query',busy)
    client=net.OverpassClient()
    with pytest.raises(requests.ConnectionError):client.request({'data':'example'})
    assert not [c for c in calls if c[0]=='wait']
    assert len(client.cooldown)==3
    assert len(queries)==6
    assert len({url for url,_ in queries})==3
    assert [when for _,when in queries]==[100]*3+[220]*3
    assert all(end == 340 for end in client.cooldown.values())


@pytest.mark.parametrize('mirror_seconds,recovery_succeeds',[(10,True),(20,True),(10,False)])
def test_advertised_slot_gets_one_recovery_after_all_mirrors_fail(transport,monkeypatch,mirror_seconds,recovery_succeeds):
    clock=SimpleNamespace(now=100.0,waits=[])
    def sleep(seconds):
        clock.waits.append(seconds)
        clock.now+=seconds
    monkeypatch.setattr(net,'time',SimpleNamespace(monotonic=lambda:clock.now,sleep=sleep))
    client=net.OverpassClient()
    main=net.settings.overpass_url
    attempts=[]
    saved=[]
    monkeypatch.setattr(net.osm_cache,'save',lambda *args:saved.append(args))
    def attempt(base,data,deadline):
        attempts.append((base,clock.now,deadline))
        if base==main:
            if len(attempts)==1:
                raise net.EndpointUnavailable('服务排队约 35 秒',cooldown=35,slot=True)
            if recovery_succeeds:
                return {'elements':[{'type':'node','id':1}]}
            raise net.EndpointUnavailable('仍在排队',cooldown=35,slot=True)
        clock.now+=mirror_seconds
        raise net.EndpointUnavailable('网关超时（HTTP 504）')
    monkeypatch.setattr(client,'_attempt',attempt)
    if recovery_succeeds:
        assert client.request({'data':'[out:json];node(1);out;'})['elements'][0]['id']==1
        assert client.working==main
        assert len(saved)==1
    else:
        with pytest.raises(requests.ConnectionError):
            client.request({'data':'[out:json];node(1);out;'})
        assert client.working is None
        assert not saved
    assert [base for base,_,_ in attempts[:4]]==[main,*net.MIRRORS,main]
    assert len(attempts)==(4 if recovery_succeeds else 6)
    # Mirrors can consume the cooldown themselves; otherwise wait precisely
    # for the remainder. No early query and no new deadline on recovery.
    assert attempts[3][1]==max(135.0,100.0+2*mirror_seconds)
    if recovery_succeeds:
        assert sum(clock.waits)==max(0.0,35.0-2*mirror_seconds)
    assert all(deadline==100.0+net.QUERY_SECONDS for _,_,deadline in attempts)
    assert all(0 < seconds <= net.HEARTBEAT_SECONDS for seconds in clock.waits)


@pytest.mark.parametrize('recovery_succeeds',[True,False])
def test_advertised_slot_survives_successful_tiles_until_mirrors_fail(transport,monkeypatch,fake_clock,recovery_succeeds):
    client=net.OverpassClient()
    main=net.settings.overpass_url
    mirror,coffee=net.MIRRORS
    attempts=[]
    saved=[]
    monkeypatch.setattr(net.osm_cache,'save',lambda *args:saved.append(args))
    def attempt(base,data,deadline):
        tile=data['data']
        attempts.append((tile,base,fake_clock.now,deadline))
        if tile=='river-tile-2' and base==main:
            raise net.EndpointUnavailable('服务排队约 35 秒',cooldown=35,slot=True)
        if tile in ('river-tile-2','river-tile-3'):
            assert base==mirror
            fake_clock.now+=15
            return {'elements':[{'type':'way','id':int(tile[-1])}]}
        if base==mirror:
            raise net.EndpointUnavailable('网关超时（HTTP 504）')
        if base==coffee:
            fake_clock.now+=70
            raise requests.ReadTimeout('response timed out')
        if recovery_succeeds:
            return {'elements':[{'type':'way','id':4}]}
        raise net.EndpointUnavailable('仍在排队',cooldown=35,slot=True)
    monkeypatch.setattr(client,'_attempt',attempt)

    for tile in (2,3):
        assert client.request({'data':f'river-tile-{tile}'})['elements'][0]['id']==tile
        assert client.working==mirror
        assert main in client.queued
        assert client.cooldown[main]==135.0
    assert fake_clock.now==130.0
    if recovery_succeeds:
        assert client.request({'data':'river-tile-4'})['elements'][0]['id']==4
        assert client.working==main
        assert main not in client.queued
        assert main not in client.cooldown
        assert len(saved)==3
    else:
        with pytest.raises(requests.ConnectionError):
            client.request({'data':'river-tile-4'})
        assert len(saved)==2

    # The main service still has five seconds of cooldown when tile 4 starts.
    # Coffee consumes that remainder, so recover the remembered slot once.
    assert [(tile,base,when) for tile,base,when,_ in attempts[:6]]==[
        ('river-tile-2',main,100.0),
        ('river-tile-2',mirror,100.0),
        ('river-tile-3',mirror,115.0),
        ('river-tile-4',mirror,130.0),
        ('river-tile-4',coffee,130.0),
        ('river-tile-4',main,200.0),
    ]
    assert all(deadline==130.0+net.QUERY_SECONDS for tile,_,_,deadline in attempts if tile=='river-tile-4')
    if recovery_succeeds:
        assert not fake_clock.waits
    else:
        for base in (main,mirror,coffee):
            assert sum(tile=='river-tile-4' and host==base for tile,host,_,_ in attempts)<=2


@pytest.mark.parametrize('failure',['429','504','network'])
def test_new_transient_failure_replaces_old_slot_and_recovers_after_new_cooldown(transport,monkeypatch,fake_clock,failure):
    client=net.OverpassClient()
    main=net.settings.overpass_url
    mirror,coffee=net.MIRRORS
    attempts=[]
    main_attempts=[]
    def attempt(base,data,deadline):
        attempts.append((data['data'],base))
        if data['data']=='first-tile':
            if base==main:
                raise net.EndpointUnavailable('服务排队约 35 秒',cooldown=35,slot=True)
            return {'elements':[]}
        if base==main:
            main_attempts.append(fake_clock.now)
            if len(main_attempts)==2:
                return {'elements':[]}
            if failure=='network':
                raise requests.ConnectTimeout('connection timed out')
            raise net.EndpointUnavailable(f'HTTP {failure}',cooldown=60)
        if base==coffee:
            # The new failure's cooldown, rather than the stale slot time,
            # controls when recovery is allowed.
            fake_clock.now+=70
        raise requests.ReadTimeout('mirror timed out')
    monkeypatch.setattr(client,'_attempt',attempt)

    assert client.request({'data':'first-tile'})=={'elements':[]}
    assert main in client.queued
    fake_clock.now=135.0
    assert client.request({'data':'next-tile'})=={'elements':[]}
    assert attempts[:5]==[
        ('first-tile',main),('first-tile',mirror),
        ('next-tile',mirror),('next-tile',main),('next-tile',coffee),
    ]
    assert attempts[5:]==[('next-tile',mirror),('next-tile',main)]
    assert main not in client.queued
    assert main not in client.cooldown
    assert main_attempts==[135.0,205.0]
    assert fake_clock.now==205.0
    assert not fake_clock.waits


@pytest.mark.parametrize('limited_by',['query','stage'])
def test_slot_recovery_cannot_extend_original_query_or_stage_budget(transport,monkeypatch,limited_by):
    clock=SimpleNamespace(now=100.0,waits=[])
    monkeypatch.setattr(net,'time',SimpleNamespace(monotonic=lambda:clock.now,sleep=clock.waits.append))
    client=net.OverpassClient()
    if limited_by=='query':
        monkeypatch.setattr(net,'QUERY_SECONDS',30)
    else:
        client.stage_deadline=130.0
    attempts=[]
    def attempt(base,data,deadline):
        attempts.append((base,deadline))
        if base==net.settings.overpass_url:
            raise net.EndpointUnavailable('服务排队约 35 秒',cooldown=35,slot=True)
        clock.now+=2
        raise requests.ConnectTimeout('unreachable')
    monkeypatch.setattr(client,'_attempt',attempt)
    with pytest.raises(requests.ConnectionError,match='排队时间超过本次查询剩余预算'):
        client.request({'data':'[out:json];node(1);out;'})
    assert [base for base,_ in attempts]==[net.settings.overpass_url,*net.MIRRORS]
    assert all(deadline==130.0 for _,deadline in attempts)
    assert not clock.waits


@pytest.mark.parametrize('status_text,known_slot',[
    ('Rate limit: 2\nSlot available after: 2030-01-01T00:00:00Z, in 35 seconds.',True),
    ('Currently running queries:',False),
])
def test_only_advertised_slot_time_makes_status_recoverable(transport,monkeypatch,status_text,known_slot):
    calls,session=transport
    monkeypatch.setattr(net.settings,'overpass_rate_limit',True)
    monkeypatch.setattr(net,'slot_wait',lambda text:35)
    def get(self,url,**kwargs):
        calls.append(('status',url))
        return SimpleNamespace(status_code=200,text=status_text,raise_for_status=lambda:None)
    monkeypatch.setattr(session,'get',get)
    with pytest.raises(net.EndpointUnavailable) as error:
        net.OverpassClient()._fetch(net.settings.overpass_url,{'data':'example'},Queue(),Event())
    assert error.value.slot is known_slot
    assert error.value.cooldown==35
    assert len(calls)==1 and calls[0][1].endswith('/status')


def test_cached_response_never_contacts_network(transport,monkeypatch):
    calls,_=transport
    monkeypatch.setattr(net.osm_cache,'retrieve',lambda key:{'elements':[]})
    assert net.OverpassClient().request({'data':'example'})=={'elements':[]}
    assert not calls


@pytest.mark.parametrize('remark',['runtime error: timed out',''])
def test_partial_query_error_is_not_saved_as_success(transport,monkeypatch,fake_clock,remark):
    calls,session=transport
    def partial(self,url,**kwargs):
        return response(result={'elements':[],'remark':remark})
    monkeypatch.setattr(session,'query',partial)
    saved=[]
    monkeypatch.setattr(net.osm_cache,'save',lambda *args:saved.append(args))
    with pytest.raises(requests.ConnectionError):net.OverpassClient().request({'data':'example'})
    assert not saved


@pytest.mark.parametrize('transfer_error',[
    requests.exceptions.ChunkedEncodingError('IncompleteRead: connection broken'),
    requests.exceptions.ContentDecodingError('gzip stream truncated'),
])
def test_interrupted_body_recovers_without_parsing_or_caching_partial_data(transport,monkeypatch,fake_clock,transfer_error):
    _,session=transport
    main=net.settings.overpass_url+'/interpreter'
    queries=[]
    saved=[]
    complete={'elements':[{'type':'node','id':1},{'type':'node','id':2}]}
    def query(self,url,**kwargs):
        queries.append((url,fake_clock.now))
        if url!=main:
            return response(503)
        if sum(endpoint==main for endpoint,_ in queries)==1:
            value=response()
            def interrupted(chunk_size):
                # Even a syntactically complete JSON prefix is not complete
                # until iter_content confirms the whole transfer succeeded.
                yield b'{"elements":[{"type":"node","id":1}]}'
                raise transfer_error
            value.iter_content=interrupted
            return value
        return response(result=complete)
    monkeypatch.setattr(session,'query',query)
    monkeypatch.setattr(net.osm_cache,'save',lambda key,result:saved.append(result) or True)
    assert net.OverpassClient().request({'data':'[out:json];node(1);out;'})==complete
    assert saved==[complete]
    assert queries==[(main,100),*( (base+'/interpreter',100) for base in net.MIRRORS ),(main,160)]
    assert sum(fake_clock.waits)==60


@pytest.mark.parametrize('remark,transient',[
    ('runtime error: Query timed out in "query" at line 1 after 26 seconds.',True),
    ('runtime error: timed out',True),
    ('runtime timeout',True),
    ('runtime error: Query ran out of memory.',False),
    ('parse error: Unknown type "timeout"',False),
    ('',False),
])
def test_fetch_rejects_all_remarks_but_only_explicit_timeout_is_recoverable(transport,monkeypatch,remark,transient):
    _,session=transport
    monkeypatch.setattr(session,'query',lambda *args,**kwargs:response(result={
        'elements':[{'type':'node','id':1}],'remark':remark}))
    with pytest.raises(requests.RequestException) as caught:
        net.OverpassClient()._fetch(net.settings.overpass_url,{'data':'example'},Queue(),Event())
    assert isinstance(caught.value,net.EndpointUnavailable) is transient
    assert net.recoverable_error(caught.value) is transient
    if transient:
        assert caught.value.cooldown==60


def test_gateway_timeout_immediately_tries_mirrors_then_bounded_recovery(transport,monkeypatch,fake_clock):
    calls,session=transport
    posts=[]
    def busy(self,url,**kwargs):
        posts.append((url,fake_clock.now))
        return response(504)
    monkeypatch.setattr(session,'query',busy)
    with pytest.raises(requests.ConnectionError):net.OverpassClient().request({'data':'example'})
    assert len(posts)==6
    assert len({url for url,_ in posts})==3
    assert [when for _,when in posts]==[100]*3+[160]*3
    assert not [c for c in calls if c[0]=='wait']


def test_busy_status_is_respected_without_waiting_or_querying_that_host(transport,monkeypatch):
    calls,session=transport
    original_get=session.get
    def get(self,url,**kwargs):
        if url.endswith('/status'):
            return SimpleNamespace(status_code=200,text='Currently running queries:',raise_for_status=lambda:None)
        return original_get(self,url,**kwargs)
    monkeypatch.setattr(session,'get',get)
    assert net.OverpassClient().request({'data':'example'})=={'elements':[]}
    queries=[c for c in calls if c[0]=='query']
    assert len(queries)==1
    assert 'maps.mail.ru' in queries[0][1]
    assert not [c for c in calls if c[0]=='wait']


def test_server_query_budget_is_capped_without_changing_logical_cache_key(transport,monkeypatch):
    calls,_=transport
    saved=[]
    monkeypatch.setattr(net.osm_cache,'save',lambda *args:saved.append(args))
    data={'data':'[out:json][timeout:180][maxsize:536870912];node(1);out;'}
    net.OverpassClient().request(data)
    assert data['data'].startswith('[out:json][timeout:180]')
    assert all('[timeout:25][maxsize:67108864]' in c[2]['params']['data'] for c in calls if c[0]=='query')
    assert 'timeout%3A180' in saved[0][0]
    assert 'maxsize%3A536870912' in saved[0][0]


@pytest.mark.parametrize('opening,expected',[
    ('[out:json]', '[out:json][timeout:25][maxsize:67108864]'),
    ('[out:json][timeout:180][maxsize:536870912]', '[out:json][timeout:25][maxsize:67108864]'),
    ('[out:json][timeout:7][maxsize:1048576]', '[out:json][timeout:7][maxsize:1048576]'),
    ('[out:json][timeout : 12 ][maxsize : 33554432 ]', '[out:json][timeout:12][maxsize:33554432]'),
])
def test_query_budget_changes_only_opening_settings(opening,expected):
    tail=';way["name"="[timeout:180][maxsize:536870912]"](1,2,3,4);/* [timeout:999] */out;'
    assert net.bounded_query(opening+tail)==expected+tail


def test_query_without_opening_settings_is_preserved():
    query='way["name"="[timeout:180]"](1,2,3,4);out;'
    assert net.bounded_query(query)==query


@pytest.mark.parametrize('cancelled',[False,True])
def test_short_slot_wait_is_exact_and_cancellation_prevents_query(transport,monkeypatch,cancelled):
    calls,session=transport
    # Exercise _fetch itself so no actual sleep, worker, or socket is involved.
    monkeypatch.setattr(net.settings,'overpass_rate_limit',True)
    monkeypatch.setattr(net,'slot_wait',lambda text:19)
    def successful_query(self,url,**kwargs):
        calls.append(('query',url,kwargs,self.headers.copy()))
        return response()
    monkeypatch.setattr(session,'query',successful_query)
    waits=[]
    class Abandoned:
        def wait(self,seconds):
            waits.append(seconds)
            return cancelled
        def is_set(self):
            return cancelled
    updates=Queue()
    client=net.OverpassClient()
    if cancelled:
        with pytest.raises(requests.Timeout,match='已停止等待此服务'):
            client._fetch('https://overpass-api.de/api',{'data':'[out:json];node(1);out;'},updates,Abandoned())
    else:
        assert client._fetch('https://overpass-api.de/api',{'data':'[out:json];node(1);out;'},updates,Abandoned())=={'elements':[]}
    assert waits==[19]
    assert len([c for c in calls if c[0]=='status'])==1
    assert len([c for c in calls if c[0]=='query'])==(0 if cancelled else 1)
    assert '等待 19 秒' in updates.get_nowait()[1]
    assert not [c for c in calls if c[0]=='wait']


def test_wall_clock_deadline_bounds_dns_or_slow_response_and_rejects_late_result(transport,monkeypatch):
    calls,_=transport
    release=Event()
    done=Event()
    saved=[]
    client=net.OverpassClient()
    monkeypatch.setattr(net,'ENDPOINT_SECONDS',0.04)
    monkeypatch.setattr(net,'QUERY_SECONDS',0.2)
    monkeypatch.setattr(net,'HEARTBEAT_SECONDS',0.01)
    monkeypatch.setattr(net.osm_cache,'save',lambda *args:saved.append(args))
    def fetch(base,data,updates,abandoned):
        release.wait(2)
        done.set()
        return {'elements':[{'id':1}]}
    monkeypatch.setattr(client,'_fetch',fetch)
    events=[]
    start=time.monotonic()
    try:
        with net.download_status(events.append):
            with pytest.raises(requests.ConnectionError):
                client.request({'data':'example'})
        assert time.monotonic()-start < 0.8
        assert any('已用' in event for event in events)
        assert not saved
        assert client.working is None
    finally:
        release.set()
        done.wait(1)
    assert not saved


def test_overall_query_budget_stops_before_remaining_endpoints(transport,monkeypatch):
    client=net.OverpassClient()
    calls=[]
    monkeypatch.setattr(net,'ENDPOINT_SECONDS',1)
    monkeypatch.setattr(net,'QUERY_SECONDS',0.04)
    def fetch(base,data,updates,abandoned):
        calls.append(base)
        abandoned.wait(1)
        return {'elements':[]}
    monkeypatch.setattr(client,'_fetch',fetch)
    with pytest.raises(requests.ConnectionError,match='等待上限'):
        client.request({'data':'example'})
    assert len(calls)==1


def test_stage_budget_and_settings_are_restored_after_failure(transport,monkeypatch):
    previous=net._overpass._overpass_request
    previous_downloader=net._overpass._download_overpass_network
    monkeypatch.setattr(net,'STAGE_SECONDS',-1)
    with pytest.raises(requests.ConnectionError,match='等待上限'):
        with net.bounded_requests():
            net._overpass._overpass_request({'data':'uncached'})
    assert net._overpass._overpass_request is previous
    assert net._overpass._download_overpass_network is previous_downloader
    assert net.client.stage_deadline is None
    assert not transport[0]


@pytest.mark.parametrize('remark',['timed out',''])
def test_cached_partial_result_is_not_used(transport,monkeypatch,remark):
    monkeypatch.setattr(net.osm_cache,'retrieve',lambda key:{'elements':[],'remark':remark})
    assert net.OverpassClient().request({'data':'example'})=={'elements':[]}
    assert any(call[0]=='query' for call in transport[0])


def test_retry_after_http_date():
    from datetime import datetime,timedelta,timezone
    from email.utils import format_datetime
    future=format_datetime(datetime.now(timezone.utc)+timedelta(seconds=120))
    assert 119 <= net.retry_after(future) <= 120
    assert net.retry_after('invalid')==60


@pytest.mark.parametrize('query,parameter',[
    ('[out:json];way["highway"](36.58,109.47,36.65,109.53);out;', 'params'),
    ('[out:json];way(poly:"'+'36.6 109.5 '*1000+'");out;', 'data'),
])
def test_short_queries_use_get_and_long_queries_use_post(transport,query,parameter):
    calls,_=transport
    net.OverpassClient().request({'data':query})
    queries=[call for call in calls if call[0]=='query']
    assert queries
    assert all(parameter in call[2] for call in queries)
    assert all(call[2][parameter]['data']==net.bounded_query(query) for call in queries)


def test_compact_downloader_hook_is_scoped_and_restored(transport):
    from swmmanywhere.osm_queries import download_network
    previous_request=net._overpass._overpass_request
    previous_downloader=net._overpass._download_overpass_network
    with net.bounded_requests():
        assert net._overpass._download_overpass_network is download_network
        assert net._overpass._overpass_request==net.client.request
    assert net._overpass._download_overpass_network is previous_downloader
    assert net._overpass._overpass_request is previous_request
