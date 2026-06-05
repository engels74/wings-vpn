# Pelican Wings (`wings-vpn` fork)

Wings is Pelican's server control plane, built for the rapidly changing gaming industry and designed to be
highly performant and secure. Wings provides an HTTP API allowing you to interface directly with running server
instances, fetch server logs, generate backups, and control all aspects of the server lifecycle.

In addition, Wings ships with a built-in SFTP server allowing your system to remain free of Pelican specific
dependencies, and allowing users to authenticate with the same credentials they would normally use to access the Panel.

## About this fork

This repository is a fork of upstream Pelican Wings with support for Docker container network mode. Setting
`docker.network.network_mode` to `container:<name>` makes Wings-created server containers share the network namespace
of another container, such as a reverse proxy or VPN container.

In this mode, Docker networking belongs to the target container. Configure published ports, routing, and firewall/VPN
behavior on that target container rather than on each game server container.

Example `config.yml`:

```yaml
debug: false
uuid: ((REDACTED))
token_id: ((REDACTED))
token: ((REDACTED))
api:
  host: 0.0.0.0
  port: 8080
  ssl:
    enabled: false
    cert: /etc/letsencrypt/live/wings.domain.com/fullchain.pem
    key: /etc/letsencrypt/live/wings.domain.com/privkey.pem
  upload_limit: 256
docker:
  network:
    interface: 172.20.0.1
    name: caddy_backbone
    network_mode: container:caddy
    interfaces:
      v4:
        subnet: 172.20.0.0/24
        gateway: 172.20.0.1
system:
  data: /var/lib/pelican/volumes
  sftp:
    bind_port: 2022
allowed_mounts: []
remote: 'http://panel.domain.com'
allowed_origins:
  - https://panel.domain.com
```

## Documentation

* [Panel Documentation](https://pelican.dev/docs/panel/getting-started)
* [Wings Documentation](https://pelican.dev/docs/wings/install)
* Or, get additional help [via Discord](https://discord.gg/pelican-panel)

## Reporting Issues

Feel free to report fork-specific issues or feature requests in [GitHub Issues](https://github.com/engels74/wings-vpn/issues/new).
