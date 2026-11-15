"""Derive tables only from saved measurements; no dependencies besides Python."""
import json
from pathlib import Path
import statistics
import sys


def main(folder):
    runs = [json.loads(p.read_text()) for p in sorted(Path(folder).glob('run-*.json'))]
    assert len(runs)==18, f'Expected 18 runs, found {len(runs)}'
    assert all(r['correct'] and r['error'] is None for r in runs), 'Failed runs: inspect raw JSON'
    assert len({r['revision'] for r in runs})==1
    summary=[]
    for rate in (2,40):
        subset=[r for r in runs if r['rate']==rate]
        assert len({r['plan_sha256'] for r in subset})==1
        assert len({json.dumps(r['bounds']) for r in subset})==1
        assert len({tuple(r['final_rows']) for r in subset})==1
        for mode in ('istream','polling','keyset'):
            group=[r for r in subset if r['mode']==mode]
            assert len(group)==3
            def med(function):
                return statistics.median(function(r) for r in group)
            summary.append(dict(rate=rate, mode=mode,
                initial_bytes=med(lambda r:r['traffic']['initial']['bytes']),
                update_bytes=med(lambda r:r['traffic']['updates']['bytes']),
                requests=med(lambda r:r['traffic']['updates']['requests']),
                pages=med(lambda r:r['traffic']['updates'].get('pages',0)),
                reloads=med(lambda r:r['traffic']['updates'].get('range_loads',0)),
                initial_ms=med(lambda r:r['initial_ms']),
                catchup_ms=med(lambda r:statistics.mean(c['catchup_ms'] for c in r['checkpoints'] if not c['recovery'])),
                recovery_ms=med(lambda r:next(c['recovery_ms'] for c in r['checkpoints'] if c['recovery'])),
                write_tps=med(lambda r:r['write_tps']),
                write_mean_ms=med(lambda r:r['write_mean_ms']),
                write_p95_ms=med(lambda r:r['write_p95_ms']),
                server_cpu_s=med(lambda r:r['server']['cpu_s']),
                server_peak_mib=med(lambda r:r['server']['peak_rss_kib']/1024),
                harness_cpu_s=med(lambda r:r['harness_cpu_s']),
                wal_bytes=med(lambda r:r['wal_bytes']),
                pg_commits=med(lambda r:r['pg']['xact_commit']),
                pg_hits=med(lambda r:r['pg']['blks_hit']),
                pg_reads=med(lambda r:r['pg']['blks_read'])))
    Path(folder,'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    print('| tx/s | Вариант | Initial B | Updates B | Req | Pages | Reloads | Initial ms | Catch-up ms | Recovery ms |')
    print('|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|')
    for r in summary:
        print('| {rate} | {mode} | {initial_bytes:.0f} | {update_bytes:.0f} | {requests:.0f} | {pages:.0f} | {reloads:.0f} | {initial_ms:.2f} | {catchup_ms:.2f} | {recovery_ms:.2f} |'.format(**r))
    print('\n| tx/s | Вариант | Writer tx/s | Mean tx ms | p95 tx ms | Server CPU s | Peak MiB | WAL B | PG hits | PG reads |')
    print('|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|')
    for r in summary:
        print('| {rate} | {mode} | {write_tps:.2f} | {write_mean_ms:.3f} | {write_p95_ms:.3f} | {server_cpu_s:.2f} | {server_peak_mib:.1f} | {wal_bytes:.0f} | {pg_hits:.0f} | {pg_reads:.0f} |'.format(**r))
    print('\n| tx/s | Вариант | Повтор | Updates B | Req | Pages | Reloads | Catch-up ms | Recovery ms | Writer p95 ms | OK |')
    print('|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---|')
    for r in sorted(runs,key=lambda r:(r['rate'],r['mode'],r['repeat'])):
        update=r['traffic']['updates']
        catchup=statistics.mean(c['catchup_ms'] for c in r['checkpoints'] if not c['recovery'])
        recovery=next(c['recovery_ms'] for c in r['checkpoints'] if c['recovery'])
        print(f"| {r['rate']} | {r['mode']} | {r['repeat']+1} | {update['bytes']} | {update['requests']} | {update.get('pages',0)} | {update.get('range_loads',0)} | {catchup:.2f} | {recovery:.2f} | {r['write_p95_ms']:.3f} | yes |")


if __name__=='__main__':
    main(sys.argv[1])
