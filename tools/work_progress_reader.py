"""Socket-activated local progress projection. No input commands or task text."""
import json
import sys
import time
from work_progress import _read_events, _default_db_path

if __name__ == '__main__':
    try:
        rows = _read_events(_default_db_path(), time.time())
        if len(rows) > 4096:
            raise ValueError('progress projection too large')
        result = {'events': rows}
    except Exception:
        result = {'error': 'progress source unavailable'}
    sys.stdout.write(json.dumps(result, separators=(',', ':')) + '\n')
