"""Paired comparison of two bench results: compare.py A B [--by cond|level] [--iters 10000]
Prints mean CER of A and B, B-A with a paired bootstrap 95% CI, and files better/worse."""
import argparse
import json
import os

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))


def load(label):
    path = label if label.endswith('.json') else os.path.join(HERE, 'results', label + '.json')
    rows = json.load(open(path, encoding='utf-8'))['rows']
    return {(r['cond'], r['id']): r for r in rows}


def boot(d, iters, rng):
    if len(d) == 0:
        return float('nan'), float('nan')
    idx = rng.integers(0, len(d), (iters, len(d)))
    means = d[idx].mean(axis=1)
    return np.percentile(means, 2.5), np.percentile(means, 97.5)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('a')
    p.add_argument('b')
    p.add_argument('--by', default='level', choices=('level', 'cond', 'none'))
    p.add_argument('--iters', type=int, default=10000)
    args = p.parse_args()
    A, B = load(args.a), load(args.b)
    keys = sorted(set(A) & set(B))
    groups = {}
    for k in keys:
        r = A[k]
        names = {'level': [r['level'], r['kind']], 'cond': [r['cond'], r['kind']], 'none': [r['kind']]}[args.by]
        for name in names + ['all']:
            groups.setdefault(name, []).append(k)
    rng = np.random.default_rng(7)
    print(f'{args.a} -> {args.b}')
    print(f"{'group':16s} {'n':>4s} {'A':>6s} {'B':>6s} {'B-A':>7s}  {'95% CI':>17s}  better worse")
    order = sorted(groups, key=lambda g: (g in ('normal', 'whisper', 'all'), g))
    for g in order:
        ks = groups[g]
        a = np.array([A[k]['cer'] for k in ks])
        b = np.array([B[k]['cer'] for k in ks])
        d = b - a
        lo, hi = boot(d, args.iters, rng)
        flag = ' *' if lo > 0 or hi < 0 else ''
        print(f'{g:16s} {len(ks):4d} {a.mean():6.3f} {b.mean():6.3f} {d.mean():+7.4f}  [{lo:+.4f},{hi:+.4f}]  '
              f'{int((d < 0).sum()):6d} {int((d > 0).sum()):5d}{flag}')


if __name__ == '__main__':
    main()
