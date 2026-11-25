#!/usr/bin/env python3
"""CSC6410 Experiment 5: simultaneous CUBIC/BBR fairness test.

Use from the *existing* Mininet CLI with h1--s1--h2 and iperf3 servers
on h2 TCP 5201 and 5202:
  py __import__('runpy').run_path('exp5_compete.py')['run'](net, 1000)
  py __import__('runpy').run_path('exp5_compete.py')['run'](net, 250)

Each run launches both 60-second clients BEFORE waiting for either one.
Output: two raw iperf3 JSON files, an ss snapshot, tc qdisc counters, and
competition_summary.csv under exp5_outputs/ in the working directory.
"""

from __future__ import annotations

import csv
import json
import re
import subprocess
import time
from pathlib import Path

OUT = Path.cwd() / 'exp5_outputs'
COLUMNS = [
    'queue_limit_pkts', 'loss_pct', 'duration_s',
    'cubic_receiver_mbps', 'bbr_receiver_mbps', 'total_receiver_mbps',
    'cubic_sender_mbps', 'bbr_sender_mbps',
    'cubic_retransmissions', 'bbr_retransmissions',
    'jain_fairness', 'tc_drops_during_run',
    'cubic_json_file', 'bbr_json_file', 'ss_file', 'tc_file',
]


def _drops(text):
    m = re.search(r'\(dropped\s+(\d+)', text, flags=re.I)
    return int(m.group(1)) if m else None


def _get_rate(data, name):
    try:
        return float(data['end'][name]['bits_per_second']) / 1e6
    except (KeyError, ValueError, TypeError) as exc:
        raise RuntimeError('iperf3 JSON is missing valid end.' + name) from exc


def _get_data_socket(ss_output, data):
    """Extract an actual iperf3 data socket by local port when available."""
    ports = [str(c['local_port']) for c in data.get('start', {}).get('connected', [])
             if c.get('local_port') is not None]
    lines = ss_output.splitlines()
    for port in ports:
        for i, line in enumerate(lines[:-1]):
            if re.search(r'\bESTAB\b', line) and re.search(r':' + re.escape(port) + r'\s', line):
                return line.strip() + '\n' + lines[i + 1].strip()
    return '(data socket not observed in snapshots)'


def run(net, queue_limit=1000, duration_s=60):
    """Compare one CUBIC and one BBR flow on the same shaped link at 0% random loss."""
    q = int(queue_limit)
    duration = int(duration_s)
    if q not in (1000, 250):
        raise ValueError('queue_limit must be 1000 (deep) or 250 (shallow)')
    if duration != 60:
        raise ValueError('Use 60 seconds for the assigned competition experiment')

    h1, h2, s1 = (net.get(name) for name in ('h1', 'h2', 's1'))
    available = h1.cmd('sysctl net.ipv4.tcp_available_congestion_control').split('=')[-1].split()
    if not {'cubic', 'bbr'}.issubset(set(available)):
        raise RuntimeError('Both CUBIC and BBR must be available in h1: ' + str(available))

    listening = h2.cmd('ss -ltn')
    for port in (5201, 5202):
        if ':' + str(port) not in listening:
            raise RuntimeError('No h2 iperf3 server on port %d. Start it before this test.' % port)

    OUT.mkdir(parents=True, exist_ok=True)
    prefix = 'competition_queue%d' % q
    cubic_file = OUT / (prefix + '_cubic.json')
    bbr_file = OUT / (prefix + '_bbr.json')
    err_files = {algo: OUT / (prefix + '_' + algo + '_stderr.txt') for algo in ('cubic', 'bbr')}
    ss_file = OUT / (prefix + '_ss.txt')
    tc_file = OUT / (prefix + '_tc.txt')
    csv_file = OUT / 'competition_summary.csv'
    all_outputs = [cubic_file, bbr_file, *err_files.values(), ss_file, tc_file]
    existing = [str(p) for p in all_outputs if p.exists()]
    if existing:
        raise FileExistsError('Results already exist; not overwriting: ' + ', '.join(existing))

    cmd = 'tc qdisc replace dev s1-eth2 root netem delay 100ms loss 0%% rate 20mbit limit %d' % q
    result = s1.cmd(cmd)
    if any(token in result.lower() for token in ('error', 'not supported', 'invalid', 'no such')):
        raise RuntimeError('Could not configure netem: ' + result)
    before = s1.cmd('tc -s qdisc show dev s1-eth2')

    print('\n=== CUBIC vs BBR simultaneously; 0%% random loss; queue=%d; duration=%ds ===' % (q, duration))
    print('s1$', cmd)
    print('Both flows will start on h1 and send toward h2 concurrently.')
    args = {
        'cubic': ['iperf3', '-c', '10.0.0.2', '-p', '5201', '-C', 'cubic', '-t', str(duration), '-J'],
        'bbr': ['iperf3', '-c', '10.0.0.2', '-p', '5202', '-C', 'bbr', '-t', str(duration), '-J'],
    }
    procs = {}
    handles = {}
    try:
        for algo, jf in [('cubic', cubic_file), ('bbr', bbr_file)]:
            fout = jf.open('w', encoding='utf-8')
            ferr = err_files[algo].open('w', encoding='utf-8')
            handles[algo] = (fout, ferr)
            procs[algo] = h1.popen(args[algo], stdout=fout, stderr=ferr)
            print('h1$', ' '.join(args[algo]))
        time.sleep(8)
        first_ss = h1.cmd('ss -ti')
        time.sleep(22)
        middle_ss = h1.cmd('ss -ti')
        for algo in ('cubic', 'bbr'):
            try:
                procs[algo].wait(timeout=duration + 60)
            except subprocess.TimeoutExpired:
                raise RuntimeError(algo + ' iperf3 client did not terminate')
    finally:
        for p in procs.values():
            if p.poll() is None:
                p.terminate()
        for pair in handles.values():
            for f in pair:
                f.close()

    after = s1.cmd('tc -s qdisc show dev s1-eth2')
    before_d, after_d = _drops(before), _drops(after)
    drops_delta = (max(0, after_d - before_d)
                   if before_d is not None and after_d is not None else '')

    outputs = {}
    for algo, path in [('cubic', cubic_file), ('bbr', bbr_file)]:
        if procs[algo].returncode != 0:
            raise RuntimeError('%s iperf3 returned %s; see %s' %
                               (algo, procs[algo].returncode, err_files[algo]))
        data = json.loads(path.read_text(encoding='utf-8'))
        if data.get('error'):
            raise RuntimeError('%s iperf3 reported: %s' % (algo, data['error']))
        actual = data.get('end', {}).get('sender_tcp_congestion')
        if actual and algo.lower() != str(actual).lower():
            raise RuntimeError('Expected %s but iperf3 reports %s' % (algo, actual))
        outputs[algo] = data

    c_rx = _get_rate(outputs['cubic'], 'sum_received')
    b_rx = _get_rate(outputs['bbr'], 'sum_received')
    fairness = (c_rx + b_rx) ** 2 / (2 * (c_rx ** 2 + b_rx ** 2)) if c_rx + b_rx else None
    ss_file.write_text(
        'h1$ ss -ti (while both iperf3 processes were running; t~8s):\n' + first_ss +
        '\nh1$ ss -ti (t~30s):\n' + middle_ss +
        '\nCUBIC data socket:\n' + _get_data_socket(middle_ss, outputs['cubic']) +
        '\nBBR data socket:\n' + _get_data_socket(middle_ss, outputs['bbr']) + '\n',
        encoding='utf-8')
    tc_file.write_text('s1$ ' + cmd + '\nBefore:\n' + before +
                       '\nAfter:\n' + after + '\nDrop delta: ' + str(drops_delta) + '\n',
                       encoding='utf-8')

    row = {
        'queue_limit_pkts': q,
        'loss_pct': 0,
        'duration_s': duration,
        'cubic_receiver_mbps': round(c_rx, 3),
        'bbr_receiver_mbps': round(b_rx, 3),
        'total_receiver_mbps': round(c_rx + b_rx, 3),
        'cubic_sender_mbps': round(_get_rate(outputs['cubic'], 'sum_sent'), 3),
        'bbr_sender_mbps': round(_get_rate(outputs['bbr'], 'sum_sent'), 3),
        'cubic_retransmissions': outputs['cubic']['end']['sum_sent'].get('retransmits', ''),
        'bbr_retransmissions': outputs['bbr']['end']['sum_sent'].get('retransmits', ''),
        'jain_fairness': round(fairness, 5) if fairness is not None else '',
        'tc_drops_during_run': drops_delta,
        'cubic_json_file': cubic_file.name,
        'bbr_json_file': bbr_file.name,
        'ss_file': ss_file.name,
        'tc_file': tc_file.name,
    }
    already = csv_file.exists()
    with csv_file.open('a', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        if not already:
            writer.writeheader()
        writer.writerow(row)

    print('CUBIC receiver: %.3f Mb/s; sender: %.3f Mb/s; retransmissions: %s' %
          (row['cubic_receiver_mbps'], row['cubic_sender_mbps'], row['cubic_retransmissions']))
    print('BBR   receiver: %.3f Mb/s; sender: %.3f Mb/s; retransmissions: %s' %
          (row['bbr_receiver_mbps'], row['bbr_sender_mbps'], row['bbr_retransmissions']))
    print('Total receiver throughput: %.3f Mb/s' % row['total_receiver_mbps'])
    print('Jain fairness index (two flows): %s; tc drops during run: %s' %
          (row['jain_fairness'], row['tc_drops_during_run']))
    print('Saved:', csv_file, cubic_file.name, bbr_file.name, ss_file.name, tc_file.name)
    return row
