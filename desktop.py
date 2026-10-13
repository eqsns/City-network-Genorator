"""Desktop launcher; the frozen executable also dispatches isolated workers."""
from __future__ import annotations
import json
import os
import shutil
import sys
import threading
import traceback
from pathlib import Path
from bootstrap import ROOT, ASSETS
from desktop_platform import (APP_NAME, data_location_allowed, open_folder, paths_for,
                              preferred_data, report_startup_failure, show_message)


def worker(args):
    config, events, job_id, title = args
    # Windowed executables don't have stdout/stderr. Keep raw diagnostics per job.
    log = None
    if sys.stdout is None or sys.stderr is None:
        log = (Path(config).parent/'run.log').open('a',encoding='utf-8',buffering=1)
        sys.stdout = sys.stderr = log
    def send(event):
        event['job_id'] = job_id
        with Path(events).open('a',encoding='utf-8') as file:
            file.write(json.dumps(event,ensure_ascii=False)+'\n')
    def progress(phase,percent,message):
        send(dict(type='progress',phase=phase,percent=percent,message=message))
    try:
        from execution_control import WorkerControl
        control = WorkerControl(Path(config).parent / 'task-control.json', send)
        control.checkpoint()
        progress('prepare',0,'正在加载计算引擎')
        from swmmanywhere.progress import execution_hooks
        with execution_hooks(control.checkpoint, lambda: control.paused_seconds):
            from generator import main
            stats = main(Path(config),title,progress)
            control.checkpoint()
            send(dict(type='result',stats=stats))
        return 0
    except Exception as exc:
        traceback.print_exc()
        send(dict(type='error',message=str(exc)))
        return 1
    finally:
        if log:
            log.close()


def main():
    from app import JobStore, make_server, ACTIVE
    import webview
    paths = paths_for(ROOT)
    runtime = ASSETS/'native'/'WebView2'
    if paths.platform == 'win32' and runtime.exists():
        if os.name=='nt' and sys.getwindowsversion().build<22000:
            # Microsoft's Fixed Runtime requirement for Win10 AppContainer renderers.
            # Grant read/execute on shipped runtime binaries only, never on user data.
            import subprocess
            subprocess.run(['icacls',str(runtime),'/grant','*S-1-15-2-2:(OI)(CI)(RX)',
                            '/grant','*S-1-15-2-1:(OI)(CI)(RX)'],
                           capture_output=True,check=True,creationflags=subprocess.CREATE_NO_WINDOW)
        webview.settings['WEBVIEW2_RUNTIME_PATH'] = str(runtime)
    data = preferred_data(paths)
    # A selected writable data directory is remembered on this computer.
    preference = paths.preference
    window = webview.create_window(APP_NAME,html='<p style="font:18px sans-serif;padding:36px">正在打开工作空间…</p>',
                                  width=1440,height=900,min_size=(1024,680),background_color='#f4f7f8')
    state = {}

    class Bridge:
        def load_view(self):
            try:
                return json.loads((state['store'].data/'map-view.json').read_text(encoding='utf-8'))
            except (OSError,ValueError):
                return None

        def save_view(self,view):
            import math
            try:
                lat=float(view['center']['lat']);lng=float(view['center']['lng']);zoom=float(view['zoom'])
                if not all(math.isfinite(v) for v in [lat,lng,zoom]) or not (-85.1<=lat<=85.1 and 3<=zoom<=19):
                    return False
                clean=dict(center={'lat':lat,'lng':lng},zoom=zoom,bbox=view.get('bbox'))
                if clean['bbox'] is not None:
                    from app import validate
                    clean['bbox']=validate(dict(name='view',bbox=clean['bbox']))[1]
                with state['store'].lock:
                    target=state['store'].data/'map-view.json'
                    temp=target.with_suffix('.tmp')
                    temp.write_text(json.dumps(clean),encoding='utf-8');temp.replace(target)
                return True
            except (KeyError,TypeError,ValueError,OSError):
                return False

        def export_file(self,job_id,kind):
            store = state['store']
            from app import KINDS
            with store.lock:
                job = store.jobs.get(job_id)
                if not job or kind not in KINDS:
                    return {'error':'成果不存在'}
                source = store.file(job,kind)
            if not source.is_file():
                return {'error':'文件尚未生成'}
            selected = window.create_file_dialog(webview.FileDialog.SAVE,save_filename=source.name)
            if not selected:
                return {'cancelled':True}
            target = Path(selected if isinstance(selected,str) else selected[0])
            try:
                if target.resolve()!=source.resolve():
                    shutil.copy2(source,target)
                if kind=='inp':
                    storm = source.parent/'storm.dat'
                    if storm.resolve()!=(target.parent/'storm.dat').resolve():
                        shutil.copy2(storm,target.parent/'storm.dat')
                return {'path':str(target)}
            except OSError as exc:
                return {'error':str(exc)}

        def open_output(self):
            open_folder(state['store'].data)

    bridge=Bridge()
    window.expose(bridge.export_file,bridge.open_output,bridge.load_view,bridge.save_view)

    def open_workspace():
        nonlocal data
        while True:
            try:
                if not data_location_allowed(data, paths.platform):
                    raise OSError('请在应用程序包之外选择数据目录。')
                data.mkdir(parents=True,exist_ok=True)
                check=data/'.write-check'
                check.write_text('ok')
                check.unlink()
                break
            except OSError:
                window.create_confirmation_dialog(APP_NAME,'数据目录不可写。请选择可写文件夹保存成果。')
                selected=window.create_file_dialog(webview.FileDialog.FOLDER)
                if not selected:
                    window.destroy()
                    return
                data=Path(selected[0])/'SWMManywhere-data'
                if not data_location_allowed(data, paths.platform):
                    continue
                preference.parent.mkdir(parents=True,exist_ok=True)
                preference.write_text(str(data),encoding='utf-8')
        from filelock import FileLock, Timeout
        data_lock=FileLock(data/'.studio.lock')
        try:
            data_lock.acquire(timeout=0)
        except Timeout:
            show_message(APP_NAME,'这个工作空间已经打开，请使用现有窗口。')
            window.destroy()
            return
        state['lock']=data_lock
        store=JobStore(data)
        state['store']=store
        server=make_server(store)
        state.update(store=store,server=server)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        window.load_url(f'http://127.0.0.1:{server.server_port}/#token={store.token}')

    def launch():
        # pywebview starts this function in a thread, outside main()'s exception handler.
        try:
            open_workspace()
        except Exception:
            report_startup_failure(paths, traceback.format_exc())
            window.destroy()

    def closing():
        store=state.get('store')
        if store and any(j['status'] in ACTIVE for j in store.jobs.values()):
            if not window.create_confirmation_dialog(APP_NAME,'取消当前任务并退出？暂停的任务也会取消，关闭软件后不能接续。选择“否”保留当前任务。'):
                return False
            store.close()
        return True

    window.events.closing += closing
    icon = ASSETS / 'assets' / ('app-icon.ico' if paths.platform == 'win32' else 'app-icon.png')
    try:
        webview.start(launch,gui=paths.renderer,private_mode=False,
                      user_agent=paths.user_agent,storage_path=str(paths.webview),icon=str(icon))
    finally:
        if 'store' in state:
            state['store'].close()
        if 'server' in state:
            state['server'].shutdown()
            state['server'].server_close()
        if 'lock' in state:
            state['lock'].release()


if __name__=='__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    if '--worker' in sys.argv:
        sys.exit(worker(sys.argv[sys.argv.index('--worker')+1:]))
    if '--serve' in sys.argv:
        from app import JobStore, make_server
        store=JobStore(paths_for(ROOT).data)
        server=make_server(store)
        print(f'http://127.0.0.1:{server.server_port}/#token={store.token}',flush=True)
        try:
            server.serve_forever()
        finally:
            store.close()
    else:
        try:
            main()
        except Exception:
            report_startup_failure(paths_for(ROOT), traceback.format_exc())
