# The contract xtm-one of XTM One, undone: the registration of the platform is ended, as its
# identifier names it in the data of its enrollment. A platform never registered is no error.
import json
import os
import sys
import urllib.error
import urllib.request

platform = (json.load(sys.stdin).get('enrollment') or {}).get('platform', '')
if not platform:
    sys.exit(0)
request = urllib.request.Request(
    f'http://localhost:4000/api/v1/platform/register/{platform}',
    method='DELETE',
    headers={'Authorization': f"Bearer {os.environ['PLATFORM_REGISTRATION_TOKEN']}"},
)
try:
    urllib.request.urlopen(request, timeout=30)
except urllib.error.HTTPError as error:
    if error.code != 404:
        sys.exit(f'xtm-one: DELETE /api/v1/platform/register/{platform} answered {error.code} {error.read().decode(errors="replace")}')
