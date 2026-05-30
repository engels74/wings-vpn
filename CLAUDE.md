# CLAUDE.md

This file provides guidance to AI coding agents when working in this repository.

## Project overview

This is **Pelican Wings** (Go module `github.com/pelican-dev/wings`), the Pelican server
control plane daemon. It exposes an HTTP API and a built-in SFTP server to manage the full
lifecycle of game servers running as Docker containers, talking back to a Pelican "Panel"
over a remote HTTP API.

This repo (`wings-vpn`) is a fork. Its distinguishing feature over upstream is **Docker
container network mode** (`network.network_mode: container:<name>`), which makes a server
container share another container's network namespace. Most fork-specific logic lives in
`environment/docker/container.go` and `config/config_docker.go`.

Where things live:
- `wings.go` — `main()`; just seeds RNG and calls `cmd.Execute()`.
- `cmd/` — cobra commands; `cmd/root.go` is the boot sequence (config → logging → remote
  client → DB → server manager → cron → SFTP → HTTP).
- `config/` — global singleton configuration. `config/config_docker.go` holds Docker/network config.
- `server/` — `Server` lifecycle, `Manager`, configuration sync, power, console, filesystem.
- `environment/` — abstraction over runtimes; `environment/docker/` is the only implementation.
- `router/` — gin HTTP API (`router.go` wires all routes); `router/websocket/` is the console WS.
- `remote/` — HTTP client to the Panel (`remote.Client` interface in `remote/http.go`).
- `sftp/` — built-in SFTP server.
- `internal/` — cron, database (sqlite via gorm), models, progress, ufs.

## Commands

Requires Go 1.25+. The binary only builds/runs on Linux (it drives Docker).

```bash
go build ./...                       # build all packages (works on any OS)
go test ./...                        # run all tests
go test -race ./...                  # CI parity for races (see Makefile `test`)
go test ./config/ -run TestDockerNetworkConfiguration_IsContainerNetworkMode  # single test
go vet ./...                         # static analysis
gofumpt -w .                         # format (stricter gofmt; see flake.nix)
golangci-lint run                    # meta-linter (available in nix devShell)
make build                           # cross-compile linux amd64+arm64 into build/
```

CI (`.github/workflows/push.yaml`) runs: `go mod download`, build (linux, amd64+arm64,
`CGO_ENABLED=0`), `go test`, then `go test -race` (`CGO_ENABLED=1`, amd64 only). CodeQL also runs.

## High-level architecture

Boot flow (`cmd/root.go` `rootCmdRun`): load config singleton → init logging → build
`remote.New(...)` Panel client → init sqlite DB → build `server.Manager`, which fetches all
servers from the Panel and calls `Manager.InitServer` per server → start cron, SFTP, and the
gin HTTP server.

`Manager.InitServer` (`server/manager.go`) is the wiring hub: it builds a `Server`, calls
`SyncWithConfiguration` (data from the Panel), constructs a `filesystem.Filesystem`, assembles
`environment.Settings` + `environment.NewConfiguration`, then `docker.New(...)` to attach a
Docker `Environment`. Only a Docker environment is supported today — this is hard-coded.

The `environment.ProcessEnvironment` interface (`environment/environment.go`) is the contract
every runtime implements: `Create`, `Attach`, `SendCommand`, `Start`/`Stop` (power), `State`,
`Destroy`, etc. `environment/docker/` implements it; `container.go` builds the container spec,
`api.go` wraps the Docker client (with a faster JSON decoder for `ContainerInspect`).

HTTP routes are all registered in `router/router.go` `Configure`. Three tiers: signed-URL
routes (downloads/uploads), the public JWT-authorized console websocket, and everything else
behind `middleware.RequireAuthorization()`; server-scoped routes additionally use
`middleware.ServerExists()` under the `/api/servers/:server` group.

## Task workflows

**Add an HTTP API endpoint**
1. Add the route in `router/router.go` under the correct tier/group (protected vs server-scoped).
2. Implement the handler in the relevant `router/server_*.go` file; pull the server with the
   existing middleware-provided context (don't re-fetch).
3. Server-scoped routes must be inside the `/api/servers/:server` group so auth + existence checks apply.

**Add a config field**
1. Add the field to the right struct in `config/config.go` (or `config/config_docker.go` for
   Docker), with `yaml:`/`json:` tags and a `default:` tag where appropriate (defaults are
   applied via `creasty/defaults`).
2. Read it anywhere via `config.Get()`; never mutate the returned struct (it's a copy) —
   use `config.Update(func(c *Configuration){...})`.

**Change Docker container creation / networking**
- Edit `environment/docker/container.go` `Create`. Container network mode
  (`container:<name>`) requires special-casing many fields — see the rule below.

## Decision tables

| Situation | Use this | Avoid |
| --- | --- | --- |
| Read configuration | `config.Get()` (returns an immutable copy) | Caching/holding the pointer or mutating it |
| Mutate configuration | `config.Update(func(c){...})` | Writing through `config.Get()` |
| Wrap/return an error | `emperror.dev/errors` (`errors.WithStack`, `errors.Wrap`, `errors.WrapIf`) | stdlib `fmt.Errorf` for internal errors |
| Log | `github.com/apex/log` (`log.WithField(...).Info`) | stdlib `log` / `fmt.Println` |
| JSON encode/decode | `github.com/goccy/go-json` (already the project default) | `encoding/json` in hot paths |
| Talk to the Panel | the `remote.Client` interface (`remote/http.go`) | ad-hoc `http.Client` calls |
| Add a server runtime | implement `environment.ProcessEnvironment` | branching on env type inline |

## Code patterns and examples

Configuration access is via a thread-safe global singleton; the getter returns a copy:

```go
cfg := config.Get()
if a.DefaultMapping.Port != 0 && !cfg.Docker.Network.IsContainerNetworkMode() {
    // ...
}
```

Container network mode detection lives on the config struct (`config/config_docker.go`):

```go
func (c DockerNetworkConfiguration) IsContainerNetworkMode() bool {
    return strings.HasPrefix(c.Mode, "container:") && len(c.Mode) > len("container:")
}
```

## Project-specific rules

- When editing `environment/docker/container.go` `Create`, guard network-related fields with
  `cfg.Docker.Network.IsContainerNetworkMode()`. In container network mode the container
  inherits the target's network namespace, so you must **skip**: the `SERVER_IP=127.0.0.1`→
  interface-IP rewrite, `hostname`/`domainname`, DNS, port bindings, macvlan endpoint config,
  and `ForceOutgoingIP`. Setting any of these in that mode causes a Docker API error or binds
  to a non-existent address.
- `EnvironmentVariables()` returns the Configuration's **shared backing slice**. Before
  mutating it (e.g. the `SERVER_IP` rewrite) call `slices.Clone(...)` first — mutating in place
  races with concurrent readers and persists rewritten values into stored config (see the
  comment block in `container.go` `Create`, fixed in commit `eaf68ed2`).
- Keep this fork's `Network.Mode` semantics in mind: `Mode` doubles as both the legacy default
  network name (`pelican_nw`) and the Docker `--network` value, so `container:<name>` is the
  only value that triggers container-network behavior.

## References

- `README.md` — links to upstream Pelican documentation and Discord.
- `CHANGELOG.md` — release history; release flow is driven by `.github/workflows/release.yaml`
  (tags → binaries + changelog extraction).
- `.augment/rules/go-dev-pro.md` — opinionated modern-Go reference. Note: it advocates
  `log/slog`, stdlib `net/http` routing, and stdlib testing; this codebase predates those
  choices and uses `apex/log`, gin, and `goblin`/`testify` instead. Follow the existing
  project conventions over the generic guide when they conflict.

## Uncertainty

Tests use a mix of `franela/goblin` (BDD style) and `stretchr/testify`. There is no single
canonical test style; match the neighboring `_test.go` file in the package you are editing.
