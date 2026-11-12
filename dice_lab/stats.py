"""Статистика физических бросков без интерфейса.

    python -m dice_lab.stats --rolls 500
    python -m dice_lab.stats --sides 20 --rolls 2000 --seed 7

Скрипт не подкручивает результат: каждый бросок — новый набор начальных
условий и полная симуляция Bullet до остановки. Печатаются частоты,
нечитаемые грани и хи-квадрат (чем ближе к числу степеней свободы, тем
ближе распределение к равномерному).
"""
import argparse
import random
import statistics
from collections import Counter

from .geometry import DIE_ORDER
from .physics import DicePhysics, settle

# Критические значения хи-квадрат при p = 0.001.
CRITICAL_001 = {
    3: 16.27, 5: 20.52, 7: 24.32, 9: 27.88, 11: 31.26, 19: 43.82,
}


def chi_square(counts, sides, total):
    expected = total / sides
    return sum((counts.get(value, 0) - expected) ** 2 / expected
               for value in range(1, sides + 1))


def run(sides, rolls, seed=None):
    engine = DicePhysics()
    if seed is not None:
        engine.rng = random.Random(seed)
    values = []
    statuses = Counter()
    durations = []
    for _ in range(rolls):
        engine.throw([sides])
        outcome = settle(engine)
        statuses[outcome.status] += 1
        durations.append(outcome.elapsed)
        if outcome.status == 'OK':
            values.append(outcome.values[0])
    counts = Counter(values)
    total = len(values)
    chi2 = chi_square(counts, sides, total) if total else float('nan')
    return {
        'sides': sides,
        'rolls': rolls,
        'ok': total,
        'counts': counts,
        'statuses': statuses,
        'chi2': chi2,
        'critical': CRITICAL_001.get(sides - 1),
        'mean_time': statistics.mean(durations),
        'max_time': max(durations),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rolls', type=int, default=300,
                        help='бросков на каждый тип кубика')
    parser.add_argument('--sides', type=int, choices=DIE_ORDER,
                        help='только один тип кубика')
    parser.add_argument('--seed', type=int, default=None,
                        help='детерминированный генератор для проверки')
    args = parser.parse_args()

    sides_list = [args.sides] if args.sides else list(DIE_ORDER)
    for sides in sides_list:
        result = run(sides, args.rolls, args.seed)
        counts = result['counts']
        spread = ' '.join(f'{value}:{counts.get(value, 0)}'
                          for value in range(1, sides + 1))
        bad = result['rolls'] - result['ok']
        print(f'D{sides}: OK {result["ok"]}/{result["rolls"]}'
              f'  нечитаемых/ошибок {bad}'
              f'  ср. {result["mean_time"]:.2f} c'
              f'  макс. {result["max_time"]:.2f} c')
        print(f'  {spread}')
        print(f'  хи-квадрат {result["chi2"]:.2f}'
              f' (df={sides - 1}, критич. p=0.001:'
              f' {result["critical"]})')
        print(f'  статусы: {dict(result["statuses"])}')


if __name__ == '__main__':
    main()
