import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .settings import ROOT, RUNTIME
from .progress import parse
from .overall import DatasetProgress
from .artifacts import publish

def now():
    return datetime.now(timezone.utc).isoformat()

class JobManager:
    def __init__(self):
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='geofetch')
        self.jobs = {}
        self.processes = {}
        for path in RUNTIME.glob('*/job.json'):
            try:
                job = json.loads(path.read_text(encoding='utf-8'))
                if job['status'] in {'queued', 'running', 'cancelling'}:
                    job.update(status='interrupted', finished_at=now())
                job.setdefault('progress', dict(stage='已完成' if job['status']=='completed' else '未记录进度', percent=100. if job['status']=='completed' else None))
                job['progress'].setdefault('overall_percent', 100. if job['status']=='completed' else 0.)
                job['progress'].setdefault('overall_estimated', job['status']!='completed')
                self.jobs[job['id']] = job
                self.save(job)
            except (ValueError, KeyError, OSError):
                continue

    def save(self, job):
        folder = RUNTIME / job['id']
        folder.mkdir(parents=True, exist_ok=True)
        temp = folder / 'job.tmp'
        temp.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(folder / 'job.json')

    def create(self, request):
        with self.lock:
            key = uuid.uuid4().hex[:12]
            output = str(Path(request['output']))
            Path(output).mkdir(parents=True, exist_ok=True)
            request['output'] = str(RUNTIME / key / 'products')
            Path(request['output']).mkdir(parents=True)
            job = dict(id=key, status='queued', created_at=now(), finished_at=None,
                       datasets=request['datasets'], output=output, files=[], completed=[], failed=[], current=None,
                       progress=dict(stage='等待开始', percent=None, overall_percent=0., overall_estimated=True))
            self.jobs[key] = job
            self.save(job)
            (RUNTIME / key / 'request.json').write_text(json.dumps(request, ensure_ascii=False), encoding='utf-8')
            self.executor.submit(self.run, key)
            return dict(job)

    def run(self, key):
        job = self.jobs[key]
        try:
            with self.lock:
                if job['status'] == 'cancelled':
                    return
                job['status'] = 'running'
                self.save(job)
            with (RUNTIME / key / 'log.txt').open('a', encoding='utf-8', buffering=1) as log:
                for dataset in job['datasets']:
                    with self.lock:
                        if job['status'] == 'cancelling':
                            break
                        job['current'] = dataset
                        estimator = DatasetProgress(dataset)
                        job['progress'] = estimator.update(dict(stage='准备数据', percent=None))
                        self.save(job)
                        log.write(f'\n--- {dataset} 开始 ---\n')
                        env = {**os.environ, 'PYTHONPATH': str(ROOT / 'backend'), 'PYTHONUTF8': '1', 'PYTHONUNBUFFERED': '1'}
                        proc = subprocess.Popen([sys.executable, '-u', '-m', 'geofetch.worker', str(RUNTIME / key / 'request.json'), dataset],
                                                cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                                text=True, encoding='utf-8', errors='replace', bufsize=1,
                                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                        self.processes[key] = proc
                    for line in proc.stdout:
                        update = parse(line)
                        if update is not None:
                            with self.lock:
                                job['progress'] = estimator.update(update)
                                self.save(job)
                        else:
                            log.write(line)
                    proc.stdout.close()
                    code = proc.wait()
                    if code == 0 and job['status'] != 'cancelling':
                        with self.lock:
                            job['progress'] = dict(stage='保存成果', percent=None, dataset=dataset, overall_percent=99., overall_estimated=True)
                            self.save(job)
                        products = publish(RUNTIME / key / 'products' / dataset, job['output'], dataset)
                        with self.lock:
                            job['files'].extend(products)
                            self.save(job)
                        staging = (RUNTIME / key / 'products' / dataset).resolve()
                        if staging.exists() and RUNTIME.resolve() in staging.parents:
                            shutil.rmtree(staging)
                        log.write(f'成果已保存到：{job["output"]}\n')
                    with self.lock:
                        self.processes.pop(key, None)
                        if job['status'] == 'cancelling':
                            break
                        job['completed' if code == 0 else 'failed'].append(dataset)
                        if code == 0:
                            job['progress'] = estimator.finish()
                        log.write(f'--- {dataset} 结束，退出码 {code} ---\n')
                        self.save(job)
            with self.lock:
                job['status'] = 'cancelled' if job['status'] == 'cancelling' else ('failed' if job['failed'] else 'completed')
                job.update(current=None, finished_at=now())
                if job['status'] == 'completed':
                    job['progress'] = dict(stage='已完成', percent=100., overall_percent=100., overall_estimated=False)
                self.save(job)
        except Exception as exc:
            with self.lock:
                job.update(status='failed', current=None, finished_at=now(), error=str(exc))
                self.save(job)

    def cancel(self, key):
        with self.lock:
            job = self.jobs[key]
            if job['status'] in {'queued', 'running'}:
                job['status'] = 'cancelled' if job['status'] == 'queued' else 'cancelling'
                if job['status'] == 'cancelled':
                    job.update(finished_at=now(), current=None)
                if key in self.processes:
                    self.processes[key].terminate()
                self.save(job)
            return dict(job)

manager = JobManager()
