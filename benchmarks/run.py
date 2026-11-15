"""Sequential end-to-end pilot. Raw JSON and child logs go only to /results."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import resource
import statistics
import subprocess
import sys
import time
import uuid

import aiohttp
import asyncpg

from sparse import SparseClient
from .transport import Meter

STARTS = (101, 2001, 7001)
BOUNDS = [{'lower': [100000-i*10, i], 'upper': [100000-(i+100)*10, i+100]}
          for i in STARTS]
URL = 'http://127.0.0.1:8080'
OUT = Path('/results')


def percentile(values, fraction):
    return sorted(values)[min(len(values)-1, int(len(values)*fraction))]


async def oracle(conn):
    # Independent SQL (does not call any HTTP/server row selection helper).
    result = []
    async with conn.transaction(isolation='repeatable_read', readonly=True):
        for bounds in BOUNDS:
            lo, hi = bounds['lower'], bounds['upper']
            rows = await conn.fetch(
                """SELECT id,name,score FROM items WHERE active=true
                AND (score<$1 OR (score=$1 AND id>=$2))
                AND (score>$3 OR (score=$3 AND id<$4))
                ORDER BY score DESC,id ASC""", *lo, *hi)
            result.append([{'key': [r['score'],r['id']], 'row':dict(r)} for r in rows])
    return result


class PollClient:
    def __init__(self, meter, keyset):
        self.meter, self.keyset = meter, keyset
        self.states = [None]*3

    async def load(self, index):
        entries, token = [], None
        try:
            while True:
                body = ({'cursor': token} if token else
                        {'actualRange': BOUNDS[index], 'pageSize':25})
                async with self.meter.post(URL+('/page' if self.keyset else '/range'), json=body) as response:
                    response.raise_for_status()
                    data = await response.json()
                entries.extend(data['rows'])
                token = data.get('cursor')
                if not token:
                    break
            # Publish only a complete consistent range; never partial pages.
            self.states[index] = entries
            self.meter.add('range_loads')
        finally:
            if token:
                async with self.meter.request('DELETE', URL+'/scan/'+token) as response:
                    response.raise_for_status()

    async def refresh(self):
        await asyncio.gather(*(self.load(i) for i in range(3)))

    async def follow(self):
        while True:
            start = time.monotonic()
            await self.refresh()
            await asyncio.sleep(max(0, .25-(time.monotonic()-start)))

    def current(self):
        return self.states


class StreamClient(SparseClient):
    async def bootstrap_fixed(self):
        first = await self.post('/fetch/first', {'query':self.query, 'n':1})
        self.context_id = first['contextId']
        for i, bounds in enumerate(BOUNDS):
            snapshot = await self.post('/fetch/range', {'contextId':self.context_id, 'actualRange':bounds})
            self.add(str(i), snapshot)
            self.session.add('range_loads')

    async def refetch(self, name):
        await super().refetch(name)
        self.session.add('range_loads')

    def current(self):
        return [self.fragments[str(i)].ordered_rows() for i in range(3)]


def operations(rate):
    rng = random.Random(42)
    plan = []
    index = 0
    for phase in range(3):
        transactions = []
        for _ in range(rate*2):
            r = index % 3
            base = STARTS[r]
            dest = STARTS[(r+1) % 3] if (index//3) % 2 == 0 else base
            inserted = 100001+index
            deleted = 100001+index-3 if index >= 3 else base+20
            transactions.append([
                ["UPDATE items SET name=$1 WHERE id=$2", f'value-{index:06}', base+5],
                ["UPDATE items SET score=$1 WHERE id=$2", 100000-dest*10-65, base+6],
                ["UPDATE items SET active=NOT active WHERE id=$1", base+7],
                ["INSERT INTO items VALUES ($1,$2,$3,TRUE)", inserted, f'insert-{index:06}', 100000-base*10-rng.randrange(100,900)],
                ["DELETE FROM items WHERE id=$1", deleted],
            ])
            index += 1
        control = [["UPDATE items SET name=$1 WHERE id=$2", f'control-{phase}-{r}', base+5]
                   for r,base in enumerate(STARTS)]
        plan.append({'transactions':transactions, 'control':control})
    return plan


async def prepare(admin, mode):
    database = 'istream_pilot_'+uuid.uuid4().hex[:12]
    await admin.execute(f'CREATE DATABASE {database}')
    dsn = os.environ['ADMIN_DSN'].rsplit('/',1)[0]+'/'+database
    conn = await asyncpg.connect(dsn)
    files = sorted(Path('/app/migrations').glob('*.sql'))
    for migration in files if mode == 'istream' else files[:1]:
        await conn.execute(migration.read_text())
    rng = random.Random(42)
    async with conn.transaction():
        await conn.copy_records_to_table('items', records=[
            (i, f'item-{rng.getrandbits(48):012x}',100000-i*10,True) for i in range(1,10001)])
    await conn.execute('VACUUM ANALYZE items')
    await conn.execute('SELECT pg_stat_force_next_flush()')
    await asyncio.sleep(1.1)
    # Assert that the baselines have neither clock nor capture triggers.
    if mode != 'istream':
        assert await conn.fetchval("SELECT to_regclass('stream_clock') IS NULL")
        assert await conn.fetchval("SELECT count(*) FROM pg_trigger WHERE tgrelid='items'::regclass AND NOT tgisinternal") == 0
    return dsn, conn


async def start_server(mode, dsn, log):
    file = log.open('w')
    process = subprocess.Popen([sys.executable,'-m', 'istream.app' if mode=='istream' else 'benchmarks.baseline'],
                               env={**os.environ,'DATABASE_URL':dsn}, stdout=file, stderr=subprocess.STDOUT)
    try:
        async with asyncio.timeout(15):
            while True:
                if process.poll() is not None:
                    raise RuntimeError('Server failed: '+str(log))
                try:
                    reader, writer = await asyncio.open_connection('127.0.0.1',8080)
                    writer.close()
                    await writer.wait_closed()
                    break
                except OSError:
                    await asyncio.sleep(.05)
    except BaseException:
        process.terminate()
        await asyncio.to_thread(process.wait)
        file.close()
        raise
    return process, file


async def stop_task(task):
    if task:
        task.cancel()
        result = (await asyncio.gather(task, return_exceptions=True))[0]
        if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
            raise result


def server_resources(pid):
    fields = Path(f'/proc/{pid}/stat').read_text().split()
    status = Path(f'/proc/{pid}/status').read_text().splitlines()
    return {'cpu_s':(int(fields[13])+int(fields[14]))/os.sysconf('SC_CLK_TCK'),
            'peak_rss_kib':int(next(line for line in status if line.startswith('VmHWM:')).split()[1])}


async def run_one(admin, mode, rate, repeat):
    plan = operations(rate)
    label = f'{rate}-{repeat}-{mode}'
    result = dict(mode=mode, rate=rate, repeat=repeat, bounds=BOUNDS,
                  plan_sha256=hashlib.sha256(json.dumps(plan,sort_keys=True).encode()).hexdigest(),
                  revision=os.environ.get('CODE_REV'), error=None)
    dsn, writer = await prepare(admin,mode)
    observer = await asyncpg.connect(dsn)
    process, log = await start_server(mode,dsn,OUT/f'server-{label}.log')
    task = None
    samples = []
    active = []
    checkpoints = []
    async def transaction(statements, category):
        begin = time.monotonic()
        async with writer.transaction():
            for sql,*args in statements:
                await writer.execute(sql,*args)
            before_commit = time.monotonic()
        ack = time.monotonic()
        samples.append(dict(category=category, begin=begin, before_commit=before_commit,
                            commit_ack=ack, latency_ms=(ack-begin)*1000))
    try:
        pg_before = dict(await observer.fetchrow(
            "SELECT xact_commit,xact_rollback,blks_read,blks_hit,tup_returned,tup_fetched,"
            "tup_inserted,tup_updated,tup_deleted,temp_bytes FROM pg_stat_database WHERE datname=current_database()"))
        wal_start = await observer.fetchval('SELECT pg_current_wal_insert_lsn()::text')
        cpu_start = resource.getrusage(resource.RUSAGE_SELF)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=3)) as session:
            meter = Meter(session)
            client = StreamClient(meter,URL) if mode=='istream' else PollClient(meter,mode=='keyset')
            initial_start = time.monotonic()
            if mode=='istream':
                await client.bootstrap_fixed()
            else:
                await client.refresh()
            result['initial_ms'] = (time.monotonic()-initial_start)*1000
            assert client.current() == await oracle(observer)
            async def follow():
                if mode=='istream':
                    await client.follow(120)
                else:
                    await client.follow()
            meter.phase = 'warmup'
            task = asyncio.create_task(follow())
            await asyncio.sleep(1)
            meter.phase = 'updates'
            measured_start = time.monotonic()
            for phase, block in enumerate(plan):
                offline = phase == 1
                if offline:
                    await stop_task(task)
                    task = None
                    offline_start = time.monotonic()
                phase_start = time.monotonic()
                for i, statements in enumerate(block['transactions']):
                    await asyncio.sleep(max(0, phase_start+i/rate-time.monotonic()))
                    await transaction(statements,'load')
                # Keep each active phase at two seconds, even at the low rate.
                await asyncio.sleep(max(0, phase_start+2-time.monotonic()))
                active.append({'transactions':len(block['transactions']), 'seconds':time.monotonic()-phase_start})
                await transaction(block['control'],'control')
                target_version = (await observer.fetchval('SELECT version FROM stream_clock WHERE id=1')) if mode=='istream' else None
                expected = await oracle(observer)
                ack = samples[-1]['commit_ack']
                if offline:
                    resumed = time.monotonic()
                    result['offline_seconds'] = resumed-offline_start
                    task = asyncio.create_task(follow())
                else:
                    resumed = ack
                async with asyncio.timeout(5):
                    while True:
                        if task.done():
                            await task
                            raise RuntimeError('Follower stopped early')
                        normalized = mode!='istream' or all(f.applied_version==target_version for f in client.fragments.values())
                        if normalized and client.current()==expected:
                            break
                        await asyncio.sleep(.001)
                applied = time.monotonic()
                checkpoints.append(dict(phase=phase, recovery=offline,
                                        catchup_ms=(applied-ack)*1000,
                                        recovery_ms=(applied-resumed)*1000 if offline else None,
                                        commit_lower_ms=(applied-samples[-1]['before_commit'])*1000))
                # Writes remain paused until every fragment equals one SQL snapshot.
                assert client.current()==await oracle(observer)
            result['measured_seconds'] = time.monotonic()-measured_start
            await stop_task(task)
            task = None
            assert client.current()==await oracle(observer)
            result['correct'] = True
            result['final_rows'] = [len(r) for r in client.current()]
            result['traffic'] = {phase:dict(counts) for phase,counts in meter.counts.items()}
            result['checkpoints'] = checkpoints
            result['write_samples'] = samples
            result['active_phases'] = active
            result['write_tps'] = sum(p['transactions'] for p in active)/sum(p['seconds'] for p in active)
            result['write_mean_ms'] = statistics.mean(s['latency_ms'] for s in samples)
            result['write_p95_ms'] = percentile([s['latency_ms'] for s in samples],.95)
            result['wal_bytes'] = await observer.fetchval('SELECT pg_wal_lsn_diff(pg_current_wal_insert_lsn(),$1::text::pg_lsn)::bigint',wal_start)
            usage = resource.getrusage(resource.RUSAGE_SELF)
            result['harness_cpu_s'] = usage.ru_utime+usage.ru_stime-cpu_start.ru_utime-cpu_start.ru_stime
            result['server'] = server_resources(process.pid)
    except (Exception, asyncio.CancelledError) as error:
        result['error'] = repr(error)
        result['correct'] = False
    finally:
        try:
            await stop_task(task)
        except Exception as error:
            result['error'] = result['error'] or repr(error)
            result['correct'] = False
        process.terminate()
        try:
            await asyncio.wait_for(asyncio.to_thread(process.wait),10)
        except TimeoutError:
            process.kill()
            await asyncio.to_thread(process.wait)
        log.close()
        await writer.close()
        # Allow backend statistics to flush after connections close.
        await asyncio.sleep(1.1)
        await observer.execute('SELECT pg_stat_clear_snapshot()')
        result['pg'] = dict(await observer.fetchrow(
            "SELECT xact_commit,xact_rollback,blks_read,blks_hit,tup_returned,tup_fetched,"
            "tup_inserted,tup_updated,tup_deleted,temp_bytes FROM pg_stat_database WHERE datname=current_database()"))
        result['pg'] = {k:v-pg_before[k] for k,v in result['pg'].items()}
        await observer.close()
    (OUT/f'run-{label}.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k in ('mode','rate','repeat','correct','error','write_tps')}),flush=True)
    return result


async def main():
    OUT.mkdir(exist_ok=True)
    admin = await asyncpg.connect(os.environ['ADMIN_DSN'])
    assert await admin.fetchval('SELECT current_database()')=='benchmark'
    environment = {'python':sys.version, 'platform':platform.platform(),
                   'postgres':await admin.fetchval('SELECT version()'),
                   'freeze':subprocess.check_output([sys.executable,'-m','pip','freeze'],text=True),
                   'revision':os.environ.get('CODE_REV'), 'rows':10000, 'seed':42,
                   'interval_s':.25, 'page_size':25, 'warmup_s':1,
                   'write_phases_s':[2,2,2], 'offline_phase':1,
                   'repeats':1 if '--smoke' in sys.argv else 3}
    (OUT/'environment.json').write_text(json.dumps(environment,indent=2)+'\n')
    results=[]
    try:
        # Rotate order to reduce systematic order/cache bias; never overlap runs.
        modes=['istream','polling','keyset']
        for rate in ((2,) if '--smoke' in sys.argv else (2,40)):
            for repeat in range(1 if '--smoke' in sys.argv else 3):
                for mode in modes[repeat:]+modes[:repeat]:
                    results.append(await asyncio.wait_for(run_one(admin,mode,rate,repeat),60))
    finally:
        await admin.close()
    if any(not r['correct'] for r in results):
        raise SystemExit('Pilot contains failures; inspect raw results')


if __name__=='__main__':
    asyncio.run(main())
