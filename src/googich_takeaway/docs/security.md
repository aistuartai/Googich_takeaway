# Security and backups

## What is protected, and how

- **Your password** is stored as an Argon2 hash. Failed logins are slowed down, and sessions end
  after seven days or when you log out.
- **Credentials** you enter (the Immich API key, Google service account keys, the SMB password,
  notification addresses) are encrypted with AES-256-GCM before they are stored, and the
  interface never shows them again.
- **The master key** that encrypts them is read from a Docker secret (`secrets/master.key` in the
  example `compose.yaml`), not from the database.
- **Google access** is read-only, limited to the folder you share with the service account.
- **Logs** pass through a filter that removes keys, tokens and passwords before anything is
  written.
- **The web interface** refuses requests from other sites, sends a strict Content Security Policy,
  and runs no third-party scripts.
- **The container** runs as an unprivileged user, with a read-only file system and no Linux
  capabilities.

## First start

Until a password is set, the app asks for a one-time setup token shown in its log, so nobody else
on the network can claim a fresh install. Set the password straight after starting it.

## Reaching it from outside your network

The interface is meant for your local network. To reach it from elsewhere, put it behind a
reverse proxy with HTTPS (Nginx Proxy Manager, Caddy, Traefik). It works behind a proxy without
changes; the proxy must pass the original `Host` header, which Nginx Proxy Manager does by
default.

Setting `GOOGICH_TRUSTED_PROXIES` to the proxy's address is recommended: the session cookie is
then marked secure, and failed logins are slowed per visitor instead of for everyone coming
through the proxy. Only that address's `X-Forwarded-Proto` and `X-Forwarded-For` headers are
believed. In `compose.yaml`:

```yaml
    environment:
      GOOGICH_TRUSTED_PROXIES: 192.168.1.10   # the proxy's address; separate several with commas
```

## Backups

Back up the **`data` folder**: it holds the database (what was downloaded and uploaded, your
settings and encrypted credentials) and the logs.

> [!IMPORTANT]
> Keep **`secrets/master.key`** somewhere separate and safe. Without it the stored credentials
> cannot be read, and you would enter them again. Stored next to the database in the same
> backup, it lets anyone with the backup read the credentials.

If a backup containing both was exposed, replace the credentials: create a new Immich API key and
a new service account key, and change the SMB password.

## Keeping the master key out of Proxmox backups

If the app runs in a Proxmox container that is backed up whole, every backup holds the master
key next to the credentials it encrypts. Move the key onto the Proxmox host and mount it back in:
Proxmox never includes bind mounts in container backups. On the Proxmox host, as root, with the
container stopped (123 and `/opt/googich` are examples):

```bash
CT=123
install -d -m 700 /srv/googich-secrets
pct pull $CT /opt/googich/secrets/master.key /srv/googich-secrets/master.key
# The container's uid 10001 is uid 110001 on the host in an unprivileged container.
chown -R 110001:110001 /srv/googich-secrets && chmod 600 /srv/googich-secrets/master.key
pct exec $CT -- rm /opt/googich/secrets/master.key
pct set $CT -mp0 /srv/googich-secrets,mp=/opt/googich/secrets
pct start $CT
```

If the app inside the container runs as a different uid, add 100000 to it for the host-side
owner. Backups taken before the move still contain the key; delete them, or replace the stored
credentials so the old copies become useless.
