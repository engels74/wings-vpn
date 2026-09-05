---
type: "agent_requested"
description: "Go 1.27 coding guidelines"
---
# Go 1.27 Production Coding Guidelines

Go 1.27 (released August 2026) is a conservative language on a fast-moving standard library. The compiler, runtime, and `go` tool do most of the heavy lifting; the winning strategy is to lean on the standard library and a small set of maintained tools rather than pulling in frameworks. Go 1.27 is exceptional at network services, CLIs, and concurrent systems that must stay predictable under load: static binaries, a container-aware scheduler, the Green Tea garbage collector (enabled by default since 1.26, which improves marking and scanning of small objects through better locality and CPU scalability), and first-class observability. Optimize for correctness, explicit error and context propagation, clear ownership of goroutines and resources, and zero-dependency solutions where the standard library already suffices.

Agents most often write wrong-but-plausible Go by importing habits from other ecosystems: reaching for `gorilla/mux`, `logrus`, `github.com/pkg/errors`, or `github.com/google/uuid` when the standard library now covers routing, structured logging, error wrapping, and UUIDs; writing exception-style control flow instead of `if err != nil` with `%w` wrapping; leaking goroutines that no one cancels; ignoring `context.Context`; and treating `panic` as an error-handling mechanism. This document shows what current, idiomatic Go looks like so those mistakes are easy to avoid.

## Language baseline and what changed recently

Set the language version in `go.mod` with the `go` directive; it gates which language and standard-library features the compiler and `go vet` permit. `go test` runs the `stdversion` vet analyzer by default in 1.27, so using a symbol newer than your declared `go` version is now a build-time failure rather than a runtime surprise.

Features worth using deliberately, annotated with the release that stabilized them:

- **Generic methods** (stable since 1.27): a method may now declare its own type parameters. Interface methods still may not, and interface methods cannot be implemented by generic methods. Use sparingly — most generic code belongs in package-level functions.
- **Struct-literal field selectors** (stable since 1.27): a composite-literal key may be any valid field selector, including a promoted embedded field, not only a top-level field name.
- **`new(expr)`** (stable since 1.26): `new` accepts an expression and returns a pointer to a variable initialized with it — handy for `*string`/`*int` fields.
- **Range-over-func iterators and `iter`** (stable since 1.23): write `for v := range seq` where `seq` is an `iter.Seq[V]`. Prefer returning `iter.Seq`/`iter.Seq2` for lazy sequences; do not hand-roll channel-based iterators for in-process iteration.
- **`slices` and `maps`** (stable since 1.21): use `slices.Sort`, `slices.Contains`, `slices.SortFunc`, `maps.Keys` (returns an iterator), `slices.Collect`. These replace most hand-written loops and `sort.Slice`.
- **`min`, `max`, `clear` builtins** (stable since 1.21).
- **Per-iteration loop variables** (since 1.22): each iteration gets a fresh loop variable, so the classic "capture the loop variable in a goroutine" bug is gone. Do not add `v := v` shadowing to new code.
- **`math/rand/v2`** (since 1.22; `Rand.N` generic method added 1.27): use it instead of `math/rand`. It has a better API and cannot be seeded into a global insecure default by accident.
- **`unique`** (since 1.23) for interning comparable values, **`weak`** (since 1.24) for weak pointers, **`os.Root`** (since 1.24) for filesystem access confined to a directory tree (use it whenever you open paths derived from untrusted input).
- **Swiss-table maps** (since 1.24): faster maps with no code change.

Excluded from recommended patterns: the `simd` and `simd/archsimd` packages are experimental and gated behind `GOEXPERIMENT=simd` — do not use them in application code. The `runtime/secret` package (secret-erasure) is niche and for cryptographic code only.

## JSON: encoding/json is now v2-backed

In Go 1.27 the classic `encoding/json` package is backed by the new v2 implementation, and `encoding/json/v2` plus its low-level companion `encoding/json/jsontext` are available without any build flag (the `GOEXPERIMENT=jsonv2` gate is gone). Existing `encoding/json` code keeps working: marshaling and unmarshaling behavior is preserved, but the exact text of error messages may differ. Per the release notes, "Marshal performance is broadly at parity with the previous implementation, while unmarshal performance is significantly faster."

For new code, prefer `encoding/json/v2` when you want its stricter, more interoperable defaults — it rejects invalid UTF-8 and duplicate object member names, and takes variadic `Options` instead of relying on struct-tag flags alone.

```go
package catalog

import (
	"encoding/json/v2"
	"fmt"
	"os"
)

type Product struct {
	ID    string   `json:"id"`
	Name  string   `json:"name"`
	Price float64  `json:"price"`
	Tags  []string `json:"tags,omitzero"`
}

// LoadProducts streams-decodes a file of products. UnmarshalRead reads directly
// from an io.Reader without buffering the whole input into a []byte first.
func LoadProducts(path string) ([]Product, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, fmt.Errorf("open catalog: %w", err)
	}
	defer f.Close()

	var products []Product
	if err := json.UnmarshalRead(f, &products); err != nil {
		return nil, fmt.Errorf("decode catalog: %w", err)
	}
	return products, nil
}
```

Note the `omitzero` tag option (preferred over v1's `omitempty`, which has surprising behavior for zero-but-present values). If you must keep exact v1 semantics under the new engine, the `encoding/json` package exposes `Options` to pin them. `GOEXPERIMENT=nojsonv2` restores the original v1 implementation entirely, but per the release notes "This opt-out is expected to be removed in a future release," so do not rely on it.

## Application and HTTP routing architecture

Use the standard library router. Since Go 1.22, `http.ServeMux` supports method matching and path wildcards, which covers the overwhelming majority of routing needs. Reach for `chi` only when you genuinely need composable sub-router mounting with shared middleware groups across many route trees; do not add `gorilla/mux` (archived and superseded) or a web "framework" like Gin/Echo/Fiber for a standard JSON API.

Patterns are `"METHOD /path/{param}"`; `{param}` matches one segment, `{path...}` matches the rest. More specific patterns win, and conflicting registrations panic at startup. Read wildcards with `r.PathValue("param")`.

```go
package main

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"time"
)

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	slog.SetDefault(logger)

	srv := &http.Server{
		Addr:              ":8080",
		Handler:           routes(logger),
		ReadHeaderTimeout: 5 * time.Second,
		IdleTimeout:       60 * time.Second,
	}

	// Graceful shutdown: stop accepting on SIGINT/SIGTERM, drain in-flight requests.
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()

	go func() {
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			logger.Error("server failed", "err", err)
			os.Exit(1)
		}
	}()

	<-ctx.Done()
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		logger.Error("graceful shutdown failed", "err", err)
	}
}

func routes(logger *slog.Logger) http.Handler {
	mux := http.NewServeMux()
	h := &userHandler{logger: logger}
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	})
	mux.HandleFunc("GET /users/{id}", h.getUser)
	mux.HandleFunc("POST /users", h.createUser)

	// Middleware is ordinary handler wrapping — no framework needed.
	return requestLogger(logger, mux)
}

func requestLogger(logger *slog.Logger, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		next.ServeHTTP(w, r)
		logger.InfoContext(r.Context(), "request",
			"method", r.Method, "path", r.URL.Path, "dur", time.Since(start))
	})
}
```

Handlers should decode with `encoding/json/v2`, validate, call a service, and encode a response. Always pass `r.Context()` down; it carries cancellation when the client disconnects.

```go
package main

import (
	"encoding/json/v2"
	"errors"
	"log/slog"
	"net/http"
)

type userHandler struct {
	logger *slog.Logger
	svc    UserService // an interface, injected for testability
}

func (h *userHandler) getUser(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	u, err := h.svc.ByID(r.Context(), id)
	switch {
	case errors.Is(err, ErrNotFound):
		http.Error(w, "user not found", http.StatusNotFound)
		return
	case err != nil:
		h.logger.ErrorContext(r.Context(), "lookup user", "id", id, "err", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	if err := json.MarshalWrite(w, u); err != nil {
		h.logger.ErrorContext(r.Context(), "encode user", "err", err)
	}
}
```

Keep dependencies explicit: a handler struct holds its collaborators as interfaces, wired in `main`. Avoid package-level global state and `init()` side effects.

## Data layer: pgx for PostgreSQL, sqlc for typed queries

For PostgreSQL, use `github.com/jackc/pgx/v5` with its native `pgxpool` connection pool. Prefer pgx's native interface over `database/sql` when the app is PostgreSQL-only — you get binary protocol, batching, `COPY`, and `LISTEN/NOTIFY`. `lib/pq` is in maintenance mode; do not use it. Use `database/sql` + the pgx stdlib adapter only when a dependency requires the `database/sql` interface.

For query mapping, generate type-safe code from SQL with `sqlc` rather than hand-scanning rows or adopting an ORM. GORM is a full ORM for a different style of development; it hides SQL and its performance cliffs make it a poor default for services that care about query shape. Reserve `sqlx` for codebases already committed to `database/sql`.

Pool setup with sane lifecycle:

```go
package store

import (
	"context"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

func NewPool(ctx context.Context, dsn string) (*pgxpool.Pool, error) {
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		return nil, fmt.Errorf("parse dsn: %w", err)
	}
	cfg.MaxConns = 10
	cfg.MinConns = 2
	cfg.MaxConnLifetime = 30 * time.Minute
	cfg.MaxConnIdleTime = 5 * time.Minute
	cfg.HealthCheckPeriod = time.Minute

	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return nil, fmt.Errorf("create pool: %w", err)
	}
	// Fail fast if the database is unreachable at startup.
	pingCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	if err := pool.Ping(pingCtx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("ping: %w", err)
	}
	return pool, nil
}
```

sqlc config (`sqlc.yaml`) targeting pgx/v5:

```yaml
version: "2"
sql:
  - engine: "postgresql"
    queries: "db/query.sql"
    schema: "db/migrations"
    gen:
      go:
        package: "db"
        out: "internal/db"
        sql_package: "pgx/v5"
        emit_pointers_for_null_types: true
```

A query file (`db/query.sql`) drives generation:

```sql
-- name: GetUser :one
SELECT id, email, created_at FROM users WHERE id = $1;

-- name: CreateUser :one
INSERT INTO users (id, email) VALUES ($1, $2)
RETURNING id, email, created_at;
```

Transactions: acquire a `pgx.Tx`, defer a rollback that is a no-op after commit, and pass the tx to sqlc's `WithTx`.

```go
func (s *Store) Transfer(ctx context.Context, from, to string, cents int64) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin: %w", err)
	}
	defer tx.Rollback(ctx) // safe: no-op if already committed

	q := s.queries.WithTx(tx)
	if err := q.Debit(ctx, db.DebitParams{ID: from, Cents: cents}); err != nil {
		return fmt.Errorf("debit: %w", err)
	}
	if err := q.Credit(ctx, db.CreditParams{ID: to, Cents: cents}); err != nil {
		return fmt.Errorf("credit: %w", err)
	}
	return tx.Commit(ctx)
}
```

For primary keys, prefer time-ordered UUIDv7 (`uuid.NewV7()` from the standard library, see below) so B-tree indexes stay compact.

### Migrations

Use `goose` for schema migrations: plain SQL files with `-- +goose Up` / `-- +goose Down` markers, applied by the `goose` CLI or embedded via `embed.FS` and run at deploy time. It is lightweight and gives full control over SQL. `golang-migrate` is a reasonable alternative; pick one and keep the migration directory the single source of schema truth that sqlc reads for type generation.

```sql
-- +goose Up
CREATE TABLE users (
    id         uuid PRIMARY KEY,
    email      text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- +goose Down
DROP TABLE users;
```

## UUIDs: use the standard library

Go 1.27 adds a standard-library `uuid` package implementing RFC 9562 with a cryptographically secure source. Drop `github.com/google/uuid` from new code — the standard type `UUID [16]byte` is comparable, usable as a map key, and convertible to `google/uuid`'s type with a single cast if needed.

```go
import "uuid"

id := uuid.NewV7()          // time-ordered; best for DB keys
random := uuid.NewV4()      // purely random
generic := uuid.New()       // "I want a UUID, don't care which"
zero := uuid.Nil()          // 00000000-0000-0000-0000-000000000000
parsed, err := uuid.Parse("0198a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b")
```

Per the package docs, "NewV7 returns a new version 7 UUID. Version 7 UUIDs contain a timestamp in the most significant 48 bits, and at least 62 bits of random data. NewV7 always returns UUIDs which sort in increasing order, except when the system clock moves backwards" — which is exactly why V7 keeps B-tree primary-key indexes compact. Note `uuid.Nil()` and `uuid.Max()` are functions, not package variables, so they cannot be accidentally reassigned. The package intentionally provides only V4/V7 constructors; for v1/v3/v5 or richer inspection, a third-party library still has a role.

## Errors: wrap with %w, inspect with Is/As/Join

Return errors, do not panic across API boundaries. Wrap with `%w` to preserve the chain, add context at each layer, and let callers inspect with `errors.Is`/`errors.As`. Do not use `github.com/pkg/errors` — its capabilities are in the standard library.

```go
package users

import (
	"context"
	"errors"
	"fmt"
)

var ErrNotFound = errors.New("user not found")

type ValidationError struct {
	Field string
	Msg   string
}

func (e *ValidationError) Error() string {
	return fmt.Sprintf("validation failed on %s: %s", e.Field, e.Msg)
}

func (s *Service) Rename(ctx context.Context, id, name string) error {
	if name == "" {
		return &ValidationError{Field: "name", Msg: "must not be empty"}
	}
	if err := s.repo.UpdateName(ctx, id, name); err != nil {
		return fmt.Errorf("rename user %s: %w", id, err) // wrap, keep the chain
	}
	return nil
}

// Caller side:
func handle(err error) {
	var ve *ValidationError
	switch {
	case errors.As(err, &ve):
		// 400: report ve.Field
	case errors.Is(err, ErrNotFound):
		// 404
	}
}
```

Use `errors.Join` to combine independent failures (for example, several field validations, or cleanup errors alongside a primary error). Add `%w` only when callers should be able to unwrap; a wrapped error is part of your API surface.

## Concurrency: context, errgroup, and clean goroutine ownership

Every goroutine needs a clear owner and a way to stop. Pass `context.Context` as the first parameter of any function that blocks, does I/O, or spawns work, and honor cancellation. For a group of goroutines that can fail, use `errgroup` from `golang.org/x/sync` — it cancels siblings on the first error and bounds concurrency with `SetLimit`.

```go
package fetch

import (
	"context"
	"fmt"
	"net/http"

	"golang.org/x/sync/errgroup"
)

// FetchAll downloads all URLs concurrently, cancels everything on the first
// failure, and never leaks a goroutine because errgroup owns their lifetimes.
func FetchAll(ctx context.Context, urls []string) (map[string]int, error) {
	g, ctx := errgroup.WithContext(ctx)
	g.SetLimit(8) // bound in-flight requests

	results := make([]int, len(urls))
	for i, u := range urls { // per-iteration i, u since Go 1.22 — safe to capture
		g.Go(func() error {
			req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
			if err != nil {
				return fmt.Errorf("build request %s: %w", u, err)
			}
			resp, err := http.DefaultClient.Do(req)
			if err != nil {
				return fmt.Errorf("get %s: %w", u, err)
			}
			defer resp.Body.Close()
			results[i] = resp.StatusCode
			return nil
		})
	}
	if err := g.Wait(); err != nil {
		return nil, err
	}
	out := make(map[string]int, len(urls))
	for i, u := range urls {
		out[u] = results[i]
	}
	return out, nil
}
```

Each goroutine writes to its own slice index (no shared-write race) and the map is built after `Wait`. Use `sync.WaitGroup` (with `WaitGroup.Go`, added in 1.25) for fire-and-collect work that cannot fail; use a buffered semaphore or `errgroup.SetLimit` to bound concurrency; never start a goroutine you cannot stop.

Go 1.27's `goroutineleak` profile — previously an experiment in 1.26 and now generally available, exposed at `/debug/pprof/goroutineleak` via `net/http/pprof` — detects goroutines permanently blocked on unreachable channels or mutexes. Wire up `net/http/pprof` in services and check it when you suspect leaks.

## Logging with log/slog

Use the standard `log/slog` for structured logging. Do not add `logrus` or `zap` for a new service — slog is the ecosystem standard, allocation-light, and integrates with OpenTelemetry. Prefer a JSON handler in production, set a default logger once, and use the `...Context` variants so trace correlation and cancellation metadata flow through.

```go
logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{
	Level: slog.LevelInfo,
}))
slog.SetDefault(logger)

logger.InfoContext(ctx, "order placed",
	"order_id", order.ID,
	"amount_cents", order.AmountCents,
	"user_id", order.UserID,
)
```

Attach request-scoped attributes with `logger.With(...)` and pass the derived logger down. Never log secrets or full request bodies.

## Configuration

Read configuration from environment variables (the twelve-factor default for containers). For a handful of values, `os.Getenv`/`os.LookupEnv` plus a small parser is enough — do not pull in a library. When you need layered sources (env + file + flags) and typed unmarshaling, use `koanf` v2: it is lighter than `viper`, has no global singleton, and only compiles the providers you import. Prefer `viper` only if you specifically need its remote-config/hot-reload feature set.

```go
package config

import (
	"fmt"
	"strings"
	"time"

	"github.com/knadh/koanf/v2"
	"github.com/knadh/koanf/providers/env"
)

type Config struct {
	Addr        string        `koanf:"addr"`
	DatabaseURL string        `koanf:"database_url"`
	Timeout     time.Duration `koanf:"timeout"`
}

func Load() (Config, error) {
	k := koanf.New(".")
	// Map APP_DATABASE_URL -> database_url, etc.
	_ = k.Load(env.Provider("APP_", ".", func(s string) string {
		return strings.ToLower(strings.TrimPrefix(s, "APP_"))
	}), nil)

	cfg := Config{Addr: ":8080", Timeout: 15 * time.Second} // defaults
	if err := k.Unmarshal("", &cfg); err != nil {
		return Config{}, fmt.Errorf("unmarshal config: %w", err)
	}
	if cfg.DatabaseURL == "" {
		return Config{}, fmt.Errorf("APP_DATABASE_URL is required")
	}
	return cfg, nil
}
```

Secrets come from the platform's secret store injected as environment variables; never commit them or bake them into images.

## CLI tools

For a simple tool, the standard `flag` package is sufficient. For multi-command CLIs with subcommands, completion, and generated help, use `cobra` — it is the de facto standard (kubectl, hugo, gh). `urfave/cli` is a lighter alternative with a more compositional API; choose it for smaller tools where cobra's generator scaffolding is overkill.

## Testing

Use the standard `testing` package with table-driven tests and `t.Run` subtests. For comparisons, prefer plain `if got != want` for scalars and `google/go-cmp` for structs, slices, and maps — its diffs are precise and its `cmpopts` handle floats, unexported fields, and unordered collections. Add `testify` only when a team already standardizes on it; do not mix assertion styles within a package.

```go
package users_test

import (
	"testing"

	"github.com/google/go-cmp/cmp"
	"github.com/google/go-cmp/cmp/cmpopts"
)

func TestNormalize(t *testing.T) {
	tests := map[string]struct {
		in   User
		want User
	}{
		"trims and lowercases email": {
			in:   User{Email: "  Alice@Example.COM "},
			want: User{Email: "alice@example.com"},
		},
		"empty stays empty": {
			in:   User{},
			want: User{},
		},
	}
	for name, tc := range tests {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			got := Normalize(tc.in)
			if diff := cmp.Diff(tc.want, got, cmpopts.IgnoreFields(User{}, "CreatedAt")); diff != "" {
				t.Errorf("Normalize() mismatch (-want +got):\n%s", diff)
			}
		})
	}
}
```

Key testing capabilities:

- **`t.Context()`** (since 1.24): a context cancelled just before test cleanup — pass it to code under test instead of `context.Background()`.
- **`testing/synctest`** (GA since 1.25): test concurrent, time-dependent code deterministically. `synctest.Test` runs a function in a bubble with a fake clock that advances instantly when all goroutines block; `synctest.Wait` blocks until every goroutine in the bubble is durably blocked. Go 1.27 adds `synctest.Sleep` (a `time.Sleep` + `synctest.Wait` in one). Use it to test timeouts and tickers without real delays.
- **`net/http/httptest.NewTestServer`** (new in 1.27): an httptest server on an in-memory network that works inside a synctest bubble.
- **Fuzzing** (`func FuzzX(f *testing.F)`) for parsers and anything handling untrusted bytes.
- **Benchmarks with `testing.B.Loop`** (since 1.24): `for b.Loop() { ... }` prevents dead-code elimination and excludes setup from timing — use it instead of `for i := 0; i < b.N; i++`.

```go
func TestTimeout(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		defer cancel()
		start := time.Now()
		<-ctx.Done() // fake clock advances instantly once all goroutines block
		if got := time.Since(start); got != time.Second {
			t.Fatalf("elapsed = %v, want 1s", got)
		}
	})
}

func BenchmarkParse(b *testing.B) {
	data := loadFixture()
	b.ReportAllocs()
	for b.Loop() {
		_, _ = Parse(data)
	}
}
```

For mocking, prefer hand-written fakes or small in-memory implementations of your interfaces — they are clearer and survive refactors. When you need generated, interaction-verifying mocks, use `go.uber.org/mock` (the maintained successor to the archived `github.com/golang/mock`) with `mockgen` and `//go:generate` directives. Run tests with `go test -race ./...` in CI; the race detector catches data races that no unit test will.

## Tooling and toolchain config

**Formatting.** `gofmt` is mandatory and non-negotiable; run `goimports` (or `gofumpt` for a stricter superset) to also manage import grouping. Configure formatters through golangci-lint's `formatters` section so local and CI formatting agree.

**Linting.** Use `golangci-lint` v2 as the single meta-linter; it bundles `staticcheck`, `go vet`, `errcheck`, `gosec`, and dozens more behind one cached runner. The v2 config requires `version: "2"` at the top; a v1 config will not parse. Start with the `standard` set plus a focused selection and grow deliberately — over-enabling drowns signal in noise.

`.golangci.yml`:

```yaml
version: "2"
run:
  timeout: 5m
linters:
  default: standard
  enable:
    - errcheck
    - govet
    - staticcheck
    - revive
    - gosec
    - unconvert
    - unparam
    - misspell
    - bodyclose
  settings:
    errcheck:
      check-type-assertions: true
  exclusions:
    rules:
      - path: _test\.go
        linters:
          - gosec
          - unparam
formatters:
  enable:
    - gofumpt
    - goimports
```

Common commands (put these behind a `Makefile` or `Taskfile`):

```bash
go build ./...
go test -race -cover ./...
go test -json ./...          # machine-readable output; 1.27 adds OutputType annotations
golangci-lint run
golangci-lint fmt            # apply configured formatters
go vet ./...                 # also runs in `go test`
govulncheck ./...            # reachability-aware vulnerability scan
```

**Vulnerability scanning.** Run `govulncheck` (`golang.org/x/vuln`) in CI; unlike generic SCA it reports only vulnerabilities in functions your code actually reaches, so its findings are actionable. Fail the build on new findings.

**Dependencies and tools.** Manage dependencies with Go modules. Pin developer tools as module dependencies using the `tool` directive in `go.mod` (since 1.24) and run them with `go tool`, so every contributor and CI uses the same version — no more blank-import `tools.go` files.

```
// go.mod excerpt
tool (
	github.com/sqlc-dev/sqlc/cmd/sqlc
	go.uber.org/mock/mockgen
	github.com/pressly/goose/v3/cmd/goose
)
```

Run with `go tool sqlc generate`, `go tool mockgen ...`. Use `go mod tidy` to keep requires clean — in 1.27 it also consolidates duplicate require blocks into the standard two-block layout. Use `go.work` only for multi-module local development; do not commit `go.work` for single-module repos.

## Observability with OpenTelemetry

Use the OpenTelemetry Go SDK for traces and metrics (both covered by the project's stability guarantees) and the `otelslog` bridge to route `log/slog` records through the OTel logs pipeline with trace correlation. Instrument HTTP with `otelhttp` from the contrib repository — a one-line handler wrap gives you spans per request. The logs signal API is not yet frozen, so pin versions and read the changelog on upgrade.

```go
import (
	"go.opentelemetry.io/contrib/bridges/otelslog"
	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
)

// Route slog through OpenTelemetry so every log line carries trace_id/span_id.
slog.SetDefault(otelslog.NewLogger("orders-service"))

// Wrap the mux so each request is a span.
handler := otelhttp.NewHandler(routes(logger), "http.server")
```

## Containerization

Build static binaries with `CGO_ENABLED=0` and ship them on a distroless or scratch base for a minimal attack surface. Use a multi-stage build; run as non-root.

```dockerfile
FROM golang:1.27 AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /app ./cmd/server

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /app /app
USER nonroot:nonroot
ENTRYPOINT ["/app"]
```

The runtime is container-aware: since Go 1.25, on Linux the runtime considers the cgroup CPU bandwidth limit, and `GOMAXPROCS` defaults to that limit when it is lower than the number of logical CPUs — so you generally do not need to set it manually. (It tracks the cgroup "CPU limit", not "CPU requests".) For releases and multi-platform builds, `goreleaser` v2 is the standard tool but is optional.

## Anti-patterns to avoid

| Wrong | Why | Right |
|-------|-----|-------|
| `import "github.com/google/uuid"` in new code | Superseded by the standard `uuid` package in 1.27 | `import "uuid"`; use `uuid.NewV7()` |
| `github.com/pkg/errors` for wrapping | Its features are in the standard library | `fmt.Errorf("...: %w", err)`, `errors.Is/As/Join` |
| `logrus`/`zap` for a new service | Not preferred here; slog is the standard | `log/slog` with a JSON handler |
| `gorilla/mux`/Gin for a standard API | gorilla/mux archived; frameworks unnecessary | `http.ServeMux` with `"GET /x/{id}"` patterns |
| `lib/pq` driver | In maintenance mode | `github.com/jackc/pgx/v5` + `pgxpool` |
| `v := v` loop-variable shadowing in new code | Unnecessary since Go 1.22 semantics | Capture the loop variable directly |
| `math/rand` global funcs | Weak API, easy to misuse | `math/rand/v2` |
| Starting a goroutine with no cancellation | Leaks; caught by the `goroutineleak` profile | `errgroup.WithContext` or explicit `ctx` + stop |
| `panic` for expected failures | Panics are for programmer bugs, not control flow | Return an `error`, wrap with `%w` |
| Ignoring `context.Context` on I/O calls | No cancellation, no timeout, no trace propagation | Take `ctx` first arg; use `NewRequestWithContext`, pgx `ctx` methods |
| `for i := 0; i < b.N; i++` in benchmarks | Dead-code elimination and timing pitfalls | `for b.Loop() { ... }` |
| Not closing `resp.Body` / rows | Leaks connections | `defer resp.Body.Close()` / `defer rows.Close()` |
| v1 `.golangci.yml` with golangci-lint v2 | v2 cannot parse v1 config | Add `version: "2"`; migrate with `golangci-lint migrate` |
| `time.Sleep` in concurrency tests | Flaky, slow | `testing/synctest` with the fake clock |

## Version & compatibility

| Component | Target | Notes |
|-----------|--------|-------|
| Go toolchain / language | 1.27 (patch 1.27.1) | Released Aug 2026; set `go 1.27` in `go.mod` |
| Minimum OS (macOS) | macOS 13 Ventura | 1.27 discontinued support for earlier versions |
| github.com/jackc/pgx/v5 | v5.10.0 | Native pgx + pgxpool |
| github.com/sqlc-dev/sqlc | v1.31.1 | Typed query codegen; `sql_package: pgx/v5` |
| github.com/pressly/goose/v3 | v3.28.0 | SQL migrations |
| uuid | stdlib (1.27) | `NewV4`/`NewV7`; no external dep |
| github.com/google/go-cmp | v0.7.0 | Test comparisons |
| github.com/stretchr/testify | v1.11.1 | Only if team standardizes on it |
| go.uber.org/mock | v0.6.0 | Maintained gomock fork + mockgen |
| github.com/knadh/koanf/v2 | v2.3.4 | Layered config |
| golang.org/x/sync | v0.22.0 | errgroup, semaphore |
| go.opentelemetry.io/otel (+ /sdk) | v1.46.0 | Traces/metrics stable; logs API not frozen |
| github.com/golangci/golangci-lint/v2 | v2.13.2 | Config `version: "2"`; bundles staticcheck |
| honnef.co/go/tools (staticcheck) | 2025.1.1 | Runs via golangci-lint |
| golang.org/x/vuln (govulncheck) | v1.7.0 | CI vulnerability scan |
| github.com/goreleaser/goreleaser/v2 | v2.18.0 | Optional release tooling |

- **Research date:** 2026-09-05
