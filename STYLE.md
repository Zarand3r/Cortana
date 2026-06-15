# STYLE.md — Cortana engineering conventions

The owner's standing preferences for how code in this repo is written. These are
**living conventions** — iterate on them via PR. Coding agents (and humans) should
follow them by default and flag, in the PR description, any place a convention was
deliberately broken and why.

> Scope: these sit on top of `karpathy-guidelines` (surgical changes, no
> overcomplication) and `principal-production-engineer` (simple design, visible
> failure). When a convention here conflicts with "keep it simple," simple wins —
> note it in the PR.

---

## 1. No optional or conditional imports

**Do:** import everything at module top level, unconditionally.

**Don't:** use `if TYPE_CHECKING:` blocks, function-local imports for
type-hint-only deps, or `try/except ImportError` to make a dependency "optional".

**Why:** conditional imports hide the real dependency graph, split a symbol's
behavior between "type time" and "run time," and are usually a band-aid for a
circular import that should be fixed structurally. Keep module dependencies
**one-directional** so plain imports never cycle. In this repo the chain is
`memory → perception → backends → config`, and `config` imports nothing from the
package — so every import is a normal top-level import.

```python
# good — backends depends on config; config depends on nothing; no cycle
from cortana.config import Config

# bad — hides the dependency, splits type vs runtime
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from cortana.config import Config
```

**The one allowed exception:** a genuinely *native/heavy* dependency that must not
be required to import pure logic (e.g. PyObjC, `mlx_lm`). Import it **lazily inside
the function that uses it**, and say so in a comment. This is about not forcing a
hard runtime dep on importers, *not* about types.

```python
def capture_screen():
    import Quartz  # lazy: keep this module importable without PyObjC
    ...
```

If avoiding a conditional import would create a real cycle, **the cycle is the
bug** — break it by moving the shared type down to a module both sides can import.

## 2. Use abstract base classes to define closed contracts

**Do:** when there is a small, known (closed) set of implementations that share an
interface, define an `abc.ABC` with `@abstractmethod`s, make every implementation
inherit it, and type factories/consumers against the ABC.

**Don't:** duck-type across implementations you own, or rely on `getattr(obj,
"method", default)` when a real contract exists.

**Why:** the ABC is an enforced, discoverable contract: it fails loudly at
instantiation if an implementation forgets a method, gives factories a concrete
return type, and documents the seam in one place. (For *open* extension points
consumed structurally, a `typing.Protocol` is the better tool — but our backend set
is closed per the design's Non-Goals, so ABC is right here.)

```python
class LLMBackend(ABC):
    model: str
    @abstractmethod
    def generate(self, prompt: str) -> str: ...

def make_backend(...) -> LLMBackend:   # concrete, typed contract
    ...
```

Corollary: keep the contract **uniform** across implementations — every backend
exposes `.model` as the same kind of value (an identifier string), never the loaded
model object on one and a string on another.

## 3. Enums + `match` over string-keyed `if` ladders

**Do:** model a closed set of choices as an `Enum` (a `str, Enum` when the values
are also wire/config strings). Coerce untrusted input at the boundary with
`MyEnum(value)` — that gives free validation (`ValueError` on unknown). Dispatch
with `match`/`case`.

**Don't:** scatter `if name == "ollama": ... elif name == "mlx": ...` ladders, or
repeat the set of valid strings in multiple places.

**Why:** the enum is the single source of truth for valid values; `match` keeps
dispatch in one rigid block; `Enum(value)` centralizes validation so unknown values
fail the same way everywhere.

```python
class Backend(str, Enum):
    FAKE = "fake"; OLLAMA = "ollama"; MLX = "mlx"

def make_backend(name: str | Backend, cfg=None) -> LLMBackend:
    backend = Backend(name)          # ValueError on unknown — no manual raise
    match backend:
        case Backend.FAKE:   return FakeLLMBackend()
        case Backend.OLLAMA: return OllamaBackend(model=..., host=...)
        case Backend.MLX:    return MLXBackend(model=...)
    raise AssertionError(f"unhandled backend: {backend!r}")  # unreachable guard
```

## 4. Configuration lives in TOML files under a top-level `config/`

**Do:** keep runtime configuration in `config/*.toml`, read with the stdlib
`tomllib`. Keep **in-code defaults authoritative** (the app runs with zero config);
the file *overrides* defaults, and a missing/partial file falls back cleanly. Bind
file keys to struct fields through a single explicit map. Ship a template that
mirrors the defaults, and **test that loading it reproduces the defaults** so the
file and code never drift.

**Don't:** use formats that need a third-party parser or build step (e.g. jsonnet,
YAML) unless there's a concrete need — `tomllib` is in the stdlib, jsonnet/yaml are
not. Don't spread config across env vars + literals + flags without one clear
source.

**Why:** TOML is human-editable, comment-friendly, and zero-dependency to read;
a top-level `config/` keeps it discoverable and out of the source tree; "code
defaults + file overrides" means the agent always runs and config is purely
additive.

```python
cfg = Config.load()                  # config/cortana.toml if present, else defaults
cfg = Config.load("path/to.toml")    # explicit
```

```toml
# config/cortana.toml — every key optional; overrides the in-code default
[llm]
model = "qwen2.5:7b-instruct"
```

---

## How to iterate on this file

Add or amend a convention in a PR, with: the rule (do/don't), a one-line **why**,
and a concrete code example from this repo. Keep each convention testable or
grep-able where possible (e.g. "no `TYPE_CHECKING` in `cortana/`") so it can be
mechanically enforced later.
