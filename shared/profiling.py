"""Opt-in diagnostics: python main.py --profile (or VTT_PROFILE=1).
CPU profile covers the main/UI thread; tracemalloc covers Python allocations
in all threads, not native Qt images, SDL buffers or total process RSS.
"""
import atexit
import cProfile
import os
from pathlib import Path
import pstats
import sys
import time
import tracemalloc

from shared import app_paths


def enable():
    if '--profile' not in sys.argv and os.environ.get('VTT_PROFILE') != '1':
        return
    if '--profile' in sys.argv:
        sys.argv.remove('--profile')
    folder = Path(os.environ.get(
        'VTT_PROFILE_DIR', str(Path(app_paths.home_dir()) / 'profiles')))
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S') + '-' + str(os.getpid())
    profiler = cProfile.Profile()
    tracemalloc.start(10)
    profiler.enable()

    def finish():
        profiler.disable()
        profiler.dump_stats(str(folder / (stamp + '.prof')))
        current, peak = tracemalloc.get_traced_memory()
        snapshot = tracemalloc.take_snapshot()
        with (folder / (stamp + '.txt')).open('w', encoding='utf-8') as out:
            out.write(f'Python allocations current={current} peak={peak} bytes\n')
            out.write('Not total RSS; Qt/SDL native allocations are excluded.\n')
            for row in snapshot.statistics('lineno')[:30]:
                out.write(str(row) + '\n')
            pstats.Stats(profiler, stream=out).sort_stats('cumulative').print_stats(60)
        tracemalloc.stop()
    atexit.register(finish)
