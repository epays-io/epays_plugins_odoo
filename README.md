# epays-odoo

The ePays payment provider for **Odoo 18.0** (Community and Enterprise): customers pay on the ePays
hosted payment page from the Odoo shop, the customer portal and payment links, and Odoo checks
every result with ePays before it confirms the order and posts the payment.

| | |
|---|---|
| Module | [`payment_epays`](payment_epays/) |
| Odoo | 18.0 only (this branch) |
| Licence | LGPL-3 |
| Price | Free |

The module documentation, including installation, ePays onboarding, configuration and the
reconciliation fields, is in [`payment_epays/README.rst`](payment_epays/README.rst).

## Install

A payment provider contains Python code, so Odoo loads it only from a folder on the server's
**addons path**. *Apps > Import Module* cannot install it, and **Odoo Online (SaaS) is not
supported** (it runs no third-party Python modules). Odoo.sh, on-premise and Docker all work.

Below, replace `<database>` with your Odoo database name. The paths match a standard Linux install
(Odoo from the official package, configuration in `/etc/odoo/odoo.conf`, service `odoo`); adjust
them to your server.

### 1. Put the module on the server

From the release zip (`payment_epays-18.0.1.0.3.zip`):

```sh
sudo mkdir -p /opt/odoo/custom-addons
sudo unzip payment_epays-18.0.1.0.3.zip -d /opt/odoo/custom-addons
sudo chown -R odoo:odoo /opt/odoo/custom-addons
ls /opt/odoo/custom-addons/payment_epays/__manifest__.py    # must exist
```

Or from this repository:

```sh
sudo git clone --branch 18.0 --depth 1 https://github.com/<org>/epays-odoo.git /opt/odoo/epays-odoo
sudo chown -R odoo:odoo /opt/odoo/epays-odoo
```

### 2. Add the folder to the addons path

Edit `/etc/odoo/odoo.conf` and append the folder that **contains** `payment_epays`
(`/opt/odoo/custom-addons`, or `/opt/odoo/epays-odoo` for a clone) to `addons_path`. Behind a
reverse proxy (nginx, a load balancer), also enable `proxy_mode`:

```ini
[options]
addons_path = /usr/lib/python3/dist-packages/odoo/addons,/opt/odoo/custom-addons
proxy_mode = True
```

### 3. Install the module

With the command line (installs the shop and accounting too; drop them if already installed):

```sh
sudo systemctl stop odoo
sudo -u odoo odoo -c /etc/odoo/odoo.conf -d <database> \
    -i website_sale,account_payment,payment_epays --stop-after-init
sudo systemctl start odoo
```

Or from the web: `sudo systemctl restart odoo`, then in Odoo activate the developer mode
(Settings > Developer Tools), open *Apps > Update Apps List*, search for **ePays** and click
**Install**.

Check the result:

```sh
sudo -u odoo psql -d <database> -c \
    "SELECT name, state, latest_version FROM ir_module_module WHERE name = 'payment_epays';"
# payment_epays | installed | 18.0.1.0.3
```

**ePays not under Payment Providers?** Odoo lists only the providers of the company selected in
the top-right company switcher. If the module is not *Installed*, check that the `payment_epays`
folder is on `addons_path`, run *Update Apps List*, and read the install log (it names the cause,
e.g. an Odoo version other than 18.0):

```sh
sudo -u odoo psql -d <database> -c "SELECT id, state, company_id FROM payment_provider WHERE code = 'epays';"
sudo grep -iE "payment_epays.*(error|warning)" /var/log/odoo/odoo-server.log
```

### Docker (official `odoo:18` image)

Mount the folder that contains `payment_epays` on `/mnt/extra-addons`, which the image already has
on its addons path, then install:

```sh
# docker-compose.yml, service odoo:  volumes: ["./addons:/mnt/extra-addons"]
mkdir -p addons && unzip payment_epays-18.0.1.0.3.zip -d addons
docker compose run --rm odoo odoo -d <database> -i website_sale,account_payment,payment_epays \
    --stop-after-init
docker compose restart odoo
```

With plain `docker run`, pass the same volume (`-v "$PWD/addons:/mnt/extra-addons"`) and the
database settings (`-e HOST=<postgres host> -e USER=odoo -e PASSWORD=<password>`).

### Odoo.sh

Add the module to your Odoo.sh project repository and push; Odoo.sh builds the branch with it:

```sh
git clone git@github.com:<you>/<odoo-sh-project>.git && cd <odoo-sh-project>
git checkout <staging-branch>
unzip /path/to/payment_epays-18.0.1.0.3.zip -d .       # the module folder at the root
git add payment_epays
git commit -m "Add the ePays payment provider"
git push
```

Or keep it as a submodule that follows this repository:
`git submodule add -b 18.0 https://github.com/<org>/epays-odoo.git epays-odoo`. When the build is
ready, install **ePays** from *Apps* on that branch, test it, then merge into production. Staging
builds are neutralised: their ePays master keys are cleared, so enter sandbox keys there.

### Windows (Odoo installer)

```powershell
Expand-Archive .\payment_epays-18.0.1.0.3.zip -DestinationPath "C:\odoo\custom-addons"
# add C:\odoo\custom-addons to addons_path in "C:\Program Files\Odoo 18.0\server\odoo.conf"
Restart-Service odoo-server-18.0      # the service name may differ; see services.msc
```

then *Apps > Update Apps List* and install **ePays**.

### Update the plugin

Updating takes three steps: **replace the files**, **update the module in the database**, then
**check**. The second step is the one people forget: replacing the files alone is not enough, and
Odoo then fails with `column payment_provider.epays_... does not exist` (see *If something goes
wrong* below). Payments in progress are not affected: they complete after the update.

#### Before you start

```sh
# The version installed now (compare with the one in the new zip's name):
sudo -u odoo psql -d <database> -c \
    "SELECT latest_version FROM ir_module_module WHERE name = 'payment_epays';"

# A backup of the database and of the current module folder, to roll back if needed:
sudo -u odoo pg_dump -Fc <database> > /tmp/<database>-before-epays-update.dump
sudo cp -a /opt/odoo/custom-addons/payment_epays /tmp/payment_epays-previous
```

On Odoo.sh, update a **staging** branch first; Odoo.sh keeps its own backups.

#### 1. Replace the files

**Linux, from the zip**, replace the whole folder (do not unzip over it: a file removed in the new
version would stay behind):

```sh
sudo rm -rf /opt/odoo/custom-addons/payment_epays
sudo unzip payment_epays-18.0.1.0.3.zip -d /opt/odoo/custom-addons
sudo chown -R odoo:odoo /opt/odoo/custom-addons/payment_epays
```

**Linux, from a clone** of this repository:

```sh
sudo -u odoo git -C /opt/odoo/epays-odoo pull
```

**Docker**, replace the folder mounted on `/mnt/extra-addons`:

```sh
rm -rf addons/payment_epays && unzip payment_epays-18.0.1.0.3.zip -d addons
```

**Windows**:

```powershell
Remove-Item -Recurse -Force "C:\odoo\custom-addons\payment_epays"
Expand-Archive .\payment_epays-18.0.1.0.3.zip -DestinationPath "C:\odoo\custom-addons"
```

**Odoo.sh**, replace the folder in your project repository and push; because the new version has
a higher `version`, Odoo.sh updates the module on its own when it builds the branch (skip step 2):

```sh
cd <odoo-sh-project>
git rm -r -q payment_epays && unzip /path/to/payment_epays-18.0.1.0.3.zip -d .
git add payment_epays && git commit -m "Update the ePays payment provider to 18.0.1.0.3" && git push
```

(with a submodule instead: `git -C epays-odoo pull`, then commit and push the submodule change).

#### 2. Update the module in the database

From the command line (recommended; it also works when the web interface cannot start):

```sh
sudo systemctl stop odoo
sudo -u odoo odoo -c /etc/odoo/odoo.conf -d <database> -u payment_epays --stop-after-init
sudo systemctl start odoo
```

- Docker: `docker compose run --rm odoo odoo -d <database> -u payment_epays --stop-after-init`,
  then `docker compose restart odoo`.
- Windows: restart the Odoo service (`Restart-Service odoo-server-18.0`), then use **Upgrade** in
  the browser as below; or stop the service and run the installer's `odoo-bin` with
  `-c <odoo.conf> -d <database> -u payment_epays --stop-after-init`.
- Several databases on one server: run the update for each of them.

Or from the browser, after restarting Odoo: activate the developer mode, open *Apps*, remove the
*Apps* filter, search for **ePays**, open its menu (**⋮**) and click **Upgrade**.

The update runs the version's migrations on its own (for example 18.0.1.0.1 removed the old BHD
limit, 18.0.1.0.3 moved the saved server IP address); nothing else needs to be done by hand.

#### 3. Check

```sh
sudo -u odoo psql -d <database> -c \
    "SELECT latest_version FROM ir_module_module WHERE name = 'payment_epays';"
# 18.0.1.0.3
```

Then reload Odoo in the browser (Ctrl+F5, so that the new screens load), open *Invoicing >
Configuration > Payment Providers > ePays* and click **Test connection**.

#### If something goes wrong

| Symptom | Fix |
|---|---|
| `psycopg2.errors.UndefinedColumn: column payment_provider.epays_... does not exist` | Step 2 was not run on this database. Run it. With 18.0.1.0.2, the browser *Upgrade* itself could fail with this error: use the command line once, or install 18.0.1.0.3 or later. |
| A new button or field is missing | Reload with Ctrl+F5. If still missing, the module was not updated: check the version (step 3). |
| *Upgrade* is not offered in Apps | The new files are not the ones Odoo loads: check the folder is on `addons_path` and restart Odoo. |
| ePays is not under Payment Providers | Switch to the right company (top right), or see *ePays not under Payment Providers?* above. |
| Anything else | Read the log: `sudo grep -iE "payment_epays.*(error\|warning)" /var/log/odoo/odoo-server.log` |

To roll back, stop Odoo, restore the previous folder and the database backup, then start Odoo:

```sh
sudo systemctl stop odoo
sudo rm -rf /opt/odoo/custom-addons/payment_epays
sudo cp -a /tmp/payment_epays-previous /opt/odoo/custom-addons/payment_epays
sudo -u odoo dropdb <database> && sudo -u odoo createdb <database>
sudo -u odoo pg_restore -d <database> /tmp/<database>-before-epays-update.dump
sudo systemctl start odoo
```

#### Version history

| Version | Changes | Update notes |
|---|---|---|
| 18.0.1.0.3 | This server's IP address stored as a system parameter | Fixes *Upgrade* from the browser failing with `epays_server_ip does not exist`. |
| 18.0.1.0.2 | *Find this server's IP address*; ePays provider in every company | Adds a column: update from the command line. |
| 18.0.1.0.1 | Payments in other currencies converted to BHD; master keys never sent to the browser | Removes the old BHD-only limits. |
| 18.0.1.0.0 | First version | |

### After installing

Installing creates an **ePays** provider, **Disabled**, in every company (Odoo lists only the
providers of the company you are working in; companies created later get one too). Open
*Invoicing > Configuration > Payment Providers > ePays*, enter the API ID and master key, click
**Test connection**, switch to **Test**,
**Sync from ePays**, pay one order, then set it to **Enabled**. The steps are detailed in
[`payment_epays/README.rst`](payment_epays/README.rst) (*How to use it*). Ask ePays to register
the shop's domain and the server's public IP address: **Find this server's IP address** on the
provider form shows it, with a copy button.

## Development

Run the tests with Docker:

```sh
docker network create epays-net
docker run -d --name epays-pg --network epays-net \
  -e POSTGRES_USER=odoo -e POSTGRES_PASSWORD=odoo -e POSTGRES_DB=postgres postgres:16
docker run --rm --network epays-net -e HOST=epays-pg -e USER=odoo -e PASSWORD=odoo \
  -v "$PWD":/mnt/extra-addons odoo:18 \
  odoo -d test -i payment_epays,website_sale,account_payment \
  --test-enable --test-tags /payment_epays --stop-after-init
```

Without `sale`/`account_payment`, the tests that need them are skipped. CI
([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs both configurations.

Linting: `pre-commit install && pre-commit run --all-files` (pylint-odoo mandatory checks,
including the manifest).

**Do not add stored fields to `payment.provider`.** A new column there makes *Apps > Upgrade*
fail on every database that does not have it yet (Odoo recomputes the providers' colour, which
reads every column, before the upgrade adds it). Keep new provider settings in a system parameter
or in a model of the addon; `test_the_addon_adds_no_new_column_to_payment_provider` guards this.

Update the translation template after changing a user-facing string:

```sh
odoo -d test --i18n-export=/mnt/extra-addons/payment_epays/i18n/payment_epays.pot \
  --modules=payment_epays --stop-after-init
```

then merge it into `payment_epays/i18n/ar.po` (for example with `msgmerge`).

## Releases

The Odoo Apps Store scans the `18.0` branch of this repository. Each GitHub release also carries
a zip of the `payment_epays` folder, built without caches:

```sh
version=$(python3 -c "import ast;print(ast.literal_eval(open('payment_epays/__manifest__.py').read())['version'])")
mkdir -p dist && zip -r "dist/payment_epays-$version.zip" payment_epays -x '*/__pycache__/*' '*.pyc'
```

Bump `version` in `__manifest__.py` for every release that installed databases must pick up, and
put any data change for existing databases in `payment_epays/migrations/<version>/`.
