#!/usr/bin/env sh
# Всё, что можно проверить без ключей и сети.
set -e
PY="${PY:-python3}"
if [ -x .venv/bin/python ]; then PY=.venv/bin/python; fi

echo "== компиляция =="
"$PY" -m compileall -q inspobot tests

echo "== тесты =="
"$PY" -m unittest discover -s tests -t . "$@"
