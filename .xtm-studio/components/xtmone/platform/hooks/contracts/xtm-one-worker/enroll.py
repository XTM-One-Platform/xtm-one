# The contract xtm-one-worker of XTM One: what a worker shares with the platform, read from the
# environment of the platform's container. Its enrollment has no data.
import json
import os
import sys

json.load(sys.stdin)
env = os.environ
json.dump(
    {
        'database_url': env['DATABASE_URL'],
        'redis_url': env['REDIS_URL'],
        'redis_key_prefix': env.get('REDIS_KEY_PREFIX', ''),
        's3_endpoint': env['S3_ENDPOINT'],
        's3_access_key': env['S3_ACCESS_KEY'],
        's3_secret_key': env['S3_SECRET_KEY'],
        's3_bucket': env['S3_BUCKET'],
        's3_use_ssl': env.get('S3_USE_SSL', 'false'),
        'secret_key': env['SECRET_KEY'],
        'base_url': env['BASE_URL'],
        'registration_token': env['PLATFORM_REGISTRATION_TOKEN'],
        'admin_email': env['ADMIN_EMAIL'],
        'admin_password': env['ADMIN_PASSWORD'],
    },
    sys.stdout,
)
