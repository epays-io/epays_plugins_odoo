# Part of ePays Payment Provider. See LICENSE file for full copyright and licensing details.
""" Start, configure and stop the Odoo shops of the end-to-end tests. See `e2e/README.md`.

    python e2e/scripts/shops.py up [--api local|sandbox] [--with-18.0.1.0.3]
    python e2e/scripts/shops.py down
    python e2e/scripts/shops.py status

The ePays credentials come from `E2E_EPAYS_API_ID` and `E2E_EPAYS_MASTER_KEY`. For the local API,
they can also come from the merchant that its smoke tool created
(`%LOCALAPPDATA%/epays-smoke/state.json`). They are passed to Odoo through the environment of one
command, and never written or printed.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

E2E_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = E2E_DIR.parent
STATE_DIR = E2E_DIR / '.state'
NETWORK = 'epays-e2e'
POSTGRES = 'epays-e2e-pg'
ODOO_IMAGE = 'odoo:18'
POSTGRES_IMAGE = 'postgres:16'
MODULES = 'website_sale,account_payment,payment_epays'
INSTALL_TIMEOUT = 900
# The version a live shop ran when ePays refused its every click (409 IDEMPOTENCY_CONFLICT).
V103_COMMIT = 'fb77586'
V103_DIR = STATE_DIR / 'v18.0.1.0.3'

SHOPS = {
    'current': {'port': 8069, 'db': 'e2e_current'},
    'v103': {'port': 8070, 'db': 'e2e_v103'},
}
# The ePays API the shops use: the addon's mode, and the host of its payment pages.
APIS = {
    'local': {'mode': 'localhost', 'host': 'localhost:7124'},
    'sandbox': {'mode': 'sandbox', 'host': 'testapi.epays.io'},
}


def run(*args, check=True, **kwargs):
    return subprocess.run(args, check=check, text=True, capture_output=True, **kwargs)


def container(key):
    return f'epays-e2e-{key}'


def user_data():
    return Path(os.environ.get('LOCALAPPDATA') or Path.home())


def credentials_file(api):
    """ `%LOCALAPPDATA%/epays-e2e/<api>.json`: `{"apiId", "masterKey", "domain"}`, kept outside the
    repository. """
    path = user_data() / 'epays-e2e' / f'{api}.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}


def credentials(api):
    """ Return the ePays API id and master key: from the environment, the credentials file, or the
    local API smoke tool's state. """
    api_id, key = os.environ.get('E2E_EPAYS_API_ID'), os.environ.get('E2E_EPAYS_MASTER_KEY')
    if api_id and key:
        print(f"ePays {api} merchant {api_id} (from E2E_EPAYS_API_ID)")
        return api_id, key
    saved = credentials_file(api)
    if saved.get('apiId') and saved.get('masterKey'):
        print(f"ePays {api} merchant {saved['apiId']} (from %LOCALAPPDATA%/epays-e2e/{api}.json)")
        return saved['apiId'], saved['masterKey']
    state = user_data() / 'epays-smoke' / 'state.json'
    if api == 'local' and state.exists():
        data = json.loads(state.read_text(encoding='utf-8'))
        print(f"ePays local merchant {data['childApiId']} (the smoke tool's, from {state})")
        return data['childApiId'], data['childMasterKey']
    sys.exit(f"Set E2E_EPAYS_API_ID and E2E_EPAYS_MASTER_KEY: a merchant of the {api} ePays API.")


def ca_certificate():
    """ Write the certificate Odoo must trust for `https://localhost:7124` to the state folder. """
    target = STATE_DIR / 'ca.pem'
    given = os.environ.get('E2E_CA_CERT')
    if given:
        shutil.copyfile(given, target)
        return
    if not shutil.which('dotnet'):
        sys.exit("Set E2E_CA_CERT to the local ePays API's certificate (PEM): dotnet is missing.")
    run('dotnet', 'dev-certs', 'https', '--export-path', str(target), '--format', 'PEM',
        '--no-password')
    # Only the certificate is needed: never keep the exported private key.
    (STATE_DIR / 'ca.key').unlink(missing_ok=True)


def v103_addons():
    if not V103_DIR.exists():
        run('git', '-C', str(REPO_DIR), 'worktree', 'add', '--detach', str(V103_DIR), V103_COMMIT)
    return V103_DIR


def start_postgres():
    run('docker', 'network', 'create', NETWORK, check=False)
    if run('docker', 'ps', '-q', '-f', f'name=^{POSTGRES}$').stdout.strip():
        return
    run('docker', 'rm', '-f', POSTGRES, check=False)
    run('docker', 'run', '-d', '--name', POSTGRES, '--network', NETWORK,
        '-e', 'POSTGRES_USER=odoo', '-e', 'POSTGRES_PASSWORD=odoo', '-e', 'POSTGRES_DB=postgres',
        POSTGRES_IMAGE)
    for _attempt in range(60):
        if run('docker', 'exec', POSTGRES, 'pg_isready', '-U', 'odoo', check=False).returncode == 0:
            return
        time.sleep(1)
    sys.exit("Postgres did not start.")


def start_shop(key, addons, api):
    shop = SHOPS[key]
    name = container(key)
    run('docker', 'rm', '-f', name, check=False)
    command = (
        'cat /etc/ssl/certs/ca-certificates.crt /e2e/state/ca.pem > /tmp/ca.pem 2>/dev/null; '
        f'(python3 /e2e/docker/forward.py /e2e/state/delay_{key}.flag > /tmp/forward.log 2>&1 &); '
        f'exec odoo --db_host {POSTGRES} --db_user odoo --db_password odoo -d {shop["db"]} '
        f'--db-filter=^{shop["db"]}$ -i {MODULES} --without-demo='
    )
    # The sandbox is reached through the container's proxy, which can hold its answers.
    proxy = ['-e', 'HTTPS_PROXY=http://127.0.0.1:3128'] if api == 'sandbox' else []
    run('docker', 'run', '-d', '--name', name, '--network', NETWORK, '-p', f'{shop["port"]}:8069',
        '-e', 'REQUESTS_CA_BUNDLE=/tmp/ca.pem', *proxy,
        # The filestore outlives the container, as the database does: without it a shop started
        # again on its database serves no assets.
        '-v', f'{name}-data:/var/lib/odoo',
        '-v', f'{addons}:/mnt/extra-addons:ro',
        '-v', f'{E2E_DIR / "docker"}:/e2e/docker:ro',
        '-v', f'{STATE_DIR}:/e2e/state',
        ODOO_IMAGE, 'sh', '-c', command)


def wait_installed(key):
    deadline = time.time() + INSTALL_TIMEOUT
    while time.time() < deadline:
        logs = run('docker', 'logs', container(key), check=False)
        if 'Registry loaded in' in logs.stdout + logs.stderr:
            return
        if not run('docker', 'ps', '-q', '-f', f'name=^{container(key)}$').stdout.strip():
            sys.exit(f"{container(key)} stopped: see `docker logs {container(key)}`.")
        time.sleep(5)
    sys.exit(f"{container(key)} did not finish installing in {INSTALL_TIMEOUT}s.")


def configure(key, api, api_id, master_key):
    shop = SHOPS[key]
    env = {
        'E2E_BASE_URL': f'http://127.0.0.1:{shop["port"]}',
        'E2E_EPAYS_MODE': APIS[api]['mode'],
        'E2E_EPAYS_API_ID': api_id,
        'E2E_EPAYS_MASTER_KEY': master_key,
        'E2E_EPAYS_DOMAIN': merchant_domain(api),
        'E2E_CUSTOMER_COUNTRY': os.environ.get('E2E_CUSTOMER_COUNTRY', 'BH'),
    }
    # `-e NAME` passes the value from this process's environment, so it is not on the command line.
    args = [arg for name in env for arg in ('-e', name)]
    script = (E2E_DIR / 'docker' / 'configure.py').read_text(encoding='utf-8')
    result = run(
        'docker', 'exec', '-i', *args, container(key),
        'odoo', 'shell', '-d', shop['db'], '--db_host', POSTGRES, '--db_user', 'odoo',
        '--db_password', 'odoo', '--no-http', '--log-level=warn',
        input=script, env={**os.environ, **env}, check=False,
    )
    line = next((line for line in result.stdout.splitlines() if line.startswith('configured')), '')
    if not line:
        sys.exit(f"Configuring {container(key)} failed:\n{result.stderr[-2000:]}")
    print(f"{container(key)}: {line}, http://127.0.0.1:{shop['port']}")


def merchant_domain(api):
    """ The domain the shops send: one the merchant's licence allows. """
    domain = os.environ.get('E2E_EPAYS_DOMAIN') or credentials_file(api).get('domain')
    if api == 'local':
        return domain or 'localhost'
    if not domain:
        sys.exit("Set E2E_EPAYS_DOMAIN: a domain the sandbox merchant's licence allows.")
    return domain


def up(api, with_v103):
    STATE_DIR.mkdir(exist_ok=True)
    merchant_domain(api)
    api_id, master_key = credentials(api)
    if api == 'local':
        ca_certificate()
    else:
        (STATE_DIR / 'ca.pem').unlink(missing_ok=True)
    # The tests read which API the shops use, to know its payment pages.
    (STATE_DIR / 'api.json').write_text(json.dumps({'api': api, **APIS[api]}), encoding='utf-8')
    start_postgres()
    shops = {'current': REPO_DIR}
    if with_v103:
        shops['v103'] = v103_addons()
    for key, addons in shops.items():
        start_shop(key, addons, api)
    for key in shops:
        wait_installed(key)
        configure(key, api, api_id, master_key)


def down():
    for name in [container(key) for key in SHOPS] + [POSTGRES]:
        run('docker', 'rm', '-f', name, check=False)
    for key in SHOPS:
        run('docker', 'volume', 'rm', f'{container(key)}-data', check=False)
    run('docker', 'network', 'rm', NETWORK, check=False)
    if V103_DIR.exists():
        run('git', '-C', str(REPO_DIR), 'worktree', 'remove', '--force', str(V103_DIR), check=False)
    shutil.rmtree(STATE_DIR, ignore_errors=True)
    print("removed the shops, their database and the state folder")


def status():
    names = '|'.join([container(key) for key in SHOPS] + [POSTGRES])
    print(run('docker', 'ps', '-a', '--format', '{{.Names}}\t{{.Status}}\t{{.Ports}}',
              '-f', f'name={names}').stdout or "no shop running")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('command', choices=('up', 'down', 'status'))
    parser.add_argument('--api', choices=tuple(APIS), default='local',
                        help="the local ePays API (default) or the ePays sandbox")
    parser.add_argument('--with-18.0.1.0.3', dest='with_v103', action='store_true',
                        help="also start 18.0.1.0.3 on port 8070, to reproduce its 409")
    args = parser.parse_args()
    if args.command == 'up':
        up(args.api, args.with_v103)
    elif args.command == 'down':
        down()
    else:
        status()


if __name__ == '__main__':
    main()
