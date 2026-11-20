# The contract xtm-one of XTM One: the registration of a platform of the suite. Runs in the XTM
# One container. The platform registers itself, with the token every platform sends: this hook
# answers it, { "token": ... }. The data of its enrollment:
#   platform  the identifier the platform registers as: opencti, openaev or opencrq
import json
import os
import re
import sys

data = json.load(sys.stdin).get('enrollment') or {}
platform = data.get('platform', '')
if not re.fullmatch(r'[a-z]+', platform):
    sys.exit(f'xtm-one: enrollment.platform is the identifier the platform registers as, opencti, openaev or opencrq, not {platform!r}')
json.dump({'token': os.environ['PLATFORM_REGISTRATION_TOKEN']}, sys.stdout)
