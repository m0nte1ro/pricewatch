# Alternative: native Debian/systemd deployment

Docker Compose is the recommended deployment. This older native deployment requires Python and host packages; it is retained for existing installations only. Run the commands from the repository root.

## Debian LXC deployment

Use a Debian 13 LXC for the commands below; its Python 3.13 satisfies the application's requirement. Debian 12's default Python 3.11 does not. See [Debian's Python package](https://packages.debian.org/trixie/python3.13).

Run these commands **inside the LXC, from the checked-out repository**. They install the application under `/opt/pricewatch`, configuration under `/etc/pricewatch`, and data under `/var/lib/pricewatch`.

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv rsync sqlite3 ca-certificates
python3 -c 'import sys; assert sys.version_info >= (3, 12), "Python 3.12+ required"'
sudo useradd --system --user-group --home-dir /var/lib/pricewatch \
  --shell /usr/sbin/nologin pricewatch
sudo install -d -m 0755 /opt/pricewatch
sudo install -d -m 0750 -o root -g pricewatch /etc/pricewatch
sudo install -d -m 0700 -o pricewatch -g pricewatch /var/lib/pricewatch
sudo rsync -a --exclude='.git' --exclude='.venv' --exclude='data' \
  --exclude='.env' --exclude='.agents' --exclude='.codex' --exclude='.aws' \
  --exclude='__pycache__' --exclude='.pytest_cache' --exclude='.ruff_cache' \
  ./ /opt/pricewatch/
sudo chown -R root:root /opt/pricewatch
sudo python3 -m venv /opt/pricewatch/.venv
sudo /opt/pricewatch/.venv/bin/pip install -r /opt/pricewatch/requirements.lock
sudo /opt/pricewatch/.venv/bin/pip install --no-deps /opt/pricewatch
sudo install -m 0640 -o root -g pricewatch /opt/pricewatch/.env.example \
  /etc/pricewatch/pricewatch.env
sudo install -m 0644 /opt/pricewatch/deploy/pricewatch.service \
  /etc/systemd/system/pricewatch.service
sudo systemctl daemon-reload
sudo systemctl enable --now pricewatch
sudo systemctl status pricewatch --no-pager
curl --fail http://127.0.0.1:8080/health
```

The service runs migrations before starting, as the unprivileged `pricewatch` user. It binds `0.0.0.0:8080`. Open `http://LXC_IP:8080` on your LAN. The service has a read-only system filesystem except its state directory, private temporary directory and restrictive file permissions. If the user already exists, skip `useradd`.

```bash
sudo journalctl -u pricewatch -f
sudo systemctl restart pricewatch
```

Before upgrades, take a backup, stop the service, update application code/dependencies, and restart. Do not overwrite `/etc/pricewatch/pricewatch.env` or `/var/lib/pricewatch` on upgrades. Schema upgrades run automatically at service start. There is no Docker or Kubernetes requirement.

### Optional Playwright

Normal HTTP is preferred. Enable browser rendering only when a specific retailer needs it. Browser fallback is serialized, blocks off-domain requests, and stops on human-verification challenges. It uses more memory than HTTP parsing.

```bash
sudo /opt/pricewatch/.venv/bin/pip install '/opt/pricewatch[browser]'
sudo /opt/pricewatch/.venv/bin/python -m playwright install-deps chromium
sudo -u pricewatch env PLAYWRIGHT_BROWSERS_PATH=/var/lib/pricewatch/browsers \
  /opt/pricewatch/.venv/bin/python -m playwright install chromium
sudo systemctl restart pricewatch
```

Then enable the browser fallback in Settings. For local development use `.venv/bin/pip install -e '.[browser]'` and `.venv/bin/python -m playwright install chromium`. Browser installation details: [Playwright browser documentation](https://playwright.dev/python/docs/browsers).

## Back up and restore

Use SQLite's backup API while the service is running. Copying only the `.db` file while WAL is active can lose recent transactions.

```bash
sudo install -d -m 0700 -o pricewatch -g pricewatch /var/lib/pricewatch/backups
sudo -u pricewatch /opt/pricewatch/.venv/bin/python /opt/pricewatch/scripts/backup.py \
  /var/lib/pricewatch/pricewatch.db \
  /var/lib/pricewatch/backups/pricewatch-$(date +%Y%m%d-%H%M%S).db
```

Keep backups outside the LXC too. Store `/etc/pricewatch/pricewatch.env` securely with the database backup. To restore a chosen backup, stop the service first and move the current database plus its WAL/SHM files out of the way (keep them for rollback):

```bash
sudo systemctl stop pricewatch
pricewatch_restore_dir="/var/lib/pricewatch/pre-restore-$(date +%Y%m%d-%H%M%S)"
sudo install -d -m 0700 "$pricewatch_restore_dir"
for pricewatch_file in /var/lib/pricewatch/pricewatch.db /var/lib/pricewatch/pricewatch.db-wal /var/lib/pricewatch/pricewatch.db-shm; do
  if sudo test -f "$pricewatch_file"; then
    sudo mv "$pricewatch_file" "$pricewatch_restore_dir/"
  fi
done
sudo install -m 0600 -o pricewatch -g pricewatch /path/to/chosen-backup.db \
  /var/lib/pricewatch/pricewatch.db
sudo systemctl start pricewatch
```

