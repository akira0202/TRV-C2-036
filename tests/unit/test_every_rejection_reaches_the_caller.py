"""Every failure path tells the caller something, and tells it in the caller's terms.

A failed run reaches the caller as a terminal failure whose text names the exception type and
nothing else, so the node that refuses has to send the reason itself before returning. Paths that
did not showed up on a live run as a chat saying only "agent failed", while the audit trail
carried the reason all along.

`explain()` renders any unknown code as a generic fallback, so a missing entry is not a crash —
it is a sentence that says nothing, and for a condition arising inside the run it says the wrong
thing ("check the documented request shape"). Both halves are pinned here.

The scan pairs each refusal with one reason. Walking a branch and setting a flag on the first
`emit_progress(` in it proves only that the branch says *something* somewhere: a progress line
("reading the document") satisfies it, and one reason serves every later refusal in the same
branch. So each refusal consumes the pending reason, and what was sent is read, not just that
something was: the argument must reach the caller through `explain()` or through a sentence
pinned in `failure_message`. A bare string does not qualify. `TestTheScanItselfIsSound` holds
those three cases against the scanner directly.

Intervening statements are allowed — `emit_trace_event()` between the reason and the return is
the ordinary shape, not a defect.

What the scan cannot see is whether the code chosen for a branch is the *right* one for that
condition: a well-formed reason that names the wrong cause reads as correct here. Live runs and
review cover that.

The scan reads the branch, not a window of lines: an emit a few lines above a return may belong
to the branch before it, and one separated by a comment is still in the same branch. Two kinds of
error return are excluded — a projection made by `get_output`/`merge_output`/`route`, which
re-shapes a decision taken upstream, and a bare `return {"status": ERROR}`, which re-asserts one
already recorded and already reported. Emitting at either would tell the caller the same thing
twice.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from src.services import failure_message
from src.services.failure_message import explain

_SRC = sorted(pathlib.Path("src").rglob("*.py"))
_NOT_A_REFUSAL = frozenset({"get_output", "merge_output", "merge_response", "route", "_continue_or_end"})
# A value some nodes compare against to ask whether a reason has a sentence of its own. Never sent.
_SENTINEL = "__no_such_code__"


def _is_refusal(stmt: ast.stmt) -> bool:
    if isinstance(stmt, ast.Raise) and "SecurityViolationError" in ast.unparse(stmt):
        return True
    if not (isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Dict)):
        return False
    if "AgentStatus.ERROR" not in ast.unparse(stmt.value):
        return False
    keys = {k.value for k in stmt.value.keys if isinstance(k, ast.Constant)}
    return keys != {"status"}


def _reason_sent(
    stmt: ast.stmt,
    bindings: dict[str, tuple[str, ...]],
    params: frozenset[str],
    assigns: dict[str, list[tuple[str, str]]],
) -> tuple[str, str] | None:
    """Classify an `emit_progress(...)` statement by how its argument reaches the caller.

    ("literal", code)     explain("code")            — the code is checked against explain()
    ("pinned", name)      a name held in failure_message — its sentence is checked
    ("exception", cls)    explain(<caught exception>.<attr>) — the raise sites of that exception
                          are the bounded source of the code, and every code they use is checked
    ("param", name)       explain(<a parameter of this function>) — the codes its call sites hand
                          it are checked by TestTheWordingWasWrittenForThisAgent
    ("local", name)       explain(<a name this function assigns>) — every value assigned to it is
                          the bounded source, and each one is checked
    ("guarded", src)      a conditional picking between codes — deliberately prepared for a code
                          with no sentence of its own, so its call sites are not held to one
    ("unresolved", src)   explain(<anything else>) — the code cannot be read here and no bounded
                          source is available, so nothing shows a sentence reaches the caller
    ("raw", src)          never goes through explain() at all
    """
    if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
        return None
    call = stmt.value
    if getattr(call.func, "id", "") != "emit_progress" or not call.args:
        return None
    arg = call.args[0]
    if isinstance(arg, ast.Name):
        return ("pinned", arg.id)
    if not any(isinstance(n, ast.Call) and getattr(n.func, "id", "") == "explain" for n in ast.walk(arg)):
        return ("raw", ast.unparse(arg))
    if isinstance(arg, ast.IfExp):  # picks between codes — see the docstring
        return ("guarded", ast.unparse(arg))
    if isinstance(arg, ast.Call) and getattr(arg.func, "id", "") == "explain" and arg.args:
        inner = arg.args[0]
        if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
            return ("literal", inner.value)
        if (
            isinstance(inner, ast.Attribute)
            and isinstance(inner.value, ast.Name)
            and inner.value.id in bindings
        ):
            return ("exception", "|".join(bindings[inner.value.id]))
        if isinstance(inner, ast.Name) and inner.id in params:
            return ("param", inner.id)
        if isinstance(inner, ast.Name) and inner.id in assigns:
            return ("local", inner.id)
    return ("unresolved", ast.unparse(arg))


def _exception_bindings(tree: ast.AST) -> dict[str, tuple[str, ...]]:
    """`except SomeError as exc:` — the name a handler binds, and what it binds it to."""
    out: dict[str, tuple[str, ...]] = {}
    for handler in ast.walk(tree):
        if not isinstance(handler, ast.ExceptHandler) or not handler.name or handler.type is None:
            continue
        types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
        names = tuple(
            n for n in (getattr(t, "attr", None) or getattr(t, "id", None) for t in types) if n
        )
        if names:
            out.setdefault(handler.name, names)
    return out


def _returned_codes(tree: ast.AST, consts: dict[str, str]) -> dict[str, tuple[set[str], bool]]:
    """Each function in this file, and the codes it returns — a mapper such as
    `_rejection_code(detail)` that turns a condition into a code is a bounded source too.

    A function with a return the scan cannot read is marked unreadable rather than trusted for
    the returns it could read.
    """
    out: dict[str, tuple[set[str], bool]] = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        codes: set[str] = set()
        readable = True
        for node in ast.walk(fn):
            if not isinstance(node, ast.Return) or node.value is None:
                continue
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                codes.add(value.value)
            elif isinstance(value, ast.Name) and value.id in consts:
                codes.add(consts[value.id])
            else:
                readable = False
        out[fn.name] = (codes, readable and bool(codes))
    return out


def _assigned_codes(
    fn: ast.AST,
    bindings: dict[str, tuple[str, ...]],
    consts: dict[str, str],
    returns: dict[str, tuple[set[str], bool]],
) -> dict[str, list[tuple[str, str]]]:
    """Every value a function assigns to each of its names, as far as it can be read statically.

    `code = "too_deep"` in one branch and `code = r.code` in another are both bounded sources: the
    first names the code outright, the second hands over whatever the exception was raised with.
    An assignment neither of those can be read is recorded as such, so the scan can say so rather
    than pass.
    """
    out: dict[str, list[tuple[str, str]]] = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                source = ("literal", value.value)
            elif (
                isinstance(value, ast.Attribute)
                and isinstance(value.value, ast.Name)
                and value.value.id in bindings
            ):
                source = ("exception", "|".join(bindings[value.value.id]))
            elif isinstance(value, ast.Name) and value.id in consts:
                source = ("literal", consts[value.id])
            elif (
                isinstance(value, ast.Call)
                and getattr(value.func, "id", "") in returns
                and returns[value.func.id][1]
            ):
                for code in sorted(returns[value.func.id][0]):
                    out.setdefault(target.id, []).append(("literal", code))
                continue
            else:
                source = ("opaque", ast.unparse(value))
            out.setdefault(target.id, []).append(source)
    return out


def _module_consts() -> dict[str, str]:
    """Names bound to a string at module level — `ERR_X = "..."` written once and raised or
    assigned by name elsewhere.

    Only module level: a name assigned inside a function belongs to that function, and reading it
    as though it were a constant would resolve an unrelated local of the same name (`code`, say)
    to whichever literal some other function happened to assign.
    """
    out: dict[str, str] = {}
    for path in _SRC:
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)):
                continue
            if not isinstance(node.value.value, str):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out[target.id] = node.value.value
    return out


def _call_site_literals(
    trees: list[ast.AST], fname: str, index: int | None, param: str
) -> tuple[set[str], bool]:
    """Every string literal the call sites of `fname` put in `param`, and whether all of them
    could be read. A call site passing anything else makes the set no longer a bound."""
    codes: set[str] = set()
    readable = True
    seen = False
    for tree in trees:
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call):
                continue
            name = getattr(call.func, "attr", None) or getattr(call.func, "id", None)
            if name != fname:
                continue
            seen = True
            arg = call.args[index] if index is not None and index < len(call.args) else None
            for keyword in call.keywords:
                if keyword.arg == param:
                    arg = keyword.value
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                codes.add(arg.value)
            else:
                readable = False
    return codes, readable and seen


def _raise_arg_codes(
    value: ast.expr | None,
    consts: dict[str, str],
    params: dict[str, int | None],
    assigns: dict[str, list[tuple[str, str]]],
    fname: str | None,
    trees: list[ast.AST],
) -> tuple[set[str], bool]:
    """The codes one `raise Exc(<arg>)` can carry, and whether the arg could be read at all."""
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return {value.value}, True
    if isinstance(value, ast.Name):
        if value.id in consts:
            return {consts[value.id]}, True
        if fname is not None and value.id in params:
            return _call_site_literals(trees, fname, params[value.id], value.id)
        if value.id in assigns:
            codes: set[str] = set()
            for how, what in assigns[value.id]:
                if how != "literal":
                    return set(), False
                codes.add(what)
            return codes, bool(codes)
    return set(), False


def _raised_codes() -> dict[str, tuple[set[str], bool]]:
    """Every code each exception class is raised with, across the whole source tree, and whether
    every one of its raise sites could be read.

    A handler that sends `explain(exc.code)` shows the caller whichever code the raise chose, so
    the raise sites are the bounded source that reason resolves within — and the bound only holds
    if all of them are readable. A class raised once with a literal and once with a value the scan
    cannot follow is recorded as unreadable, not as the one literal: the unread raise is exactly
    the one that can put the generic sentence in front of the caller.

    A raise inside a helper is read the same way a reason is: a literal, a module constant, a
    parameter (whose call sites are then read), or a name the helper assigns.
    """
    trees = [ast.parse(path.read_text(encoding="utf-8")) for path in _SRC]
    consts = _module_consts()
    out: dict[str, tuple[set[str], bool]] = {}

    def record(node: ast.Raise, params, assigns, fname):
        if not isinstance(node.exc, ast.Call):
            return
        cls = getattr(node.exc.func, "attr", None) or getattr(node.exc.func, "id", None)
        if not cls:
            return
        codes, readable = out.get(cls, (set(), True))
        first = node.exc.args[0] if node.exc.args else None
        found, ok = _raise_arg_codes(first, consts, params, assigns, fname, trees)
        out[cls] = (codes | found, readable and ok)

    for tree in trees:
        bindings = _exception_bindings(tree)
        returns = _returned_codes(tree, consts)
        inside: set[int] = set()
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            names = [a.arg for a in [*fn.args.posonlyargs, *fn.args.args]]
            offset = 1 if names and names[0] == "self" else 0
            params = {n: i - offset for i, n in enumerate(names) if i >= offset}
            params.update({a.arg: None for a in fn.args.kwonlyargs})
            assigns = _assigned_codes(fn, bindings, consts, returns)
            for node in ast.walk(fn):
                if isinstance(node, ast.Raise):
                    inside.add(id(node))
                    record(node, params, assigns, fn.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Raise) and id(node) not in inside:
                record(node, {}, {}, None)
    return out


def _bodies(
    node: ast.AST,
    fname: str | None = None,
    params: frozenset[str] = frozenset(),
    assigns: dict[str, list[tuple[str, str]]] | None = None,
    bindings: dict[str, tuple[str, ...]] | None = None,
    consts: dict[str, str] | None = None,
    returns: dict[str, tuple[set[str], bool]] | None = None,
):
    bindings = {} if bindings is None else bindings
    consts = {} if consts is None else consts
    returns = {} if returns is None else returns
    assigns = {} if assigns is None else assigns
    seen: set[int] = set()
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = child.name
            args = child.args
            here = frozenset(a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs])
            written = _assigned_codes(child, bindings, consts, returns)
        else:
            name, here, written = fname, params, assigns
        for attr in ("body", "orelse", "finalbody"):
            body = getattr(child, attr, None)
            if isinstance(body, list) and id(body) not in seen:
                seen.add(id(body))
                yield body, name, here, written
        yield from _bodies(child, name, here, written, bindings, consts, returns)


def _carried(classes: str, raised: dict[str, tuple[set[str], bool]]) -> tuple[set[str], bool]:
    """The codes a caught exception can carry, and whether every raise of it could be read.

    A class the scan never saw raised is unreadable rather than empty — nothing shows what it
    puts in front of the caller.
    """
    codes: set[str] = set()
    readable = True
    for cls in classes.split("|"):
        found = raised.get(cls)
        if found is None:
            return set(), False
        codes |= found[0]
        readable = readable and found[1]
    return codes, readable


def _fault(
    kind: str,
    value: str,
    raised: dict[str, tuple[set[str], bool]],
    assigns: dict[str, list[tuple[str, str]]],
) -> str | None:
    """Why this reason would not reach the caller as a reason, or None if it would."""
    if kind == "raw":
        return f"sends `{value}`, which never goes through explain()"
    if kind == "literal":
        if explain(value).startswith(failure_message._FALLBACK):
            return f"'{value}' reaches the caller through the generic rule"
        return None
    if kind == "pinned":
        sentence = getattr(failure_message, value, None)
        if not isinstance(sentence, str) or not sentence.strip():
            return f"`{value}` is not a sentence held in failure_message"
        if sentence.startswith(failure_message._FALLBACK):
            return f"`{value}` is the generic fallback"
        return None
    if kind == "exception":
        codes, readable = _carried(value, raised)
        if not readable:
            return f"sends a code carried by `{value}`, which is raised somewhere with a code that cannot be read here"
        if not codes:
            return f"sends a code carried by `{value}`, and no code it is raised with can be read here"
        generic = sorted(c for c in codes if c != _SENTINEL and explain(c).startswith(failure_message._FALLBACK))
        if generic:
            return f"sends a code carried by `{value}`, which is raised with {generic} — answered generically"
        return None
    if kind == "local":
        sources = assigns.get(value, [])
        if not sources:
            return f"sends `{value}`, and nothing assigned to it can be read here"
        codes: set[str] = set()
        for how, what in sources:
            if how == "literal":
                codes.add(what)
            elif how == "exception":
                carried, readable = _carried(what, raised)
                if not readable:
                    return f"sends `{value}`, taken from `{what}`, which is raised somewhere with a code that cannot be read here"
                if not carried:
                    return f"sends `{value}`, taken from `{what}`, whose raise sites cannot be read here"
                codes |= carried
            else:
                return f"sends `{value}`, assigned from `{what}`, which cannot be read here"
        generic = sorted(c for c in codes if c != _SENTINEL and explain(c).startswith(failure_message._FALLBACK))
        if generic:
            return f"sends `{value}`, which is assigned {generic} — answered generically"
        return None
    if kind == "unresolved":
        return f"sends `{value}`, whose code cannot be read here and has no bounded source to check"
    # "param"   — the codes its call sites hand it are checked below, against the same rule.
    # "guarded" — a conditional that picks between codes is prepared for one with no sentence.
    return None


def _unguarded_reason_params(tree: ast.AST) -> dict[str, tuple[list[str], set[str]]]:
    """Helpers that send `explain(<parameter>)` with no fallback of their own.

    A helper that picks between two codes (`explain(reason) if _has_sentence(reason) else
    explain(code)`) is deliberately prepared for a reason with no sentence, so its call sites are
    not held to one. A helper that sends `explain(reason)` outright is not: whatever its callers
    hand it is what the caller of the agent reads.
    """
    out: dict[str, tuple[list[str], set[str]]] = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        params = [a.arg for a in fn.args.args]
        sent: set[str] = set()
        for stmt in ast.walk(fn):
            if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)):
                continue
            if getattr(stmt.value.func, "id", "") != "emit_progress" or not stmt.value.args:
                continue
            arg = stmt.value.args[0]
            if isinstance(arg, ast.IfExp):  # picks between codes — see the docstring
                continue
            if (
                isinstance(arg, ast.Call)
                and getattr(arg.func, "id", "") == "explain"
                and arg.args
                and isinstance(arg.args[0], ast.Name)
                and arg.args[0].id in params
            ):
                sent.add(arg.args[0].id)
        if sent:
            out[fn.name] = (params, sent)
    return out


def _reasons_handed_to(tree: ast.AST, helpers: dict[str, tuple[list[str], set[str]]]) -> set[str]:
    """Every string literal a call site puts in such a helper's reason position."""
    found: set[str] = set()
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call):
            continue
        name = getattr(call.func, "attr", None) or getattr(call.func, "id", None)
        if name not in helpers:
            continue
        params, sent = helpers[name]
        positional = params[1:] if params and params[0] == "self" else params
        for i, arg in enumerate(call.args):
            if i < len(positional) and positional[i] in sent and isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                found.add(arg.value)
        for kw in call.keywords:
            if kw.arg in sent and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                found.add(kw.value.value)
    return found


_RAISED: dict[str, tuple[set[str], bool]] | None = None
_CONSTS: dict[str, str] | None = None


def _raised() -> dict[str, tuple[set[str], bool]]:
    global _RAISED
    if _RAISED is None:
        _RAISED = _raised_codes()
    return _RAISED


def _consts() -> dict[str, str]:
    global _CONSTS
    if _CONSTS is None:
        _CONSTS = _module_consts()
    return _CONSTS


def _scan(src: str, raised: dict[str, tuple[set[str], bool]] | None = None) -> tuple[list[int], list[str]]:
    """Return (refusal lines, faults). Each refusal consumes the reason pending in its branch."""
    tree = ast.parse(src)
    raised = _raised() if raised is None else raised
    bindings = _exception_bindings(tree)
    consts = _consts()
    returns = _returned_codes(tree, consts)
    refusals: list[int] = []
    faults: list[str] = []
    for body, fname, params, assigns in _bodies(tree, bindings=bindings, consts=consts, returns=returns):
        pending: tuple[str, str, int] | None = None
        for stmt in body:
            sent = _reason_sent(stmt, bindings, params, assigns)
            if sent is not None:
                pending = (sent[0], sent[1], stmt.lineno)
                continue
            if _is_refusal(stmt) and fname not in _NOT_A_REFUSAL:
                refusals.append(stmt.lineno)
                if pending is None:
                    faults.append(f"line {stmt.lineno}: refuses with no reason sent in this branch")
                else:
                    kind, value, emit_line = pending
                    why = _fault(kind, value, raised, assigns)
                    if why is not None:
                        faults.append(f"line {stmt.lineno}: the reason at line {emit_line} {why}")
                pending = None  # one reason answers one refusal
    return sorted(set(refusals)), faults


def _explained_codes(src: str) -> set[str]:
    return {
        call.args[0].value
        for call in ast.walk(ast.parse(src))
        if isinstance(call, ast.Call)
        and getattr(call.func, "id", "") == "explain"
        and call.args
        and isinstance(call.args[0], ast.Constant)
        and isinstance(call.args[0].value, str)
        and call.args[0].value != _SENTINEL
    }


class TestNoFailurePathIsSilent:
    @pytest.mark.parametrize("path", _SRC, ids=lambda p: p.name)
    def test_every_refusal_sends_its_own_reason(self, path):
        _, faults = _scan(path.read_text(encoding="utf-8"))
        assert not faults, f"{path}:\n  " + "\n  ".join(faults)

    def test_the_scan_finds_refusals_so_it_cannot_pass_vacuously(self):
        """The assertion above passes trivially on an empty result — if a refactor renamed the
        status constant, say. A fixed count would be brittle as nodes come and go; that it finds
        any at all is what makes the assertion mean something."""
        assert sum(len(_scan(p.read_text(encoding="utf-8"))[0]) for p in _SRC) > 0


# A code this agent answers with a sentence of its own, so the samples below turn on the
# pairing under test and not on which codes this particular agent happens to register.
_A_CODE = sorted(failure_message._EXACT)[0]
# A code this agent does not answer, so explain() renders it through the generic rule.
_NO_SENTENCE = "__a_code_with_no_sentence__"


class TestTheScanItselfIsSound:
    """The scan replaced one that a wrong implementation could satisfy. These hold it to that."""

    _CORRECT = f"""
def execute(self, state):
    if not ok:
        emit_progress(explain("{_A_CODE}"))
        emit_trace_event("rejected", {{}}, state)
        return {{"status": AgentStatus.ERROR.value, "error_log": ["{_A_CODE}"]}}
"""
    _PROGRESS_LINE = """
def execute(self, state):
    if not ok:
        emit_progress("reading the document")
        return {"status": AgentStatus.ERROR.value, "error_log": ["bad"]}
"""
    _ONE_REASON_TWO_REFUSALS = f"""
def execute(self, state):
    if not ok:
        emit_progress(explain("{_A_CODE}"))
        if a:
            return {{"status": AgentStatus.ERROR.value, "error_log": ["{_A_CODE}"]}}
        return {{"status": AgentStatus.ERROR.value, "error_log": ["something_else"]}}
"""

    def test_a_reason_paired_with_its_refusal_passes(self):
        assert _scan(self._CORRECT)[1] == []

    def test_a_progress_line_is_not_a_reason(self):
        assert _scan(self._PROGRESS_LINE)[1], "a bare progress string satisfied the scan"

    def test_one_reason_does_not_answer_two_refusals(self):
        assert _scan(self._ONE_REASON_TWO_REFUSALS)[1], "one reason covered a second refusal"

    _FROM_AN_EXCEPTION = """
def execute(self, state):
    try:
        work()
    except Boom as exc:
        emit_progress(explain(exc.code))
        return {"status": AgentStatus.ERROR.value, "error_log": [exc.code]}
"""
    _CODE_CANNOT_BE_READ = """
def execute(self, state):
    if not ok:
        emit_progress(explain(pick_a_code(state)))
        return {"status": AgentStatus.ERROR.value, "error_log": ["x"]}
"""

    def test_a_code_taken_from_an_exception_passes_when_every_raise_is_answered(self):
        assert _scan(self._FROM_AN_EXCEPTION, {"Boom": ({_A_CODE}, True)})[1] == []

    def test_a_code_taken_from_an_exception_fails_when_one_raise_is_not(self):
        """`explain(exc.code)` shows whichever code the raise chose, so one unanswered raise site
        is enough for the caller to be handed the generic sentence."""
        faults = _scan(self._FROM_AN_EXCEPTION, {"Boom": ({_A_CODE, _NO_SENTENCE}, True)})[1]
        assert faults, "an exception raised with an unanswered code satisfied the scan"

    def test_a_code_taken_from_an_exception_fails_when_one_raise_cannot_be_read(self):
        """An exception raised once with a literal and once with a value the scan cannot follow is
        not bounded by that literal — the unread raise is the one that can reach the caller with
        the generic sentence, so the codes it *could* read are not grounds to pass."""
        faults = _scan(self._FROM_AN_EXCEPTION, {"Boom": ({_A_CODE}, False)})[1]
        assert faults, "an exception with an unreadable raise satisfied the scan on its readable one"

    def test_an_exception_whose_raises_cannot_be_read_is_not_taken_on_faith(self):
        assert _scan(self._FROM_AN_EXCEPTION, {})[1], "an unreadable exception satisfied the scan"

    _THROUGH_A_LOCAL = f"""
def execute(self, state):
    if a:
        code = "{_A_CODE}"
    else:
        code = "{{other_2}}"
    emit_progress(explain(code))
    return {{{{"status": AgentStatus.ERROR.value, "error_log": [code]}}}}
"""
    _LOCAL_FROM_SOMEWHERE_UNREADABLE = """
def execute(self, state):
    code = pick_a_code(state)
    emit_progress(explain(code))
    return {"status": AgentStatus.ERROR.value, "error_log": [code]}
"""

    def test_a_code_held_in_a_local_passes_when_every_branch_assigns_an_answered_one(self):
        assert _scan(self._THROUGH_A_LOCAL.format(other_2=_A_CODE), {})[1] == []

    def test_a_code_held_in_a_local_fails_when_one_branch_assigns_an_unanswered_one(self):
        """The name is the same at the emit; which branch ran decides what the caller reads, so
        every branch that assigns it is part of the bounded source."""
        faults = _scan(self._THROUGH_A_LOCAL.format(other_2=_NO_SENTENCE), {})[1]
        assert faults, "a branch assigning an unanswered code satisfied the scan"

    def test_a_local_assigned_from_something_unreadable_is_not_taken_on_faith(self):
        assert _scan(self._LOCAL_FROM_SOMEWHERE_UNREADABLE, {})[1], "an unreadable local passed"

    def test_a_code_that_cannot_be_read_at_all_is_not_taken_on_faith(self):
        """Being inside `explain(...)` is not itself proof: the scan has to be able to name the
        code, or a bounded set it comes from."""
        assert _scan(self._CODE_CANNOT_BE_READ, {})[1], "an unreadable code satisfied the scan"


class TestTheWordingWasWrittenForThisAgent:
    @pytest.mark.parametrize("path", _SRC, ids=lambda p: p.name)
    def test_every_code_is_answered_deliberately(self, path):
        unanswered = sorted(
            c
            for c in _explained_codes(path.read_text(encoding="utf-8"))
            if explain(c).startswith(failure_message._FALLBACK)
        )
        assert not unanswered, f"{path}: reaches the caller through the generic rule: {unanswered}"

    @pytest.mark.parametrize("path", _SRC, ids=lambda p: p.name)
    def test_a_reason_handed_to_a_helper_is_answered_too(self, path):
        """A helper that sends `explain(reason)` puts its callers' codes in front of the caller;
        the file-wide check above only sees codes written as `explain("...")` at the emit."""
        tree = ast.parse(path.read_text(encoding="utf-8"))
        unanswered = sorted(
            c
            for c in _reasons_handed_to(tree, _unguarded_reason_params(tree))
            if c != _SENTINEL and explain(c).startswith(failure_message._FALLBACK)
        )
        assert not unanswered, f"{path}: handed to a helper but answered generically: {unanswered}"

    def test_an_internal_condition_is_not_described_as_a_field_of_the_request(self):
        """The `missing_` prefix rule renders "<key> is missing from the request" — naming a
        handoff between nodes as though the caller had sent it."""
        for code in getattr(failure_message, "_INTERNAL_CODES", ()):
            sentence = explain(code)
            assert failure_message._INTERNAL in sentence, f"{code} is not answered as service-side"
            assert "from the request" not in sentence, f"{code} describes an internal condition as a request field"
