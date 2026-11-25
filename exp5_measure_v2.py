#!/usr/bin/env python3
"""CSC6410 EXP5 measurements, revised to select the TCP DATA socket.

Run inside an existing Mininet CLI (h1--s1--h2):
  py __import__('runpy').run_path('exp5_measure_v2.py')['repair']()
  py __import__('runpy').run_path('exp5_measure_v2.py')['run'](net, 'cubic', 0.1, 1)

Preserves original experiment JSON, ss snapshots, tc snapshots, and previous
CSV rows. 'repair' makes one backup and corrects the existing CSV RTT/cwnd
from the real data socket (not the shorter-lived iperf3 control socket).
"""

from __future__ import annotations
import csv
import json
import re
import subprocess
import time
from pathlib import Path

LOSSES = (0., .1, .5, 1., 2.)
OLD_COLUMNS = [
    'algorithm', 'loss_pct', 'repetition', 'duration_s', 'queue_limit_pkts',
    'sender_mbps', 'receiver_mbps', 'retransmissions', 'ss_rtt_ms',
    'ss_cwnd_segments', 'json_file', 'ss_file', 'tc_file',
]
NEW_COLUMNS = OLD_COLUMNS + ['iperf_mean_rtt_ms', 'tc_drops_during_run']
OUT = Path.cwd() / 'exp5_outputs'


def _tag(n):
    return ('%g' % n).replace('.', 'p')


def _number(pattern, text):
    m = re.search(pattern, text, re.I)
    return float(m.group(1)) if m else None


def _data_socket(ss_text, payload):
    """Return RTT and cwnd from iperf DATA socket by its local TCP port."""
    connected = payload.get('start', {}).get('connected', [])
    ports = [str(x['local_port']) for x in connected if x.get('local_port')]
    lines = ss_text.splitlines()
    for port in ports:
        for i, line in enumerate(lines[:-1]):
            if re.search(r'\bESTAB\b', line) and re.search(r':%s(?:\s|$)' % re.escape(port), line):
                info = lines[i + 1] if i + 1 < len(lines) else ''
                rtt = _number(r'\brtt:([0-9.]+)', info)
                cwnd = _number(r'\bcwnd:([0-9.]+)', info)
                return rtt, cwnd, (line.strip() + '\n' + info.strip())
    return None, None, ''


def _mean_rtt(payload):
    streams = payload.get('end', {}).get('streams', [])
    vals = [s.get('sender', {}).get('mean_rtt') for s in streams]
    vals = [float(x) / 1000 for x in vals if isinstance(x, (float, int))]
    return round(sum(vals) / len(vals), 3) if vals else ''


def _drops(tc_output):
    v = _number(r'\(dropped\s+(\d+)', tc_output)
    return int(v) if v is not None else None


def _read_rows(csv_path):
    with csv_path.open('r', newline='', encoding='utf-8-sig') as f:
        return list(csv.DictReader(f))


def _write_rows(csv_path, rows):
    tmp = csv_path.with_name(csv_path.name + '.tmp')
    with tmp.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=NEW_COLUMNS, extrasaction='ignore')
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, '') for k in NEW_COLUMNS})
    tmp.replace(csv_path)


def repair():
    """Correct existing rows' sampled RTT/cwnd from the proper data socket."""
    OUT.mkdir(parents=True, exist_ok=True)
    csv_path = OUT / 'measurements.csv'
    if not csv_path.exists():
        print('No measurements.csv yet; nothing to repair.')
        return
    import shutil
    backup = OUT / 'measurements_before_rtt_fix.csv'
    if not backup.exists():
        shutil.copy2(csv_path, backup)
        print('Original CSV backed up to:', backup)
    rows = _read_rows(csv_path)
    for row in rows:
        jf = OUT / row.get('json_file', '')
        sf = OUT / row.get('ss_file', '')
        if jf.is_file() and sf.is_file():
            payload = json.loads(jf.read_text(encoding='utf-8'))
            rtt, cwnd, chosen = _data_socket(sf.read_text(encoding='utf-8'), payload)
            if rtt is not None:
                row['ss_rtt_ms'] = str(round(rtt, 3))
            if cwnd is not None:
                row['ss_cwnd_segments'] = str(int(cwnd))
            row['iperf_mean_rtt_ms'] = str(_mean_rtt(payload))
            print('Updated %s: data-socket RTT=%s ms, cwnd=%s, iperf mean RTT=%s ms' %
                  (jf.name, row.get('ss_rtt_ms'), row.get('ss_cwnd_segments'),
                   row['iperf_mean_rtt_ms']))
        # Do not invent per-run drops for old runs: their tc stats are cumulative.
        row.setdefault('tc_drops_during_run', '')
    _write_rows(csv_path, rows)
    print('Corrected CSV:', csv_path)


def run(net, algorithm='cubic', loss_pct=0, repetition=1,
        duration_s=30, queue_limit=1000):
    cc = str(algorithm).lower().strip()
    loss = float(loss_pct)
    rep = int(repetition)
    duration = int(duration_s)
    limit = int(queue_limit)
    if cc not in ('cubic', 'bbr'):
        raise ValueError('algorithm must be cubic or bbr')
    if loss not in LOSSES:
        raise ValueError('loss_pct must be 0, 0.1, 0.5, 1, or 2')
    if rep not in (1, 2, 3):
        raise ValueError('repetition must be 1, 2, or 3')
    if duration <= 0 or limit <= 0:
        raise ValueError('duration/queue limit must be positive')
    h1, h2, s1 = [net.get(n) for n in ('h1', 'h2', 's1')]
    OUT.mkdir(parents=True, exist_ok=True)
    stem = '%s_loss%s_run%d' % (cc, _tag(loss), rep)
    jf = OUT / (stem + '.json')
    sf = OUT / (stem + '_ss.txt')
    tf = OUT / (stem + '_tc.txt')
    summary = OUT / 'measurements.csv'
    if jf.exists():
        raise FileExistsError('Existing result will NOT be overwritten: ' + str(jf))
    if summary.exists():
        with summary.open('r', newline='', encoding='utf-8-sig') as f:
            cols = csv.DictReader(f).fieldnames or []
        if cols != NEW_COLUMNS:
            print('Updating older CSV format before the new run...')
            repair()
    avail = h1.cmd('sysctl net.ipv4.tcp_available_congestion_control')
    if cc not in avail.split('=')[-1].split():
        raise RuntimeError(cc + ' unavailable in h1: ' + avail)
    if ':5201' not in h2.cmd('ss -ltn'):
        raise RuntimeError('Run h2 iperf3 -s -D first')

    command = ('tc qdisc replace dev s1-eth2 root netem '
               'delay 100ms loss %g%% rate 20mbit limit %d' % (loss, limit))
    print('\n=== %s loss=%g%%, run=%d, duration=%ds, queue=%d ===' %
          (cc.upper(), loss, rep, duration, limit))
    tc_response = s1.cmd(command)
    if any(x in tc_response.lower() for x in ('error', 'invalid', 'not supported')):
        raise RuntimeError('tc failed: ' + tc_response)
    print('s1$', command)
    before = s1.cmd('tc -s qdisc show dev s1-eth2')
    print('tc dropped before run:', _drops(before))
    changed = h1.cmd('sysctl -w net.ipv4.tcp_congestion_control=' + cc)
    if not changed.strip().endswith('= ' + cc):
        raise RuntimeError('Could not set ' + cc + ': ' + changed)
    print('h1$', changed.strip())
    args = ['iperf3', '-c', '10.0.0.2', '-p', '5201', '-C', cc,
            '-t', str(duration), '-J']
    print('h1$', ' '.join(args))
    with jf.open('w', encoding='utf-8') as fh:
        proc = h1.popen(args, stdout=fh, stderr=subprocess.PIPE)
        time.sleep(min(5, max(1, duration / 3)))
        ss_text = h1.cmd('ss -ti')
        _, err = proc.communicate()
    sf.write_text('Algorithm: %s; loss=%g%%; run=%d\nh1$ ss -ti\n%s' %
                  (cc, loss, rep, ss_text), encoding='utf-8')
    after = s1.cmd('tc -s qdisc show dev s1-eth2')
    before_n, after_n = _drops(before), _drops(after)
    delta = max(0, after_n - before_n) if before_n is not None and after_n is not None else ''
    tf.write_text('s1$ %s\nBefore iperf:\n%s\nAfter iperf:\n%s\nDrops during run: %s\n' %
                  (command, before, after, delta), encoding='utf-8')
    if proc.returncode != 0:
        raise RuntimeError('iperf3 failed: ' + err.decode(errors='replace'))
    payload = json.loads(jf.read_text(encoding='utf-8'))
    if payload.get('error'):
        raise RuntimeError('iperf3 JSON error: ' + str(payload['error']))
    sent = payload['end']['sum_sent']
    received = payload['end']['sum_received']
    rtt, cwnd, socket_line = _data_socket(ss_text, payload)
    mean_rtt = _mean_rtt(payload)
    with sf.open('a', encoding='utf-8') as fh:
        fh.write('\nChosen DATA socket (by local port from JSON):\n')
        fh.write((socket_line or '(matching data socket not observed)') + '\n')
    row = dict(algorithm=cc, loss_pct=loss, repetition=rep, duration_s=duration,
        queue_limit_pkts=limit,
        sender_mbps=round(sent['bits_per_second'] / 1_000_000, 3),
        receiver_mbps=round(received['bits_per_second'] / 1_000_000, 3),
        retransmissions=sent.get('retransmits', ''),
        ss_rtt_ms=round(rtt, 3) if rtt is not None else '',
        ss_cwnd_segments=int(cwnd) if cwnd is not None else '',
        json_file=jf.name, ss_file=sf.name, tc_file=tf.name,
        iperf_mean_rtt_ms=mean_rtt, tc_drops_during_run=delta)
    rows = _read_rows(summary) if summary.exists() else []
    rows.append(row)
    _write_rows(summary, rows)
    print('Sender: %.3f Mb/s | Receiver: %.3f Mb/s | Retransmits: %s' %
          (row['sender_mbps'], row['receiver_mbps'], row['retransmissions']))
    print('Data-socket RTT (ss -ti): %s ms | cwnd: %s seg' %
          (row['ss_rtt_ms'], row['ss_cwnd_segments']))
    print('Mean RTT (iperf3 JSON): %s ms | qdisc drops DURING this run: %s' %
          (mean_rtt, delta))
    print('Files:', jf.name, sf.name, tf.name, 'and measurements.csv')
    return row
