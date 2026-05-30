---
type: "agent_requested"
description: "Go coding guidelines"
---
# Go 1.25 — Authoritative Coding Guidelines

Go is a small, statically-typed, garbage-collected language whose distinguishing strength is *production engineering*: fast compiles, a single statically-linked binary, a deep standard library, first-class concurrency, and a culture that prizes readability and explicit error handling over cleverness. Go 1.25 (August 2025) is the current stable release; it ships container-aware `GOMAXPROCS`, a stabilized `testing/synctest`, `sync.WaitGroup.Go`, an experimental `encoding/json/v2`, and a refined standard library that already absorbed the major features of recent cycles (generics in 1.18, `slog`/`slices`/`maps`/`cmp` in 1.21, the per-iteration loop variable and `net/http` routing patterns in 1.22, range-over-func and `unique` in 1.23, `omitzero`/tool directive/`testing.B.Loop`/`os.Root`/`T.Context` in 1.24).

The single biggest way agents write wrong-but-plausible Go is by importing habits from another ecosystem: throwing exceptions instead of returning errors, hiding goroutines behind "async" wrappers, building deep class hierarchies through embedding, defining interfaces at the producer side and never at the consumer, reaching for a web framework when `net/http` is now sufficient, reaching for `logrus`/`zap` when `log/slog` is in the standard library, using `panic`/`recover` for control flow, or assuming generics are the answer when a small interface is the better tool. Optimize for: a flat package layout under `internal/`, small interfaces defined at the consumer, errors returned and wrapped with `%w`, context as the first argument, goroutines that have a known stop condition, table-driven tests, and configuration through the standard library before third-party dependencies.

- Research date: 2026-05-30
- Research basis: current official docs, release notes, specifications, changelogs, and primary repositories.

## Module layout, `go.mod`, and the toolchain

Use modules. One module per repository is the common case; one repository can house multiple binaries under `cmd/`. There is no official "standard project layout" — the Go team's own guide is at go.dev/doc/modules/layout. Use `internal/` aggressively: the compiler refuses imports of `internal/...` from outside the module, which gives you free encapsulation. Avoid `pkg/` unless you are publishing a library and want to mark "these packages are explicitly importable."

A realistic layout for a server with one or more binaries:

```
myservice/
├── go.mod
├── go.sum
├── default.pgo                # optional, see PGO section
├── .golangci.yml
├── cmd/
│   ├── api/
│   │   └── main.go
│   └── worker/
│       └── main.go
└── internal/
    ├── http/
    │   ├── server.go
    │   ├── routes.go
    │   └── middleware.go
    ├── store/
    │   ├── postgres.go
    │   └── store.go           # interface defined here, at the consumer
    ├── user/
    │   ├── service.go
    │   └── user.go
    └── platform/
        ├── config/
        └── log/
```

A realistic `go.mod` for a Go 1.25 service. The `go` directive declares the minimum language version (mandatory since Go 1.21); the `toolchain` directive (Go 1.21) suggests which toolchain to pin builds to; the `tool` directive (Go 1.24) replaces the old `tools.go` blank-import trick:

```
module github.com/acme/myservice

go 1.25.0

toolchain go1.25.4

require (
	github.com/jackc/pgx/v5 v5.7.1
	golang.org/x/sync v0.10.0
)

tool (
	golang.org/x/tools/cmd/stringer
	github.com/sqlc-dev/sqlc/cmd/sqlc
)
```

Run tools by their module path: `go tool stringer -type=Status`, `go tool sqlc generate`. Cached automatically in the build cache (Go 1.24). To add a tool: `go get -tool github.com/sqlc-dev/sqlc/cmd/sqlc@latest`.

Daily commands:

```
go mod tidy            # reconcile go.mod/go.sum with imports
go mod download        # populate the module cache
go build ./...         # build every package
go test ./...          # test every package
go vet ./...           # built-in static analysis (always run; CI must enforce)
go run ./cmd/api       # run a binary by package path
go install ./cmd/api   # install to $GOBIN
```

Use `go.work` (Go 1.18) only for multi-module local development; do not commit `go.work` to a single-module repo. `GOPATH` is dead for development — never reach for it.

## Error handling

Go has no exceptions. Functions that can fail return `error` as their last result. Three rules cover 99% of code:

1. **Wrap with `%w`** when you re-return so callers can inspect the cause with `errors.Is`/`errors.As`.
2. **Define sentinel errors** for conditions callers must branch on; define **typed errors** when callers need structured data.
3. **Never use `panic`/`recover` for control flow.** `panic` is for programmer bugs (nil-deref, "this can't happen"). `recover` is appropriate only at goroutine boundaries that must not crash the process (e.g. an HTTP middleware top-level recover, a worker pool dispatcher).

```go
package store

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
)

// Sentinel errors: stable, comparable with errors.Is.
var (
	ErrNotFound = errors.New("store: not found")
	ErrConflict = errors.New("store: conflict")
)

// Typed error: structured detail for the caller.
type ValidationError struct {
	Field   string
	Message string
}

func (e *ValidationError) Error() string {
	return fmt.Sprintf("validation: %s: %s", e.Field, e.Message)
}

func (s *PostgresStore) GetUser(ctx context.Context, id string) (*User, error) {
	var u User
	err := s.db.QueryRowContext(ctx, `SELECT id, email FROM users WHERE id=$1`, id).
		Scan(&u.ID, &u.Email)
	switch {
	case errors.Is(err, sql.ErrNoRows):
		return nil, fmt.Errorf("get user %q: %w", id, ErrNotFound)
	case err != nil:
		return nil, fmt.Errorf("get user %q: %w", id, err)
	}
	return &u, nil
}
```

Inspecting errors at the boundary:

```go
u, err := store.GetUser(ctx, id)
switch {
case errors.Is(err, store.ErrNotFound):
	http.Error(w, "not found", http.StatusNotFound)
	return
case err != nil:
	var ve *store.ValidationError
	if errors.As(err, &ve) {
		http.Error(w, ve.Error(), http.StatusBadRequest)
		return
	}
	slog.ErrorContext(r.Context(), "get user", "err", err)
	http.Error(w, "internal", http.StatusInternalServerError)
	return
}
```

`errors.Join` (Go 1.20) returns a single error wrapping multiple. Per the Go 1.20 release notes: "The new function `errors.Join` returns an error wrapping a list of errors which may be obtained again if the error type implements the `Unwrap() []error` method." Use it when collecting errors from parallel work or accumulating validation failures; `errors.Is`/`errors.As` traverse joined errors:

```go
var errs []error
for _, item := range items {
	if err := validate(item); err != nil {
		errs = append(errs, fmt.Errorf("item %q: %w", item.ID, err))
	}
}
if err := errors.Join(errs...); err != nil {
	return err
}
```

Decisions:

| Situation | Use |
|---|---|
| Caller needs to branch on a specific outcome | sentinel `var ErrX = errors.New(...)` and `errors.Is` |
| Caller needs structured fields (field name, code) | custom struct type with `Error() string`; check with `errors.As` |
| Just propagating with context | `fmt.Errorf("doing X: %w", err)` |
| Hiding the underlying cause is intentional | `fmt.Errorf("doing X: %v", err)` (note `%v`, not `%w`) |
| Aggregating many independent failures | `errors.Join(errs...)` |
| Unrecoverable programmer error | `panic` (and crash) |

There is no `try`/`check`. The Go team explicitly rejected it; the canonical pattern remains `if err != nil { return ..., err }`. Don't fight this.

## Concurrency: goroutines, channels, `sync`, `context`

The Go mantra is "Don't communicate by sharing memory; share memory by communicating," but a `sync.Mutex` is exactly the right tool for protecting a struct's internal state. Use channels for coordination between goroutines; use mutexes for protecting data structures.

### Goroutines must have a known stop condition

Every `go func()` you write must have a way to exit. Goroutines blocked forever on a channel send/receive, or a `for { ... }` with no exit, are leaks. Use `context.Context` for cancellation.

```go
func worker(ctx context.Context, jobs <-chan Job) {
	for {
		select {
		case <-ctx.Done():
			return
		case j, ok := <-jobs:
			if !ok {
				return
			}
			process(ctx, j)
		}
	}
}
```

### `sync.WaitGroup.Go` (Go 1.25) — prefer over manual `Add`/`Done`

```go
import "sync"

func processAll(items []Item) {
	var wg sync.WaitGroup
	for _, item := range items { // Go 1.22: each iteration has its own `item`
		wg.Go(func() {            // Go 1.25: Add/Done is handled internally
			process(item)
		})
	}
	wg.Wait()
}
```

### `errgroup` for fallible parallel work

For parallel goroutines that return errors and should cancel each other on first failure, use `golang.org/x/sync/errgroup`. It is the canonical choice — not a hand-rolled error channel:

```go
import (
	"context"
	"fmt"
	"golang.org/x/sync/errgroup"
)

func fetchAll(ctx context.Context, urls []string) ([]Response, error) {
	g, ctx := errgroup.WithContext(ctx)
	g.SetLimit(8) // bound concurrency
	results := make([]Response, len(urls))
	for i, url := range urls {
		g.Go(func() error {
			r, err := fetch(ctx, url)
			if err != nil {
				return fmt.Errorf("fetch %s: %w", url, err)
			}
			results[i] = r
			return nil
		})
	}
	if err := g.Wait(); err != nil {
		return nil, err
	}
	return results, nil
}
```

`errgroup.WithContext` cancels the derived context the moment any goroutine returns a non-nil error; the other goroutines observe `ctx.Done()` and bail out. Use `SetLimit(n)` to cap concurrency; `TryGo` to attempt without blocking.

### Mutex vs channel decision table

| Use a mutex when | Use a channel when |
|---|---|
| Protecting fields of a single struct | Handing off ownership of a value |
| Caches, counters, in-memory maps | Producer/consumer pipelines |
| Read-heavy with `sync.RWMutex` | Fan-out / fan-in |
| Short critical sections | Coordinating shutdown |

A read-heavy cache:

```go
type Cache struct {
	mu   sync.RWMutex
	data map[string]string
}

func (c *Cache) Get(k string) (string, bool) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	v, ok := c.data[k]
	return v, ok
}

func (c *Cache) Set(k, v string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.data[k] = v
}
```

### `context`: rules that aren't optional

- `context.Context` is always the **first** parameter, named `ctx`.
- Never store a `Context` in a struct field; pass it through every call.
- Never pass `nil`; use `context.TODO()` if you genuinely don't have one yet, otherwise `context.Background()`.
- `context.WithValue` is for request-scoped data crossing API boundaries (request IDs, auth subjects) — not for passing optional parameters. Use a typed, unexported key.
- `context.WithTimeout`/`WithDeadline`/`WithCancel` return a `cancel` function; always `defer cancel()`.

```go
type ctxKey int
const (
	keyRequestID ctxKey = iota
	keyUser
)

func WithRequestID(ctx context.Context, id string) context.Context {
	return context.WithValue(ctx, keyRequestID, id)
}
func RequestID(ctx context.Context) string {
	s, _ := ctx.Value(keyRequestID).(string)
	return s
}
```

`context.Cause` (Go 1.20) retrieves the error that caused cancellation when you use `WithCancelCause`:

```go
ctx, cancel := context.WithCancelCause(parent)
go func() {
	if err := startup(ctx); err != nil {
		cancel(fmt.Errorf("startup: %w", err))
	}
}()
<-ctx.Done()
if cause := context.Cause(ctx); cause != nil {
	slog.Error("shutdown", "cause", cause)
}
```

`context.WithoutCancel` (Go 1.21) detaches cancellation while keeping values — use for background tasks spawned from a request handler that must outlive the request:

```go
func (h *Handler) Send(w http.ResponseWriter, r *http.Request) {
	go func() {
		// keep request-scoped values (request ID, tenant) but ignore client disconnect
		bg := context.WithoutCancel(r.Context())
		bg, cancel := context.WithTimeout(bg, 30*time.Second)
		defer cancel()
		if err := h.mail.Send(bg, payload); err != nil {
			slog.ErrorContext(bg, "send mail", "err", err)
		}
	}()
	w.WriteHeader(http.StatusAccepted)
}
```

`context.AfterFunc` (Go 1.21) schedules a function to run in its own goroutine after a context is canceled, returning a `stop` function — per the Go 1.21 release notes, "The new `AfterFunc` function registers a function to run after a context has been cancelled." Useful for cleanup hooks that should run on any kind of cancellation:

```go
stop := context.AfterFunc(ctx, func() { conn.Close() })
defer stop()
```

### Data races: always test with `-race` in CI

```
go test -race ./...
go build -race -o bin/api ./cmd/api   # for staging only; ~2-10x overhead
```

The race detector finds real races deterministically when they happen at runtime. It's not optional in CI.

## Generics

Generics arrived in Go 1.18. The constraint surface matters more than the type-parameter syntax:

- `any` — anything; like `interface{}`.
- `comparable` — supports `==` and `!=`; required for map keys and set membership.
- `cmp.Ordered` (Go 1.21, package `cmp`) — supports `<`, `<=`, `>`, `>=`; all built-in numeric types and `string`.
- Custom constraints — an interface listing methods, type sets, or both. `~T` means "any type whose underlying type is T."

When to reach for generics vs interfaces:

| Use generics when | Use an interface when |
|---|---|
| Container/algorithm where element type doesn't matter to behavior (Map, Filter, Set, Heap) | Behavior varies by implementation (Reader, Writer, Store) |
| You need the concrete type back out (no boxing) | You only need to call methods |
| You're writing on top of `cmp.Ordered`/`comparable` | The set of types is open and supplied by the consumer |

Idiomatic generic helpers (don't ship your own — these now exist in `slices` and `maps`, see below):

```go
import "cmp"

func Map[T, U any](in []T, f func(T) U) []U {
	out := make([]U, len(in))
	for i, v := range in {
		out[i] = f(v)
	}
	return out
}

func Filter[T any](in []T, keep func(T) bool) []T {
	out := in[:0:0]
	for _, v := range in {
		if keep(v) {
			out = append(out, v)
		}
	}
	return out
}

func Clamp[T cmp.Ordered](v, lo, hi T) T {
	if v < lo { return lo }
	if v > hi { return hi }
	return v
}

type Set[T comparable] map[T]struct{}

func (s Set[T]) Add(v T) { s[v] = struct{}{} }
func (s Set[T]) Has(v T) bool { _, ok := s[v]; return ok }
```

The Go 1.21 built-ins `min`, `max`, and `clear` are predeclared — no import:

```go
hi := max(a, b, c)
lo := min(a, b)
clear(m)             // empties a map
clear(buf)           // zeroes a slice's elements
```

### `slices`, `maps`, `cmp` standard packages

Use the standard `slices` (Go 1.21) and `maps` (Go 1.21) packages before writing your own. With Go 1.23 they gained iterator-returning members — note that in the standard library `maps.Keys` and `maps.Values` now return `iter.Seq[...]` (in contrast to `golang.org/x/exp/maps`, where they return slices):

```go
import (
	"cmp"
	"slices"
	"maps"
)

xs := []int{3, 1, 4, 1, 5, 9, 2, 6}
slices.Sort(xs)                                  // 1, 1, 2, 3, 4, 5, 6, 9
i, found := slices.BinarySearch(xs, 4)           // 3, true
xs = slices.Compact(xs)                          // remove adjacent duplicates
top := slices.Max(xs)                            // 9
slices.SortFunc(users, func(a, b User) int {     // stable comparator
	return cmp.Compare(a.Name, b.Name)
})

// Iterator-returning (Go 1.23):
for i, v := range slices.All(xs)     { _, _ = i, v }
for v := range slices.Values(xs)     { _ = v }
sorted := slices.Sorted(maps.Values(m))          // collect into a sorted []V
for chunk := range slices.Chunk(xs, 100) { handle(chunk) } // pages

// Maps:
keys := slices.Collect(maps.Keys(m))             // []K
for k, v := range maps.All(m) { _, _ = k, v }
```

`cmp.Compare(a, b)` returns -1/0/+1; `cmp.Or(a, b, ...)` returns the first non-zero argument — perfect for default values.

## Iterators and range-over-func (Go 1.23)

A push iterator is a function that calls a `yield` callback for each value. Two canonical types in `iter`:

```go
type Seq[V any]     func(yield func(V) bool)
type Seq2[K, V any] func(yield func(K, V) bool)
```

`yield` returns `false` when the consumer wants to stop; the iterator must return promptly. The compiler rewrites `for v := range seq` into the right calls.

Implementing a custom iterator over a tree:

```go
package tree

import "iter"

type Node[T any] struct {
	Value    T
	Children []*Node[T]
}

// PreOrder returns an iterator that walks the tree depth-first.
func (n *Node[T]) PreOrder() iter.Seq[T] {
	return func(yield func(T) bool) {
		var walk func(*Node[T]) bool
		walk = func(node *Node[T]) bool {
			if node == nil { return true }
			if !yield(node.Value) { return false }
			for _, c := range node.Children {
				if !walk(c) { return false }
			}
			return true
		}
		walk(n)
	}
}
```

Consume it:

```go
for v := range root.PreOrder() {
	fmt.Println(v)
	if v == target { break }   // yield returns false; the iterator returns
}
```

A `Seq2` is the natural shape for "(value, error)" streams or "(key, value)" pairs:

```go
func Lines(r io.Reader) iter.Seq2[int, string] {
	return func(yield func(int, string) bool) {
		s := bufio.NewScanner(r)
		for i := 0; s.Scan(); i++ {
			if !yield(i, s.Text()) { return }
		}
	}
}

for i, line := range Lines(f) { ... }
```

Range-over-int (Go 1.22) — sometimes that's all you want:

```go
for i := range 10 { fmt.Println(i) }   // 0..9
```

## The Go 1.22 loop-variable change

In Go 1.22+, each iteration of a `for` loop has its own copy of the loop variable. The historical "capture the loop variable in a goroutine" bug no longer applies in modules declaring `go 1.22` or later:

```go
for i, v := range items {
	go func() {
		process(i, v) // safe in Go 1.22+; each iteration has its own i, v
	}()
}
```

You no longer need the old `i, v := i, v` shadow. (The `copyloopvar` golangci-lint check flags now-redundant shadows.) `go vet` understands this; older code that depended on a *single shared* loop variable would have been buggy anyway.

## `net/http`: server, routing, client

The standard library is the right starting point for HTTP. As of Go 1.22, `http.ServeMux` supports method+path patterns and path wildcards — reach for `chi` only when you specifically need composable middleware groups or sub-routers.

### A production HTTP server with timeouts and graceful shutdown

```go
package main

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"
)

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	slog.SetDefault(logger)

	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	})
	mux.HandleFunc("GET /users/{id}", getUser)
	mux.HandleFunc("POST /users", createUser)
	mux.HandleFunc("DELETE /users/{id}", deleteUser)

	srv := &http.Server{
		Addr:              ":8080",
		Handler:           recoverMW(logging(mux)),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       2 * time.Minute,
		MaxHeaderBytes:    1 << 20,
		ErrorLog:          slog.NewLogLogger(logger.Handler(), slog.LevelError),
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	go func() {
		slog.Info("listening", "addr", srv.Addr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			slog.Error("server failed", "err", err)
			os.Exit(1)
		}
	}()

	<-ctx.Done()
	slog.Info("shutting down")

	// Use a fresh context — the signal context is already cancelled.
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		slog.Error("graceful shutdown failed", "err", err)
	}
}
```

The shutdown-context-from-cancelled-parent mistake is the most common production bug here: deriving the shutdown timeout from the already-cancelled signal context makes `Shutdown` return immediately and drop in-flight requests. Always derive from `context.Background()`.

### Routing patterns (Go 1.22)

```go
mux.HandleFunc("GET /items/{id}",        getItem)        // method + path param
mux.HandleFunc("PUT /items/{id}",        updateItem)     // method specific
mux.HandleFunc("GET /files/{path...}",   serveFile)      // trailing wildcard
mux.HandleFunc("GET /items/{$}",         listItems)      // exact "/items/"

func getItem(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	_ = id
}
```

`GET` also matches `HEAD`. Conflicting patterns panic at registration with a detailed message — that's a design feature, not a bug; fix the routes.

### Middleware

Middleware is `func(http.Handler) http.Handler`. Compose by wrapping:

```go
func logging(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		sw := &statusWriter{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(sw, r)
		slog.InfoContext(r.Context(), "http",
			"method", r.Method, "path", r.URL.Path,
			"status", sw.status, "dur", time.Since(start))
	})
}

func recoverMW(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if rec := recover(); rec != nil {
				slog.ErrorContext(r.Context(), "panic", "err", rec)
				http.Error(w, "internal", http.StatusInternalServerError)
			}
		}()
		next.ServeHTTP(w, r)
	})
}
```

### HTTP client

Reuse `*http.Client`. Never use `http.DefaultClient` for outbound calls — it has no timeout. Configure once at startup:

```go
var httpClient = &http.Client{
	Timeout: 10 * time.Second,
	Transport: &http.Transport{
		MaxIdleConns:        100,
		MaxIdleConnsPerHost: 10,
		IdleConnTimeout:     90 * time.Second,
		DialContext: (&net.Dialer{
			Timeout:   5 * time.Second,
			KeepAlive: 30 * time.Second,
		}).DialContext,
		TLSHandshakeTimeout:   5 * time.Second,
		ExpectContinueTimeout: 1 * time.Second,
	},
}

func getJSON(ctx context.Context, url string, out any) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil { return err }
	resp, err := httpClient.Do(req)
	if err != nil { return err }
	defer resp.Body.Close()
	if resp.StatusCode/100 != 2 {
		return fmt.Errorf("status %d", resp.StatusCode)
	}
	return json.NewDecoder(resp.Body).Decode(out)
}
```

Always pass `ctx` via `http.NewRequestWithContext`. Always close `resp.Body`. Always check status before decoding.

## Structured logging with `log/slog`

`log/slog` (Go 1.21) is the standard structured logger. Use it for new code. Do not reach for `logrus` — its own README states "Logrus is in maintenance-mode. We will not be introducing new features. It's simply too hard to do in a way that won't break many people's projects." `zap` is still excellent but external; pick it only when you've measured an allocation problem with the stdlib JSON handler.

```go
import (
	"log/slog"
	"os"
)

logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{
	Level:     slog.LevelInfo,
	AddSource: true,
}))
slog.SetDefault(logger)

slog.Info("user created", "user_id", u.ID, "tenant", u.Tenant)
slog.Error("save failed", "err", err, "user_id", u.ID)

// Pre-bind fields for a sub-component:
log := slog.Default().With("component", "billing")
log.Info("charged", "amount", 1299, "currency", "USD")

// Context-aware (passes through handler middleware):
slog.InfoContext(ctx, "received request", "path", r.URL.Path)
```

Use the typed helpers when you care about allocation in hot paths:

```go
slog.LogAttrs(ctx, slog.LevelInfo, "received request",
	slog.String("path", r.URL.Path),
	slog.Int("status", 200),
	slog.Duration("dur", elapsed),
)
```

Group related fields. Go 1.25 added `slog.GroupAttrs(key string, attrs ...Attr) Attr` — per the package docs, "GroupAttrs returns an Attr for a Group Value consisting of the given Attrs. GroupAttrs is a more efficient version of Group that accepts only Attr values."

```go
slog.LogAttrs(ctx, slog.LevelInfo, "http",
	slog.GroupAttrs("req",
		slog.String("method", r.Method),
		slog.String("path", r.URL.Path),
	),
)
```

For sensitive types, implement `slog.LogValuer` so they redact themselves anywhere they're logged:

```go
type Password string
func (Password) LogValue() slog.Value { return slog.StringValue("REDACTED") }
```

Use `slog.NewTextHandler` for human-readable local dev; `slog.NewJSONHandler` for production. Pick the level via env var at startup; do not litter the code with conditionals.

## JSON

`encoding/json` is the workhorse. The two struct-tag options that matter are `omitempty` and `omitzero` (Go 1.24). They are not the same:

- `omitempty` omits zero-length slices/maps/strings, the literal `false`, `0`, and `nil` pointers — but **not** structs like `time.Time{}` (it has no length, isn't a pointer). This is the long-standing wart.
- `omitzero` (Go 1.24) omits when the value equals its type's zero value, or when the type defines `IsZero() bool` returning true. This is what you want for time values, custom value types, and structs.

```go
type Event struct {
	ID         string    `json:"id"`
	Tags       []string  `json:"tags,omitempty"`        // omit empty slice
	Note       string    `json:"note,omitempty"`        // omit empty string
	OccurredAt time.Time `json:"occurred_at,omitzero"`  // omit time.Time zero
	Score      *float64  `json:"score,omitempty"`       // omit nil pointer
}
```

Rule of thumb: use `omitempty` for maps/slices/strings; use `omitzero` for everything else.

Custom marshal/unmarshal: implement `MarshalJSON`/`UnmarshalJSON` only when the default doesn't suffice. Prefer wrapper types over per-field hacks.

```go
type Status int
const (
	StatusUnknown Status = iota
	StatusActive
	StatusArchived
)

func (s Status) MarshalJSON() ([]byte, error) {
	switch s {
	case StatusActive:   return []byte(`"active"`), nil
	case StatusArchived: return []byte(`"archived"`), nil
	default:             return []byte(`"unknown"`), nil
	}
}
func (s *Status) UnmarshalJSON(b []byte) error {
	switch string(b) {
	case `"active"`:   *s = StatusActive
	case `"archived"`: *s = StatusArchived
	default:           *s = StatusUnknown
	}
	return nil
}
```

For streaming, use `json.Decoder`/`json.Encoder` so you don't materialize the whole document:

```go
dec := json.NewDecoder(r.Body)
dec.DisallowUnknownFields() // reject extra fields
for {
	var item Item
	if err := dec.Decode(&item); err != nil {
		if errors.Is(err, io.EOF) { break }
		return err
	}
	if err := process(item); err != nil { return err }
}
```

`encoding/json/v2` (Go 1.25, `GOEXPERIMENT=jsonv2`) is **experimental**. Do not use in production unless you have explicitly opted in for benchmarking; the API and error messages may still change, and it is not covered by the Go 1 compatibility promise. The plain `encoding/json` API continues to work.

## Testing

Use the standard `testing` package. Reach for `testify` only when its `require`/`assert` helpers clearly reduce noise — `require` for fatal-on-fail in setup, `assert` for cumulative checks. Don't pull in Ginkgo/Gomega; they fight the language.

### Table-driven tests, subtests, parallelism

```go
package billing_test

import "testing"

func TestComputeTax(t *testing.T) {
	t.Parallel()
	cases := map[string]struct {
		amount  int64
		region  string
		want    int64
		wantErr bool
	}{
		"basic US":       {1000, "US-CA", 925, false},
		"zero amount":    {0, "US-CA", 0, false},
		"unknown region": {1000, "XX-ZZ", 0, true},
	}
	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			got, err := ComputeTax(tc.amount, tc.region)
			if (err != nil) != tc.wantErr {
				t.Fatalf("err = %v, wantErr=%v", err, tc.wantErr)
			}
			if got != tc.want {
				t.Errorf("got %d, want %d", got, tc.want)
			}
		})
	}
}
```

In Go 1.22+ each iteration has its own `tc` — no shadowing needed.

### `t.Context()` (Go 1.24) and `t.Cleanup`

Per the Go 1.24 release notes, "The new `T.Context` and `B.Context` methods return a context that's canceled after the test completes and before test cleanup functions run." Use it instead of `context.Background()` in tests:

```go
func TestStore_Save(t *testing.T) {
	ctx := t.Context()
	db := openTestDB(t)
	t.Cleanup(func() { db.Close() })

	if err := db.Save(ctx, &User{ID: "u1"}); err != nil {
		t.Fatal(err)
	}
}
```

`t.TempDir()` for filesystem fixtures, `t.Chdir()` (Go 1.24) to switch working directory with auto-restore, `t.Setenv()` to set env vars with auto-restore.

### Benchmarks with `testing.B.Loop` (Go 1.24)

```go
func BenchmarkParse(b *testing.B) {
	data := loadTestdata(b) // setup not counted toward measurement
	for b.Loop() {
		if _, err := Parse(data); err != nil {
			b.Fatal(err)
		}
	}
}
```

`b.Loop` excludes setup/teardown automatically, prevents dead-code elimination of the body, and runs the benchmark function once. Prefer it over `for i := 0; i < b.N; i++`. Use `b.ReportAllocs()` when allocation behavior matters; run with `go test -bench=. -benchmem -count=10` and compare with `benchstat`.

### Testing concurrent code with `testing/synctest` (Go 1.25)

`synctest.Test` runs a function in an isolated "bubble" with a virtual clock and deterministic goroutine scheduling. Time advances only when every goroutine in the bubble is blocked. This makes time-dependent tests fast and flake-free:

```go
import (
	"testing"
	"testing/synctest"
	"time"
)

func TestCache_Expiry(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		c := NewCache(100 * time.Millisecond)
		c.Set("k", "v")
		if v, ok := c.Get("k"); !ok || v != "v" {
			t.Fatalf("expected hit")
		}
		time.Sleep(200 * time.Millisecond) // instant in the bubble
		if _, ok := c.Get("k"); ok {
			t.Fatalf("expected expiry")
		}
	})
}
```

`synctest.Wait()` blocks until every other goroutine in the bubble is durably blocked — use it to assert that background work has reached a quiescent state. The older `synctest.Run` API from the Go 1.24 experiment is deprecated; use `Test` and remove the `GOEXPERIMENT=synctest` flag.

### Fuzzing (Go 1.18)

```go
func FuzzParse(f *testing.F) {
	f.Add("hello=world")
	f.Add("a=b&c=d")
	f.Fuzz(func(t *testing.T, in string) {
		_, _ = Parse(in) // must not panic
	})
}
```

Run with `go test -fuzz=FuzzParse -fuzztime=30s`. Commit failing corpus entries under `testdata/fuzz/`.

### `httptest`

```go
func TestHandler(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(myHandler))
	t.Cleanup(srv.Close)

	resp, err := srv.Client().Get(srv.URL + "/items/42")
	if err != nil { t.Fatal(err) }
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Fatalf("status=%d", resp.StatusCode)
	}
}
```

Use `httptest.NewRecorder` for direct handler tests when you don't need a real server.

### Golden files

Compare large outputs against on-disk fixtures, with `-update` to regenerate:

```go
var update = flag.Bool("update", false, "rewrite golden files")

func TestRender(t *testing.T) {
	got := Render(input)
	path := filepath.Join("testdata", "render.golden")
	if *update {
		_ = os.WriteFile(path, got, 0o644)
	}
	want, err := os.ReadFile(path)
	if err != nil { t.Fatal(err) }
	if !bytes.Equal(got, want) {
		t.Errorf("mismatch (run with -update if intentional)")
	}
}
```

## Interfaces

Two rules:

1. **Define interfaces at the consumer**, not at the producer. The producer ships a concrete type; the consumer declares the minimum surface it needs. This is the opposite of Java/C# and the single most common ecosystem-import mistake.
2. **Small interfaces.** `io.Reader`, `io.Writer`, `fmt.Stringer`, `error`. Big interfaces are an anti-pattern.

```go
// internal/user/service.go — consumer defines what it needs.
package user

import "context"

type Store interface {
	Get(ctx context.Context, id string) (*User, error)
	Save(ctx context.Context, u *User) error
}

type Service struct{ store Store }

func New(s Store) *Service { return &Service{store: s} }

// internal/store/postgres.go — producer just returns the concrete type.
package store

type PostgresStore struct{ db *sql.DB }
func NewPostgres(db *sql.DB) *PostgresStore { return &PostgresStore{db: db} }
// methods Get(ctx, id) and Save(ctx, u) — no interface declared here
```

`any` is the predeclared alias for `interface{}` (Go 1.18). Use it.

Type switches and assertions:

```go
switch v := x.(type) {
case string: return len(v)
case []byte: return len(v)
case fmt.Stringer: return len(v.String())
default: return 0
}

// Two-value assert avoids panic on mismatch:
s, ok := x.(string)
if !ok { /* handle */ }
```

Compile-time interface conformance assertion (use sparingly, at package init):

```go
var _ Store = (*PostgresStore)(nil)
```

## Structs, methods, receivers

The consistency rule: **pick value or pointer receivers per type and stick with it.** Mixing causes confusion and breaks interface satisfaction in subtle ways (only the pointer's method set contains pointer-receiver methods).

Use pointer receivers when: the method mutates the receiver; the struct is large; the struct contains a `sync.Mutex` (never copy a mutex). Use value receivers for small, immutable-by-convention value types.

Zero values should be useful. `bytes.Buffer{}`, `sync.Mutex{}`, `sync.WaitGroup{}`, `strings.Builder{}` are all usable without a constructor. Design your types to follow this:

```go
type RateLimiter struct {
	mu     sync.Mutex
	rate   int
	bucket map[string]int
}

func (r *RateLimiter) Allow(key string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.bucket == nil {
		r.bucket = make(map[string]int)
	}
	r.bucket[key]++
	return r.bucket[key] <= r.rate
}
```

Composition over inheritance. Embed for "is-a" semantics, but only when you genuinely want method promotion:

```go
type Cache struct {
	sync.RWMutex                  // promotes Lock/Unlock/RLock/RUnlock
	data map[string]string
}

type AuditedStore struct {
	*PostgresStore                // promotes all *PostgresStore methods
	audit AuditLog
}
```

## `defer`, `panic`, `recover`

`defer` runs in LIFO order at function return. Use it for cleanup paired with acquisition: `Lock`/`Unlock`, `Open`/`Close`, `Begin`/`Rollback`. The `defer` happens at *function* return — not block return — which means **`defer` inside a loop accumulates until the function exits**:

```go
// BUG: holds N files open until processAll returns.
func processAll(paths []string) error {
	for _, p := range paths {
		f, err := os.Open(p)
		if err != nil { return err }
		defer f.Close()  // wrong place
		// ...
	}
	return nil
}

// FIX: extract the body so defer scopes to one iteration.
func processAll(paths []string) error {
	for _, p := range paths {
		if err := processOne(p); err != nil { return err }
	}
	return nil
}
func processOne(p string) error {
	f, err := os.Open(p)
	if err != nil { return err }
	defer f.Close()
	// ...
	return nil
}
```

Named return values + `defer` to enrich errors:

```go
func Transfer(ctx context.Context, from, to string, amount int64) (err error) {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil { return fmt.Errorf("begin: %w", err) }
	defer func() {
		if err != nil {
			_ = tx.Rollback()
			return
		}
		err = tx.Commit()
	}()
	// ... do work using tx; just `return err` on failure
	return nil
}
```

`recover` only meaningfully fires when called *directly* in a deferred function. Use it at goroutine boundaries:

```go
func safeGo(fn func()) {
	go func() {
		defer func() {
			if r := recover(); r != nil {
				slog.Error("goroutine panic", "err", r,
					"stack", string(debug.Stack()))
			}
		}()
		fn()
	}()
}
```

Never use `panic`/`recover` to emulate exceptions in normal control flow. The cost is real, the stack traces are worse, and Go programmers reading your code will dislike it.

## Memory and performance

### Slices, `make`, aliasing

A slice is a `{ptr, len, cap}` header. Pre-allocate when you know the size; otherwise `append` grows in amortized O(1):

```go
out := make([]Result, 0, len(items))   // cap=len(items), len=0
for _, it := range items { out = append(out, transform(it)) }
```

Beware aliasing: `s[i:j]` shares the backing array. To break the alias, copy or use the full-slice expression `s[i:j:j]` to clamp capacity:

```go
header := payload[:headerLen:headerLen]   // can't accidentally grow into body
body   := payload[headerLen:]
```

`bytes.Clone`/`slices.Clone` create independent copies.

### `strings.Builder` for concatenation

```go
var b strings.Builder
b.Grow(estimatedSize)
for _, s := range parts { b.WriteString(s) }
result := b.String()
```

Never build strings with repeated `+` in a loop.

### `sync.Pool` for transient, expensive allocations

```go
var bufPool = sync.Pool{
	New: func() any { return new(bytes.Buffer) },
}

func render(v any) ([]byte, error) {
	buf := bufPool.Get().(*bytes.Buffer)
	defer func() { buf.Reset(); bufPool.Put(buf) }()
	if err := json.NewEncoder(buf).Encode(v); err != nil {
		return nil, err
	}
	return bytes.Clone(buf.Bytes()), nil
}
```

Items in a `sync.Pool` may be freed by the GC at any time; pool only what you can re-create.

### `unique` package (Go 1.23) for interning

`unique.Make[T comparable](v T) Handle[T]` returns a handle that compares equal whenever the values compare equal. The runtime keeps one canonical copy and reclaims it when no handles remain — built on weak pointers. Use for high-cardinality value sets (tag strings, tenant IDs, addresses):

```go
import "unique"

type Tag struct{ h unique.Handle[string] }

func NewTag(s string) Tag      { return Tag{unique.Make(s)} }
func (t Tag) String() string   { return t.h.Value() }
func (t Tag) Equal(o Tag) bool { return t.h == o.h }   // pointer-comparison fast
```

### Escape analysis and profile-guided optimization

`go build -gcflags='-m'` shows what escapes to the heap. Treat it as a tool, not a religion.

PGO is on by default since Go 1.21: drop a `default.pgo` file (a CPU pprof) in the main package directory and `go build` will use it. Collect one from production traffic:

```go
import _ "net/http/pprof"   // exposes /debug/pprof/* on the default ServeMux
```

```
curl -o default.pgo "http://prod.example/debug/pprof/profile?seconds=60"
git add cmd/api/default.pgo
go build ./cmd/api          # next build uses PGO automatically
```

Per the official Go PGO user guide (go.dev/doc/pgo): "As of Go 1.22, benchmarks for a representative set of Go programs show that building with PGO improves performance by around 2-14%."

## Containers and `GOMAXPROCS` (Go 1.25)

Per the Go 1.25 release notes: "On Linux, the runtime considers the CPU bandwidth limit of the cgroup containing the process, if any. If the CPU bandwidth limit is lower than the number of logical CPUs available, GOMAXPROCS will default to the lower limit. On all OSes, the runtime periodically updates GOMAXPROCS if the number of logical CPUs available or the cgroup CPU bandwidth limit change." This only kicks in for modules that declare `go 1.25` or later.

The Go team's own announcement (go.dev/blog/container-aware-gomaxprocs) credits the maintainers of `go.uber.org/automaxprocs` as the inspiration: "Go 1.25 provides more sensible default behavior for many container workloads by setting GOMAXPROCS based on container CPU limits… thanks… to feedback from the maintainers of go.uber.org/automaxprocs from Uber, which has long provided similar behavior to its users." Remove `automaxprocs` and any manual `GOMAXPROCS=…` overrides from new Go 1.25 code; if you do need to override, set the env var or call `runtime.GOMAXPROCS(n)` (which disables the cgroup-aware logic).

## Tooling: `gofmt`, `go vet`, `golangci-lint`, `staticcheck`

`gofmt` (`go fmt ./...`) is non-negotiable. `goimports` extends it with import management; many editors run it on save. `go vet ./...` is mandatory in CI. `golangci-lint` v2 (current config schema `version: "2"`) bundles `staticcheck` and — per the project's homepage — "includes over a hundred linters"; it is the standard meta-linter.

A realistic, copy-ready `.golangci.yml` (v2 schema):

```yaml
version: "2"

run:
  timeout: 5m
  tests: true
  modules-download-mode: readonly

linters:
  default: none
  enable:
    - govet
    - staticcheck       # bundles SA*, ST*, S*, QF* checks
    - errcheck
    - ineffassign
    - unused
    - revive
    - bodyclose         # http.Response.Body must be closed
    - contextcheck      # non-inherited context.Context
    - errorlint         # errors.Is/As over ==, %w over %v
    - copyloopvar       # leftover loop-var shadows (Go 1.22+)
    - intrange          # encourages `for i := range N`
    - nilerr            # `return nil` when err != nil
    - rowserrcheck
    - sqlclosecheck
    - noctx             # http.NewRequest -> NewRequestWithContext
    - gosec
    - gocritic
    - testifylint
    - paralleltest
  settings:
    revive:
      rules:
        - name: exported
        - name: var-naming
        - name: error-return
    gocritic:
      enabled-tags: [diagnostic, performance, style]

formatters:
  enable:
    - gofumpt           # stricter gofmt
    - goimports
  settings:
    goimports:
      local-prefixes: github.com/acme/myservice

issues:
  max-issues-per-linter: 0
  max-same-issues: 0
  exclusions:
    rules:
      - path: _test\.go
        linters: [errcheck, gosec]
```

Install golangci-lint with the official script, pinned to a version:

```
curl -sSfL https://raw.githubusercontent.com/golangci/golangci-lint/HEAD/install.sh | sh -s -- -b ./bin v2.4.0
./bin/golangci-lint run
./bin/golangci-lint fmt    # apply formatters
```

A minimal `Makefile` for daily use:

```
.PHONY: fmt lint test bench vet ci
fmt:
	go tool golangci-lint fmt
vet:
	go vet ./...
lint:
	go tool golangci-lint run
test:
	go test -race -count=1 ./...
bench:
	go test -bench=. -benchmem -run=^$$ ./...
ci: vet lint test
```

Build tags use the modern `//go:build` syntax (the old `// +build` form is deprecated and `gofmt` rewrites it):

```go
//go:build integration

package store_test
```

Run with `go test -tags=integration ./...`.

`go generate` directives live next to the code they generate. With Go 1.24's `tool` directive in `go.mod`, drop the `tools.go` workaround:

```go
//go:generate go tool stringer -type=Status
```

## Library currency

| Job | Use | Don't use for new code |
|---|---|---|
| Structured logging | `log/slog` (Go 1.21) | `logrus` (maintenance mode), `log` (stdlib, unstructured), `zap`/`zerolog` unless you've measured a need |
| HTTP routing | `net/http` ServeMux (Go 1.22) | `gorilla/mux` (use only if you need its specific regex/host features); reach for `chi` only when you need composable sub-routers |
| Fallible parallel work | `golang.org/x/sync/errgroup` | Hand-rolled error channels with `WaitGroup` |
| Goroutine bookkeeping | `sync.WaitGroup.Go` (Go 1.25) | Manual `Add`/`Done` pairs |
| Linting | `golangci-lint` v2 | Running individual linters by hand; `gometalinter` (defunct) |
| Dep mgmt | Go modules + `toolchain` directive | `dep`, `glide`, `godep`, GOPATH (all dead) |
| Tool deps | `tool` directive in `go.mod` (Go 1.24) | `tools.go` blank imports |
| Interning | `unique` package (Go 1.23) | `go4.org/intern` (superseded) |
| Test assertions | stdlib `testing` (+ `testify/require` for setup) | Ginkgo/Gomega for ordinary Go services |
| Configuration | `flag` + env vars; `kelseyhightower/envconfig` if heavy | Viper (heavy; usually overkill) |
| UUIDs | `github.com/google/uuid` | rolling your own |
| Postgres driver | `github.com/jackc/pgx/v5` | `lib/pq` (effectively frozen) |
| Container `GOMAXPROCS` | Go 1.25 runtime default | `go.uber.org/automaxprocs` (now obsolete for `go 1.25` modules) |

## Anti-patterns to avoid

| Wrong | Right |
|---|---|
| `if err != nil { return err }` everywhere with no context | `return fmt.Errorf("doing X for %q: %w", id, err)` |
| `panic(err)` inside library code | Return the error; let the caller decide |
| `interface{}` parameters "to be flexible" | Define a small interface at the consumer; or use generics |
| Producer-defined interfaces no one else implements | Define interfaces at the consumer; producer returns concrete types |
| Storing `context.Context` in a struct field | Pass `ctx` as the first parameter to every method that needs it |
| `http.Get(url)` / `http.DefaultClient` | A configured `*http.Client` with timeouts; `http.NewRequestWithContext` |
| Deriving shutdown timeout from a cancelled signal context | `context.WithTimeout(context.Background(), …)` for shutdown |
| `defer f.Close()` inside a `for` loop | Extract one-iteration body into its own function |
| `time.Sleep` in tests of time-dependent code | `testing/synctest` (Go 1.25) with a virtual clock |
| `for i := 0; i < b.N; i++` in new benchmarks | `for b.Loop()` (Go 1.24) |
| `omitempty` on `time.Time` and expecting zero to disappear | `omitzero` (Go 1.24) |
| `for i, v := range xs { go func() { use(i, v) }() }` capture bug | Fixed by Go 1.22 per-iteration variables — but `go vet`/`copyloopvar` confirm |
| `log.Printf` for new services | `slog.InfoContext(ctx, ...)` with structured attributes |
| Manual `wg.Add(1); go func(){ defer wg.Done(); … }()` | `wg.Go(func(){ … })` (Go 1.25) |
| `recover` to handle expected error conditions | Errors are values; return them |
| Goroutine with no exit condition | Drive every goroutine off `<-ctx.Done()` or a closed channel |
| `_ = json.Unmarshal(b, &v)` ignoring the error | Always check; configure `DisallowUnknownFields` when relevant |
| Mixing value and pointer receivers on one type | Pick one; usually pointer if any method mutates or the struct embeds a mutex |
| `GOMAXPROCS` overrides / `automaxprocs` import | Let Go 1.25's cgroup-aware default run; only override if you've measured a need |
| `dep`, `GOPATH` workflows | Modules. They've been the standard since 1.11 and the only path since 1.16. |
| Importing `encoding/json/v2` in production | Experimental under `GOEXPERIMENT=jsonv2`; do not ship |

## Version & compatibility quick reference

| Feature | Version | Notes |
|---|---|---|
| Generics (type parameters) | 1.18 | `any` alias added |
| Fuzzing | 1.18 | `f.Fuzz`, corpus under `testdata/fuzz/` |
| Workspaces (`go.work`) | 1.18 | Local multi-module dev |
| `errors.Join`, `context.WithCancelCause`, `context.Cause` | 1.20 | |
| PGO (`default.pgo`) | 1.21 | GA in 1.21; default-on |
| `log/slog`, `slices`, `maps`, `cmp`, `min`/`max`/`clear` | 1.21 | |
| `context.WithoutCancel`, `context.AfterFunc` | 1.21 | |
| `toolchain` directive in `go.mod` | 1.21 | |
| Per-iteration loop variables | 1.22 | When `go 1.22` in `go.mod` |
| Range-over-int (`for i := range 10`) | 1.22 | |
| `http.ServeMux` method + path patterns, `Request.PathValue` | 1.22 | |
| Range-over-func, `iter.Seq`/`iter.Seq2` | 1.23 | |
| Iterator forms in `slices`/`maps` (`All`, `Values`, `Collect`, `Sorted`, `Chunk`, `Keys`) | 1.23 | |
| `unique` package | 1.23 | Built on runtime weak pointers |
| `omitzero` JSON tag | 1.24 | Use for `time.Time` and value structs |
| `tool` directive in `go.mod` | 1.24 | Replaces `tools.go` |
| `testing.B.Loop`, `testing.T.Context`, `t.Chdir` | 1.24 | |
| `os.Root` (directory-scoped FS) | 1.24 | Per release notes: "Methods on `os.Root` operate within the directory and do not permit paths that refer to locations outside the directory, including ones that follow symbolic links out of the directory." |
| `runtime.AddCleanup` | 1.24 | Replaces `SetFinalizer` |
| `sync.WaitGroup.Go` | 1.25 | Mistake-proof goroutine spawn |
| `testing/synctest` (`Test`, `Wait`) GA | 1.25 | Virtual clock for concurrent tests |
| `slog.GroupAttrs` | 1.25 | More efficient `slog.Group` for `Attr`-only groups |
| Container-aware `GOMAXPROCS` | 1.25 | Reads cgroup CPU bandwidth on Linux |
| `encoding/json/v2` | 1.25 | **Experimental** — `GOEXPERIMENT=jsonv2` only |
