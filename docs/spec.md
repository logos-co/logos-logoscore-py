# logoscore-py Specification

## Overview

`logoscore-py` is a Python control surface for the **logoscore daemon** — the
headless runtime that hosts Logos modules and exposes their methods and events
over RPC. It lets a Python program launch a daemon, load modules into it, call
methods, subscribe to events, and provision authentication tokens, all without
hand-writing subprocess invocations or parsing CLI text.

It exists for one primary audience: **test suites and automation tooling** that
need to drive a *real* Logos runtime from Python. A module author wants to smoke-test
a freshly built plugin against a genuine distribution of the daemon; a CI pipeline
wants to exercise the wire stack end-to-end — the local socket, and the mutually
authenticated `tls_tcp` sessions of Remote Runtime Control and peering; an
orchestration script wants several daemons running side by side, possibly across
container boundaries or linked to one another. `logoscore-py` is the layer that makes
those journeys ordinary Python — context managers, method calls, callbacks, and
exceptions — instead of shell plumbing.

Conceptually it is a **thin client over the logoscore command surface**: every
operation it offers corresponds to a logoscore command, and every value it returns
is the daemon's own structured response. It introduces no new module-management
semantics of its own — it adopts the daemon's behavior, exit-code contract, and
data shapes wholesale, and presents them with Python ergonomics. What it adds is
*lifecycle orchestration* (spawning and tearing down daemons in isolated state,
locally or in containers), *connection management* (a local dial description per
well-known module, or a pairing with a daemon elsewhere), and *idiomatic
surfacing* (typed exceptions, background-threaded event delivery, argument and
result conversion at the language boundary).

### Where it sits in the platform

```
   Python test / automation code
            │
            ▼
   ┌──────────────────────┐
   │     logoscore-py     │   launch · load · call · watch · tokens
   └──────────┬───────────┘
              │  drives the command surface, parses structured responses
              ▼
   ┌──────────────────────┐
   │   logoscore daemon   │   headless module runtime
   └──────────┬───────────┘
              │  hosts Logos modules; RPC over the local socket, and over
              │  tls_tcp for Remote Runtime Control and peering
              ▼
   ┌──────────────────────┐
   │   Logos modules      │   process-isolated plugins exposing methods + events
   └──────────────────────┘
```

The daemon (`logos-logoscore-cli`) is the CLI runtime over the Logos core library;
the modules it hosts are the same process-isolated plugins built across the Logos
platform. `logoscore-py` is a *frontend-edge* component — it neither links the core
nor speaks the RPC protocol itself; it commands the daemon, which does both.

### Design principles

| Principle | What it means here |
|-----------|--------------------|
| **No new semantics** | Every capability mirrors a daemon command. Behavior, return shapes, and failure modes are the daemon's; the wrapper does not reinterpret them. |
| **Isolation by default** | A spawned daemon gets its own private configuration scope, so concurrent daemons never collide and nothing leaks into the developer's global state. |
| **Connection is per-module** | A daemon serves two well-known modules on *separate* listeners. The wrapper always describes a local connection per module, never as a single collapsed endpoint. |
| **Disk config is authoritative** | A local connection is an on-disk connection description; a remote one is a pairing the client keeps in its own configuration scope. Neither is an environment override. |
| **The daemon decides remote access** | Pairing grants a remote client nothing; the daemon's remote policy names each method it may call. |
| **Idiomatic surfacing** | Lifecycle is a context manager; failures are typed exceptions keyed off the daemon's exit-code contract; events arrive on a background thread via a callback. |

---

## Domain Model

### Key concepts

| Term | Definition |
|------|------------|
| **logoscore daemon** | The headless runtime process that hosts Logos modules and exposes them over RPC. `logoscore-py` launches and/or dials it; it is the authority for all module operations. |
| **Module** | A process-isolated Logos plugin hosted by the daemon. Its public surface is a set of invokable methods (and the events it emits). |
| **Well-known modules** | Two modules the daemon always serves — **`core_service`** (the management gateway: load/unload, status, proxied method calls) and **`capability_module`** (the authorization handshake). Each is served on its *own* listener, which is why a connection must be described per module. |
| **Invokable method** | A method a module exposes for remote invocation. Calling one is the core "do something" operation; arguments are positional and the daemon returns the method's result value. |
| **Event** | A fire-and-forget message a module emits. A subscription streams events as they occur. |
| **Connection description** | The per-module dial specification for a daemon on the same host: for each well-known module, the local socket to dial. |
| **Daemon endpoint** | One module's slice of a connection description. |
| **Runtime-state signal** | The daemon publishes its live runtime state (instance identity, resolved listener endpoints) once it has finished binding. Its appearance is the readiness signal the wrapper waits for before declaring a daemon up. |
| **Token** | A signed credential the daemon issues per authorized local client; the daemon validates an RPC connection against it. The daemon stores only a hash at rest; the raw value is handed to the client once. |
| **Transport** | How a client's connection is carried: the same-host **local** socket, the only client transport. (The legacy plaintext TCP and server-only TLS transports, with their JSON/CBOR codecs, are gone.) |
| **Remote Runtime Control** | Operating a daemon from a client elsewhere: the client **pairs** once through a runtime-control invite the daemon mints and accepts, then runs its commands on the daemon over a mutually authenticated `tls_tcp` session. The client holds its key in its own configuration scope and needs no daemon or token. |
| **Remote policy** | The daemon's per-client grants: which management methods, and which module methods, each paired client may call. Pairing alone grants nothing, and a module wildcard never covers management methods. |
| **Peering** | Two runtimes linked so a module on one calls a module on the other: the provider exports it, the consumer imports it as a forwarding facade. |
| **Subscription** | A live event stream from a module, delivered to a callback on a background thread until cancelled. |
| **Tagged-bytes form** | The platform's canonical encoding for binary values crossing the JSON boundary. Byte arrays travel as a tagged object and are decoded back to raw bytes exactly once, at the client boundary. |
| **Structured result wrapper** | A common module return shape — `success` / `value` / `error` — used by methods that report ok-or-error outcomes. The same shape is returned whether the call was local or remote. |
| **Image flavor** | For containerized daemons, the packaging of the daemon image: **portable** (self-contained, matches released binaries) vs **dev** (linked against an external store). User modules supplied to a containerized daemon must match the image's flavor. |

### The two well-known modules and why connection is per-module

The single most load-bearing fact in this system's design: the daemon serves
`core_service` and `capability_module` on **distinct listeners**. A client cannot
reach the daemon through one endpoint — it must know how to dial each module
separately, because the authorization handshake against `capability_module`
happens before (and alongside) any request to `core_service`. The general, correct
way to describe a local connection is therefore a **per-module connection
description**, with one endpoint entry per well-known module.

A remote client needs none of this: Remote Runtime Control reaches `core_service`
alone, and the daemon's remote policy stands in for the authorization handshake.

---

## Features and Functional Requirements

### Daemon lifecycle

The system offers three ways to obtain a usable connection to a daemon:

1. **Launch a local daemon.** Spawn a daemon as a managed local process with its
   own isolated configuration scope, so multiple daemons can coexist without
   collision and the developer's global state is never touched. The lifecycle is a
   context manager: entering it starts the daemon and waits until it is ready;
   leaving it shuts the daemon down cleanly and removes any state directory the
   wrapper created.

2. **Launch a containerized daemon.** Run the daemon inside a container and operate
   it from the host over Remote Runtime Control — the right choice for testing
   against a real distributed build. The wrapper mounts the host directories the
   daemon reads and writes, waits for readiness, pairs a host-side client with the
   daemon, grants it methods, and hands it back.

3. **Connect to an existing daemon.** Attach to a daemon already running — on the
   same host (using its published configuration, or an explicit per-module
   connection description from a scope the daemon does not own), or elsewhere
   (pairing with it for Remote Runtime Control).

**Requirements**

- A launched local daemon MUST run in an isolated configuration scope by default, so
  concurrent daemons never interfere and the global default scope is never polluted.
- Launch MUST block until the daemon signals readiness, and MUST surface the daemon's
  own diagnostic output if it fails to come up or never becomes ready.
- A launched daemon MUST be torn down cleanly on context exit, with escalation
  (request shutdown → terminate → force-kill) if a clean stop does not complete in
  time, and any wrapper-created state directory MUST be removed.
- A caller MAY supply a configuration or persistence location to keep it after the
  daemon exits (e.g. to pre-seed or inspect persisted state); wrapper-created
  locations are cleaned up, caller-supplied ones are left intact.

### Connection and transport

- A client MUST be obtainable from a launched daemon with no transport arguments;
  the daemon's published per-module endpoints are sufficient to dial it.
- A client in a configuration scope the daemon does not own MUST be able to reach
  a same-host daemon through a per-module connection description plus a token.
- The only client transport is the same-host **local** socket. A daemon elsewhere
  MUST be reached with **Remote Runtime Control**: pairing through a runtime-control
  invite the daemon mints and accepts, after which every command runs on the daemon.
- Pairing MUST grant nothing by itself; the daemon's remote policy MUST decide each
  management method and each module method a paired client calls, and a refused
  call MUST surface as a typed exception carrying the refusal's code.
- A module call between two runtimes is **peering**, not a client connection: the
  system MUST support standing up two linked daemons, one exporting modules and the
  other importing them.
- A standalone way to write a per-module connection description to disk MUST exist,
  so callers can produce a dial spec without holding a live client.

### Module management and method calls

The client exposes the daemon's module-management surface and method invocation:

| Operation | Behavior |
|-----------|----------|
| **status** | Report overall daemon and module health. |
| **stats** | Report per-module resource usage. |
| **list modules** | List known modules, or only loaded ones. |
| **module info** | Report a module's metadata, methods, and events. |
| **load / unload / reload module** | Manage a module's loaded state; dependencies are resolved by the daemon. |
| **call** | Invoke a method on a loaded module with positional arguments, returning the method's result value. |
| **stop** | Ask the daemon to shut down cleanly. |
| **subscribe to events** | Stream a module's events (all of them, or a single named event) to a callback. |

**Requirements**

- Each operation MUST return the daemon's structured response parsed into native
  Python values, and MUST raise a typed exception on failure (see Error Model).
- A method call MUST return the method's **result value** directly (unwrapped from
  the daemon's response envelope), so the caller works with the value, not the
  protocol envelope.
- Argument conversion at the language boundary MUST follow the daemon's argument
  contract: booleans render as their textual form, filesystem-path arguments are
  passed as file references so the daemon loads their contents, and byte values are
  conveyed in the daemon's expected byte form. Other scalars pass through for the
  daemon's own type coercion.
- Result conversion MUST decode the platform's tagged-bytes form back to raw bytes,
  applied recursively so tagged values nested in maps and lists are decoded too.
- The structured result wrapper (`success` / `value` / `error`) returned by ok-or-error
  methods MUST come through identically whether the call was local or remote.

### Event subscriptions

- A subscription MUST deliver each event to the caller's callback on a background
  thread, without blocking the caller's main flow.
- A subscription MUST be cancellable; cancellation MUST stop the underlying watch and
  join the delivery thread, escalating the stop signal if the watch does not exit
  promptly.
- A subscription MUST expose whether it is still alive, and MUST be usable as a
  context manager so it is cancelled on scope exit.
- Errors during delivery (a malformed event, or an exception raised by the caller's
  callback) MUST be routed to an optional error callback, or logged if none is given,
  and MUST NOT tear down the subscription on a single bad event.

### Token provisioning

- Issuing, revoking, and listing tokens MUST be possible **without a running daemon**,
  operating directly on a daemon's configuration scope, so credentials can be
  provisioned before or alongside the daemon.
- Issuing a token MUST return the freshly minted raw token to the caller (the only
  moment it is available in the clear); the daemon's stored form is a hash.
- Listing tokens MUST never reveal raw token values.
- Revoking a non-existent token MUST surface the daemon's failure as a typed exception.

### Containerized and multi-host operation

- A containerized daemon MUST be operated over Remote Runtime Control: the container
  shares the host's network, the daemon's control endpoint listens on a known
  loopback port, and the wrapper pairs a host-side client with it (minting and
  accepting the invite inside the container) and grants it methods.
- The wrapper MUST let the caller change a paired client's grants while the daemon
  runs.
- Files the containerized daemon writes with restrictive ownership MUST be readable
  through the container (not assumed to be host-readable), and the system MUST provide
  a way to extract such content and to read the daemon's runtime-state and identity.
- A facility MUST exist to **build module plugins inside a container** for ABI
  compatibility with the daemon image, accepting one or more remote flake references
  and producing a host modules directory ready to mount. Local (host-filesystem)
  flake references are explicitly **not** supported by this facility, because the
  build runs in a one-shot container without the host filesystem mounted.

### Diagnostics

- A facility MUST report whether a container runtime is available and whether a given
  image is present, and MUST provide a way to pick a free host port.
- An opt-in setting MUST allow the daemon's diagnostic output (its warning/debug
  trail) to be mirrored to the caller's process for troubleshooting. Response output
  that may carry secrets (e.g. a freshly issued raw token) MUST NOT be mirrored.

---

## Error Model

Failures are surfaced as a typed exception hierarchy keyed off the daemon's exit-code
contract. The base type carries the failing exit code, the daemon's error code (when
present), and its diagnostic output.

| Daemon exit code | Meaning | Exception |
|------------------|---------|-----------|
| 0 | success | — (value returned) |
| 1 | general error | base error |
| 2 | no daemon reachable | daemon-not-running error |
| 3 | module error (not found, load/unload failed) | module error |
| 4 | method error (not found, call failed, timed out) | method error |

**Requirements**

- A non-zero daemon exit MUST be mapped to the corresponding exception subtype; an
  unrecognized code MUST map to the base error rather than being silently dropped.
- A method call that returns an error outcome MUST raise the method-error type with
  the daemon's message and error code attached.
- Output that fails to parse as the expected structured response MUST raise the base
  error with the daemon's diagnostics attached, rather than returning malformed data.

---

## Use Cases and Workflows

### 1. Local module testing

A module author has built a plugin and wants to confirm its methods work against a
real daemon. They launch a local daemon over their module directory, obtain a client,
load the module, introspect its methods, and call them — all inside a context manager
that guarantees the daemon and its temporary state are cleaned up afterward.

```
launch local daemon (isolated scope)
  → obtain client
  → load module
  → list/introspect modules
  → call methods, assert on results
  → (context exit) stop daemon, clean up temp state
```

### 2. Event round-trip

The author subscribes to a module's events, triggers activity that should emit them,
and asserts the callback fired with the expected payloads — then cancels the
subscription. Delivery happens on a background thread; cancellation stops the watch
and joins the thread.

### 3. Container smoke test

To test against a real distributed build, the author launches a containerized daemon
over their compiled plugins. The wrapper waits for readiness, pairs a host-side client
with the daemon over Remote Runtime Control, and returns it. The same method and event
matrix that passes locally is replayed through that pairing, and the daemon's policy
is shown to refuse what it does not grant.

### 4. Remote operation

A daemon runs on another host. Its operator turns Remote Runtime Control on and mints
a runtime-control invite; the caller's client redeems it from a configuration scope of
its own, the operator accepts the pairing and grants the client methods in the remote
policy, and the client's commands then run on the daemon.

### 5. Linked runtimes

A module on one daemon calls a module on another. The two daemons pair; the provider
exports the module, the consumer imports it as a facade that forwards each call and
event, and the provider's policy decides which of the consumer's modules may call.

### 6. Token provisioning

Before standing up a daemon — or to add a client to a long-running one — the caller
issues a named token against the daemon's configuration scope, distributes the raw
value to the client, and later lists or revokes it. These operations need no running
daemon.

### 7. ABI-safe module builds

A plugin compiled on a mismatched platform will not load in the Linux daemon
container. The caller builds the module flake(s) inside a container matching the
daemon image's build base, producing a host modules directory that loads cleanly when
mounted into the daemon.

---

## Behavioral Guarantees and Boundaries

- **Mirrors the daemon, adds no module semantics.** The wrapper's behavior is the
  daemon's behavior. It does not load modules, validate arguments, or interpret
  results beyond the language-boundary conversions described above.
- **The daemon must be reachable as a command.** The system drives a daemon command
  surface; that surface must be available to the calling process. The wrapper has no
  in-process fallback and embeds no native bindings.
- **One operation, one invocation.** Each operation is an independent round-trip to
  the daemon. This favors correctness and isolation over throughput — the system is a
  control and testing surface, not a high-frequency RPC path.
- **Local connections are per-module; remote operation is a pairing.** Same-host
  launched daemons can be dialed from their published config with no extra input; a
  daemon elsewhere is operated only through a pairing its operator accepted, and only
  with the methods its policy grants.
- **Flavor must match.** For containerized daemons, user-supplied module plugins must
  match the daemon image's flavor (self-contained vs externally linked) and the
  daemon platform; mismatched plugins will not load.
- **Secrets stay out of mirrored output.** When diagnostic mirroring is enabled, only
  the daemon's diagnostic trail is mirrored — never response output that may contain
  raw tokens.
