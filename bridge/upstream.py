"""Remind the user when OpenLess ships live-text support for remote input.

DotVoice asked OpenLess (Open-Less/openless#1147) to forward the session's live
transcript. Until a release contains it, DotVoice checks the public GitHub API
twice a day (anonymous, nothing about the user is sent) and tells the user once
when an OpenLess release newer than the merge exists. If DotVoice already
receives OpenLess partials, checking stops by itself.
"""
import json
from pathlib import Path
import time
import urllib.request

REPO = 'Open-Less/openless'
PR = 1147
PR_URL = f'https://github.com/{REPO}/pull/{PR}'
INTERVAL = 12 * 3600


def _get(url, timeout=10):
    request = urllib.request.Request(url, headers={'Accept': 'application/vnd.github+json',
                                                   'User-Agent': 'DotVoice-update-check'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode('utf-8'))


class UpstreamWatch:
    def __init__(self, path, fetch=_get, clock=time.time):
        self.path = Path(path)
        self.fetch, self.clock = fetch, clock
        try:
            self.state = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            self.state = {}
        if not isinstance(self.state, dict):
            self.state = {}  # a damaged file ("[]", "null") must not break every check
        if not isinstance(self.state.get('checked_at'), (int, float, type(None))):
            self.state.pop('checked_at')

    def _save(self):
        try:
            self.path.write_text(json.dumps(self.state, ensure_ascii=False), encoding='utf-8')
        except OSError:
            pass

    @property
    def finished(self):
        return self.state.get('status') in ('notified', 'supported', 'closed')

    def supported(self):
        """OpenLess already sends partials: nothing to remind about."""
        if self.state.get('status') != 'supported':
            self.state['status'] = 'supported'
            self._save()

    def due(self):
        last = self.state.get('checked_at')
        return not self.finished and (last is None or self.clock() - last >= INTERVAL)

    def check(self):
        """Returns a reminder dict once (merged + released, or closed), else None."""
        self.state['checked_at'] = self.clock()
        try:
            pr = self.fetch(f'https://api.github.com/repos/{REPO}/pulls/{PR}')
            merged = pr.get('merged_at')
            if not merged:
                if pr.get('state') == 'closed':
                    self.state['status'] = 'closed'
                    return {'kind': 'closed', 'url': PR_URL}
                self.state['status'] = 'waiting'
                return None
            self.state['merged_at'] = merged
            releases = self.fetch(f'https://api.github.com/repos/{REPO}/releases?per_page=10')
            if not isinstance(releases, list):
                return None  # rate limited ({"message": ...}): try again later
            newer = [r for r in releases if isinstance(r, dict)
                     and (r.get('published_at') or '') > merged and not r.get('draft')]
            if not newer:
                self.state['status'] = 'merged'
                return None
            release = min(newer, key=lambda r: r['published_at'])
            self.state['status'] = 'notified'
            return {'kind': 'released', 'version': release.get('tag_name') or release.get('name') or '',
                    'url': release.get('html_url') or PR_URL}
        except Exception:  # offline, rate-limited, API change: try again later
            return None
        finally:
            self._save()
