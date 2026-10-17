"""Запуск генератора из командной строки: python run_gen.py 2025-01-01 2025-01-31"""

import datetime as dt
import sys

from gen.db import connect
from gen.engine import run
from gen.world import ensure_world


def main() -> None:
    date_from = dt.date.fromisoformat(sys.argv[1])
    date_to = dt.date.fromisoformat(sys.argv[2])
    conn = connect()
    ensure_world(conn)

    def progress(done, total):
        print(f"\r{done} из {total} дней", end="", flush=True)

    totals = run(conn, date_from, date_to, progress)
    print("\n", totals)


if __name__ == "__main__":
    main()
