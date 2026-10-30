from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import matplotlib

matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

MEDIA = Path(__file__).resolve().parent
ROOT = MEDIA.parents[1]
INK = '#172C29'
MUTED = '#586B65'
RULE = '#D7DED9'
GREEN = '#166653'
PAPER = '#FAFBF8'
AUTHOR = 'Created By Nazmi Efe Armutcu (Aylin)'

plt.rcParams.update({'font.family': 'DejaVu Sans', 'svg.fonttype': 'none'})


def save(fig, name):
    for ext in ('svg', 'png'):
        path = MEDIA / f'{name}.{ext}'
        fig.savefig(path, dpi=140, facecolor=PAPER)
        if ext == 'svg':
            lines = path.read_text(encoding='utf-8').splitlines()
            path.write_text('\n'.join(line.rstrip() for line in lines)+'\n', encoding='utf-8')
    plt.close(fig)


def matrix():
    fig = plt.figure(figsize=(12, 7.4), facecolor=PAPER)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set(xlim=(0, 1), ylim=(0, 1))
    ax.axis('off')
    ax.text(.045, .945, 'CROCODILE  /  DATA LAYER COMPARISON', fontsize=10,
            color=GREEN, weight='bold', va='top')
    ax.text(.045, .89, 'Choose the layer your workflow needs.', fontsize=23,
            color=INK, weight='bold', va='top')
    ax.text(.045, .826, 'Source-access libraries and a local dataset engine have different jobs.',
            fontsize=11, color=MUTED, va='top')

    left, top, rowh = .045, .74, .085
    widths = [.16, .275, .25, .27]
    xs = [left]
    for width in widths:
        xs.append(xs[-1] + width)
    ax.add_patch(Rectangle((xs[1]-.009, top - 6*rowh-.009), widths[1], 6*rowh+.075,
                           facecolor='#ECF3ED', edgecolor='none'))
    headers = ['RESPONSIBILITY', 'Crocodile', 'CCXT', 'yfinance']
    for x, header in zip(xs, headers):
        ax.text(x, top+.032, header, fontsize=11 if x == left else 14,
                color=GREEN if header == 'Crocodile' else INK, weight='bold', va='bottom')
    rows = [
        ('Primary role', 'Collect, store, query, replay', 'Unified exchange access', 'Yahoo Finance access'),
        ('Source access', 'Native + library adapters', 'Exchange APIs', 'Yahoo endpoints'),
        ('Typical output', 'Typed records + provenance', 'Unified API objects', 'DataFrames + ticker data'),
        ('Dataset storage', 'Partitioned Parquet lake', 'Application adds a store', 'Application adds a store'),
        ('Recorded replay', 'Ordered lake records', 'Application builds replay', 'Application builds replay'),
        ('Order execution', 'No execution service', 'Supported exchange APIs', 'No execution API'),
    ]
    for i, row in enumerate(rows):
        y = top-i*rowh
        ax.plot([left, .955], [y, y], lw=.8, color=RULE)
        for col, value in enumerate(row):
            ax.text(xs[col], y-rowh*.48, value, fontsize=10.4 if col else 10.1,
                    color=INK if col else MUTED, va='center',
                    weight='normal')
    bottom = top-6*rowh
    ax.plot([left, .955], [bottom, bottom], lw=.8, color=RULE)
    ax.text(.045, .184, 'Default project scope; no custom storage or replay extensions assumed.',
            fontsize=10, color=MUTED, va='top')
    ax.text(.045, .148, 'Crocodile can use CCXT and yfinance as connectors. This is not a speed or quality ranking.',
            fontsize=9.4, color=MUTED, va='top')
    ax.text(.045, .094, 'Sources: Crocodile source · CCXT manual · yfinance API reference  |  30 Oct 2026',
            fontsize=9, color=MUTED, va='top')
    ax.text(.045, .045, AUTHOR, fontsize=9, color=INK, va='bottom')
    save(fig, 'data-layer-comparison')


def storage():
    text = (MEDIA / 'benchmark-run.md').read_text(encoding='utf-8')
    m = re.search(r'([\d.]+) MB JSON → ([\d.]+) MB', text)
    if not m:
        raise ValueError('Documented benchmark size row is missing')
    json_mib, parquet_mib = map(float, m.groups())
    machine = re.search(r'\*\*Machine:\*\* (.+)', text).group(1)
    data = {
        'date': '2026-10-30', 'fixture': '500000 synthetic canonical Trade records',
        'base_rng_seed': 42, 'trade_rng_seed': 43, 'json_mib_rounded': json_mib,
        'parquet_mib_rounded': parquet_mib,
        'source': 'benchmark-run.md',
        'size_unit': 'MiB (benchmark labels this MB; bytes / 1048576)',
        'source_commit': 'ba55eb03e5a34c1879188d9b11cc42f8b65e0aac',
        'machine': machine, 'attribution': AUTHOR,
    }
    (MEDIA/'comparison-data.json').write_text(json.dumps(data, indent=2)+'\n', encoding='utf-8')

    fig = plt.figure(figsize=(12, 6.8), facecolor=PAPER)
    fig.text(.055, .935, 'CROCODILE  /  MEASURED STORAGE', fontsize=10, color=GREEN, weight='bold')
    fig.text(.055, .854, 'Same trade rows. Different storage cost.', fontsize=23, color=INK, weight='bold')
    fig.text(.055, .802, '500,000 synthetic trades · equivalent fields · base seed 42 · lower is smaller',
             fontsize=11, color=MUTED)
    ax = fig.add_axes([.235, .38, .65, .28], facecolor=PAPER)
    ax.barh([1, 0], [json_mib, parquet_mib], height=.39,
            color=['#94A6A0', GREEN], zorder=3)
    ax.set_yticks([1,0], ['Uncompressed JSON', 'Parquet / Zstd-5'], fontsize=11, color=INK)
    ax.tick_params(axis='y', length=0, pad=14)
    ax.set_xlim(0, json_mib*1.13)
    ax.set_xticks([0,40,80,120,160])
    ax.tick_params(axis='x', colors=MUTED, length=0, labelsize=10)
    ax.set_xlabel('Serialized size (MiB)', fontsize=10, color=MUTED, labelpad=12)
    ax.xaxis.grid(True, color=RULE, lw=.8, zorder=0)
    for spine in ax.spines.values():
        spine.set_visible(False)
    for y, value in zip([1,0], [json_mib, parquet_mib]):
        ax.text(value+json_mib*.018, y, f'{value:.1f}', va='center', fontsize=13,
                weight='bold', color=INK)
    fig.text(.055, .235, 'Source: docs/media/benchmark-run.md · local Windows run, 30 Oct 2026',
             fontsize=10, color=MUTED)
    fig.text(.055, .185, 'Offline fixture; JSON is uncompressed. This isolates format + compression, not live-feed performance.',
             fontsize=9.4, color=MUTED)
    fig.text(.055, .135, 'MiB = 1,048,576 bytes. Source values are rounded; the full report records the machine and workload.',
             fontsize=9.4, color=MUTED)
    fig.text(.055, .065, AUTHOR, fontsize=9, color=INK)
    save(fig, 'storage-comparison')


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--matrix-only', action='store_true')
    args = p.parse_args()
    matrix()
    if not args.matrix_only:
        storage()
