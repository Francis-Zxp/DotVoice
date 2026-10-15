"""Check the public release exactly as an older DotVoice client would."""
import argparse
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge.updates import UpdateWatch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--installed-version', required=True)
    parser.add_argument('--expected-version', required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as folder:
        result = UpdateWatch(Path(folder) / 'update.json', args.installed_version).check(force=True)
    print(json.dumps(result, ensure_ascii=True, indent=2))
    if result['status'] != 'available' or result['latest_version'] != args.expected_version:
        raise SystemExit('Public channel did not offer the expected update')


if __name__ == '__main__':
    main()
