---
type: "agent_requested"
description: "Go service coding guidelines"
---

# Go 1.27 Service Engineering: The Standard-Library-First Reference

Go 1.27 (released August 2026) is the current stable release. Write Go services as if the standard library is your framework: `net/http` for routing and servers, `log/slog` for logging, `context` for cancellation, `database/sql`+`pgx` (or pgx native) for PostgreSQL, and the `testing` package for tests. The language is small and the toolchain is exceptional; the value you add is idiomatic, boring, correct code. Optimize for: explicit error wrapping, context propagation on every I/O boundary, zero-allocation-conscious hot paths, and tests that use fake clocks (`testing/synctest`) instead of real sleeps.

The single biggest way agents write wrong-but-plausible Go is by importing habits from other ecosystems: reaching for a heavyweight web framework (gin/echo/fiber) when `net/http`'s enhanced `ServeMux` (Go 1.22) already routes by method and path wildcards; using `interface{}` instead of `any`; `io/ioutil` instead of `io`/`os`; `github.com/pkg/errors` instead of `fmt.Errorf("%w")`; `golang.org/x/exp/slices` instead of the stdlib `slices`; `math/rand` instead of `math/rand/v2`; `gorilla/mux` instead of stdlib routing; and `github.com/golang/mock` (archived June 2023) instead of `go.uber.org/mock`. This document shows the current, correct way once.

- **Research date:** 2026-08-22
- **Research basis:** current official docs, release notes, specifications, changelogs, and primary repositories.

## Language core: generics, `any`, iterators, and modern built-ins

Use `any`, never `interface{}` — they are identical, but `any` (Go 1.18) is the idiom and `gofmt`'s rewrite rules will convert it. Generics are first-class; Go 1.27 adds **generic methods** (a method may declare its own type parameters), removing the old need to hoist generic helpers to package scope.

```go
package collections

import "cmp"

// Generic function with a type constraint.
func Max[T cmp.Ordered](xs []T) (T, bool) {
	var zero T
	if len(xs) == 0 {
		return zero, false
	}
	m := xs[0]
	for _, x := range xs[1:] {
		if x > m {
			m = x
		}
	}
	return m, true
}

// Generic method (Go 1.27): the method introduces its own type parameter.
type Registry struct{ data map[string]any }

func (r *Registry) Get[T any](key string) (T, bool) {
	var zero T
	v, ok := r.data[key]
	if !ok {
		return zero, false
	}
	t, ok := v.(T)
	return t, ok
}
```

Prefer the standard `slices`, `maps`, and `cmp` packages (stable since Go 1.21) over hand-rolled loops or `golang.org/x/exp/slices`. The `x/exp` versions are superseded — do not import them in new code.

```go
import (
	"cmp"
	"slices"
)

type User struct {
	Name string
	Age  int
}

func demo(users []User) {
	// Sort by multiple keys.
	slices.SortFunc(users, func(a, b User) int {
		return cmp.Or(
			cmp.Compare(a.Age, b.Age),
			cmp.Compare(a.Name, b.Name),
		)
	})

	// Binary search, contains, min/max.
	_ = slices.Contains(users, User{"Ana", 30})
	oldest := slices.MaxFunc(users, func(a, b User) int { return cmp.Compare(a.Age, b.Age) })
	_ = oldest
}
```

### Range-over-func iterators (`iter`, stable Go 1.23)

Iterators are functions of type `iter.Seq[V]` (`func(yield func(V) bool)`) or `iter.Seq2[K,V]`. Return one from a method named `All` (or a descriptive name) and callers `range` over it. `yield` returning `false` means the consumer stopped early — you must return immediately.

```go
package store

import "iter"

type Set[E comparable] struct{ m map[E]struct{} }

// All returns an iterator over the set's elements. Convention: name it All.
func (s *Set[E]) All() iter.Seq[E] {
	return func(yield func(E) bool) {
		for e := range s.m {
			if !yield(e) { // consumer broke out of the loop
				return
			}
		}
	}
}

// Filter composes over any Seq without allocating an intermediate slice.
func Filter[E any](seq iter.Seq[E], keep func(E) bool) iter.Seq[E] {
	return func(yield func(E) bool) {
		for v := range seq {
			if keep(v) && !yield(v) {
				return
			}
		}
	}
}
```

Use `iter.Pull` only when you need to drive two iterators in lockstep or look ahead; always `defer stop()`. Prefer push-style `range` for everything else.

### `unique` (Go 1.23) and other modern helpers

`unique.Make` interns comparable values, returning a `unique.Handle[T]` that compares cheaply by pointer identity and lets the GC collect canonical entries via weak references. Use it for high-cardinality repeated values (labels, zone names) — not as a default.

```go
import "unique"

type Label struct{ Key, Value string }

var h1 = unique.Make(Label{"env", "prod"})
var h2 = unique.Make(Label{"env", "prod"})
// h1 == h2 is a fast pointer comparison; h1.Value() returns the Label.
```

Use `math/rand/v2` (not `math/rand`) — it has a better API (`rand.IntN`, `rand.N[T]`) and is auto-seeded. For struct fields shared with C or hardware layouts, embed `structs.HostLayout` (Go 1.23) to signal host memory layout.

## HTTP services: routing, server config, and graceful shutdown

**Use the standard library `net/http` for routing.** Since Go 1.22, `ServeMux` matches HTTP methods and path wildcards, which eliminates the historical reason to reach for a third-party router. Do not add gin, echo, fiber, chi, or gorilla/mux to a new service by reflex.

Decision table:

| Need | Use |
|---|---|
| Routing by method + path params, middleware | stdlib `net/http.ServeMux` |
| net/http-compatible router with sub-router grouping, large middleware set | `chi` (only if you truly need grouping ergonomics) |
| Batteries-included (binding+validation), large ecosystem, single service | `gin` |
| fasthttp throughput, willing to lose net/http compatibility & HTTP/2 | `fiber` (rarely worth it) |
| gorilla/mux | Do not use for new code — stdlib mux covers it |

Wildcards must span whole path segments: `/users/{id}`, `/files/{path...}` (trailing catch-all), and `{$}` for exact-match end. Extract with `r.PathValue`. More specific patterns win; conflicts panic at registration.

```go
package main

import (
	"encoding/json"
	"log/slog"
	"net/http"
)

func newRouter(app *App) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	})
	mux.HandleFunc("GET /users/{id}", app.getUser)
	mux.HandleFunc("POST /users", app.createUser)
	// Wrap the whole mux in middleware (see below).
	return recoverMW(logMW(mux))
}

func (a *App) getUser(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	u, err := a.svc.User(r.Context(), id)
	if err != nil {
		writeErr(w, r, err)
		return
	}
	writeJSON(w, http.StatusOK, u)
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
```

Middleware is just `func(http.Handler) http.Handler` — no framework needed:

```go
func logMW(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		sw := &statusWriter{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(sw, r)
		slog.InfoContext(r.Context(), "http",
			"method", r.Method, "path", r.URL.Path,
			"status", sw.status, "dur", time.Since(start))
	})
}

type statusWriter struct {
	http.ResponseWriter
	status int
}

func (s *statusWriter) WriteHeader(code int) {
	s.status = code
	s.ResponseWriter.WriteHeader(code)
}
```

**Always configure `http.Server` timeouts** — the zero-value server has none, which is a production DoS risk. Implement graceful shutdown with signal handling and a shutdown context.

```go
func run(ctx context.Context, h http.Handler) error {
	srv := &http.Server{
		Addr:              ":8080",
		Handler:           h,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      15 * time.Second,
		IdleTimeout:       60 * time.Second,
	}

	ctx, stop := signal.NotifyContext(ctx, os.Interrupt, syscall.SIGTERM)
	defer stop()

	errCh := make(chan error, 1)
	go func() { errCh <- srv.ListenAndServe() }()

	select {
	case err := <-errCh:
		if err != nil && !errors.Is(err, http.ErrServerClosed) {
			return err
		}
		return nil
	case <-ctx.Done():
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		return srv.Shutdown(shutdownCtx)
	}
}
```

## JSON: `encoding/json` v1, and the new v2

**Critical for Go 1.27:** `encoding/json/v2` and `encoding/json/jsontext` are now in the standard library, and the classic `encoding/json` package is now *backed by the v2 implementation*. The v1 API is fully supported and you are not required to migrate. Per the official Go 1.27 Release Notes: "Marshal performance is broadly at parity with the previous implementation, while unmarshal performance is significantly faster" (the go-json-experiment jsonbench suite measures v2 unmarshaling at roughly 2.7×–10.2× faster than v1). Error message text may differ slightly even if you never touch v2.

v2 chooses stricter, more interoperable defaults than v1: it **rejects invalid UTF-8** in strings and **rejects duplicate object member names**. It also matches field names case-sensitively. This matters for security: v1's case-insensitive matching produced real auth/parser-differential bugs — e.g. CVE-2026-27896 in the Model Context Protocol Go SDK (<1.3.1), where case-insensitive matching violated JSON-RPC 2.0's exact-match requirement and let a malicious peer send messages with non-standard casing to bypass intermediary inspection (documented in Trail of Bits' June 2025 "Unexpected security footguns in Go's parsers"). Note that v1's duplicate-key handling silently takes the *last* value with no way to prevent it — v2 rejects duplicates outright. Use v2 for new strict API boundaries; it accepts variadic `Options`.

```go
import (
	json "encoding/json/v2"
	"encoding/json/jsontext" // low-level token/value streaming
)

type CreateUser struct {
	Name  string `json:"name"`
	Email string `json:"email"`
}

func decode(r io.Reader) (CreateUser, error) {
	var in CreateUser
	// UnmarshalRead reads and decodes in one call; rejects duplicate keys & bad UTF-8.
	if err := json.UnmarshalRead(r, &in); err != nil {
		return CreateUser{}, fmt.Errorf("decode body: %w", err)
	}
	return in, nil
}
```

The `format` struct tag for custom time layouts is not part of the 1.27 release; `time.Time` marshals as RFC 3339. If a dependency breaks on the new implementation you may temporarily build with `GOEXPERIMENT=nojsonv2` to restore the pure v1 engine — this opt-out is expected to be removed in a future release, so treat it as a migration bridge, not a permanent setting. Do **not** ship production builds that depend on `GOEXPERIMENT` flags for correctness.

## Errors: wrapping, `Is`/`As`/`Join`, and sentinel design

Wrap errors with `fmt.Errorf` and `%w` to preserve the chain; never use `github.com/pkg/errors` (superseded by stdlib since Go 1.13). Inspect with `errors.Is` (sentinel match) and `errors.As` (type match). Combine multiple errors with `errors.Join` (Go 1.20).

```go
import "errors"

var ErrNotFound = errors.New("not found")

func (s *Service) User(ctx context.Context, id string) (User, error) {
	u, err := s.repo.ByID(ctx, id)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return User{}, fmt.Errorf("user %s: %w", id, ErrNotFound)
		}
		return User{}, fmt.Errorf("load user %s: %w", id, err)
	}
	return u, nil
}

// Custom error carrying structured data; implement Error() and let As() extract it.
type ValidationError struct {
	Field, Msg string
}

func (e *ValidationError) Error() string { return e.Field + ": " + e.Msg }

func handle(err error) {
	var ve *ValidationError
	if errors.As(err, &ve) {
		// use ve.Field
	}
	// Aggregate independent failures:
	_ = errors.Join(errValidateName(), errValidateEmail())
}
```

Map errors to HTTP status at the edge, once:

```go
func writeErr(w http.ResponseWriter, r *http.Request, err error) {
	var ve *ValidationError
	switch {
	case errors.Is(err, ErrNotFound):
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "not found"})
	case errors.As(err, &ve):
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": ve.Error()})
	default:
		slog.ErrorContext(r.Context(), "unhandled", "err", err)
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal"})
	}
}
```

## Structured logging with `log/slog`

`log/slog` (Go 1.21) is the standard structured logger — do not add zap or zerolog to a new service unless you have a measured hot-path bottleneck. Use `slog.NewJSONHandler` in production and text in dev; set a default with `slog.SetDefault`. Always call the `Context` variants (`InfoContext`, `ErrorContext`) so trace correlation and handler middleware can read from context.

```go
func initLogger(production bool) *slog.Logger {
	opts := &slog.HandlerOptions{Level: slog.LevelInfo}
	var h slog.Handler
	if production {
		h = slog.NewJSONHandler(os.Stdout, opts)
	} else {
		h = slog.NewTextHandler(os.Stdout, opts)
	}
	l := slog.New(h)
	slog.SetDefault(l)
	return l
}
```

Prefer strongly-typed attributes (`slog.String`, `slog.Int`) in hot paths to avoid the any-boxing of loose key-value pairs. Attach request-scoped fields with `logger.With(...)`. Pass the error object itself, not its string. Implement `slog.LogValuer` to redact sensitive types automatically:

```go
type Password string

func (Password) LogValue() slog.Value { return slog.StringValue("REDACTED") }

func work(ctx context.Context, l *slog.Logger, u User) {
	l = l.With(slog.String("user_id", u.ID))
	if err := doThing(ctx); err != nil {
		l.ErrorContext(ctx, "do thing failed", slog.Any("err", err))
	}
}
```

## Concurrency: context, errgroup, channels, and once helpers

Propagate `context.Context` as the first parameter of every function that does I/O or can block; never store it in a struct. Cancel derived contexts with `defer cancel()`. Respect `ctx.Err()` at loop and select boundaries.

For fan-out with error propagation and bounded concurrency use `golang.org/x/sync/errgroup`. `errgroup.WithContext` cancels the group on the first error; `SetLimit(n)` bounds active goroutines.

```go
import "golang.org/x/sync/errgroup"

func fetchAll(ctx context.Context, ids []string, fetch func(context.Context, string) (Item, error)) ([]Item, error) {
	g, ctx := errgroup.WithContext(ctx)
	g.SetLimit(8) // bound concurrency to protect downstream

	out := make([]Item, len(ids))
	for i, id := range ids { // Go 1.22+: loop var is per-iteration, no shadowing needed
		g.Go(func() error {
			item, err := fetch(ctx, id)
			if err != nil {
				return fmt.Errorf("fetch %s: %w", id, err)
			}
			out[i] = item // distinct index per goroutine → no lock needed
			return nil
		})
	}
	if err := g.Wait(); err != nil {
		return nil, err
	}
	return out, nil
}
```

Use `sync.OnceFunc` / `sync.OnceValue` (Go 1.21) instead of hand-written `sync.Once` + boolean for lazy init:

```go
var loadConfig = sync.OnceValue(func() Config {
	return mustParseConfig()
})
```

Note (Go 1.27 runtime): channels created by `time` (e.g. `time.After`, `time.Tick`) are now always unbuffered/synchronous, and a new `goroutineleak` profile (`/debug/pprof/goroutineleak`) reports goroutines permanently blocked on unreachable primitives — use it to catch leaks in tests and staging. Prefer `context`-scoped cancellation over `time.After` in select loops to avoid timer leaks.

## Data layer: PostgreSQL with pgx

For PostgreSQL, use `github.com/jackc/pgx/v5` — `v5` is the current stable major (it supports the two most recent Go releases and PostgreSQL 14+). Two modes:

| Mode | Import | When |
|---|---|---|
| Native pgx + pool | `github.com/jackc/pgx/v5/pgxpool` | New service, PostgreSQL-only, want binary protocol, `COPY`, `LISTEN/NOTIFY`, best scan performance |
| `database/sql` adapter | `github.com/jackc/pgx/v5/stdlib` | Need `database/sql` interface for shared tooling/libraries |

Prefer native `pgxpool` for a new PostgreSQL service — it bypasses `database/sql` reflection and uses the binary protocol. Every production app should use the pool, not raw `pgx.Conn`. lib/pq is effectively in maintenance mode; do not choose it for new code.

```go
import (
	"context"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

func newPool(ctx context.Context, dsn string) (*pgxpool.Pool, error) {
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		return nil, fmt.Errorf("parse dsn: %w", err)
	}
	cfg.MaxConns = 10
	cfg.MinConns = 2
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return nil, fmt.Errorf("connect: %w", err)
	}
	if err := pool.Ping(ctx); err != nil {
		return nil, fmt.Errorf("ping: %w", err)
	}
	return pool, nil
}

type UserRepo struct{ pool *pgxpool.Pool }

func (r *UserRepo) ByID(ctx context.Context, id string) (User, error) {
	rows, _ := r.pool.Query(ctx, `SELECT id, name, email FROM users WHERE id=$1`, id)
	u, err := pgx.CollectExactlyOneRow(rows, pgx.RowToStructByName[User])
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return User{}, ErrNotFound
		}
		return User{}, fmt.Errorf("query user: %w", err)
	}
	return u, nil
}
```

Always use parameterized queries (`$1`, `$2`) — never string-concatenate SQL. Use `pgx.CollectRows` with `RowToStructByName` for scanning. Wrap multi-statement writes in a transaction with `pool.Begin` and `defer tx.Rollback(ctx)` (rollback after a successful commit is a safe no-op).

For migrations, `pressly/goose` and `golang-migrate/migrate` are the two dominant tools. Recommendation: use **goose** for services that want SQL migrations plus optional Go migrations and embeddable migrations via `//go:embed`; use **golang-migrate** if you need language-agnostic CLI migrations shared across a polyglot team. `tern` is a reasonable pgx-native alternative but has a smaller community.

```sql
-- migrations/00001_create_users.sql
-- +goose Up
CREATE TABLE users (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    email      TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- +goose Down
DROP TABLE users;
```

## Testing: table-driven, httptest, synctest, golden files

Use the standard `testing` package with table-driven subtests as the default. On the testify-vs-stdlib debate: `github.com/stretchr/testify` remains the mainstream pragmatic choice for assertions, but idiomatic Go leans toward plain stdlib table tests. Recommendation for new code: **prefer stdlib `testing` with table-driven subtests**; add testify only where a repo already standardizes on it.

```go
func TestClassify(t *testing.T) {
	tests := []struct {
		name string
		in   int
		want string
	}{
		{"negative", -1, "neg"},
		{"zero", 0, "zero"},
		{"positive", 5, "pos"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := Classify(tt.in); got != tt.want {
				t.Errorf("Classify(%d) = %q, want %q", tt.in, got, tt.want)
			}
		})
	}
}
```

Use `t.Context()` (Go 1.24) for a context auto-canceled at test end, `t.Chdir` (Go 1.24) for working-directory isolation (panics in parallel tests — the CWD is process-global), and `t.TempDir()` for scratch files.

Test HTTP handlers with `net/http/httptest`:

```go
func TestGetUser(t *testing.T) {
	app := newTestApp(t)
	req := httptest.NewRequest(http.MethodGet, "/users/42", nil)
	rr := httptest.NewRecorder()
	newRouter(app).ServeHTTP(rr, req)

	if rr.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rr.Code)
	}
}
```

**Use `testing/synctest` (stable since Go 1.25) for concurrent and time-dependent tests.** `synctest.Test` runs code in a "bubble" with a fake clock; time only advances when every goroutine is durably blocked, so a test of a 30-second timeout runs in microseconds and is deterministic (no flakes). `synctest.Wait` blocks until all bubble goroutines are durably blocked.

```go
import (
	"testing"
	"testing/synctest"
	"time"
)

func TestTimeout(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		start := time.Now()
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()

		<-ctx.Done() // returns "instantly" in fake time
		if elapsed := time.Since(start); elapsed != 30*time.Second {
			t.Fatalf("elapsed = %v, want 30s", elapsed)
		}
	})
}
```

Go 1.27's `httptest.NewTestServer` provides an in-memory fake network suitable for use inside a synctest bubble. Write benchmarks with `for b.Loop()` (Go 1.24), which excludes setup from timing and prevents the compiler from optimizing away the loop body — do not use the old `for range b.N` form for new benchmarks. Run tests with `-race` in CI. For golden-file tests, gate updates behind a flag: `if *update { os.WriteFile(golden, got, 0o644) }`.

## Filesystem safety with `os.Root`

Use `os.Root` / `os.OpenRoot` (Go 1.24) whenever handling user-controlled paths (uploads, archive extraction, static file serving). All operations are confined to the directory and symlinks pointing outside are refused, defeating `../` path-traversal. It does not defend against a badly chosen root (never use `/`) or bind mounts.

```go
func serveUserFile(root *os.Root, name string) ([]byte, error) {
	f, err := root.Open(name) // cannot escape the root directory
	if err != nil {
		return nil, fmt.Errorf("open %q: %w", name, err)
	}
	defer f.Close()
	return io.ReadAll(f)
}

// One-shot helper:
// f, err := os.OpenInRoot("/srv/uploads", userName)
```

## Modules, workspaces, and tooling directives

Manage dependencies with Go modules; `go.mod` declares the minimum Go version. Use `go.work` for multi-module local development. Key commands:

```bash
go mod init example.com/svc
go get github.com/jackc/pgx/v5@latest
go mod tidy         # Go 1.27: merges duplicate require blocks into two (direct/indirect)
go get go@1.27.0    # bump the module's Go directive
```

**Manage developer tools with the `tool` directive (Go 1.24)** instead of the old `tools.go` blank-import hack. Add tools with `go get -tool`; run them with `go tool`. This pins tool versions in `go.mod` and keeps CI reproducible.

```bash
go get -tool go.uber.org/mock/mockgen@latest
go get -tool github.com/pressly/goose/v3/cmd/goose@latest
go tool mockgen -source=internal/svc.go -destination=internal/mocks/svc.go -package=mocks
```

```
// go.mod excerpt
module example.com/svc

go 1.27.0

tool (
	go.uber.org/mock/mockgen
	github.com/pressly/goose/v3/cmd/goose
)
```

To avoid tool deps clashing with app deps, keep them in a separate module file: `go get -tool -modfile=tools.go.mod ...`. Use `//go:embed` for bundling static assets and migrations into the binary. `go doc` now supports `package@version` syntax (Go 1.27).

## Linting and formatting

Use `gofmt` (always) plus `golangci-lint` v2 as the aggregator (current release is v2.13.x as of August 2026). The config **requires** an explicit `version: "2"` field; v1 configs will not load in a v2 binary. `golangci-lint fmt` runs formatters (gofmt/goimports/gofumpt). `staticcheck` is bundled as a linter; `go vet` runs as part of `go test`.

Real `.golangci.yml`:

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
    - ineffassign
    - unused
    - misspell
    - bodyclose
    - errorlint      # flags non-%w error wrapping and bad errors.Is/As use
    - sloglint       # enforces consistent slog key style
  exclusions:
    presets:
      - std-error-handling
formatters:
  enable:
    - gofmt
    - goimports
```

Add `gofumpt` for stricter formatting if the team wants it. Commands: `golangci-lint run`, `golangci-lint run --fix`, `golangci-lint fmt`.

## Cryptography, FIPS, and post-quantum

Go 1.27 adds `crypto/mldsa` (post-quantum ML-DSA signatures, FIPS 204), with ML-DSA support in `crypto/x509` and `crypto/tls` (`MLDSA44`/`MLDSA65`/`MLDSA87`). FIPS 140-3 mode is selected via the `GOFIPS140` build variable and `fips140` GODEBUG (Go 1.24+). Use standard-library crypto; never roll your own. For UUIDs, Go 1.27 adds a standard-library `uuid` package — prefer it over `github.com/google/uuid` for new code where the stdlib API suffices.

## Runtime and performance

The Green Tea garbage collector is the default GC as of Go 1.26. Per go.dev's official "The Green Tea Garbage Collector" post, it delivers "reductions in garbage collection CPU costs between 10% and 40% in our benchmark suite… A 10% reduction in garbage collection CPU time is roughly the modal improvement," with an additional ~10% from vector/AVX-512 scanning on Zen 4/Ice Lake and newer CPUs (the tile38 benchmark showed a 35% reduction). Go 1.27 adds size-specialized allocation that, per the release notes, cuts the cost of some small allocations (under 80 bytes) by up to 30 percent. Use **profile-guided optimization (PGO)**: drop a `default.pgo` in the main package directory and `go build` picks it up automatically to inline hot functions.

Do **not** ship production code depending on experimental features: the `simd`/`simd/archsimd` packages require `GOEXPERIMENT=simd` and have unstable APIs — do not use them in production services. Treat any `GOEXPERIMENT`-gated behavior as non-production.

## Ecosystem libraries: mocking, validation, config, observability

| Concern | Use | Avoid / superseded |
|---|---|---|
| Mocking | `go.uber.org/mock` (mockgen) | `github.com/golang/mock` (archived June 2023) |
| Assertions | stdlib `testing` (default); `stretchr/testify` if team standard | — |
| Validation | `github.com/go-playground/validator/v10` | hand-rolled ad hoc checks |
| Config (env) | `github.com/caarlos0/env` or `kelseyhightower/envconfig` | Viper just to read a few env vars |
| Config (multi-source/file) | `spf13/viper` or `knadh/koanf` | — |
| Tracing/metrics | `go.opentelemetry.io/otel` + `otelhttp` | — |
| HTTP routing | stdlib `net/http` | `gorilla/mux` |

The original `github.com/golang/mock` is archived; its README directs users to `go.uber.org/mock` (the maintained Uber fork, current v0.6.0, Aug 2026). Generate type-safe mocks with the `-typed` flag:

```go
//go:generate go tool mockgen -source=svc.go -destination=mocks/svc.go -package=mocks
func TestHandler(t *testing.T) {
	ctrl := gomock.NewController(t)
	m := mocks.NewMockTranslator(ctrl)
	m.EXPECT().Translate("es").Return("Hola")
	// inject m ...
}
```

Validation (`go-playground/validator/v10`, current v10.30.x); opt into the future-default with `WithRequiredStructEnabled`:

```go
import "github.com/go-playground/validator/v10"

type CreateUser struct {
	Name  string `validate:"required"`
	Email string `validate:"required,email"`
	Age   uint8  `validate:"gte=0,lte=130"`
}

var validate = validator.New(validator.WithRequiredStructEnabled())

func (c CreateUser) Valid() error { return validate.Struct(c) }
```

OpenTelemetry: the core API/SDK use `v1.x` (current v1.44.0) while contrib instrumentation like `otelhttp` uses `v0.x` (current v0.66.0) — they release together but carry different version numbers, so do not expect `otelhttp` to share the core version.

```go
import "go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"

handler := otelhttp.NewHandler(mux, "my-service")
http.ListenAndServe(":8080", handler)
```

Config: there is no single winner. For a typical env-driven ("twelve-factor") service, prefer a lightweight struct-tag loader — `github.com/caarlos0/env` or `github.com/kelseyhightower/envconfig` — and avoid pulling in Viper just to read a handful of env vars. Reach for `spf13/viper` or the lighter, singleton-free `knadh/koanf` only when you genuinely need multi-source or file-based config.

## Docker: multi-stage distroless build

Build a static binary with `CGO_ENABLED=0` and ship it on a distroless static (or `scratch`) base. Use build cache mounts and `-trimpath` for reproducibility. Run as nonroot.

```dockerfile
# syntax=docker/dockerfile:1
FROM golang:1.27 AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN --mount=type=cache,target=/go/pkg/mod go mod download
COPY . .
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 GOOS=linux go build \
      -trimpath -ldflags="-s -w" -o /server ./cmd/server

FROM gcr.io/distroless/static-debian13:nonroot
COPY --from=build /server /server
USER nonroot:nonroot
EXPOSE 8080
ENTRYPOINT ["/server"]
```

`CGO_ENABLED=0` yields a fully static binary with no libc dependency. `-s -w` strips the symbol table and DWARF debug info, cutting binary size by roughly 25%. If you need HTTPS from a `scratch` image, copy `/etc/ssl/certs/ca-certificates.crt` from the build stage — distroless static already includes CA certs and a nonroot user (UID 65532).

## Project layout

There is no official Go project layout. **`github.com/golang-standards/project-layout` is not an official standard** — do not treat it as authoritative. Keep it simple: `cmd/<binary>/main.go` for entry points, `internal/` for private packages the compiler forbids importing externally, and a flat package structure named for what packages *do* (not layer names like `models`/`utils`). Put the wireup (`run(ctx)`) in `main` and keep business logic in testable packages.

```
svc/
  cmd/server/main.go
  internal/
    user/        # domain + service + repo for users
    httpapi/     # handlers, router, middleware
  migrations/    # //go:embed goose SQL
  go.mod
  .golangci.yml
  Dockerfile
```

## Anti-patterns to avoid

| Wrong | Right |
|---|---|
| `interface{}` | `any` |
| `ioutil.ReadAll` / `ioutil.ReadFile` | `io.ReadAll` / `os.ReadFile` |
| `github.com/pkg/errors` `.Wrap` | `fmt.Errorf("...: %w", err)` |
| `golang.org/x/exp/slices` | stdlib `slices` |
| `math/rand` for new code | `math/rand/v2` |
| `github.com/golang/mock` | `go.uber.org/mock` |
| `gorilla/mux` for new routing | stdlib `net/http.ServeMux` |
| Adding gin/echo/fiber reflexively | stdlib `net/http` first |
| `http.Server{}` with no timeouts | set Read/Write/Idle timeouts |
| `time.After` in a select loop | `context` cancellation / `time.NewTimer` + Stop |
| `for range b.N` benchmark | `for b.Loop()` |
| `time.Sleep` in concurrency tests | `testing/synctest` fake clock |
| Storing `context.Context` in a struct | pass `ctx` as first arg |
| String-concatenated SQL | parameterized `$1` queries |
| `os.Open(userPath)` on untrusted input | `os.Root` / `os.OpenInRoot` |
| Depending on `GOEXPERIMENT`/`simd` in prod | stable stdlib only |
| Treating `golang-standards/project-layout` as official | simple `cmd`/`internal` layout |

## Version & compatibility

| Feature | Introduced / stable |
|---|---|
| Generics, `any` | Go 1.18 |
| `slices`/`maps`/`cmp` | Go 1.21 |
| `log/slog`, `sync.OnceFunc`/`OnceValue` | Go 1.21 |
| Enhanced `ServeMux` routing, `math/rand/v2` | Go 1.22 |
| Range-over-func iterators, `iter`, `unique`, `structs.HostLayout` | Go 1.23 |
| `os.Root`, `tool` directive, `testing.B.Loop`, `t.Context`, `t.Chdir`, generic type aliases | Go 1.24 |
| `testing/synctest` stable | Go 1.25 |
| Green Tea GC default | Go 1.26 |
| Generic methods, `encoding/json/v2`, `crypto/mldsa`, stdlib `uuid`, goroutine leak profile, size-specialized malloc | Go 1.27 |
| golangci-lint v2 config (`version: "2"`) | v2 (2025+), current v2.13.x |
| pgx | v5 (current major) |
| go.uber.org/mock | v0.6.0 |
| go-playground/validator | v10.30.x |
| OpenTelemetry Go (core / otelhttp) | v1.44.0 / v0.66.0 |