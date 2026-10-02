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
changes. Setting `GOOGICH_TRUSTED_PROXIES` to the proxy's address is recommended: the session
cookie is then marked secure, and failed logins are slowed per visitor instead of for everyone
coming through the proxy.

## Backups

Back up the **`data` folder**: it holds the database (what was downloaded and uploaded, your
settings and encrypted credentials) and the logs.

Keep **`secrets/master.key`** somewhere separate and safe. Without it the stored credentials
cannot be read, and you would enter them again. Stored next to the database in the same backup,
it lets anyone with the backup read the credentials. The README explains how to keep it out of
Proxmox container backups.

If a backup containing both was exposed, replace the credentials: create a new Immich API key and
a new service account key, and change the SMB password.
