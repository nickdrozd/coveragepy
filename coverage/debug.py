# Licensed under the Apache License: http://www.apache.org/licenses/LICENSE-2.0
# For details: https://github.com/nedbat/coveragepy/blob/master/NOTICE.txt

"""Control of and utilities for debugging."""

from __future__ import annotations

import contextlib
import functools
import inspect
import io
import itertools
import os
import pprint
import re
import reprlib
import sys
import traceback
import types
import _thread

from typing import (
    cast,
    Any, Callable, IO, Iterable, Iterator, Mapping, Optional, List, Tuple,
)

from coverage.misc import human_sorted_items, isolate_module
from coverage.types import TWritable

os = isolate_module(os)


# When debugging, it can be helpful to force some options, especially when
# debugging the configuration mechanisms you usually use to control debugging!
# This is a list of forced debugging options.
FORCED_DEBUG: List[str] = []
FORCED_DEBUG_FILE = None


class DebugControl:
    """Control and output for debugging."""

    show_repr_attr = False      # For AutoReprMixin

    def __init__(
        self,
        options: Iterable[str],
        output: Optional[IO[str]],
        file_name: Optional[str] = None,
    ) -> None:
        """Configure the options and output file for debugging."""
        self.options = list(options) + FORCED_DEBUG
        self.suppress_callers = False

        filters = []
        if self.should("pid"):
            filters.append(add_pid_and_tid)
        self.output = DebugOutputFile.get_one(
            output,
            file_name=file_name,
            show_process=self.should("process"),
            filters=filters,
        )
        self.raw_output = self.output.outfile

    def __repr__(self) -> str:
        return f"<DebugControl options={self.options!r} raw_output={self.raw_output!r}>"

    def should(self, option: str) -> bool:
        """Decide whether to output debug information in category `option`."""
        if option == "callers" and self.suppress_callers:
            return False
        return (option in self.options)

    @contextlib.contextmanager
    def without_callers(self) -> Iterator[None]:
        """A context manager to prevent call stacks from being logged."""
        old = self.suppress_callers
        self.suppress_callers = True
        try:
            yield
        finally:
            self.suppress_callers = old

    def write(self, msg: str) -> None:
        """Write a line of debug output.

        `msg` is the line to write. A newline will be appended.

        """
        self.output.write(msg+"\n")
        if self.should("self"):
            caller_self = inspect.stack()[1][0].f_locals.get("self")
            if caller_self is not None:
                self.output.write(f"self: {caller_self!r}\n")
        if self.should("callers"):
            dump_stack_frames(out=self.output, skip=1)
        self.output.flush()


class DebugControlString(DebugControl):
    """A `DebugControl` that writes to a StringIO, for testing."""
    def __init__(self, options: Iterable[str]) -> None:
        super().__init__(options, io.StringIO())

    def get_output(self) -> str:
        """Get the output text from the `DebugControl`."""
        return cast(str, self.raw_output.getvalue())        # type: ignore[union-attr]


class NoDebugging(DebugControl):
    """A replacement for DebugControl that will never try to do anything."""
    def __init__(self) -> None:
        # pylint: disable=super-init-not-called
        ...

    def should(self, option: str) -> bool:
        """Should we write debug messages?  Never."""
        return False

    def write(self, msg: str) -> None:
        """This will never be called."""
        raise AssertionError("NoDebugging.write should never be called.")


def info_header(label: str) -> str:
    """Make a nice header string."""
    return "--{:-<60s}".format(" "+label+" ")


def info_formatter(info: Iterable[Tuple[str, Any]]) -> Iterator[str]:
    """Produce a sequence of formatted lines from info.

    `info` is a sequence of pairs (label, data).  The produced lines are
    nicely formatted, ready to print.

    """
    info = list(info)
    if not info:
        return
    label_len = 30
    assert all(len(l) < label_len for l, _ in info)
    for label, data in info:
        if data == []:
            data = "-none-"
        if isinstance(data, tuple) and len(repr(tuple(data))) < 30:
            # Convert to tuple to scrub namedtuples.
            yield "%*s: %r" % (label_len, label, tuple(data))
        elif isinstance(data, (list, set, tuple)):
            prefix = "%*s:" % (label_len, label)
            for e in data:
                yield "%*s %s" % (label_len+1, prefix, e)
                prefix = ""
        else:
            yield "%*s: %s" % (label_len, label, data)


def write_formatted_info(
    write: Callable[[str], None],
    header: str,
    info: Iterable[Tuple[str, Any]],
) -> None:
    """Write a sequence of (label,data) pairs nicely.

    `write` is a function write(str) that accepts each line of output.
    `header` is a string to start the section.  `info` is a sequence of
    (label, data) pairs, where label is a str, and data can be a single
    value, or a list/set/tuple.

    """
    write(info_header(header))
    for line in info_formatter(info):
        write(f" {line}")


def exc_one_line(exc: Exception) -> str:
    """Get a one-line summary of an exception, including class name and message."""
    lines = traceback.format_exception_only(type(exc), exc)
    return "|".join(l.rstrip() for l in lines)


def short_stack(limit: Optional[int] = None, skip: int = 0) -> str:
    """Return a string summarizing the call stack.

    The string is multi-line, with one line per stack frame. Each line shows
    the function name, the file name, and the line number:

        ...
        start_import_stop : /Users/ned/coverage/trunk/tests/coveragetest.py @95
        import_local_file : /Users/ned/coverage/trunk/tests/coveragetest.py @81
        import_local_file : /Users/ned/coverage/trunk/coverage/backward.py @159
        ...

    `limit` is the number of frames to include, defaulting to all of them.

    `skip` is the number of frames to skip, so that debugging functions can
    call this and not be included in the result.

    """
    stack = inspect.stack()[limit:skip:-1]
    return "\n".join("%30s : %s:%d" % (t[3], t[1], t[2]) for t in stack)


def dump_stack_frames(
    limit: Optional[int] = None,
    out: Optional[TWritable] = None,
    skip: int = 0
) -> None:
    """Print a summary of the stack to stdout, or someplace else."""
    fout = out or sys.stdout
    fout.write(short_stack(limit=limit, skip=skip+1))
    fout.write("\n")


def clipped_repr(text: str, numchars: int = 50) -> str:
    """`repr(text)`, but limited to `numchars`."""
    r = reprlib.Repr()
    r.maxstring = numchars
    return r.repr(text)


def short_id(id64: int) -> int:
    """Given a 64-bit id, make a shorter 16-bit one."""
    id16 = 0
    for offset in range(0, 64, 16):
        id16 ^= id64 >> offset
    return id16 & 0xFFFF


def add_pid_and_tid(text: str) -> str:
    """A filter to add pid and tid to debug messages."""
    # Thread ids are useful, but too long. Make a shorter one.
    tid = f"{short_id(_thread.get_ident()):04x}"
    text = f"{os.getpid():5d}.{tid}: {text}"
    return text


class AutoReprMixin:
    """A mixin implementing an automatic __repr__ for debugging."""
    auto_repr_ignore = ["auto_repr_ignore", "$coverage.object_id"]

    def __repr__(self) -> str:
        show_attrs = (
            (k, v) for k, v in self.__dict__.items()
            if getattr(v, "show_repr_attr", True)
            and not callable(v)
            and k not in self.auto_repr_ignore
        )
        return "<{klass} @0x{id:x} {attrs}>".format(
            klass=self.__class__.__name__,
            id=id(self),
            attrs=" ".join(f"{k}={v!r}" for k, v in show_attrs),
        )


def simplify(v: Any) -> Any:                                # pragma: debugging
    """Turn things which are nearly dict/list/etc into dict/list/etc."""
    if isinstance(v, dict):
        return {k:simplify(vv) for k, vv in v.items()}
    elif isinstance(v, (list, tuple)):
        return type(v)(simplify(vv) for vv in v)
    elif hasattr(v, "__dict__"):
        return simplify({"."+k: v for k, v in v.__dict__.items()})
    else:
        return v


def pp(v: Any) -> None:                                     # pragma: debugging
    """Debug helper to pretty-print data, including SimpleNamespace objects."""
    # Might not be needed in 3.9+
    pprint.pprint(simplify(v))


def filter_text(text: str, filters: Iterable[Callable[[str], str]]) -> str:
    """Run `text` through a series of filters.

    `filters` is a list of functions. Each takes a string and returns a
    string.  Each is run in turn.

    Returns: the final string that results after all of the filters have
    run.

    """
    clean_text = text.rstrip()
    ending = text[len(clean_text):]
    text = clean_text
    for fn in filters:
        lines = []
        for line in text.splitlines():
            lines.extend(fn(line).splitlines())
        text = "\n".join(lines)
    return text + ending


class CwdTracker:
    """A class to add cwd info to debug messages."""
    def __init__(self) -> None:
        self.cwd: Optional[str] = None

    def filter(self, text: str) -> str:
        """Add a cwd message for each new cwd."""
        cwd = os.getcwd()
        if cwd != self.cwd:
            text = f"cwd is now {cwd!r}\n" + text
            self.cwd = cwd
        return text


class DebugOutputFile:
    """A file-like object that includes pid and cwd information."""
    def __init__(
        self,
        outfile: Optional[IO[str]],
        show_process: bool,
        filters: Iterable[Callable[[str], str]],
    ):
        self.outfile = outfile
        self.show_process = show_process
        self.filters = list(filters)

        if self.show_process:
            self.filters.insert(0, CwdTracker().filter)
            self.write(f"New process: executable: {sys.executable!r}\n")
            self.write("New process: cmd: {!r}\n".format(getattr(sys, "argv", None)))
            if hasattr(os, "getppid"):
                self.write(f"New process: pid: {os.getpid()!r}, parent pid: {os.getppid()!r}\n")

    @classmethod
    def get_one(
        cls,
        fileobj: Optional[IO[str]] = None,
        file_name: Optional[str] = None,
        show_process: bool = True,
        filters: Iterable[Callable[[str], str]] = (),
        interim: bool = False,
    ) -> DebugOutputFile:
        """Get a DebugOutputFile.

        If `fileobj` is provided, then a new DebugOutputFile is made with it.

        If `fileobj` isn't provided, then a file is chosen (`file_name` if
        provided, or COVERAGE_DEBUG_FILE, or stderr), and a process-wide
        singleton DebugOutputFile is made.

        `show_process` controls whether the debug file adds process-level
        information, and filters is a list of other message filters to apply.

        `filters` are the text filters to apply to the stream to annotate with
        pids, etc.

        If `interim` is true, then a future `get_one` can replace this one.

        """
        if fileobj is not None:
            # Make DebugOutputFile around the fileobj passed.
            return cls(fileobj, show_process, filters)

        the_one, is_interim = cls._get_singleton_data()
        if the_one is None or is_interim:
            if file_name is not None:
                fileobj = open(file_name, "a", encoding="utf-8")
            else:
                file_name = os.environ.get("COVERAGE_DEBUG_FILE", FORCED_DEBUG_FILE)
                if file_name in ("stdout", "stderr"):
                    fileobj = getattr(sys, file_name)
                elif file_name:
                    fileobj = open(file_name, "a", encoding="utf-8")
                else:
                    fileobj = sys.stderr
            the_one = cls(fileobj, show_process, filters)
            cls._set_singleton_data(the_one, interim)
        return the_one

    # Because of the way igor.py deletes and re-imports modules,
    # this class can be defined more than once. But we really want
    # a process-wide singleton. So stash it in sys.modules instead of
    # on a class attribute. Yes, this is aggressively gross.

    SYS_MOD_NAME = "$coverage.debug.DebugOutputFile.the_one"
    SINGLETON_ATTR = "the_one_and_is_interim"

    @classmethod
    def _set_singleton_data(cls, the_one: DebugOutputFile, interim: bool) -> None:
        """Set the one DebugOutputFile to rule them all."""
        singleton_module = types.ModuleType(cls.SYS_MOD_NAME)
        setattr(singleton_module, cls.SINGLETON_ATTR, (the_one, interim))
        sys.modules[cls.SYS_MOD_NAME] = singleton_module

    @classmethod
    def _get_singleton_data(cls) -> Tuple[Optional[DebugOutputFile], bool]:
        """Get the one DebugOutputFile."""
        singleton_module = sys.modules.get(cls.SYS_MOD_NAME)
        return getattr(singleton_module, cls.SINGLETON_ATTR, (None, True))

    @classmethod
    def _del_singleton_data(cls) -> None:
        """Delete the one DebugOutputFile, just for tests to use."""
        if cls.SYS_MOD_NAME in sys.modules:
            del sys.modules[cls.SYS_MOD_NAME]

    def write(self, text: str) -> None:
        """Just like file.write, but filter through all our filters."""
        assert self.outfile is not None
        self.outfile.write(filter_text(text, self.filters))
        self.outfile.flush()

    def flush(self) -> None:
        """Flush our file."""
        assert self.outfile is not None
        self.outfile.flush()


def log(msg: str, stack: bool = False) -> None:             # pragma: debugging
    """Write a log message as forcefully as possible."""
    out = DebugOutputFile.get_one(interim=True)
    out.write(msg+"\n")
    if stack:
        dump_stack_frames(out=out, skip=1)


def decorate_methods(
    decorator: Callable[..., Any],
    butnot: Iterable[str] = (),
    private: bool = False,
) -> Callable[..., Any]:                                    # pragma: debugging
    """A class decorator to apply a decorator to methods."""
    def _decorator(cls):                                    # type: ignore[no-untyped-def]
        for name, meth in inspect.getmembers(cls, inspect.isroutine):
            if name not in cls.__dict__:
                continue
            if name != "__init__":
                if not private and name.startswith("_"):
                    continue
            if name in butnot:
                continue
            setattr(cls, name, decorator(meth))
        return cls
    return _decorator


def break_in_pudb(func: Callable[..., Any]) -> Callable[..., Any]:  # pragma: debugging
    """A function decorator to stop in the debugger for each call."""
    @functools.wraps(func)
    def _wrapper(*args: Any, **kwargs: Any) -> Any:
        import pudb
        sys.stdout = sys.__stdout__
        pudb.set_trace()
        return func(*args, **kwargs)
    return _wrapper


OBJ_IDS = itertools.count()
CALLS = itertools.count()
OBJ_ID_ATTR = "$coverage.object_id"

def show_calls(
    show_args: bool = True,
    show_stack: bool = False,
    show_return: bool = False,
) -> Callable[..., Any]:                                    # pragma: debugging
    """A method decorator to debug-log each call to the function."""
    def _decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        def _wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            oid = getattr(self, OBJ_ID_ATTR, None)
            if oid is None:
                oid = f"{os.getpid():08d} {next(OBJ_IDS):04d}"
                setattr(self, OBJ_ID_ATTR, oid)
            extra = ""
            if show_args:
                eargs = ", ".join(map(repr, args))
                ekwargs = ", ".join("{}={!r}".format(*item) for item in kwargs.items())
                extra += "("
                extra += eargs
                if eargs and ekwargs:
                    extra += ", "
                extra += ekwargs
                extra += ")"
            if show_stack:
                extra += " @ "
                extra += "; ".join(_clean_stack_line(l) for l in short_stack().splitlines())
            callid = next(CALLS)
            msg = f"{oid} {callid:04d} {func.__name__}{extra}\n"
            DebugOutputFile.get_one(interim=True).write(msg)
            ret = func(self, *args, **kwargs)
            if show_return:
                msg = f"{oid} {callid:04d} {func.__name__} return {ret!r}\n"
                DebugOutputFile.get_one(interim=True).write(msg)
            return ret
        return _wrapper
    return _decorator


def _clean_stack_line(s: str) -> str:                       # pragma: debugging
    """Simplify some paths in a stack trace, for compactness."""
    s = s.strip()
    s = s.replace(os.path.dirname(__file__) + "/", "")
    s = s.replace(os.path.dirname(os.__file__) + "/", "")
    s = s.replace(sys.prefix + "/", "")
    return s


def relevant_environment_display(env: Mapping[str, str]) -> List[Tuple[str, str]]:
    """Filter environment variables for a debug display.

    Select variables to display (with COV or PY in the name, or HOME, TEMP, or
    TMP), and also cloak sensitive values with asterisks.

    Arguments:
        env: a dict of environment variable names and values.

    Returns:
        A list of pairs (name, value) to show.

    """
    slugs = {"COV", "PY"}
    include = {"HOME", "TEMP", "TMP"}
    cloak = {"API", "TOKEN", "KEY", "SECRET", "PASS", "SIGNATURE"}

    to_show = []
    for name, val in env.items():
        keep = False
        if name in include:
            keep = True
        elif any(slug in name for slug in slugs):
            keep = True
        if keep:
            if any(slug in name for slug in cloak):
                val = re.sub(r"\w", "*", val)
            to_show.append((name, val))
    return human_sorted_items(to_show)
